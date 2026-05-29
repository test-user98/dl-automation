"""
FastAPI server — the bridge between the customer app and the agent.

Endpoints:
  POST /jobs                    — create a new DL renewal job
  GET  /jobs/{job_id}           — get current job status + step log
  POST /jobs/{job_id}/otp       — customer submits their OTP
  POST /jobs/{job_id}/human-response  — customer answers a human-loop question
  GET  /jobs/{job_id}/stream    — SSE stream of real-time step updates
  POST /ocr/extract             — extract customer data from uploaded doc image
"""

import asyncio
import json
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, Query
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config.settings import get_settings
from config.logging_setup import configure_logging
from agent.state_manager import JobStatus
from agent.customer_seed import seed_if_empty
from api.deps import state_manager, learning_store, human_loop, ocr_service, orchestrator, customer_store
from api.status_messages import customer_job_view

settings = get_settings()
# Redacts API keys, OTPs, mobile/DL/Aadhaar numbers before they reach stdout.
configure_logging(level="INFO", json_output=False)

app = FastAPI(title="Sarathi Agent API", version="1.0.0")

# CORS — same-origin works out of the box. If a deployment needs to allow a
# specific external frontend domain, set CORS_ALLOW_ORIGINS in env (comma list).
import os as _os
_cors_origins = [
    o.strip() for o in (_os.environ.get("CORS_ALLOW_ORIGINS") or "").split(",") if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Secret", "X-Admin-Secret"],
    )


@app.on_event("startup")
async def _bootstrap_customer_store() -> None:
    """Initialise tables and seed demo data so the operator dashboard is non-empty."""
    await customer_store.init()
    report = await seed_if_empty(customer_store)
    import structlog
    structlog.get_logger(__name__).info("customer_store.bootstrap", report=report)

# Singletons — shared across requests
_state_manager  = state_manager
_learning_store = learning_store
_human_loop     = human_loop
_ocr            = ocr_service
_orchestrator   = orchestrator

# ── Onboarding + Admin + Lookup routers ───────────────────────────────────────
from api.onboard import router as onboard_router
from api.admin   import router as admin_router
from api.lookup  import router as lookup_router
app.include_router(onboard_router)
app.include_router(admin_router)
app.include_router(lookup_router)

# ── Serve customer-facing web UI ──────────────────────────────────────────────
_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")

# Disable HTML caching so a deploy reaches every open customer tab on next
# reload — frontend JS lives inline in these files, and a stale cached copy
# would silently miss OTP/captcha screen renders.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(str(_frontend_dir / "index.html"), headers=_NO_CACHE_HEADERS)


@app.get("/admin", include_in_schema=False)
async def serve_admin_ui():
    """RTO operator dashboard. The HTML itself is public — every data fetch
    inside it requires the X-Admin-Secret header, so showing the shell is fine."""
    return FileResponse(str(_frontend_dir / "admin.html"), headers=_NO_CACHE_HEADERS)


# Tiny transparent PNG so browsers stop logging 404 on every page load.
_FAVICON_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10"
    b"\x08\x06\x00\x00\x00\x1f\xf3\xffa\x00\x00\x00\x19IDATx\x9cc\xfc\xff"
    b"\xff?\x03)\x80\x89\x81DA\x00\x16\x18\x00\x00\xc0\x00\x01[F\xae0Q"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_BYTES, media_type="image/png")


# ── Auth ───────────────────────────────────────────────────────────────────────

def verify_secret(x_secret: str = Header(None)):
    if x_secret != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Models ─────────────────────────────────────────────────────────────────────

class CreateJobRequest(BaseModel):
    customer_id: str
    service: str = "DL_RENEWAL"
    state_code: str = ""
    customer_data: dict = {}
    documents: dict = {}       # {doc_type: file_path_on_server}


class OTPSubmission(BaseModel):
    otp: str


class HumanResponse(BaseModel):
    answer: str


# ── Job endpoints ──────────────────────────────────────────────────────────────

@app.post("/jobs", dependencies=[Depends(verify_secret)])
async def create_job(req: CreateJobRequest):
    job = _state_manager.new_job(
        customer_id  = req.customer_id,
        service      = req.service,
        customer_data= req.customer_data,
        documents    = req.documents,
        state_code   = req.state_code,
    )
    await _state_manager.save(job)

    # Start the agent in the background
    asyncio.create_task(_orchestrator.run_job(job.job_id))

    return {"job_id": job.job_id, "status": job.status.value}


@app.get("/jobs/{job_id}", dependencies=[Depends(verify_secret)])
async def get_job(job_id: str):
    job = await _state_manager.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return {
        "job_id":             job.job_id,
        "status":             job.status.value,
        "steps_completed":    job.steps_completed,
        "application_number": job.application_number,
        "otp_pending_type":   job.otp_pending_type,
        "error_message":      job.error_message,
        "last_url":           job.last_url,
        "step_logs":          job.step_logs[-5:],  # last 5 logs
        "updated_at":         job.updated_at,
        "customer_view":      customer_job_view(job),
    }


@app.post("/jobs/{job_id}/otp", dependencies=[Depends(verify_secret)])
async def submit_otp(job_id: str, body: OTPSubmission):
    job = await _state_manager.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in {JobStatus.WAITING_OTP, JobStatus.STUCK_HUMAN_NEEDED}:
        raise HTTPException(status_code=400, detail=f"Job is not waiting for OTP (status={job.status.value})")

    # Guardrail: STUCK_HUMAN_NEEDED is used for multiple customer prompts
    # (service selection, confirmations, captcha, etc.). Accept OTP only when
    # the pending request is genuinely OTP-related.
    if job.status == JobStatus.STUCK_HUMAN_NEEDED:
        pending = job.customer_data.get("_pending_customer_request") or {}
        action_type = str((pending or {}).get("action_type", "")).strip().lower()
        question_blob = " ".join(
            str((pending or {}).get(k, "")) for k in ("step_name", "question", "context")
        ).lower()
        if action_type and action_type != "otp":
            raise HTTPException(
                status_code=409,
                detail=f"Job currently needs '{action_type}', not OTP. Please answer the current prompt.",
            )
        if action_type != "otp" and "otp" not in question_blob:
            raise HTTPException(
                status_code=409,
                detail="Job is waiting for a different customer input, not OTP.",
            )

    otp = body.otp.strip()
    await _state_manager.store_otp(job_id, otp)
    await _human_loop.submit_response(job_id, otp)
    # Prevent stale UI loops: once OTP is accepted from customer, the job is no
    # longer "waiting for OTP" from the frontend point of view. The agent will
    # continue and can re-open OTP flow if Sarathi rejects/expires it.
    await _state_manager.transition(job, JobStatus.AGENT_RUNNING)
    return {"message": "OTP received, agent resuming"}


@app.post("/jobs/{job_id}/human-response", dependencies=[Depends(verify_secret)])
async def submit_human_response(job_id: str, body: HumanResponse):
    job = await _state_manager.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.STUCK_HUMAN_NEEDED:
        raise HTTPException(
            status_code=400,
            detail=f"Job is not waiting for customer input (status={job.status.value})",
        )

    await _human_loop.submit_response(job_id, body.answer)
    # Same as OTP endpoint: as soon as customer answer is accepted, move UI out
    # of waiting state. Agent will re-open another question only if truly needed.
    await _state_manager.transition(job, JobStatus.AGENT_RUNNING)
    return {"message": "Response received, agent resuming"}


# ── SSE status stream ──────────────────────────────────────────────────────────

@app.get("/jobs/{job_id}/stream")
async def stream_job_status(
    job_id: str,
    secret: str = Query("", min_length=0),
    x_secret: str = Header(None),
):
    """
    Server-sent events stream — client subscribes and gets live updates.
    The customer app uses this to show real-time step progress.
    """
    if (x_secret or secret) != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def event_generator():
        last_step_count = 0
        last_status = None
        last_updated_at = ""
        last_log_count = 0

        while True:
            job = await _state_manager.load(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'job not found'})}\n\n"
                break

            current_step_count = len(job.steps_completed)
            current_status     = job.status.value
            current_updated_at = job.updated_at
            current_log_count  = len(job.step_logs)

            if (
                current_step_count != last_step_count
                or current_status != last_status
                or current_updated_at != last_updated_at
                or current_log_count != last_log_count
            ):
                last_step_count = current_step_count
                last_status     = current_status
                last_updated_at = current_updated_at
                last_log_count  = current_log_count

                payload = {
                    "status":             current_status,
                    "steps_completed":    job.steps_completed,
                    "application_number": job.application_number,
                    "otp_pending_type":   job.otp_pending_type,
                    "last_step":          job.steps_completed[-1] if job.steps_completed else "",
                    "error":              job.error_message,
                    "step_logs":          job.step_logs[-5:],
                    "updated_at":         job.updated_at,
                    "customer_view":      customer_job_view(job),
                }
                yield f"data: {json.dumps(payload)}\n\n"

            # Stop streaming on terminal states
            if job.status in (
                JobStatus.COMPLETED, JobStatus.FAILED,
                JobStatus.CANCELLED, JobStatus.SUBMITTED,
            ):
                break

            await asyncio.sleep(2)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── OCR endpoint ───────────────────────────────────────────────────────────────

@app.post("/ocr/extract", dependencies=[Depends(verify_secret)])
async def ocr_extract(
    doc_type: str = Form(...),        # "driving_license" | "aadhaar" | "address_proof"
    file: UploadFile = File(...),
):
    """
    Upload a document image, get back structured JSON customer data.
    Used by the customer app before creating a job.
    """
    upload_dir = Path("./uploads")
    upload_dir.mkdir(exist_ok=True)
    file_path = str(upload_dir / file.filename)

    with open(file_path, "wb") as f:
        f.write(await file.read())

    if doc_type == "driving_license":
        data = await _ocr.extract_driving_license(file_path)
    elif doc_type == "aadhaar":
        data = await _ocr.extract_aadhaar(file_path)
    elif doc_type == "address_proof":
        data = await _ocr.extract_address_proof(file_path)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown doc_type: {doc_type}")

    return {"doc_type": doc_type, "extracted": data}


# ── Learning store — for ops/debugging ────────────────────────────────────────

@app.get("/learning/scenarios", dependencies=[Depends(verify_secret)])
async def list_scenarios():
    import aiosqlite
    rows = []
    async with aiosqlite.connect(settings.learning_db_path) as db:
        async with db.execute(
            "SELECT scenario_id, step_name, description, solution, success_count, fail_count "
            "FROM scenarios ORDER BY success_count DESC"
        ) as cur:
            cols = [d[0] for d in cur.description]
            async for row in cur:
                rows.append(dict(zip(cols, row)))
    return {"count": len(rows), "scenarios": rows}


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    import os
    return {
        "status":          "ok",
        "commit":          os.environ.get("GIT_COMMIT_SHA", "unknown"),
        "llm_primary":     settings.llm_primary,
        "llm_fallback":    settings.llm_fallback,
        "primary_paused":  settings.llm_primary_paused,
        "active_model":    settings.resolved_model_for(
                               settings.llm_fallback if settings.llm_primary_paused
                               else settings.llm_primary
                           ),
        "captcha_provider": settings.captcha_provider,
        "state_backend":   settings.state_backend,
    }
