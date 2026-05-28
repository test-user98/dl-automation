"""
CAPTCHA solving tool.

Supports:
  - 2captcha  (paid, reliable, ~15-30s)
  - capsolver  (paid, faster)
  - manual     (ask human — fallback for demo)
  - claude     (use Claude vision to solve simple text CAPTCHAs)

The agent calls solve(image_bytes) and gets back a text string to type.
"""

import asyncio
import base64
import httpx
import structlog
from typing import Optional

from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


class CaptchaSolver:

    async def solve(self, image_bytes: bytes) -> Optional[str]:
        provider = settings.captcha_provider
        log.info("captcha.solving", provider=provider)

        for attempt in range(settings.captcha_max_retries):
            result = await self._solve_once(image_bytes, provider)
            if result:
                log.info("captcha.solved", provider=provider, attempt=attempt + 1)
                return result
            log.warning("captcha.attempt_failed", attempt=attempt + 1)
            await asyncio.sleep(2)

        return None

    async def _solve_once(self, image_bytes: bytes, provider: str) -> Optional[str]:
        if provider == "2captcha":
            return await self._solve_2captcha(image_bytes)
        elif provider == "capsolver":
            return await self._solve_capsolver(image_bytes)
        elif provider == "claude":
            return await self._solve_claude(image_bytes)
        else:
            return await self._solve_manual(image_bytes)

    # ── 2captcha ───────────────────────────────────────────────────────────────

    async def _solve_2captcha(self, image_bytes: bytes) -> Optional[str]:
        b64 = base64.b64encode(image_bytes).decode()
        api_key = settings.captcha_api_key
        timeout = settings.captcha_timeout_seconds

        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            # Submit CAPTCHA
            resp = await client.post(
                "http://2captcha.com/in.php",
                data={"key": api_key, "method": "base64", "body": b64, "json": 1},
            )
            data = resp.json()
            if data.get("status") != 1:
                log.error("captcha.2captcha_submit_failed", response=data)
                return None

            captcha_id = data["request"]
            log.debug("captcha.2captcha_submitted", captcha_id=captcha_id)

            # Poll for result
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(5)
                poll = await client.get(
                    f"http://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1"
                )
                pd = poll.json()
                if pd.get("status") == 1:
                    return pd["request"]
                if pd.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                    return None

        return None

    # ── CapSolver ─────────────────────────────────────────────────────────────

    async def _solve_capsolver(self, image_bytes: bytes) -> Optional[str]:
        b64 = base64.b64encode(image_bytes).decode()
        api_key = settings.captcha_api_key
        timeout = settings.captcha_timeout_seconds

        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": api_key,
                    "task": {
                        "type": "ImageToTextTask",
                        "body": b64,
                    },
                },
            )
            data = resp.json()
            task_id = data.get("taskId")
            if not task_id:
                return None

            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(3)
                poll = await client.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": api_key, "taskId": task_id},
                )
                pd = poll.json()
                if pd.get("status") == "ready":
                    return pd.get("solution", {}).get("text")

        return None

    # ── Claude vision fallback ─────────────────────────────────────────────────

    async def _solve_claude(self, image_bytes: bytes) -> Optional[str]:
        import anthropic
        b64 = base64.b64encode(image_bytes).decode()
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a CAPTCHA image from an Indian government website. "
                            "Read the characters shown and respond with ONLY the characters, "
                            "nothing else. No spaces unless the CAPTCHA has spaces. "
                            "Be exact — case sensitive."
                        ),
                    },
                ],
            }],
        )
        text = response.content[0].text.strip()
        return text if text else None

    # ── Manual fallback ────────────────────────────────────────────────────────

    async def _solve_manual(self, image_bytes: bytes) -> Optional[str]:
        # In a real app this would push the CAPTCHA image to the customer app.
        # For local dev, log the base64 and wait.
        b64 = base64.b64encode(image_bytes).decode()
        log.info("captcha.manual_required", hint="Check CAPTCHA image and enter solution via API")
        # Caller handles waiting for the human response
        return None
