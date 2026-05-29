"""Customer self-service status lookup by phone or customer ID.

Returns a redacted view — no DL number, no DOB, no internal IDs unless
explicitly looked up. Intended for the customer to come back later
("what happened to my DL renewal?") without re-creating the job.

In-memory rate limit: max 10 calls per (key) per minute.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query

from agent.customer_store import get_store
from api.status_messages import customer_job_view
from api.deps import state_manager

router = APIRouter(tags=["lookup"])

_RATE_BUCKET: dict[str, deque] = defaultdict(deque)
_RATE_WINDOW_S = 60
_RATE_MAX      = 10


def _rate_check(key: str) -> None:
    now = time.monotonic()
    bucket = _RATE_BUCKET[key]
    while bucket and now - bucket[0] > _RATE_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= _RATE_MAX:
        raise HTTPException(status_code=429, detail="Too many lookups — please wait a minute.")
    bucket.append(now)


def _redact_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())[-10:]
    if len(digits) < 4:
        return ""
    return f"+91 {digits[:2]}******{digits[-2:]}"


@router.get("/lookup")
async def lookup(
    phone: str = Query("", min_length=0, max_length=15),
    customer_id: str = Query("", min_length=0, max_length=40),
):
    if not phone and not customer_id:
        raise HTTPException(status_code=400, detail="Provide phone or customer_id")
    key = (phone or customer_id).strip()
    _rate_check(key)

    store = get_store()
    cust = None
    if customer_id:
        cust = await store.get_customer(customer_id)
    if not cust and phone:
        cust = await store.get_customer_by_phone(phone)
    if not cust:
        # Don't leak whether the phone is registered or not. Return empty list.
        return {"found": False, "applications": []}

    apps = await store.list_applications(customer_id=cust.customer_id)
    out_apps = []
    for a in apps:
        view = None
        if a.get("current_job_id"):
            try:
                job = await state_manager.load(a["current_job_id"])
                if job:
                    view = customer_job_view(job)
            except Exception:
                view = None
        out_apps.append({
            "app_id":             a["app_id"],
            "service_type":       a["service_type"],
            "status":             a["status"],
            "application_number": a.get("application_number", ""),
            "created_at":         a.get("created_at", ""),
            "updated_at":         a.get("updated_at", ""),
            "customer_view":      view or {
                "headline": _headline_for(a["status"]),
                "subline":  _subline_for(a["status"], a.get("application_number")),
                "phase":    _phase_for(a["status"]),
            },
        })

    return {
        "found": True,
        "customer": {
            "customer_id": cust.customer_id,
            "name":        cust.name,
            "phone_mask":  _redact_phone(cust.phone),
        },
        "applications": out_apps,
    }


def _phase_for(status: str) -> str:
    return {
        "CREATED":            "connecting",
        "OCR_PROCESSING":     "connecting",
        "AGENT_QUEUED":       "connecting",
        "AGENT_RUNNING":      "filling",
        "WAITING_OTP":        "waiting",
        "STUCK_HUMAN_NEEDED": "waiting",
        "PAYMENT_PENDING":    "submitting",
        "SUBMITTED":          "done",
        "COMPLETED":          "done",
        "FAILED_RETRYING":    "retrying",
        "FAILED":             "failed",
    }.get(status, "filling")


def _headline_for(status: str) -> str:
    return {
        "CREATED":            "Application received",
        "AGENT_RUNNING":      "Filling your application",
        "WAITING_OTP":        "Waiting for your OTP",
        "STUCK_HUMAN_NEEDED": "We need one detail from you",
        "PAYMENT_PENDING":    "Confirming payment",
        "SUBMITTED":          "Application submitted",
        "COMPLETED":          "Completed",
        "FAILED_RETRYING":    "Retrying",
        "FAILED":             "Couldn't complete",
    }.get(status, "In progress")


def _subline_for(status: str, app_no: str) -> str:
    if status in ("SUBMITTED", "COMPLETED") and app_no:
        return f"Acknowledgement: {app_no}"
    if status == "WAITING_OTP":
        return "Open the customer app to enter your OTP."
    if status == "STUCK_HUMAN_NEEDED":
        return "Open the customer app to answer the agent's question."
    if status == "FAILED":
        return "Your details are saved — please restart the application."
    return "Your application is in progress."
