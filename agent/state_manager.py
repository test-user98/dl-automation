"""
Job state machine for the Sarathi agent.

Every job moves through explicit states. The full audit trail is persisted
so the agent can always resume from the last known-good checkpoint.
"""

import json
import time
import uuid
import asyncio
import aiosqlite
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from pathlib import Path

from config.settings import get_settings

settings = get_settings()


class JobStatus(str, Enum):
    CREATED            = "CREATED"
    OCR_PROCESSING     = "OCR_PROCESSING"
    OCR_DONE           = "OCR_DONE"
    AGENT_QUEUED       = "AGENT_QUEUED"
    AGENT_RUNNING      = "AGENT_RUNNING"
    WAITING_OTP        = "WAITING_OTP"          # paused — user must enter OTP
    OTP_RECEIVED       = "OTP_RECEIVED"
    PAYMENT_PENDING    = "PAYMENT_PENDING"
    SUBMITTED          = "SUBMITTED"
    PARTNER_HANDOFF    = "PARTNER_HANDOFF"
    COMPLETED          = "COMPLETED"
    FAILED_RETRYING    = "FAILED_RETRYING"       # hit error, retrying automatically
    STUCK_HUMAN_NEEDED = "STUCK_HUMAN_NEEDED"    # agent can't proceed, needs human
    CANCELLED          = "CANCELLED"
    FAILED             = "FAILED"


# Steps that are safe to restart from (agent can re-enter these without side effects)
RESTARTABLE_STEPS = {
    "state_selection",
    "popup_close",
    "service_selection",
    "dl_fetch",
    "captcha_solve",
}

# Destructive actions the agent must NEVER take unless explicitly unlocked
FORBIDDEN_ACTIONS = {
    "reset_form",
    "clear_all",
    "cancel_application",
    "delete_application",
    "go_back",       # using browser back — Sarathi breaks on back button
}


class StepLog:
    def __init__(
        self,
        step_name: str,
        status: str,
        observation: str = "",
        action_taken: str = "",
        tool_used: str = "",
        error: str = "",
        screenshot_path: str = "",
        duration_ms: int = 0,
    ):
        self.step_name     = step_name
        self.status        = status       # success | failed | skipped | retrying
        self.observation   = observation
        self.action_taken  = action_taken
        self.tool_used     = tool_used
        self.error         = error
        self.screenshot_path = screenshot_path
        self.duration_ms   = duration_ms
        self.timestamp     = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return self.__dict__


class Job:
    def __init__(
        self,
        job_id: str,
        customer_id: str,
        service: str,
        customer_data: dict,
        documents: dict,
        state_code: str = "",
    ):
        self.job_id        = job_id
        self.customer_id   = customer_id
        self.service       = service          # "DL_RENEWAL" | "DUPLICATE_DL" | etc.
        self.customer_data = customer_data    # extracted from OCR + user input
        self.documents     = documents        # {doc_type: file_path}
        self.state_code    = state_code or settings.sarathi_default_state

        self.status            = JobStatus.CREATED
        self.steps_completed: list[str] = []
        self.step_logs: list[dict]      = []
        self.retry_counts: dict[str, int] = {}

        # Live browser context — saved so we can restore after OTP wait
        self.application_number: str     = ""
        self.last_url: str               = ""
        self.session_cookies: list[dict] = []
        self.otp_pending_type: str       = ""   # "aadhaar" | "mobile"

        # Forbidden action override — set True only when human explicitly requests
        self.allow_reset: bool = False

        self.created_at    = datetime.utcnow().isoformat()
        self.updated_at    = datetime.utcnow().isoformat()
        self.error_message = ""

    def mark_step_done(self, step_name: str, log: StepLog):
        if step_name not in self.steps_completed:
            self.steps_completed.append(step_name)
        self.step_logs.append(log.to_dict())
        self.updated_at = datetime.utcnow().isoformat()

    def increment_retry(self, step_name: str) -> int:
        self.retry_counts[step_name] = self.retry_counts.get(step_name, 0) + 1
        return self.retry_counts[step_name]

    def is_step_done(self, step_name: str) -> bool:
        return step_name in self.steps_completed

    def to_dict(self) -> dict:
        return {
            "job_id":             self.job_id,
            "customer_id":        self.customer_id,
            "service":            self.service,
            "customer_data":      self.customer_data,
            "documents":          self.documents,
            "state_code":         self.state_code,
            "status":             self.status.value,
            "steps_completed":    self.steps_completed,
            "step_logs":          self.step_logs,
            "retry_counts":       self.retry_counts,
            "application_number": self.application_number,
            "last_url":           self.last_url,
            "session_cookies":    self.session_cookies,
            "otp_pending_type":   self.otp_pending_type,
            "allow_reset":        self.allow_reset,
            "created_at":         self.created_at,
            "updated_at":         self.updated_at,
            "error_message":      self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        job = cls(
            job_id=d["job_id"],
            customer_id=d["customer_id"],
            service=d["service"],
            customer_data=d["customer_data"],
            documents=d["documents"],
            state_code=d.get("state_code", ""),
        )
        job.status             = JobStatus(d["status"])
        job.steps_completed    = d.get("steps_completed", [])
        job.step_logs          = d.get("step_logs", [])
        job.retry_counts       = d.get("retry_counts", {})
        job.application_number = d.get("application_number", "")
        job.last_url           = d.get("last_url", "")
        job.session_cookies    = d.get("session_cookies", [])
        job.otp_pending_type   = d.get("otp_pending_type", "")
        job.allow_reset        = d.get("allow_reset", False)
        job.created_at         = d.get("created_at", "")
        job.updated_at         = d.get("updated_at", "")
        job.error_message      = d.get("error_message", "")
        return job


class StateManager:
    """Persists job state to SQLite (or Redis if configured)."""

    def __init__(self):
        self._db_path = settings.sqlite_db_path
        # In-memory OTP store: {job_id: otp_value}
        self._otp_store: dict[str, str] = {}

    async def _ensure_db(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    data   TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            await db.commit()

    async def save(self, job: Job):
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO jobs (job_id, data, updated_at) VALUES (?, ?, ?)",
                (job.job_id, json.dumps(job.to_dict()), job.updated_at),
            )
            await db.commit()

    async def load(self, job_id: str) -> Optional[Job]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            async with db.execute(
                "SELECT data FROM jobs WHERE job_id = ?", (job_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Job.from_dict(json.loads(row[0]))
        return None

    async def transition(self, job: Job, new_status: JobStatus, message: str = ""):
        job.status       = new_status
        job.updated_at   = datetime.utcnow().isoformat()
        if message:
            job.error_message = message
        await self.save(job)

    # ── OTP relay storage ──────────────────────────────────────────────────────

    async def store_otp(self, job_id: str, otp: str):
        """Customer submits OTP via API; stored here for agent to pick up."""
        self._otp_store[job_id] = otp

    async def poll_otp(self, job_id: str, timeout_seconds: int) -> Optional[str]:
        """Agent polls until OTP arrives or timeout."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if job_id in self._otp_store:
                return self._otp_store.pop(job_id)
            await asyncio.sleep(settings.otp_relay_poll_interval_seconds)
        return None

    @staticmethod
    def new_job(
        customer_id: str,
        service: str,
        customer_data: dict,
        documents: dict,
        state_code: str = "",
    ) -> Job:
        return Job(
            job_id=str(uuid.uuid4()),
            customer_id=customer_id,
            service=service,
            customer_data=customer_data,
            documents=documents,
            state_code=state_code,
        )
