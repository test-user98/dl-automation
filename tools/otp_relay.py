"""
OTP relay — pauses the agent and waits for the customer to enter the OTP
they received on their phone, then returns it to the agent.

The job's StateManager stores OTPs submitted via the API.
The agent calls wait_for_otp() which polls until it arrives or times out.
"""

import structlog
from typing import Optional

from agent.state_manager import Job, JobStatus, StateManager
from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


class OTPRelay:
    def __init__(self, state_manager: StateManager):
        self._sm = state_manager

    async def wait_for_otp(self, job: Job, otp_type: str) -> Optional[str]:
        """
        Transition job to WAITING_OTP, block until OTP arrives, return it.

        otp_type: "mobile" | "aadhaar"
        """
        job.otp_pending_type = otp_type
        await self._sm.transition(job, JobStatus.WAITING_OTP)
        await self._sm.save(job)

        log.info("otp.waiting", job_id=job.job_id, type=otp_type)

        otp = await self._sm.poll_otp(job.job_id, settings.otp_wait_timeout_seconds)

        if otp:
            log.info("otp.received", job_id=job.job_id, type=otp_type)
            await self._sm.transition(job, JobStatus.OTP_RECEIVED)
            job.otp_pending_type = ""
        else:
            log.warning("otp.timeout", job_id=job.job_id, type=otp_type)

        return otp

    async def submit_otp(self, job_id: str, otp: str):
        """Called by the customer via the API to submit their OTP."""
        await self._sm.store_otp(job_id, otp)
        log.info("otp.submitted_by_customer", job_id=job_id)
