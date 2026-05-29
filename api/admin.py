"""RTO operator dashboard API.

All endpoints require header `X-Admin-Secret` matching ADMIN_SECRET env
(falls back to API_SECRET_KEY if ADMIN_SECRET is empty).
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Header, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config.settings import get_settings
from agent.customer_store import CustomerStore, get_store
from api.deps import state_manager

log = structlog.get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/admin", tags=["admin"])


def _admin_secret() -> str:
    return os.environ.get("ADMIN_SECRET") or settings.api_secret_key


def require_admin(x_admin_secret: str = Header(None)):
    if x_admin_secret != _admin_secret():
        raise HTTPException(status_code=401, detail="Unauthorized")


def _store() -> CustomerStore:
    return get_store()


# ── Models ────────────────────────────────────────────────────────────────────

class NoteBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    operator_id: str = Field("operator", max_length=120)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/summary")
async def summary(x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    counts = await _store().counts()
    return {"counts": counts}


@router.get("/customers")
async def list_customers(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query(""),
    x_admin_secret: str = Header(None),
):
    require_admin(x_admin_secret)
    rows = await _store().list_customers(limit=limit, offset=offset, search=search)
    return {"count": len(rows), "items": rows}


@router.get("/customers/{phone_or_id}")
async def get_customer_detail(phone_or_id: str, x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    store = _store()
    cust = None
    if phone_or_id.upper().startswith("CUST-"):
        cust = await store.get_customer(phone_or_id)
    else:
        cust = await store.get_customer_by_phone(phone_or_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    apps = await store.list_applications(customer_id=cust.customer_id)
    docs = await store.list_documents(customer_id=cust.customer_id)
    return {
        "customer":     cust.to_dict(),
        "applications": apps,
        "documents":    docs,
    }


@router.get("/applications")
async def list_applications(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    service: str = Query(""),
    customer_id: str = Query(""),
    x_admin_secret: str = Header(None),
):
    require_admin(x_admin_secret)
    rows = await _store().list_applications(
        customer_id=customer_id, status=status, service=service,
        limit=limit, offset=offset,
    )
    return {"count": len(rows), "items": rows}


@router.get("/applications/{app_id}")
async def application_detail(app_id: str, x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    store = _store()
    app = await store.get_application(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    cust = await store.get_customer(app.customer_id)
    docs = await store.list_documents(app_id=app_id)
    notes = await store.list_notes(app_id)

    job_view = None
    if app.current_job_id:
        try:
            job = await state_manager.load(app.current_job_id)
            if job:
                from api.status_messages import customer_job_view
                job_view = {
                    "job_id":           job.job_id,
                    "status":           job.status.value,
                    "steps_completed":  job.steps_completed,
                    "step_logs":        job.step_logs[-10:],
                    "customer_view":    customer_job_view(job),
                }
        except Exception as e:
            log.warning("admin.job_view_failed", app_id=app_id, error=str(e))

    return {
        "application": {
            "app_id":             app.app_id,
            "customer_id":        app.customer_id,
            "service_type":       app.service_type,
            "status":             app.status,
            "application_number": app.application_number,
            "current_job_id":     app.current_job_id,
            "state_code":         app.state_code,
            "fee_inr":            app.fee_inr,
            "metadata":           app.metadata,
            "created_at":         app.created_at,
            "updated_at":         app.updated_at,
        },
        "customer":  cust.to_dict() if cust else None,
        "documents": docs,
        "notes":     notes,
        "job":       job_view,
    }


@router.post("/applications/{app_id}/notes")
async def add_note(app_id: str, body: NoteBody, x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    store = _store()
    app = await store.get_application(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")
    note = await store.add_note(app_id, body.text, operator_id=body.operator_id)
    return {"note": note}


@router.get("/documents/{doc_id}")
async def get_document_meta(doc_id: str, x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    doc = await _store().get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document": doc.__dict__}


@router.get("/documents/{doc_id}/preview")
async def preview_document(doc_id: str, x_admin_secret: str = Header(None)):
    require_admin(x_admin_secret)
    doc = await _store().get_document(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    path = Path(doc.file_path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=410, detail="File no longer available")
    mt = doc.mime_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return FileResponse(str(path), media_type=mt, filename=path.name)
