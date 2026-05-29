"""
Human-in-the-loop escalation.

When the agent is genuinely stuck (exhausted retries + learning store has no answer),
it packages the full context — screenshot, what it observed, what it tried — and asks
the customer directly via the app. The customer's response is fed back to the agent
which then continues.

This keeps the customer in control while removing them from the happy path entirely.
"""

import asyncio
import base64
import json
import sys
import httpx
import structlog
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from agent.state_manager import Job, JobStatus, StateManager

log = structlog.get_logger(__name__)
settings = get_settings()


class HumanRequest:
    def __init__(
        self,
        job_id: str,
        customer_id: str,
        step_name: str,
        question: str,
        context: str,
        screenshot_b64: str = "",
        options: list[str] = None,
    ):
        self.job_id         = job_id
        self.customer_id    = customer_id
        self.step_name      = step_name
        self.question       = question        # what to ask the human
        self.context        = context         # what the agent observed
        self.screenshot_b64 = screenshot_b64  # what the agent sees right now
        self.options        = options or []   # suggested answers (optional)
        self.created_at     = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return self.__dict__


class HumanResponse:
    def __init__(self, answer: str, raw: dict):
        self.answer = answer    # free text or chosen option
        self.raw    = raw


class HumanLoop:
    """
    Sends a help request to the customer and waits for their response.

    Backends:
      - polling  : customer hits POST /api/agent/{job_id}/human-response  (default, works for demo)
      - webhook  : we POST to customer app webhook URL
      - firebase : push notification via FCM
    """

    def __init__(self, state_manager: StateManager):
        self._sm = state_manager
        # In-memory store for demo polling backend
        self._pending: dict[str, Optional[str]] = {}

    async def ask(
        self,
        job: Job,
        step_name: str,
        question: str,
        context: str,
        screenshot: Optional[bytes] = None,
        options: list[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> HumanResponse:
        """
        Block until the human responds or the timeout expires.
        Returns HumanResponse with the customer's answer.
        Pass timeout_seconds to override the global setting (e.g. 5 for organ donation).
        """
        screenshot_b64 = ""
        if screenshot:
            screenshot_b64 = base64.b64encode(screenshot).decode()

        request = HumanRequest(
            job_id        = job.job_id,
            customer_id   = job.customer_id,
            step_name     = step_name,
            question      = question,
            context       = context,
            screenshot_b64= screenshot_b64,
            options       = options or [],
        )

        log.info(
            "human_loop.asking",
            job_id   = job.job_id,
            step     = step_name,
            question = question,
        )

        await self._sm.transition(job, JobStatus.STUCK_HUMAN_NEEDED, question)

        effective_timeout = timeout_seconds if timeout_seconds is not None else (
            settings.human_loop_timeout_minutes * 60
        )

        backend = settings.human_loop_backend
        if backend == "webhook":
            await self._send_webhook(request)
            response = await self._poll_for_response(job.job_id, effective_timeout)
        elif backend == "firebase":
            await self._send_firebase(request)
            response = await self._poll_for_response(job.job_id, effective_timeout)
        elif backend == "console":
            # Terminal / test mode — print question, read answer from stdin
            try:
                response = await asyncio.wait_for(
                    self._ask_console(request),
                    timeout=effective_timeout,
                )
            except asyncio.TimeoutError:
                log.info("human_loop.console_timeout", step=step_name, timeout=effective_timeout)
                response = None
        else:
            # polling — customer calls POST /jobs/{id}/human-response via API
            self._pending[job.job_id] = None
            response = await self._poll_for_response(job.job_id, effective_timeout)

        if response is None:  # noqa: SIM102
            # Timeout — escalate to partner agent console
            log.warning("human_loop.timeout", job_id=job.job_id, step=step_name)
            return HumanResponse(answer="__timeout__", raw={})

        log.info("human_loop.response_received", job_id=job.job_id, answer=response)
        return HumanResponse(answer=response, raw={"raw": response})

    async def submit_response(self, job_id: str, answer: str):
        """Called by the API when the customer submits their answer."""
        self._pending[job_id] = answer

    async def _poll_for_response(self, job_id: str, timeout_seconds: int) -> Optional[str]:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            if self._pending.get(job_id) is not None:
                return self._pending.pop(job_id)
            await asyncio.sleep(3)
        return None

    async def _ask_console(self, request: HumanRequest) -> Optional[str]:
        """
        Terminal / test mode: print the stuck question to stdout and read
        the operator's answer from stdin. Non-blocking via run_in_executor.
        """
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  AGENT NEEDS HELP  —  step: {request.step_name}")
        print(sep)
        print(f"\n{request.question}\n")
        if request.context:
            print(f"Context: {request.context[:400]}\n")
        if request.options:
            print("Options:")
            for i, opt in enumerate(request.options, 1):
                print(f"  {i}. {opt}")
            print()

        is_otp = "otp" in request.question.lower() or "otp" in request.step_name.lower()
        prompt = "Enter OTP: " if is_otp else "Your answer (or press Enter to skip): "

        loop = asyncio.get_event_loop()

        async def read_stdin() -> Optional[str]:
            try:
                return await loop.run_in_executor(None, lambda: input(prompt).strip())
            except EOFError:
                log.warning("human_loop.console_unavailable", job_id=request.job_id, step=request.step_name)
                return None

        async def read_otp_file() -> str:
            otp_path = Path("data/manual_otp.txt")
            while True:
                if otp_path.exists():
                    raw = otp_path.read_text(encoding="utf-8").strip()
                    digits = "".join(ch for ch in raw if ch.isdigit())
                    if digits:
                        try:
                            otp_path.unlink()
                        except OSError:
                            pass
                        print(f"\nAgent received OTP from {otp_path}: '{digits}'\n")
                        return digits
                await asyncio.sleep(1)

        if is_otp:
            print("You can also reply to Codex with the OTP; Codex will write it to data/manual_otp.txt.\n")
            stdin_task = asyncio.create_task(read_stdin())
            file_task = asyncio.create_task(read_otp_file())
            answer = ""
            while not answer:
                done, pending = await asyncio.wait(
                    {stdin_task, file_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if file_task in done:
                    answer = file_task.result()
                    if not stdin_task.done():
                        stdin_task.cancel()
                    break
                if stdin_task in done:
                    answer = stdin_task.result() or ""
                    if answer or sys.stdin.isatty():
                        if not file_task.done():
                            file_task.cancel()
                        break
                    # No interactive stdin. Keep waiting for data/manual_otp.txt.
                    stdin_task = asyncio.create_task(read_stdin())
        else:
            answer = await read_stdin()
            if answer is None:
                return "__timeout__"

        if not answer:
            if not sys.stdin.isatty():
                log.warning("human_loop.console_no_tty", job_id=request.job_id, step=request.step_name)
                return "__timeout__"
            if is_otp:
                # OTP is mandatory — treat empty entry as timeout, agent will re-ask
                log.warning("human_loop.otp_empty_entry", job_id=request.job_id)
                return "__timeout__"
            answer = "skip"

        # If user typed a number, map it to the option text
        if answer.isdigit() and request.options:
            idx = int(answer) - 1
            if 0 <= idx < len(request.options):
                answer = request.options[idx]

        print(f"\nAgent received: '{answer}'\n{sep}\n")
        return answer

    async def _send_webhook(self, request: HumanRequest):
        if not settings.human_loop_webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    settings.human_loop_webhook_url,
                    json=request.to_dict(),
                    headers={"X-Secret": settings.api_secret_key},
                )
        except Exception as e:
            log.error("human_loop.webhook_failed", error=str(e))

    async def _send_firebase(self, request: HumanRequest):
        # Firebase push notification — requires firebase-admin SDK configured
        try:
            import firebase_admin
            from firebase_admin import messaging

            message = messaging.Message(
                notification=messaging.Notification(
                    title="Action needed for your DL application",
                    body=request.question,
                ),
                data={
                    "job_id":    request.job_id,
                    "step":      request.step_name,
                    "context":   request.context[:500],
                    "options":   json.dumps(request.options),
                },
                token=request.customer_id,  # FCM token stored as customer_id
            )
            messaging.send(message)
        except Exception as e:
            log.error("human_loop.firebase_failed", error=str(e))

    # ── Convenience builders for common stuck scenarios ────────────────────────

    @staticmethod
    def build_otp_question(otp_type: str) -> tuple[str, str, list[str]]:
        if otp_type == "aadhaar":
            return (
                "Please enter the OTP sent to your Aadhaar-linked mobile number",
                "The Sarathi portal has sent a one-time password (OTP) to the mobile "
                "number registered with your Aadhaar card. Please check your phone and "
                "enter the 6-digit OTP below.",
                [],
            )
        else:
            return (
                "Please enter the OTP sent to your registered mobile number",
                "The Sarathi portal has sent a one-time password (OTP) to your "
                "DL-registered mobile number. Please check your phone and enter it below.",
                [],
            )

    @staticmethod
    def build_stuck_question(observation: str, what_was_tried: str) -> tuple[str, str, list[str]]:
        return (
            "Our agent got stuck on your application. What should it do next?",
            f"The agent is currently seeing: {observation}\n\nIt has already tried: {what_was_tried}",
            [
                "Try again from this step",
                "Skip this step if optional",
                "Cancel — I'll do this myself",
            ],
        )
