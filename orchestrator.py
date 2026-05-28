"""
Orchestrator — owns the full job lifecycle.

Responsibilities:
  - Boot the browser
  - Start the agent brain
  - Handle restarts if the browser session dies mid-job
  - Seed the learning store on first run
  - Mark jobs terminal (COMPLETED / FAILED)
"""

import asyncio
import structlog

from config.settings import get_settings
from agent.state_manager import StateManager, JobStatus
from agent.learning_store import LearningStore
from agent.human_loop import HumanLoop
from agent.brain import AgentBrain
from browser.controller import BrowserController
from flows.dl_renewal import DL_RENEWAL_STEPS

log = structlog.get_logger(__name__)
settings = get_settings()

SARATHI_HOME = f"{settings.sarathi_base_url}/sarathiHomePublic.do"


class Orchestrator:
    def __init__(
        self,
        state_manager: StateManager,
        learning_store: LearningStore,
        human_loop: HumanLoop,
    ):
        self._sm  = state_manager
        self._ls  = learning_store
        self._hl  = human_loop
        self._seeded = False

    async def run_job(self, job_id: str):
        """
        Entry point for running a job. Called as an async background task.
        Handles restarts transparently.
        """
        # Seed known Sarathi quirks into learning store (only once per process)
        if not self._seeded:
            await self._ls.seed_known_scenarios()
            self._seeded = True

        job = await self._sm.load(job_id)
        if not job:
            log.error("orchestrator.job_not_found", job_id=job_id)
            return

        max_restarts = 3
        restart_count = 0

        while restart_count <= max_restarts:
            browser = BrowserController()

            try:
                # Restore session cookies if we have them (resuming after OTP wait etc.)
                saved_cookies = job.session_cookies or []
                await browser.start(saved_cookies=saved_cookies)

                brain = AgentBrain(
                    browser       = browser,
                    state_manager = self._sm,
                    learning_store= self._ls,
                    human_loop    = self._hl,
                )

                # Always start from Sarathi home (fresh nav, session restored via cookies)
                await browser.goto(SARATHI_HOME)

                job = await brain.run(job)
                break  # Success — exit restart loop

            except Exception as e:
                restart_count += 1
                log.error(
                    "orchestrator.run_failed",
                    job_id=job_id,
                    attempt=restart_count,
                    error=str(e),
                )

                if restart_count > max_restarts:
                    await self._sm.transition(
                        job, JobStatus.FAILED,
                        f"Failed after {max_restarts} restarts: {str(e)}"
                    )
                    break

                log.info("orchestrator.restarting", job_id=job_id, attempt=restart_count)
                await asyncio.sleep(5)

            finally:
                try:
                    await browser.stop()
                except Exception:
                    pass

        # Final status
        if job.application_number:
            await self._sm.transition(job, JobStatus.COMPLETED)
            log.info(
                "orchestrator.completed",
                job_id=job_id,
                application_number=job.application_number,
            )
        elif job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            await self._sm.transition(job, JobStatus.FAILED, "Job ended without application number")

        log.info("orchestrator.done", job_id=job_id, final_status=job.status.value)


# ── Standalone entry point for local testing ───────────────────────────────────

async def run_test_job():
    """
    Run a test DL renewal job with dummy data.
    Replace customer_data fields with real values to test against Sarathi.
    """
    import logging
    import structlog

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

    sm = StateManager()
    ls = LearningStore()
    hl = HumanLoop(sm)
    orch = Orchestrator(sm, ls, hl)

    # Replace these with real customer data for a live test
    job = sm.new_job(
        customer_id  = "test_customer_001",
        service      = "DL_RENEWAL",
        state_code   = "DL",
        customer_data= {
            "dl_number":      "DL-0420110012345",
            "dob":            "15-05-1990",
            "name":           "Rahul Sharma",
            "mobile_number":  "9876543210",
            "email":          "",                     # optional
            "address":        "123 Main Street, New Delhi",
            "pin_code":       "110001",
            "state":          "Delhi",
            "state_code":     "DL",
            "rto_code":       "DL-04",
            "blood_group":    "B+",
            "gender":         "M",
            "aadhaar_number": "",                     # optional
        },
        documents={
            "photo":               "./sample_docs/photo.jpg",
            "signature":           "./sample_docs/signature.jpg",
            "address_proof":       "./sample_docs/aadhaar.jpg",
            "form1_self_declaration": "./sample_docs/form1.pdf",
        },
    )

    await sm.save(job)
    print(f"\n{'='*60}")
    print(f"Job created: {job.job_id}")
    print(f"Service: {job.service}")
    print(f"{'='*60}\n")

    await orch.run_job(job.job_id)

    final = await sm.load(job.job_id)
    print(f"\n{'='*60}")
    print(f"FINAL STATUS : {final.status.value}")
    print(f"APP NUMBER   : {final.application_number or 'N/A'}")
    print(f"STEPS DONE   : {final.steps_completed}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(run_test_job())
