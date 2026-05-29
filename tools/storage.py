"""
Storage layer for screenshots, CAPTCHA images, and customer uploads.

Two modes, controlled by `AWS_S3_BUCKET`:

- **disk-only** (default, current MVP behavior): write to the local path the
  caller asked for and return that as a `file://` URL. No network calls.
- **disk + S3** (when `AWS_S3_BUCKET` is set): write to disk AS WELL AS upload
  to S3. The returned `StorageResult.url` is the S3 URL (presigned by default,
  or public-read if `S3_PUBLIC_READ=true`). The local path is also returned so
  the in-process debug paths still work. If the S3 upload fails for any
  reason, the disk write still succeeds and the local URL is returned — the
  agent's flow never breaks because of a storage hiccup.

Reason for keeping the disk write alongside S3:

- The agent uses `data/latest_captcha.png` for the legacy manual-CAPTCHA file
  fallback, debug screenshots, and various local tools. Forcing S3-only would
  add an avoidable network round-trip to every agent step.
- The customer-UI doc preview endpoint already serves local files; the S3 URL
  goes onto the document record additively, so the existing /preview path
  keeps working even with a fresh container that lost the disk file.
"""

from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


@dataclass
class StorageResult:
    """Where the bytes ended up."""

    url: str                       # the canonical URL the customer/admin should use
    local_path: Optional[str]      # absolute local path, or None if S3-only
    s3_key: Optional[str]          # S3 object key, or None if not uploaded
    s3_url: Optional[str]          # the explicit S3 URL (presigned or public)
    backend: str                   # "s3+disk" | "disk"
    content_type: str

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "local_path": self.local_path,
            "s3_key": self.s3_key,
            "s3_url": self.s3_url,
            "backend": self.backend,
            "content_type": self.content_type,
        }


class Storage:
    """Thin wrapper. boto3 is imported lazily so the local dev path doesn't
    require it to be installed (it is required in the Docker image)."""

    def __init__(self) -> None:
        self._s3_client = None
        self._s3_client_attempted = False

    def _s3(self):
        if self._s3_client_attempted:
            return self._s3_client
        self._s3_client_attempted = True
        if not settings.aws_s3_bucket:
            return None
        try:
            import boto3
        except ImportError:
            log.warning("storage.boto3_missing", action="s3_disabled")
            return None
        try:
            kwargs = {"region_name": settings.aws_s3_region or "ap-south-1"}
            if settings.aws_access_key_id and settings.aws_secret_access_key:
                kwargs["aws_access_key_id"] = settings.aws_access_key_id
                kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
            self._s3_client = boto3.client("s3", **kwargs)
            log.info("storage.s3_ready", bucket=settings.aws_s3_bucket, region=kwargs["region_name"])
        except Exception as e:
            log.warning("storage.s3_init_failed", error=str(e))
            self._s3_client = None
        return self._s3_client

    @staticmethod
    def _content_type_for(filename: str, fallback: str = "application/octet-stream") -> str:
        guess, _ = mimetypes.guess_type(filename)
        return guess or fallback

    def _build_key(self, *parts: str) -> str:
        """Compose the S3 key. Leading prefix from settings.s3_key_prefix."""
        clean = [p.strip("/").replace("\\", "/") for p in parts if p]
        prefix = (settings.s3_key_prefix or "").strip("/")
        if prefix:
            clean.insert(0, prefix)
        return "/".join(clean)

    async def put_bytes(
        self,
        local_path: str,
        data: bytes,
        *,
        kind: str,
        job_id: str = "",
        customer_id: str = "",
        content_type: Optional[str] = None,
    ) -> StorageResult:
        """Write `data` to disk at `local_path` and (when S3 is configured)
        upload it to S3 under `<s3_key_prefix>/<kind>/<job_or_cust_id>/<filename>`.

        `kind` is the broad category: `screenshot`, `captcha`, `dl_upload`,
        `signature`, `photo`, etc. Used to route into separate S3 prefixes.
        """
        path = Path(local_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        absolute = path.resolve()

        ctype = content_type or self._content_type_for(path.name, "image/png")
        result = StorageResult(
            url=absolute.as_uri(),
            local_path=str(absolute),
            s3_key=None,
            s3_url=None,
            backend="disk",
            content_type=ctype,
        )

        client = self._s3()
        if client is None:
            return result

        owner_or_job = job_id or customer_id or "shared"
        key = self._build_key(kind, owner_or_job, path.name)
        try:
            await asyncio.to_thread(
                self._put_object_sync, client, key, data, ctype,
            )
            url = self._url_for_key(client, key)
            result.s3_key = key
            result.s3_url = url
            result.url = url
            result.backend = "s3+disk"
            log.info("storage.s3_put", key=key, size=len(data))
        except Exception as e:
            log.warning("storage.s3_put_failed", key=key, error=str(e))

        return result

    def _put_object_sync(self, client, key: str, data: bytes, content_type: str) -> None:
        kwargs = {
            "Bucket": settings.aws_s3_bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if settings.s3_public_read:
            kwargs["ACL"] = "public-read"
        # Resource-tag for cost attribution per project policy.
        if settings.s3_owner_tag:
            kwargs["Tagging"] = f"Owner={settings.s3_owner_tag}"
        client.put_object(**kwargs)

    def _url_for_key(self, client, key: str) -> str:
        if settings.s3_public_read:
            # Public-read URLs don't expire and don't need signing.
            return f"https://{settings.aws_s3_bucket}.s3.{settings.aws_s3_region}.amazonaws.com/{key}"
        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.aws_s3_bucket, "Key": key},
                ExpiresIn=max(60, int(settings.s3_url_expiry_seconds)),
            )
        except Exception as e:
            log.warning("storage.presign_failed", key=key, error=str(e))
            # Fall back to the unsigned URL — won't work for private buckets
            # but at least the caller has a stable handle.
            return f"https://{settings.aws_s3_bucket}.s3.{settings.aws_s3_region}.amazonaws.com/{key}"

    async def put_file(
        self,
        local_path: str,
        *,
        kind: str,
        job_id: str = "",
        customer_id: str = "",
        content_type: Optional[str] = None,
    ) -> StorageResult:
        """Upload an existing local file. Useful for paths the caller has
        already written (e.g. customer DL upload that's already on disk)."""
        path = Path(local_path)
        if not path.exists():
            raise FileNotFoundError(local_path)
        return await self.put_bytes(
            local_path=str(path),
            data=path.read_bytes(),
            kind=kind,
            job_id=job_id,
            customer_id=customer_id,
            content_type=content_type,
        )


_storage_singleton: Optional[Storage] = None


def get_storage() -> Storage:
    """Process-wide singleton. Cheap; the S3 client is lazy."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = Storage()
    return _storage_singleton


def screenshot_filename(job_id: str, label: str = "step") -> str:
    """Suggested local path for an agent screenshot keyed to a job + label."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40] or "step"
    safe_job = "".join(c if c.isalnum() or c in "-_" else "_" for c in job_id)[:40] or "shared"
    return str(Path("data") / "screenshots" / safe_job / f"{ts}_{safe_label}.png")


def stamp_screenshot_on_job(job, result: StorageResult, *, label: str = "") -> None:
    """Append the storage result to job.customer_data["screenshots"] so
    /jobs/{id} can list every image captured during the run. Kept as a free
    function so callers (brain, captcha_solver, onboard) don't need to know
    about the Job class shape. Last 100 entries are kept to bound memory."""
    try:
        screenshots = job.customer_data.setdefault("screenshots", [])
        if not isinstance(screenshots, list):
            screenshots = []
            job.customer_data["screenshots"] = screenshots
        screenshots.append({
            "url": result.url,
            "s3_url": result.s3_url,
            "s3_key": result.s3_key,
            "local_path": result.local_path,
            "backend": result.backend,
            "content_type": result.content_type,
            "label": label or "",
            "taken_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(screenshots) > 100:
            del screenshots[: len(screenshots) - 100]
    except Exception as e:  # noqa: BLE001
        log.warning("storage.stamp_failed", error=str(e))
