"""Durable customer-facing tables: customers, applications, documents, notes.

These are separate from agent.state_manager.Job — Job is the agent's working
state machine (ephemeral, per browser session). The records here survive job
restarts, multiple service requests by the same customer, and are what the
RTO operator dashboard reads.

Schema kept intentionally narrow. Heavy fields land in the `metadata` JSON
blob so we don't have to migrate on every new attribute.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from config.settings import get_settings

settings = get_settings()

DB_PATH = Path("data") / "customers.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_customer_id() -> str:
    # CUST- + 8 random uppercase alnum. Short, easy to read aloud, no ambiguous chars.
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "CUST-" + "".join(secrets.choice(alphabet) for _ in range(8))


def _new_app_id() -> str:
    return "APP-" + "".join(
        secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(10)
    )


def _new_doc_id() -> str:
    return "DOC-" + "".join(
        secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(10)
    )


def _normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    # Drop +91 country code if present
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]
    return digits[-10:] if len(digits) >= 10 else digits


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
  customer_id   TEXT PRIMARY KEY,
  phone         TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL DEFAULT '',
  email         TEXT NOT NULL DEFAULT '',
  kyc_status    TEXT NOT NULL DEFAULT 'unverified',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);

CREATE TABLE IF NOT EXISTS applications (
  app_id              TEXT PRIMARY KEY,
  customer_id         TEXT NOT NULL,
  service_type        TEXT NOT NULL,
  status              TEXT NOT NULL DEFAULT 'CREATED',
  application_number  TEXT NOT NULL DEFAULT '',
  current_job_id      TEXT NOT NULL DEFAULT '',
  state_code          TEXT NOT NULL DEFAULT '',
  fee_inr             INTEGER NOT NULL DEFAULT 0,
  metadata            TEXT NOT NULL DEFAULT '{}',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_apps_customer ON applications(customer_id);
CREATE INDEX IF NOT EXISTS idx_apps_status   ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_service  ON applications(service_type);

CREATE TABLE IF NOT EXISTS documents (
  doc_id        TEXT PRIMARY KEY,
  customer_id   TEXT NOT NULL,
  app_id        TEXT NOT NULL DEFAULT '',
  doc_type      TEXT NOT NULL,
  file_path     TEXT NOT NULL,
  mime_type     TEXT NOT NULL DEFAULT 'image/jpeg',
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  ocr_data      TEXT NOT NULL DEFAULT '{}',
  confidence    REAL NOT NULL DEFAULT 0.0,
  uploaded_at   TEXT NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
CREATE INDEX IF NOT EXISTS idx_docs_customer ON documents(customer_id);
CREATE INDEX IF NOT EXISTS idx_docs_app      ON documents(app_id);

CREATE TABLE IF NOT EXISTS notes (
  note_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  app_id       TEXT NOT NULL,
  operator_id  TEXT NOT NULL DEFAULT 'operator',
  text         TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  FOREIGN KEY (app_id) REFERENCES applications(app_id)
);
CREATE INDEX IF NOT EXISTS idx_notes_app ON notes(app_id);
"""


# ── Dataclasses (light, for typing) ───────────────────────────────────────────

@dataclass
class Customer:
    customer_id: str
    phone:       str
    name:        str = ""
    email:       str = ""
    kyc_status:  str = "unverified"
    created_at:  str = ""
    updated_at:  str = ""

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class Application:
    app_id:             str
    customer_id:        str
    service_type:       str
    status:             str = "CREATED"
    application_number: str = ""
    current_job_id:     str = ""
    state_code:         str = ""
    fee_inr:            int = 0
    metadata:           dict = field(default_factory=dict)
    created_at:         str = ""
    updated_at:         str = ""


@dataclass
class Document:
    doc_id:      str
    customer_id: str
    app_id:      str
    doc_type:    str
    file_path:   str
    mime_type:   str = "image/jpeg"
    size_bytes:  int = 0
    ocr_data:    dict = field(default_factory=dict)
    confidence:  float = 0.0
    uploaded_at: str = ""


# ── Store ─────────────────────────────────────────────────────────────────────

class CustomerStore:
    """Async aiosqlite wrapper. All methods are coroutines."""

    def __init__(self, db_path: Path = DB_PATH):
        self._path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ── Customers ────────────────────────────────────────────────────────────

    async def upsert_customer(self, phone: str, name: str = "", email: str = "") -> Customer:
        phone = _normalize_phone(phone)
        if not phone or len(phone) != 10:
            raise ValueError(f"Invalid phone: {phone!r}")

        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM customers WHERE phone = ?", (phone,)
            )).fetchone()

            if row:
                # Backfill name/email only if currently blank
                new_name  = row["name"]  or name
                new_email = row["email"] or email
                if new_name != row["name"] or new_email != row["email"]:
                    await db.execute(
                        "UPDATE customers SET name=?, email=?, updated_at=? WHERE customer_id=?",
                        (new_name, new_email, _now(), row["customer_id"]),
                    )
                    await db.commit()
                return Customer(**{k: row[k] for k in row.keys()})

            cid = _new_customer_id()
            now = _now()
            await db.execute(
                "INSERT INTO customers(customer_id, phone, name, email, kyc_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'unverified', ?, ?)",
                (cid, phone, name, email, now, now),
            )
            await db.commit()
            return Customer(customer_id=cid, phone=phone, name=name, email=email,
                            created_at=now, updated_at=now)

    async def get_customer_by_phone(self, phone: str) -> Optional[Customer]:
        phone = _normalize_phone(phone)
        if not phone:
            return None
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM customers WHERE phone = ?", (phone,)
            )).fetchone()
            return Customer(**dict(row)) if row else None

    async def get_customer(self, customer_id: str) -> Optional[Customer]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
            )).fetchone()
            return Customer(**dict(row)) if row else None

    async def list_customers(self, limit: int = 50, offset: int = 0,
                             search: str = "") -> list[dict]:
        search = (search or "").strip()
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            if search:
                like = f"%{search}%"
                q = ("SELECT c.*, "
                     "  (SELECT COUNT(*) FROM applications a WHERE a.customer_id=c.customer_id) AS app_count, "
                     "  (SELECT MAX(updated_at) FROM applications a WHERE a.customer_id=c.customer_id) AS last_activity "
                     "FROM customers c "
                     "WHERE c.phone LIKE ? OR c.name LIKE ? OR c.customer_id LIKE ? "
                     "ORDER BY c.updated_at DESC LIMIT ? OFFSET ?")
                rows = await (await db.execute(q, (like, like, like, limit, offset))).fetchall()
            else:
                q = ("SELECT c.*, "
                     "  (SELECT COUNT(*) FROM applications a WHERE a.customer_id=c.customer_id) AS app_count, "
                     "  (SELECT MAX(updated_at) FROM applications a WHERE a.customer_id=c.customer_id) AS last_activity "
                     "FROM customers c "
                     "ORDER BY c.updated_at DESC LIMIT ? OFFSET ?")
                rows = await (await db.execute(q, (limit, offset))).fetchall()
            return [dict(r) for r in rows]

    # ── Applications ─────────────────────────────────────────────────────────

    async def create_application(
        self,
        customer_id: str,
        service_type: str,
        state_code: str = "",
        fee_inr: int = 0,
        current_job_id: str = "",
        metadata: Optional[dict] = None,
    ) -> Application:
        app_id = _new_app_id()
        now = _now()
        md = json.dumps(metadata or {})
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO applications(app_id, customer_id, service_type, status, "
                "current_job_id, state_code, fee_inr, metadata, created_at, updated_at) "
                "VALUES (?, ?, ?, 'CREATED', ?, ?, ?, ?, ?, ?)",
                (app_id, customer_id, service_type, current_job_id,
                 state_code, fee_inr, md, now, now),
            )
            await db.commit()
        return Application(
            app_id=app_id, customer_id=customer_id, service_type=service_type,
            current_job_id=current_job_id, state_code=state_code, fee_inr=fee_inr,
            metadata=metadata or {}, created_at=now, updated_at=now,
        )

    async def update_application(
        self,
        app_id: str,
        *,
        status: Optional[str] = None,
        application_number: Optional[str] = None,
        current_job_id: Optional[str] = None,
        metadata_patch: Optional[dict] = None,
    ) -> Optional[Application]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM applications WHERE app_id = ?", (app_id,)
            )).fetchone()
            if not row:
                return None

            new_status     = status if status is not None else row["status"]
            new_app_no     = application_number if application_number is not None else row["application_number"]
            new_job        = current_job_id if current_job_id is not None else row["current_job_id"]
            existing_md    = json.loads(row["metadata"] or "{}")
            if metadata_patch:
                existing_md.update(metadata_patch)
            now = _now()
            await db.execute(
                "UPDATE applications SET status=?, application_number=?, current_job_id=?, "
                "metadata=?, updated_at=? WHERE app_id=?",
                (new_status, new_app_no, new_job, json.dumps(existing_md), now, app_id),
            )
            await db.commit()
            return await self.get_application(app_id)

    async def get_application(self, app_id: str) -> Optional[Application]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM applications WHERE app_id=?", (app_id,)
            )).fetchone()
            if not row:
                return None
            d = dict(row)
            d["metadata"] = json.loads(d.get("metadata") or "{}")
            return Application(**d)

    async def list_applications(
        self,
        *,
        customer_id: str = "",
        status: str = "",
        service: str = "",
        search: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        where = []
        params: list[Any] = []
        if customer_id:
            where.append("a.customer_id = ?"); params.append(customer_id)
        if status:
            where.append("a.status = ?"); params.append(status)
        if service:
            where.append("a.service_type = ?"); params.append(service)
        if search:
            like = f"%{search.strip()}%"
            where.append(
                "(a.app_id LIKE ? OR a.application_number LIKE ? "
                " OR c.phone LIKE ? OR c.name LIKE ?)"
            )
            params.extend([like, like, like, like])
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        q = (
            "SELECT a.*, c.phone AS customer_phone, c.name AS customer_name "
            "FROM applications a LEFT JOIN customers c ON a.customer_id=c.customer_id"
            f"{clause} ORDER BY a.updated_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(q, params)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["metadata"] = json.loads(d.get("metadata") or "{}")
                out.append(d)
            return out

    # ── Documents ────────────────────────────────────────────────────────────

    async def add_document(
        self,
        customer_id: str,
        doc_type: str,
        file_path: str,
        *,
        app_id: str = "",
        mime_type: str = "image/jpeg",
        ocr_data: Optional[dict] = None,
        confidence: float = 0.0,
    ) -> Document:
        doc_id = _new_doc_id()
        now = _now()
        size = 0
        try:
            size = os.path.getsize(file_path)
        except OSError:
            pass
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO documents(doc_id, customer_id, app_id, doc_type, file_path, "
                "mime_type, size_bytes, ocr_data, confidence, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, customer_id, app_id, doc_type, file_path, mime_type, size,
                 json.dumps(ocr_data or {}), float(confidence), now),
            )
            await db.commit()
        return Document(
            doc_id=doc_id, customer_id=customer_id, app_id=app_id, doc_type=doc_type,
            file_path=file_path, mime_type=mime_type, size_bytes=size,
            ocr_data=ocr_data or {}, confidence=confidence, uploaded_at=now,
        )

    async def get_document(self, doc_id: str) -> Optional[Document]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM documents WHERE doc_id=?", (doc_id,)
            )).fetchone()
            if not row:
                return None
            d = dict(row)
            d["ocr_data"] = json.loads(d.get("ocr_data") or "{}")
            return Document(**d)

    async def list_documents(self, *, customer_id: str = "", app_id: str = "") -> list[dict]:
        where = []; params: list[Any] = []
        if customer_id:
            where.append("customer_id = ?"); params.append(customer_id)
        if app_id:
            where.append("app_id = ?"); params.append(app_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                f"SELECT * FROM documents{clause} ORDER BY uploaded_at DESC",
                params,
            )).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["ocr_data"] = json.loads(d.get("ocr_data") or "{}")
                out.append(d)
            return out

    # ── Notes ────────────────────────────────────────────────────────────────

    async def add_note(self, app_id: str, text: str, operator_id: str = "operator") -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("Empty note")
        async with aiosqlite.connect(self._path) as db:
            now = _now()
            cur = await db.execute(
                "INSERT INTO notes(app_id, operator_id, text, created_at) VALUES (?, ?, ?, ?)",
                (app_id, operator_id, text, now),
            )
            await db.commit()
            return {
                "note_id":    cur.lastrowid,
                "app_id":     app_id,
                "operator_id":operator_id,
                "text":       text,
                "created_at": now,
            }

    async def list_notes(self, app_id: str) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM notes WHERE app_id=? ORDER BY created_at DESC",
                (app_id,),
            )).fetchall()
            return [dict(r) for r in rows]

    # ── Aggregates / counts for dashboard ────────────────────────────────────

    async def counts(self) -> dict:
        async with aiosqlite.connect(self._path) as db:
            customers = (await (await db.execute("SELECT COUNT(*) FROM customers")).fetchone())[0]
            apps      = (await (await db.execute("SELECT COUNT(*) FROM applications")).fetchone())[0]
            docs      = (await (await db.execute("SELECT COUNT(*) FROM documents")).fetchone())[0]
            by_status_rows = await (await db.execute(
                "SELECT status, COUNT(*) FROM applications GROUP BY status"
            )).fetchall()
            by_status = {row[0]: row[1] for row in by_status_rows}
            return {
                "customers":    customers,
                "applications": apps,
                "documents":    docs,
                "by_status":    by_status,
            }


# ── Module-level singleton-ish ────────────────────────────────────────────────

_store: Optional[CustomerStore] = None


def get_store() -> CustomerStore:
    global _store
    if _store is None:
        _store = CustomerStore()
    return _store
