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

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.settings import get_settings
from agent.state_manager import StateManager, JobStatus
from agent.learning_store import LearningStore
from agent.human_loop import HumanLoop
from tools.ocr_service import OCRService
from orchestrator import Orchestrator

settings = get_settings()
app = FastAPI(title="Sarathi Agent API", version="1.0.0")

# Singletons — shared across requests
_state_manager  = StateManager()
_learning_store = LearningStore()
_human_loop     = HumanLoop(_state_manager)
_ocr            = OCRService()
_orchestrator   = Orchestrator(_state_manager, _learning_store, _human_loop)

# ── Onboarding router ─────────────────────────────────────────────────────────
from api.onboard import router as onboard_router
app.include_router(onboard_router)

# ── Serve customer-facing web UI ──────────────────────────────────────────────
_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_frontend_dir)), name="static")

@app.get("/", include_in_schema=False)
async def serve_ui():
    return FileResponse(str(_frontend_dir / "index.html"))


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
    }


@app.post("/jobs/{job_id}/otp", dependencies=[Depends(verify_secret)])
async def submit_otp(job_id: str, body: OTPSubmission):
    job = await _state_manager.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.WAITING_OTP:
        raise HTTPException(status_code=400, detail=f"Job is not waiting for OTP (status={job.status.value})")

    await _state_manager.store_otp(job_id, body.otp.strip())
    return {"message": "OTP received, agent resuming"}


@app.post("/jobs/{job_id}/human-response", dependencies=[Depends(verify_secret)])
async def submit_human_response(job_id: str, body: HumanResponse):
    job = await _state_manager.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    await _human_loop.submit_response(job_id, body.answer)
    return {"message": "Response received, agent resuming"}


# ── SSE status stream ──────────────────────────────────────────────────────────

@app.get("/jobs/{job_id}/stream")
async def stream_job_status(job_id: str):
    """
    Server-sent events stream — client subscribes and gets live updates.
    The customer app uses this to show real-time step progress.
    """
    async def event_generator():
        last_step_count = 0
        last_status = None

        while True:
            job = await _state_manager.load(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'job not found'})}\n\n"
                break

            current_step_count = len(job.steps_completed)
            current_status     = job.status.value

            if current_step_count != last_step_count or current_status != last_status:
                last_step_count = current_step_count
                last_status     = current_status

                payload = {
                    "status":             current_status,
                    "steps_completed":    job.steps_completed,
                    "application_number": job.application_number,
                    "otp_pending_type":   job.otp_pending_type,
                    "last_step":          job.steps_completed[-1] if job.steps_completed else "",
                    "error":              job.error_message,
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
    return {
        "status":          "ok",
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
