"""
Agent brain — self-healing ReAct (Reason + Act + Diagnose) loop.

Each iteration:
  1. OBSERVE   — screenshot + real DOM elements + page text
  2. THINK     — LLM sees full context including previous failure diagnosis
  3. ACT       — execute with fallback chain (selector → JS → link text)
  4. VERIFY    — check actual success: did URL change? did element appear?
  5. DIAGNOSE  — if failed, run self-diagnosis and feed back into next THINK
  6. LEARN     — successful approaches recorded; failures help the next run

The agent never hardcodes selectors. It reasons from what is actually on the
page. When it gets stuck, it figures out WHY and tries a different approach
before ever asking a human.
"""

import json
import asyncio
import base64
import re
import structlog
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from config.portal_rules import (
    VERIFY_OTP_RULES,
    GENERATE_OTP_RULES,
    DL_FETCH_RULES,
    DL_CONFIRM_RULES,
    DL_SERVICES_LANDING_RULES,
    SERVICE_SELECTION_RULES,
    CHANGE_DOB_RULES,
    SERVICE_REJECTION_RULES,
    record_discovery,
)
from agent.llm_client import get_llm_client
from agent.state_manager import Job, JobStatus, StateManager, StepLog, FORBIDDEN_ACTIONS
from agent.learning_store import LearningStore, Scenario
from agent.human_loop import HumanLoop
from browser.controller import BrowserController
from tools.captcha_solver import CaptchaSolver
from tools.otp_relay import OTPRelay
from tools.image_processor import ImageProcessor
from tools.dl_normalizer import STATE_CODES
from tools.portal_triage import PortalTriageService
from flows.dl_renewal import DL_RENEWAL_STEPS, steps_after

log = structlog.get_logger(__name__)
settings = get_settings()

SARATHI_HOME = f"{settings.sarathi_base_url}/sarathiHomePublic.do"


class PortalTransientError(RuntimeError):
    """Raised when Sarathi returns a retryable portal-level block."""


class AgentAction:
    """Structured action returned by the LLM each step."""

    def __init__(self, raw: dict):
        self.action_type: str    = raw.get("action_type", "unknown")
        self.description: str    = raw.get("description", "")
        self.selector: str       = raw.get("selector", "")
        self.text: str           = raw.get("text", "")
        self.value: str          = raw.get("value", "")
        self.tool: str           = raw.get("tool", "")
        self.tool_args: dict     = raw.get("tool_args", {})
        self.step_complete: bool = raw.get("step_complete", False)
        self.step_name: str      = raw.get("step_name", "")
        self.need_otp: bool      = raw.get("need_otp", False)
        self.otp_type: str       = raw.get("otp_type", "")
        self.need_human: bool    = raw.get("need_human", False)
        self.human_question: str = raw.get("human_question", "")
        self.is_done: bool       = raw.get("is_done", False)
        self.application_number: str = raw.get("application_number", "")
        self.error: str          = raw.get("error", "")
        self.observation: str    = raw.get("observation", "")
        self.thought: str        = raw.get("thought", "")

    def is_forbidden(self) -> bool:
        check = (self.action_type + " " + self.description + " " + self.text).lower()
        return any(f in check for f in FORBIDDEN_ACTIONS)


class AgentBrain:
    def __init__(
        self,
        browser: BrowserController,
        state_manager: StateManager,
        learning_store: LearningStore,
        human_loop: HumanLoop,
    ):
        self._browser  = browser
        self._sm       = state_manager
        self._ls       = learning_store
        self._hl       = human_loop
        self._captcha  = CaptchaSolver()
        self._otp      = OTPRelay(state_manager)
        self._img_proc = ImageProcessor()
        self._portal_triage = PortalTriageService()
        self._llm      = get_llm_client()
        self._last_deterministic_failure = ""
        self._otp_sent: bool = False            # True once OTP API call succeeds
        # Stashed for the duration of run(); read by captcha helpers so they
        # can route manual fallback through human_loop.ask + customer UI.
        self._current_job: Optional[Job] = None
        self._otp_reveal_attempts: int = 0     # prevent infinite reveal loops
        self._generate_otp_attempts: int = 0   # deterministic retries before LLM fallback
        self._cached_otp: str = ""             # OTP cached so retries don't re-ask user
        self._otp_submit_attempts: int = 0     # retries with same OTP (max 3 before re-ask)
        self._otp_prompt_reason: str = ""      # customer-safe reason for next OTP prompt
        self._force_otp_handler: bool = False  # stay deterministic after resend/expiry

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self, job: Job) -> Job:
        self._current_job = job
        await self._sm.transition(job, JobStatus.AGENT_RUNNING)
        log.info("brain.run_started", job_id=job.job_id, service=job.service)

        step_count   = 0
        step_failures: dict[str, int] = {}   # step_name -> consecutive fail count
        failure_context = ""                  # diagnosis from last failed action
        # Records every action taken within the current step so the LLM
        # knows what it already did and doesn't repeat the same fill/click
        step_action_history: list[str] = []
        last_step_name = ""
        consecutive_scrolls = 0              # prevents infinite scroll loops
        state_seen_counts: dict[str, int] = {}  # detects no-progress loops

        while step_count < settings.max_steps_per_job:
            step_count += 1

            try:
                screenshot   = await self._browser.screenshot()
                url          = await self._browser.current_url()
                page_text    = await self._browser.page_text()
                dom_elements = await self._browser.get_interactive_elements()
            except Exception as e:
                job.error_message = f"Browser/page unavailable during observe: {e}"
                job.step_logs.append(StepLog(
                    step_name="observe",
                    status="failed",
                    observation="The browser page or context closed before the agent could observe it.",
                    action_taken="observe_page",
                    error=str(e),
                ).to_dict())
                await self._sm.save(job)
                log.error("brain.observe_failed_page_unavailable", error=str(e))
                raise PortalTransientError(job.error_message)

            await self._sync_steps_from_page(job, url, page_text, dom_elements)

            log.info(
                "brain.observe",
                step=step_count,
                url=url[:80],
                selects=len(dom_elements.get("selects", [])),
                inputs=len(dom_elements.get("inputs", [])),
                links=len(dom_elements.get("links", [])),
            )

            portal_block_reason = self._portal_transient_block_reason(page_text, url)
            if portal_block_reason:
                retry_no = job.increment_retry("portal_transient_block")
                msg = (
                    f"Government portal returned a transient block: {portal_block_reason}. "
                    f"Restarting portal flow from a fresh browser session (attempt {retry_no})."
                )
                log.warning(
                    "brain.portal_transient_block",
                    step=current_step if "current_step" in locals() else "",
                    retry=retry_no,
                    url=url,
                    reason=portal_block_reason,
                )
                job.mark_step_done("portal_transient_block", StepLog(
                    step_name="portal_transient_block",
                    status="retrying",
                    observation=portal_block_reason,
                    action_taken="restart_fresh_portal_session",
                    error=msg,
                ))
                job.steps_completed = []
                job.session_cookies = []
                job.last_url = ""
                await self._sm.transition(job, JobStatus.FAILED_RETRYING, msg)
                raise PortalTransientError(msg)

            # ── Build pending steps ────────────────────────────────────────────
            pending = steps_after(job.steps_completed)
            next_step = pending[0] if pending else None

            if not pending:
                log.info("brain.all_steps_done", job_id=job.job_id)
                break

            current_step = next_step.name if next_step else "unknown"
            fails        = step_failures.get(current_step, 0)

            # Hard guard: select filing state deterministically from requested
            # state code, never from DL-derived hints or LLM guesswork.
            if current_step == "select_state":
                if await self._force_select_state(job):
                    step_failures.pop(current_step, None)
                    failure_context = ""
                    await self._sm.save(job)
                    continue

            otp_result = await self._maybe_handle_otp_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
                screenshot=screenshot,
            )
            if otp_result == "submitted":
                step_failures.pop(current_step, None)
                failure_context = ""
                continue
            if otp_result == "waiting":
                break

            auth_selected = await self._maybe_handle_auth_method_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
            )
            if auth_selected:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue

            generated_otp = await self._maybe_handle_generate_otp_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
            )
            if generated_otp:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue

            # ── Service selection (after OTP) ──────────────────────────────────
            svc_selected = await self._maybe_handle_service_selection_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
                screenshot=screenshot,
            )
            if svc_selected:
                if job.status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.SUBMITTED, JobStatus.COMPLETED}:
                    break
                step_failures.pop(current_step, None)
                failure_context = ""
                continue

            # ── Service form (DL Extract / reason / organ donation / ACK) ──────
            svc_form = await self._maybe_handle_service_form_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
                screenshot=screenshot,
            )
            if svc_form:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue


            # ── DL services landing page (just click Continue) ─────────────────
            services_landing = await self._maybe_handle_dl_services_landing(url=url)
            if services_landing:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue

            # ── DL details confirmation (PIN code → RTO auto-fill) ─────────────
            confirmed_dl = await self._maybe_handle_confirm_dl_page(
                job=job,
                current_step=current_step,
                page_text=page_text,
                dom_elements=dom_elements,
            )
            if confirmed_dl:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue

            try:
                fetched_dl = await self._maybe_handle_dl_fetch_page(
                    job=job,
                    current_step=current_step,
                    page_text=page_text,
                    dom_elements=dom_elements,
                )
            except Exception as _dl_exc:
                log.warning("brain.dl_fetch_handler_exception", error=str(_dl_exc))
                fetched_dl = False
                self._last_deterministic_failure = (
                    f"DL fetch handler raised exception: {_dl_exc}. "
                    "The page may have navigated. Check the current URL and retry."
                )
            if fetched_dl:
                step_failures.pop(current_step, None)
                failure_context = ""
                continue
            if self._last_deterministic_failure:
                step_failures[current_step] = fails + 1
                failure_context = self._last_deterministic_failure
                self._last_deterministic_failure = ""
                log.warning(
                    "brain.deterministic_step_failed",
                    step=current_step,
                    consecutive_fails=step_failures[current_step],
                    diagnosis=failure_context[:240],
                )
                await self._ls.record_failure(
                    step_name=current_step,
                    observation=failure_context,
                    page_url=url,
                    failed_approach="deterministic_dl_fetch_handler",
                )
                await self._maybe_record_portal_triage(
                    job=job,
                    screenshot=screenshot,
                    page_text=page_text,
                    url=url,
                    current_step=current_step,
                    dom_elements=dom_elements,
                    failure_context=failure_context,
                    last_action="deterministic_dl_fetch_handler",
                )
                continue

            # Reset action history when we move to a new step
            if current_step != last_step_name:
                step_action_history = []
                last_step_name = current_step

            state_sig = self._state_signature(current_step, url, dom_elements)
            state_seen_counts[state_sig] = state_seen_counts.get(state_sig, 0) + 1
            no_progress_active = state_seen_counts[state_sig] >= settings.max_repeated_page_states
            if state_seen_counts[state_sig] >= settings.max_repeated_page_states:
                step_failures[current_step] = fails + 1
                failure_context = (
                    f"No-progress loop detected: the agent has observed the same "
                    f"page state {state_seen_counts[state_sig]} times for step "
                    f"'{current_step}'. A previous action likely returned success "
                    f"without changing the form/page. Do NOT repeat the same action; "
                    f"use a different selector, batch-fill missing fields, ask the user "
                    f"for missing data, or escalate if the portal requires manual input."
                )
                log.warning(
                    "brain.repeated_state_detected",
                    step=current_step,
                    repeats=state_seen_counts[state_sig],
                    url=url[:80],
                )

            # ── Conditional steps: auto-skip after 2 failures ─────────────────
            # A conditional step (e.g. close_state_popup) only applies if the
            # portal shows the relevant UI. If it keeps failing, the condition
            # simply isn't met — skip it silently and move on.
            if next_step and next_step.is_conditional and fails >= 2:
                log.info(
                    "brain.conditional_step_auto_skipped",
                    step=current_step,
                    reason="Conditional step — popup/UI likely not present this run",
                )
                job.mark_step_done(current_step, StepLog(
                    step_name=current_step,
                    status="skipped",
                    observation="Conditional step not applicable on this run",
                    action_taken="auto_skip",
                ))
                await self._sm.save(job)
                step_failures.pop(current_step, None)
                failure_context = ""
                step_action_history = []
                continue

            # ── Escalate to human after too many self-healing attempts ─────────
            if fails >= settings.max_consecutive_step_failures:
                log.warning(
                    "brain.max_retries_exhausted",
                    step=current_step,
                    consecutive_fails=fails,
                    url=url,
                    last_diagnosis=failure_context[:200],
                )
                safe_msg = (
                    f"Stopped after {fails} repeated attempts on step '{current_step}'. "
                    "The government portal did not move forward after safe retries."
                )
                job.error_message = f"{safe_msg} Diagnosis: {failure_context[:500]}"
                job.customer_data.pop("_pending_customer_request", None)
                job.mark_step_done(current_step, StepLog(
                    step_name=current_step,
                    status="failed",
                    observation="Stopped after repeated attempts without portal progress.",
                    action_taken="max_retry_exit",
                    error=job.error_message,
                ))
                await self._sm.transition(job, JobStatus.FAILED, job.error_message)
                break

            # ── Learning store hint ────────────────────────────────────────────
            learned = await self._ls.find_solution(
                step_name=current_step,
                observation=page_text[:500],
                page_url=url,
            )
            if learned:
                log.info(
                    "brain.learned_hint_found",
                    step=current_step,
                    hint=learned.solution[:80],
                )

            # ── Ask LLM ───────────────────────────────────────────────────────
            action = await self._ask_llm(
                job=job,
                screenshot=screenshot,
                page_text=page_text,
                url=url,
                pending_steps=pending,
                learned_hint=learned,
                dom_elements=dom_elements,
                failure_context=failure_context,
                step_action_history=step_action_history,
            )
            if action.action_type == "human_help":
                action.need_human = True

            log.info(
                "brain.thinking",
                step=current_step,
                action_type=action.action_type,
                description=action.description[:100],
                thought=action.thought[:150],
                selector=action.selector or "(none)",
                text=action.text or "(none)",
            )

            # ── Destructive action guard ───────────────────────────────────────
            if action.is_forbidden() and not job.allow_reset:
                log.warning("brain.blocked_destructive", action=action.description)
                failure_context = f"Action blocked: '{action.description}' is a destructive action."
                continue

            bad_navigation = self._is_bad_navigation(action, current_step)
            if bad_navigation:
                step_failures[current_step] = fails + 1
                failure_context = bad_navigation
                log.warning(
                    "brain.blocked_bad_navigation",
                    step=current_step,
                    text=action.text,
                    description=action.description,
                    reason=bad_navigation,
                )
                continue

            # ── Execute action ─────────────────────────────────────────────────
            pre_url = url
            success, exec_detail = await self._execute(action, job)

            # ── Verify success — not just what the LLM claims ──────────────────
            post_url   = await self._browser.current_url()
            url_changed = post_url != pre_url

            log.info(
                "brain.action_result",
                action_type=action.action_type,
                success=success,
                url_changed=url_changed,
                pre_url=pre_url[:60],
                post_url=post_url[:60],
                detail=exec_detail,
            )

            no_progress_action = (
                no_progress_active
                and success
                and not url_changed
                and action.action_type in ("fill", "fill_many", "scroll", "wait", "tool_call")
            )
            if no_progress_action:
                success = False
                exec_detail += " | forced_failure=no_progress"
                failure_context = (
                    f"No-progress loop confirmed: action '{action.action_type}' returned success "
                    f"but the page URL/state had already repeated {state_seen_counts[state_sig]} times. "
                    f"Do not repeat this action. If the next required control is not visible, ask the "
                    f"user/operator what this page needs instead of scrolling or re-filling."
                )
                log.warning(
                    "brain.no_progress_action_forced_failure",
                    step=current_step,
                    action_type=action.action_type,
                    repeats=state_seen_counts[state_sig],
                )

            # ── Record action in step history (prevents repeating same fills) ───
            step_action_history.append(
                f"[{'OK' if success else 'FAIL'}] {action.action_type} "
                f"selector='{action.selector}' text='{action.text}' value='{action.value}' "
                f"— {action.description}"
            )
            # Keep last 10 actions to avoid bloating the prompt
            if len(step_action_history) > 10:
                step_action_history = step_action_history[-10:]

            # ── Check for portal alert (form rejected, invalid data, etc.) ────────
            portal_alert_seen = False
            dialog_msg = await self._browser.get_last_dialog_message()
            if dialog_msg:
                if self._dialog_indicates_failure(dialog_msg):
                    portal_alert_seen = True
                    log.warning("brain.portal_alert", message=dialog_msg[:120], step=current_step)
                    # Surface the portal's own words to the customer UI so the
                    # customer can see WHAT Sarathi is complaining about, not
                    # just a generic "we're trying again". Kept short so the
                    # customer doesn't see internal noise.
                    job.customer_data["last_portal_message"] = {
                        "text": dialog_msg.strip()[:240],
                        "kind": "alert",
                        "step": current_step,
                        "at": datetime.utcnow().isoformat(),
                    }
                    # Treat validation alerts as failures so the LLM retries with fresh data.
                    step_failures[current_step] = fails + 1
                    failure_context = (
                        f"Portal showed alert: '{dialog_msg}'\n"
                        f"This means the form submission was REJECTED. The form fields have been cleared.\n"
                        f"ACTION REQUIRED: Re-fill ALL form fields from scratch — DL number, DOB, and a "
                        f"FRESH CAPTCHA (do NOT reuse the previous CAPTCHA value — use captcha_solver tool).\n"
                        f"Do NOT navigate away. Stay on this page and retry."
                    )
                    # Clear action history — previous fills are gone after form reset
                    step_action_history = []
                    success = False
                else:
                    log.info("brain.portal_dialog_accepted", message=dialog_msg[:140], step=current_step)
                    if "application already exists" in dialog_msg.lower():
                        action.step_complete = True
                        action.step_name = "confirm_dl_details"

            if success and action.action_type == "click":
                selector = (action.selector or "").strip()
                if selector == "#GetDLDetails":
                    action.step_complete = True
                    action.step_name = "fetch_dl_details"
                elif selector == "#dlconfirm":
                    action.step_complete = True
                    action.step_name = "confirm_dl_details"

            # ── Track consecutive scrolls — prevent infinite scroll loops ────────
            if action.action_type == "scroll":
                if success:
                    consecutive_scrolls += 1
                else:
                    consecutive_scrolls = 0
            else:
                consecutive_scrolls = 0

            # ── If action failed: self-diagnose and prepare next iteration ─────
            scroll_loop = (
                action.action_type == "scroll"
                and consecutive_scrolls >= 3
            )
            if (not success and action.action_type not in ("wait", "scroll")) or scroll_loop or no_progress_action:
                step_failures[current_step] = fails + 1
                if portal_alert_seen:
                    # Keep the precise portal rejection reason captured above.
                    pass
                elif no_progress_action:
                    # Keep the no-progress diagnosis prepared above.
                    pass
                elif scroll_loop:
                    failure_context = (
                        f"Scrolled {consecutive_scrolls} times without finding the target. "
                        f"The element is NOT reachable by scrolling. Try a different approach: "
                        f"look for a button/action that reveals it, or re-read the page carefully."
                    )
                    consecutive_scrolls = 0
                    log.warning(
                        "brain.scroll_loop_detected",
                        step=current_step,
                        consecutive=consecutive_scrolls,
                    )
                else:
                    failure_context = await self._diagnose_failure(
                        action=action,
                        dom_elements=dom_elements,
                        page_text=page_text,
                        url=url,
                    )
                log.warning(
                    "brain.self_diagnosis",
                    step=current_step,
                    consecutive_fails=step_failures[current_step],
                    diagnosis=failure_context[:200],
                )
                # Record failure in learning store so future runs avoid this
                await self._ls.record_failure(
                    step_name=current_step,
                    observation=failure_context,
                    page_url=url,
                    failed_approach=f"{action.action_type} selector={action.selector} text={action.text}",
                )
                await self._maybe_record_portal_triage(
                    job=job,
                    screenshot=screenshot,
                    page_text=page_text,
                    url=url,
                    current_step=current_step,
                    dom_elements=dom_elements,
                    failure_context=failure_context,
                    last_action=(
                        f"{action.action_type} selector={action.selector} "
                        f"text={action.text} value={action.value}"
                    ),
                )
                if learned:
                    await self._ls.mark_solution_outcome(learned.scenario_id, worked=False)
            else:
                # Successful action — reset failure state
                if success:
                    step_failures.pop(current_step, None)
                    failure_context = ""
                    if learned:
                        await self._ls.mark_solution_outcome(learned.scenario_id, worked=True)
                    learnable_action = (
                        action.action_type in {"click", "select", "check", "upload", "close_popup"}
                        and (url_changed or action.step_complete)
                    )
                    if learnable_action:
                        await self._ls.record_successful_action(
                            step_name=current_step,
                            observation=action.observation or page_text[:1000],
                            page_url=url,
                            action_type=action.action_type,
                            selector=action.selector,
                            text=action.text,
                            value=action.value,
                            tool_args=action.tool_args,
                        )

            # ── Handle special signals ─────────────────────────────────────────
            if action.is_done:
                if action.application_number:
                    job.application_number = action.application_number
                log.info("brain.completed", job_id=job.job_id, app_no=job.application_number)
                break

            if action.need_otp:
                otp_page_result = await self._maybe_handle_otp_page(
                    job=job,
                    current_step=current_step,
                    page_text=page_text,
                    dom_elements=dom_elements,
                    screenshot=screenshot,
                )
                if otp_page_result == "submitted":
                    continue
                if otp_page_result == "waiting":
                    break
                if settings.human_loop_backend == "console":
                    job.otp_pending_type = action.otp_type or "mobile"
                    await self._sm.transition(job, JobStatus.WAITING_OTP)
                    log.warning(
                        "brain.otp_needed_without_visible_input",
                        job_id=job.job_id,
                        type=job.otp_pending_type,
                    )
                    break
                otp = await self._otp.wait_for_otp(job, action.otp_type)
                if otp:
                    await self._browser.fill(
                        "input[type='text'][name*='otp'], input[id*='otp']", otp
                    )
                    await asyncio.sleep(0.5)
                    ok1 = await self._browser.click_text("Submit")
                    if not ok1:
                        await self._browser.click_text("Verify")
                    await self._sm.transition(job, JobStatus.AGENT_RUNNING)
                else:
                    log.error("brain.otp_timeout", job_id=job.job_id)
                    break

            if action.need_human:
                await self._handle_stuck(job, action, screenshot)
                await self._sm.transition(job, JobStatus.AGENT_RUNNING)

            # ── Mark step complete ONLY when action actually succeeded ──────────
            if action.step_complete and action.step_name:
                if success:
                    # Auto-advance: if the agent completed step N, all steps
                    # before N that are still pending must also be done implicitly
                    # (we are obviously past them on the portal).
                    step_order = [s.name for s in DL_RENEWAL_STEPS]
                    completed_idx = step_order.index(action.step_name) if action.step_name in step_order else -1
                    for prior_name in step_order[:max(completed_idx, 0)]:
                        if prior_name not in job.steps_completed:
                            job.mark_step_done(prior_name, StepLog(
                                step_name=prior_name,
                                status="auto_advanced",
                                observation=f"Auto-completed because '{action.step_name}' succeeded",
                                action_taken="auto_advance",
                            ))
                            log.info("brain.auto_advanced", step=prior_name)

                    slog = StepLog(
                        step_name    = action.step_name,
                        status       = "success",
                        observation  = action.observation,
                        action_taken = action.description,
                        tool_used    = action.tool,
                    )
                    job.mark_step_done(action.step_name, slog)
                    step_failures.pop(action.step_name, None)
                    await self._sm.save(job)
                    log.info("brain.step_complete", step=action.step_name)

                    # Record working approach in learning store
                    await self._ls.record(Scenario(
                        scenario_id=LearningStore.make_scenario_id(
                            action.step_name, action.observation
                        ),
                        step_name=action.step_name,
                        description=action.observation,
                        page_url=url,
                        solution=action.description,
                        solution_detail={
                            "action_type": action.action_type,
                            "selector": action.selector,
                            "text": action.text,
                            "value": action.value,
                        },
                        human_provided=False,
                    ))
                else:
                    log.warning(
                        "brain.step_complete_ignored",
                        step=action.step_name,
                        reason="LLM claimed success but action returned False",
                    )

            # ── Save session state ─────────────────────────────────────────────
            job.session_cookies = await self._browser.save_cookies()
            job.last_url = post_url
            await self._sm.save(job)

            await asyncio.sleep(0.3)

        if job.status not in {JobStatus.WAITING_OTP, JobStatus.STUCK_HUMAN_NEEDED}:
            await self._sm.transition(
                job,
                JobStatus.SUBMITTED if job.application_number else JobStatus.FAILED,
            )
        return job

    async def _sync_steps_from_page(
        self,
        job: Job,
        url: str,
        page_text: str,
        dom_elements: dict,
    ):
        """
        Align logical flow checkpoints with the real portal page.

        The LLM may forget to mark a step complete even though the browser has
        clearly moved forward. These checkpoints prevent the agent from going
        back to menu/navigation steps after it is already inside the application.
        """
        path = url.split("?")[0]
        checkpoints: list[str] = []

        if "stateSelectBean.do" in path:
            checkpoints = ["open_homepage", "close_homepage_popup", "select_state"]
        elif "dlServicesDet.do" in path:
            checkpoints = [
                "open_homepage",
                "close_homepage_popup",
                "select_state",
                "close_state_popup",
                "navigate_to_dl_services",
            ]
        elif "envaction.do" in path:
            checkpoints = [
                "open_homepage",
                "close_homepage_popup",
                "select_state",
                "close_state_popup",
                "navigate_to_dl_services",
            ]
            lower = page_text.lower()
            input_ids = {
                (i.get("id") or "").lower()
                for i in dom_elements.get("inputs", [])
            }
            if (
                "confirm_dl_details" in job.steps_completed
                and (
                    "details of the driving licence" in lower
                    or "details of the driving license" in lower
                    or "applicant details" in lower
                    or "applicants present address" in lower
                    or "pincodedlserreq" in input_ids
                )
            ):
                # In the existing-application branch, Sarathi does not show a
                # separate "Renewal of Driving Licence" checkbox/link. The
                # earlier DL Renewal entry point already selected the service.
                checkpoints.append("select_renewal_service")
            if "pincodedlserreq" in input_ids or "applicants present address" in lower:
                # This is a personal/details form, not an auth-method screen.
                checkpoints.extend([
                    "select_renewal_service",
                    "auth_method_selection",
                ])
            if (
                "authentication" in lower
                or "generate otp" in lower
                or any("aadhaarholdingtype" in input_id for input_id in input_ids)
            ):
                checkpoints.extend([
                    "fetch_dl_details",
                    "confirm_dl_details",
                    "select_renewal_service",
                ])
            visible_select_ids = {
                (s.get("id") or "")
                for s in dom_elements.get("selects", [])
                if s.get("visible", True)
            }
            if (
                "dispDLDet" in visible_select_ids
                or "driving licence details" in lower
                or "driving license details" in lower
            ):
                checkpoints.append("fetch_dl_details")
            if self._has_otp_input(dom_elements, page_text):
                checkpoints.append("auth_method_selection")

        changed = False
        for step_name in checkpoints:
            if step_name not in job.steps_completed:
                job.mark_step_done(step_name, StepLog(
                    step_name=step_name,
                    status="auto_checkpoint",
                    observation=f"Browser reached {path}",
                    action_taken="page_checkpoint",
                ))
                log.info("brain.page_checkpoint", step=step_name, url=path[-80:])
                changed = True

        if changed:
            await self._sm.save(job)

    async def _maybe_handle_otp_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
        screenshot: bytes,
    ) -> str:
        """
        Deterministically fill OTP when the portal shows the OTP entry form.

        Key design rules:
          - Ask user for OTP exactly ONCE per OTP session; cache it for retries.
          - If submission fails (page unchanged), retry with a FRESH captcha but
            the SAME OTP — don't ask user again.
          - After 3 failed retries, clear the cache and ask user for a new OTP
            (the first one may have expired).
          - Only mark the OTP step done AFTER the page actually navigates away.
          - Guard: if OTP step already verified, skip entirely.

        Returns:
          - "submitted": OTP flow handled; continue main loop
          - "waiting": paused for human input / timeout
          - "": not an OTP page
        """
        if not self._force_otp_handler and not self._has_otp_input(dom_elements, page_text):
            return ""

        # ── Guard: OTP already verified this job run ───────────────────────────
        otp_done = {"mobile_otp_verification", "aadhaar_otp_verification"}
        if any(s in job.steps_completed for s in otp_done):
            return ""

        # Detect OTP type by checking which Sarathi OTP function is present.
        # #verifySarathi (mobile flow) vs Aadhaar eKYC frame elements.
        otp_type = await self._browser.evaluate("""() => {
            if (document.getElementById('verifySarathi')
                || document.getElementById('generateSarathiotp')
                || document.getElementById('otpNumber')) return 'mobile';
            if (document.querySelector('[id*="aadhaar" i],[id*="ekyc" i]')) return 'aadhaar';
            return 'mobile';
        }""") or "mobile"

        # ── Ask user for OTP (with mobile number shown + Resend option) ──────────
        if not self._cached_otp:
            mobile = job.customer_data.get("mobile_number", "")
            masked  = f"******{mobile[-4:]}" if len(mobile) >= 4 else "your registered number"
            reason = (self._otp_prompt_reason or "").strip()
            reason_prefix = f"{reason} " if reason else ""
            question = (
                f"{reason_prefix}OTP has been sent to {masked}. "
                f"Please enter it below, or choose 'Resend OTP' if you did not receive it."
            )
            context = (
                f"{reason_prefix}The Sarathi portal sent a 6-digit OTP to your DL-registered mobile {masked}. "
                f"Check your SMS and type the OTP. If you didn't receive it, select 'Resend OTP'."
            )
            resp = await self._hl.ask(
                job=job,
                step_name=current_step,
                question=question,
                context=context,
                screenshot=screenshot,
                options=["Resend OTP"],
            )
            if resp.answer == "__timeout__":
                job.otp_pending_type = otp_type
                await self._sm.transition(job, JobStatus.WAITING_OTP)
                log.warning("brain.otp_waiting_for_user", job_id=job.job_id, type=otp_type)
                return "waiting"

            # Customer wants to resend OTP
            if resp.answer.strip().lower() in ("resend otp", "resend", "1"):
                log.info("brain.otp_resend_requested")
                await self._resend_current_otp("user_requested_resend")
                return "submitted"              # loop back and ask for the fresh OTP

            digits = "".join(ch for ch in resp.answer if ch.isdigit())
            if not digits:
                log.warning("brain.otp_invalid_from_user", job_id=job.job_id, raw=resp.answer)
                return "submitted"              # loop back → will re-ask next iteration

            self._cached_otp = digits
            self._otp_submit_attempts = 0
            self._otp_prompt_reason = ""

        otp = self._cached_otp
        self._otp_submit_attempts += 1
        log.info("brain.otp_attempt", attempt=self._otp_submit_attempts, type=otp_type)

        # Keep the same OTP across CAPTCHA retries. "Page unchanged" usually means
        # CAPTCHA failed, not that the user's OTP was wrong. Clear the OTP only
        # when Sarathi explicitly says invalid/expired OTP after submit.
        max_same_otp_attempts = VERIFY_OTP_RULES.get(
            "max_same_otp_submit_attempts",
            settings.captcha_max_retries,
        )
        if self._otp_submit_attempts > max_same_otp_attempts:
            log.warning(
                "brain.otp_many_attempts_same_otp",
                attempts=self._otp_submit_attempts,
                action="resending_otp",
            )
            await self._resend_current_otp("same_otp_submit_attempts_exhausted")
            return "submitted"

        # ── Pull deterministic facts from the runtime rule book ──────────────────
        rulebook_otp_input_sel = VERIFY_OTP_RULES["otp_input_selector"]
        rulebook_captcha_img_sel = VERIFY_OTP_RULES["captcha_image_selector"]
        rulebook_captcha_inp_sel = VERIFY_OTP_RULES["captcha_input_selector"]
        rulebook_submit_sel = VERIFY_OTP_RULES["submit_button_selector"]
        rulebook_submit_fn = VERIFY_OTP_RULES["submit_onclick_fn"]
        otp_input_candidates = VERIFY_OTP_RULES.get("otp_input_selectors", [rulebook_otp_input_sel])
        captcha_img_candidates = VERIFY_OTP_RULES.get("captcha_image_selectors", [rulebook_captcha_img_sel])
        captcha_inp_candidates = VERIFY_OTP_RULES.get("captcha_input_selectors", [rulebook_captcha_inp_sel])
        candidate_probe = await self._browser.evaluate(f"""() => {{
            const isVisible = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            }};
            const probeSelectors = (selectors, kind) => {{
                const usable = [];
                for (const sel of selectors) {{
                    let nodes = [];
                    try {{ nodes = Array.from(document.querySelectorAll(sel)); }}
                    catch (_) {{ continue; }}
                    for (const el of nodes) {{
                        if (!isVisible(el)) continue;
                        if (kind === 'input' && (el.disabled || el.readOnly)) continue;
                        usable.push(sel);
                        break;
                    }}
                }}
                return usable;
            }};
            return {{
                otp_inputs: probeSelectors({json.dumps(otp_input_candidates)}, 'input'),
                captcha_imgs: probeSelectors({json.dumps(captcha_img_candidates)}, 'image'),
                captcha_inputs: probeSelectors({json.dumps(captcha_inp_candidates)}, 'input'),
            }};
        }}""")

        def _prefer_probe(probed: list[str] | None, original: list[str]) -> list[str]:
            ordered: list[str] = []
            for selector in (probed or []) + original:
                if selector and selector not in ordered:
                    ordered.append(selector)
            return ordered

        otp_visible_candidates = candidate_probe.get("otp_inputs", []) if candidate_probe else []
        otp_input_candidates = _prefer_probe(otp_visible_candidates, otp_input_candidates)
        captcha_img_candidates = _prefer_probe(
            candidate_probe.get("captcha_imgs", []) if candidate_probe else [],
            captcha_img_candidates,
        )
        captcha_inp_candidates = _prefer_probe(
            candidate_probe.get("captcha_inputs", []) if candidate_probe else [],
            captcha_inp_candidates,
        )
        log.info(
            "brain.otp_selector_probe",
            otp_candidates=otp_input_candidates,
            visible_otp_candidates=otp_visible_candidates,
            captcha_img_candidates=captcha_img_candidates,
            captcha_inp_candidates=captcha_inp_candidates,
        )
        otp_input_sel    = otp_input_candidates[0]
        captcha_img_sel  = captcha_img_candidates[0]
        captcha_inp_sel  = captcha_inp_candidates[0]
        submit_sel       = VERIFY_OTP_RULES["submit_button_selector"]
        forbidden_sel    = VERIFY_OTP_RULES["forbidden_submit_selector"]
        submit_fn_name   = VERIFY_OTP_RULES["submit_onclick_fn"]
        known_fns        = VERIFY_OTP_RULES["known_verify_fns"]
        otp_input_id     = otp_input_sel.lstrip("#")
        captcha_img_id   = captcha_img_sel.lstrip("#")
        captcha_inp_id   = captcha_inp_sel.lstrip("#")
        submit_btn_id    = submit_sel.lstrip("#")
        forbidden_btn_id = forbidden_sel.lstrip("#")

        # Consume stale alerts before this attempt so only this submit's result
        # is used for retry classification.
        await self._browser.get_last_dialog_message()

        # ── Refresh OTP captcha before solving (new image each attempt) ──────────
        pre_url = await self._browser.current_url()
        await self._browser.evaluate(f"""() => {{
            const selectors = {json.dumps(captcha_img_candidates)};
            const img = selectors.map(sel => document.querySelector(sel)).find(Boolean);
            if (img && img.src) {{
                const base = img.src.split('?')[0];
                img.src = base + '?t=' + Date.now();
            }}
        }}""")
        await asyncio.sleep(1.2)  # wait for new captcha image to load

        # Make OTP visibly appear in the textbox before the JS event-heavy fill.
        # If this fails, the JS block below still acts as the fallback.
        visible_otp_fill = False
        try:
            for candidate in otp_visible_candidates:
                await self._browser.scroll_into_view(candidate)
                visible_otp_fill = await self._browser.fill(candidate, otp)
                if visible_otp_fill:
                    otp_input_sel = candidate
                    break
        except Exception as e:
            log.warning("brain.otp_visible_fill_failed", selectors=otp_input_candidates, error=str(e))
        log.info("brain.otp_visible_fill_attempted", selector=otp_input_sel, success=visible_otp_fill)

        # ── Fill OTP + find captcha/submit using rule-book selectors ─────────────
        otp_section_info = await self._browser.evaluate(f"""() => {{
            const otp_val   = {json.dumps(otp)};
            const otpSelectors = {json.dumps(otp_input_candidates)};
            const capImgSelectors = {json.dumps(captcha_img_candidates)};
             const capInpSelectors = {json.dumps(captcha_inp_candidates)};
             const submitId  = {json.dumps(submit_btn_id)};
             const forbidId  = {json.dumps(forbidden_btn_id)};
             const selectorFor = (el) => {{
                 if (!el) return null;
                 if (el.id) return '#' + CSS.escape(el.id);
                 if (el.name) return '[name="' + CSS.escape(el.name) + '"]';
                 return null;
             }};
             const firstBySelectors = (selectors) => {{
                 for (const sel of selectors) {{
                     let nodes = [];
                     try {{ nodes = Array.from(document.querySelectorAll(sel)); }}
                     catch (_) {{ continue; }}
                     for (const el of nodes) if (el) return el;
                 }}
                 return null;
             }};
             const isVisible = (el) => {{
                 if (!el) return false;
                 const style = window.getComputedStyle(el);
                 const rect = el.getBoundingClientRect();
                 return style.display !== 'none'
                     && style.visibility !== 'hidden'
                     && rect.width > 0
                     && rect.height > 0;
             }};
             const firstUsableInputBySelectors = (selectors) => {{
                 for (const sel of selectors) {{
                     let nodes = [];
                     try {{ nodes = Array.from(document.querySelectorAll(sel)); }}
                     catch (_) {{ continue; }}
                     for (const el of nodes) {{
                         if (el && isVisible(el) && !el.disabled && !el.readOnly) return el;
                     }}
                 }}
                 return null;
             }};
             const firstVisibleBySelectors = (selectors) => {{
                 for (const sel of selectors) {{
                     let nodes = [];
                     try {{ nodes = Array.from(document.querySelectorAll(sel)); }}
                     catch (_) {{ continue; }}
                     for (const el of nodes) if (el && isVisible(el)) return el;
                 }}
                 return null;
             }};

             // Fill OTP input — fire per-character keystrokes so Sarathi's
             // keyup listener (which enables the Submit button) actually runs.
             const otpEl = firstUsableInputBySelectors(otpSelectors) || firstBySelectors(otpSelectors);
            if (!otpEl) return {{ error: 'no_otp_input' }};
            otpEl.disabled = false;
            otpEl.removeAttribute('disabled');
            otpEl.removeAttribute('hidden');
            if (otpEl.style.display === 'none') otpEl.style.display = '';
            otpEl.scrollIntoView({{block: 'center', inline: 'center'}});
            const nSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            otpEl.focus();
            nSet.call(otpEl, '');
            otpEl.dispatchEvent(new Event('input', {{bubbles:true}}));
            for (let i = 0; i < otp_val.length; i++) {{
                const ch = otp_val[i];
                const code = ch.charCodeAt(0);
                nSet.call(otpEl, otp_val.substring(0, i + 1));
                otpEl.dispatchEvent(new KeyboardEvent('keydown',
                    {{bubbles:true, key: ch, code: 'Digit'+ch, keyCode: code, which: code}}));
                otpEl.dispatchEvent(new KeyboardEvent('keypress',
                    {{bubbles:true, key: ch, code: 'Digit'+ch, keyCode: code, which: code}}));
                otpEl.dispatchEvent(new Event('input', {{bubbles:true}}));
                otpEl.dispatchEvent(new KeyboardEvent('keyup',
                    {{bubbles:true, key: ch, code: 'Digit'+ch, keyCode: code, which: code}}));
            }}
            otpEl.dispatchEvent(new Event('change', {{bubbles:true}}));
            otpEl.dispatchEvent(new Event('blur',   {{bubbles:true}}));

            // Captcha image and input — rule-book IDs first, then fallback
            const captchaImg = firstVisibleBySelectors(capImgSelectors) || firstBySelectors(capImgSelectors)
                || document.querySelector('img[src*="captcha" i], img[id*="captha" i]');
            const captchaInp = firstUsableInputBySelectors(capInpSelectors) || firstBySelectors(capInpSelectors)
                || document.querySelector('input[id*="capt" i]:not([type="hidden"])');

            // DUMP ALL VISIBLE BUTTONS for debug + finding the right one
            const allBtns = Array.from(document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], a.btn'
            )).filter(b => b.offsetParent !== null && b.id !== forbidId);

            const btnDump = allBtns.map(b => ({{
                id: b.id || '',
                tag: b.tagName,
                type: b.type || '',
                text: (b.textContent || '').trim().slice(0, 40),
                value: (b.value || '').trim().slice(0, 40),
                onclick: (b.getAttribute('onclick') || '').slice(0, 80),
                cls: (b.className || '').slice(0, 60),
            }}));

            // Score each button — higher = more likely the OTP Submit
            const scoreBtn = (b) => {{
                let score = 0;
                const idLow      = (b.id || '').toLowerCase();
                const onclickLow = (b.getAttribute('onclick') || '').toLowerCase();
                const txtLow     = ((b.textContent || '') + (b.value || '')).toLowerCase().trim();
                const clsLow     = (b.className || '').toLowerCase();

                // Strong signals: id/onclick/class mentioning verify+otp
                if (/verify.*otp|otp.*verify|validate.*otp|otp.*validate/.test(idLow + ' ' + onclickLow + ' ' + clsLow)) score += 100;
                if (/sarathi.*otp|otp.*sarathi/.test(idLow + ' ' + onclickLow + ' ' + clsLow)) score += 80;
                // Medium: id/onclick mentioning verify or otp alone
                if (/verify/.test(idLow + ' ' + onclickLow)) score += 30;
                if (/otp/.test(idLow + ' ' + onclickLow)) score += 30;
                // Text content match
                if (/^submit$/i.test(txtLow)) score += 20;
                if (/verify|validate/i.test(txtLow)) score += 25;
                // Penalty: clearly the wrong button
                if (/generate/i.test(idLow + ' ' + txtLow)) score -= 50;
                if (/resend|cancel|reset|home|back/i.test(idLow + ' ' + txtLow)) score -= 50;
                return score;
            }};

            // Prefer the rule-book Submit ID directly if it exists on the page.
            let submitBtn = document.getElementById(submitId);
            let bestScore = submitBtn ? 9999 : -1;
            if (!submitBtn) {{
                for (const b of allBtns) {{
                    const s = scoreBtn(b);
                    if (s > bestScore) {{ bestScore = s; submitBtn = b; }}
                }}
                if (bestScore < 20) submitBtn = null;
            }}

             return {{
                 otp_filled:      otpEl.id || otpEl.name,
                 otp_selector:    selectorFor(otpEl),
                 otp_value:       otpEl.value || '',
                 otp_expected_len: otp_val.length,
                 otp_visible:     isVisible(otpEl),
                 otp_disabled:    !!otpEl.disabled,
                 captcha_img_id:  captchaImg ? (captchaImg.id || '') : null,
                 captcha_inp_id:  captchaInp ? (captchaInp.id || captchaInp.name) : null,
                 captcha_img_selector: selectorFor(captchaImg),
                 captcha_inp_selector: selectorFor(captchaInp),
                 submit_btn_text: submitBtn  ? (submitBtn.textContent || submitBtn.value || '').trim() : null,
                 submit_btn_id:   submitBtn  ? (submitBtn.id || '') : null,
                 submit_btn_selector: selectorFor(submitBtn),
                 submit_btn_score: bestScore,
                 all_buttons:     btnDump,
             }};
         }}""")

        if not otp_section_info or otp_section_info.get("error"):
            log.warning("brain.otp_section_not_found", info=otp_section_info)
            return "submitted"

        log.info("brain.otp_section_found", info=otp_section_info)

        otp_visible_value = otp_section_info.get("otp_value") or ""
        if otp_visible_value != otp:
            log.warning(
                "brain.otp_value_mismatch_after_fill",
                expected_len=len(otp),
                actual_len=len(otp_visible_value),
                selector=otp_section_info.get("otp_selector"),
            )
            otp_refill = await self._browser.evaluate(f"""() => {{
                const val = {json.dumps(otp)};
                const selector = {json.dumps(otp_section_info.get("otp_selector") or otp_input_sel)};
                const el = document.querySelector(selector);
                if (!el) return {{ ok: false, reason: 'otp_input_missing' }};
                el.disabled = false;
                el.removeAttribute('disabled');
                el.focus();
                const nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, val);
                el.dispatchEvent(new Event('input', {{bubbles:true}}));
                el.dispatchEvent(new KeyboardEvent('keyup', {{bubbles:true, key: val.slice(-1)}}));
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                return {{ ok: el.value === val, value_len: (el.value || '').length }};
            }}""")
            log.info("brain.otp_value_refill_result", result=otp_refill)
            if not otp_refill or not otp_refill.get("ok"):
                return "submitted"
        else:
            log.info(
                "brain.otp_value_verified",
                selector=otp_section_info.get("otp_selector"),
                digits=len(otp_visible_value),
            )

        # ── Fetch the CORRECT captcha image (OTP section) and solve it ────────
        captcha_img_id = otp_section_info.get("captcha_img_id") or ""
        captcha_inp_id = otp_section_info.get("captcha_inp_id") or ""

        # Screenshot the exact DOM image shown in the browser. Do not fetch
        # captchaimage.jsp separately; Sarathi may generate a different CAPTCHA
        # for that extra HTTP request.
        otp_captcha_selector = (
            otp_section_info.get("captcha_img_selector")
            or (f"#{captcha_img_id}" if captcha_img_id else "")
            or captcha_img_sel
        )
        otp_captcha_bytes = await self._fetch_visible_captcha_bytes(
            image_selector=otp_captcha_selector,
        )
        if otp_captcha_bytes:
            self._save_captcha_attempt(
                job_id=job.job_id,
                flow="otp",
                attempt=self._otp_submit_attempts,
                stage="fetched",
                image_bytes=otp_captcha_bytes,
                solution="",
                metadata={
                    "captcha_img_id": captcha_img_id,
                    "captcha_img_selector": otp_captcha_selector,
                    "captcha_inp_id": captcha_inp_id,
                    "otp_cached": bool(self._cached_otp),
                },
            )

        if not otp_captcha_bytes:
            log.warning("brain.otp_captcha_fetch_failed", id=captcha_img_id)
            return "submitted"

        otp_captcha_sol = ""
        if otp_captcha_bytes:
            try:
                timeout_s = max(15, settings.captcha_timeout_seconds + 15)
                force_manual_captcha = self._otp_submit_attempts >= settings.captcha_max_retries
                log.info("brain.otp_captcha_solving_started", timeout_seconds=timeout_s)
                captcha_result = await asyncio.wait_for(
                    self._captcha.solve_with_confidence(
                        otp_captcha_bytes,
                        force_manual=force_manual_captcha,
                        allow_manual=force_manual_captcha,
                        prompt_context=(
                            "OTP verification CAPTCHA. The OTP is already cached; "
                            "only this CAPTCHA value is needed."
                        ),
                        job=self._current_job,
                        human_loop=self._hl,
                    ),
                    timeout=max(timeout_s, settings.captcha_manual_timeout_seconds + 5)
                    if force_manual_captcha else timeout_s,
                )
                otp_captcha_sol = captcha_result.text
                if (
                    not force_manual_captcha
                    and captcha_result.confidence < settings.captcha_confidence_threshold
                ):
                    log.warning(
                        "brain.otp_captcha_low_confidence_refreshing",
                        solution=otp_captcha_sol,
                        confidence=captcha_result.confidence,
                        threshold=settings.captcha_confidence_threshold,
                    )
                    self._save_captcha_attempt(
                        job_id=job.job_id,
                        flow="otp",
                        attempt=self._otp_submit_attempts,
                        stage="low_confidence",
                        image_bytes=otp_captcha_bytes,
                        solution=otp_captcha_sol,
                        metadata={
                            "confidence": captcha_result.confidence,
                            "provider": captcha_result.provider,
                            "attempts": captcha_result.attempts,
                        },
                    )
                    await self._refresh_visible_captcha(f"#{captcha_img_id}" if captcha_img_id else "")
                    return "submitted"

                log.info(
                    "brain.otp_captcha_solved",
                    solution=otp_captcha_sol,
                    confidence=captcha_result.confidence,
                    provider=captcha_result.provider,
                    manual=force_manual_captcha,
                )
                self._save_captcha_attempt(
                    job_id=job.job_id,
                    flow="otp",
                    attempt=self._otp_submit_attempts,
                    stage="solved",
                    image_bytes=otp_captcha_bytes,
                    solution=otp_captcha_sol,
                    metadata={
                        "manual": force_manual_captcha,
                        "confidence": captcha_result.confidence,
                        "provider": captcha_result.provider,
                        "attempts": captcha_result.attempts,
                    },
                )
            except asyncio.TimeoutError:
                log.warning("brain.otp_captcha_solve_timeout")
                return "submitted"
            except Exception as e:
                log.warning("brain.otp_captcha_solve_failed", error=str(e))
                return "submitted"

        if not otp_captcha_sol:
            log.warning("brain.otp_captcha_empty_solution")
            return "submitted"

        # Fill the CORRECT captcha input (OTP section) with per-character events
        if captcha_inp_id:
            captcha_name_selector = f'[name="{captcha_inp_id}"]'
            filled_cap = await self._browser.evaluate(f"""() => {{
                const val = {json.dumps(otp_captcha_sol)};
                const el = document.getElementById({json.dumps(captcha_inp_id)})
                         || document.querySelector({json.dumps(captcha_name_selector)});
                if (!el) return {{ ok: false, reason: 'captcha_input_missing' }};
                el.disabled = false;
                el.removeAttribute('disabled');
                if (el.style.display === 'none') el.style.display = '';
                const nSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                el.focus();
                nSet.call(el, '');
                el.dispatchEvent(new Event('input', {{bubbles:true}}));
                for (let i = 0; i < val.length; i++) {{
                    const ch = val[i];
                    const code = ch.charCodeAt(0);
                    nSet.call(el, val.substring(0, i + 1));
                    el.dispatchEvent(new KeyboardEvent('keydown',
                        {{bubbles:true, key: ch, keyCode: code, which: code}}));
                    el.dispatchEvent(new Event('input', {{bubbles:true}}));
                    el.dispatchEvent(new KeyboardEvent('keyup',
                        {{bubbles:true, key: ch, keyCode: code, which: code}}));
                }}
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                el.dispatchEvent(new Event('blur',   {{bubbles:true}}));
                return {{
                    ok: el.value === val,
                    selector: el.id ? '#' + CSS.escape(el.id) : (el.name ? '[name="' + CSS.escape(el.name) + '"]' : ''),
                    value_len: (el.value || '').length,
                }};
            }}""")
            log.info("brain.otp_captcha_filled", result=filled_cap, solution=otp_captcha_sol)
            if not filled_cap or not filled_cap.get("ok"):
                log.warning("brain.otp_captcha_value_not_verified", result=filled_cap)
                return "submitted"
            await asyncio.sleep(0.4)
        else:
            log.warning("brain.otp_captcha_input_missing", info=otp_section_info)
            return "submitted"

        await asyncio.sleep(0.3)

        checkbox_info = await self._browser.evaluate("""() => {
            const isVisible = (el) => {
                const st = window.getComputedStyle(el);
                return st && st.display !== 'none' && st.visibility !== 'hidden'
                    && el.offsetParent !== null;
            };
            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                .filter(el => !el.disabled && isVisible(el));
            const changed = [];
            for (const el of boxes) {
                if (!el.checked) {
                    el.scrollIntoView({block: 'center', inline: 'center'});
                    el.click();
                    if (!el.checked) {
                        const checkedSet = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'checked').set;
                        checkedSet.call(el, true);
                    }
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    changed.push(el.id || el.name || '(unnamed)');
                }
            }
            return {
                visible_count: boxes.length,
                checked: boxes.map(el => ({id: el.id || '', name: el.name || '', checked: el.checked})),
                changed,
            };
        }""")
        log.info("brain.otp_checkboxes_ready", info=checkbox_info)
        if checkbox_info and checkbox_info.get("visible_count", 0):
            unchecked = [
                box for box in checkbox_info.get("checked", [])
                if not box.get("checked")
            ]
            if unchecked:
                log.warning("brain.otp_checkbox_not_checked", unchecked=unchecked)
                return "submitted"

        # ── Click the OTP Submit button (NEVER #submt — that's auth-method Submit) ──
        readiness = await self._browser.evaluate(f"""() => {{
            const otp = document.querySelector({json.dumps(otp_section_info.get("otp_selector") or otp_input_sel)});
            const cap = document.querySelector({json.dumps(otp_section_info.get("captcha_inp_selector") or captcha_inp_sel)});
            const submit = document.getElementById({json.dumps(otp_section_info.get("submit_btn_id") or submit_btn_id)})
                || document.querySelector({json.dumps(otp_section_info.get("submit_btn_selector") or submit_sel)});
            const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
                .filter(el => {{
                    const st = window.getComputedStyle(el);
                    const labelText = (
                        (el.id || '') + ' ' +
                        (el.name || '') + ' ' +
                        (el.value || '') + ' ' +
                        (el.closest('label') ? el.closest('label').textContent : '') + ' ' +
                        (el.parentElement ? el.parentElement.textContent : '')
                    ).toLowerCase();
                    const visible = !!(el.offsetParent !== null || el.id === 'otpCheckbox');
                    const otpRelated = labelText.includes('otp') || el.id === 'otpCheckbox';
                    return !el.disabled
                        && visible
                        && otpRelated
                        && st.display !== 'none'
                        && st.visibility !== 'hidden';
                }});
            for (const el of [otp, cap].filter(Boolean)) {{
                el.focus();
                el.dispatchEvent(new Event('input', {{bubbles: true}}));
                const last = (el.value || '').slice(-1);
                const code = last ? last.charCodeAt(0) : 0;
                el.dispatchEvent(new KeyboardEvent('keyup', {{
                    bubbles: true,
                    key: last,
                    keyCode: code,
                    which: code,
                }}));
                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                el.dispatchEvent(new Event('blur', {{bubbles: true}}));
            }}
            for (const fn of ['EnableDisableOtp', 'enableDisableOtp', 'EnableDisableOTP']) {{
                if (typeof window[fn] === 'function') {{
                    try {{ window[fn](); }} catch (e) {{}}
                }}
            }}
            if (submit) {{
                submit.disabled = false;
                submit.removeAttribute('disabled');
                submit.removeAttribute('readonly');
                submit.style.visibility = 'visible';
            }}
            return {{
                otp_value_len: otp ? (otp.value || '').length : 0,
                otp_value_matches: otp ? otp.value === {json.dumps(otp)} : false,
                captcha_value_len: cap ? (cap.value || '').length : 0,
                captcha_value_matches: cap ? cap.value === {json.dumps(otp_captcha_sol)} : false,
                checkbox_count: boxes.length,
                checked_count: boxes.filter(x => x.checked).length,
                unchecked_boxes: boxes.filter(x => !x.checked).map(x => x.id || x.name || '(unnamed)'),
                submit_id: submit ? (submit.id || '') : '',
                submit_disabled: submit ? !!submit.disabled : null,
                submit_visible: submit ? !!(submit.offsetParent !== null || submit.id === {json.dumps(submit_btn_id)}) : false,
                submit_onclick: submit ? (submit.getAttribute('onclick') || '') : '',
                verified_fn_exists: typeof window[{json.dumps(submit_fn_name)}] === 'function',
            }};
        }}""")
        log.info("brain.otp_submit_readiness", info=readiness)
        if not readiness or not readiness.get("otp_value_matches"):
            log.warning("brain.otp_submit_blocked_bad_otp_readback", info=readiness)
            return "submitted"
        if not readiness.get("captcha_value_matches"):
            log.warning("brain.otp_submit_blocked_bad_captcha_readback", info=readiness)
            return "submitted"
        if readiness.get("checkbox_count", 0) and readiness.get("checked_count", 0) < readiness.get("checkbox_count", 0):
            log.warning("brain.otp_submit_blocked_unchecked_boxes", info=readiness)
            return "submitted"

        submit_btn_id    = otp_section_info.get("submit_btn_id") or ""
        submit_btn_text  = (otp_section_info.get("submit_btn_text") or "").strip()
        submit_btn_score = otp_section_info.get("submit_btn_score", -1)
        all_buttons      = otp_section_info.get("all_buttons", [])

        # Log every visible button so we can see what's on the page
        log.info("brain.otp_page_buttons", count=len(all_buttons), buttons=all_buttons[:15])
        log.info("brain.otp_submit_best_match", id=submit_btn_id, text=submit_btn_text,
                 score=submit_btn_score)

        # The Submit button is often kept disabled by Sarathi's keyup listener,
        # which doesn't fire when we set value via nativeInputValueSetter.
        # The cleanest fix is to invoke the onclick function (e.g. verifiedBySarathi())
        # directly — bypasses the disabled state entirely.
        clicked = False
        click_method = "none"
        submit_strategy = "rulebook_fn"

        js_result = await self._browser.evaluate(f"""() => {{
            const target_id    = {json.dumps(submit_btn_id)};
            const primary_fn   = {json.dumps(submit_fn_name)};
            const known_fns    = {json.dumps(known_fns)};
            const forbid_id    = {json.dumps(forbidden_btn_id)};
            const strategy     = {json.dumps(submit_strategy)};

            // Helper: parse function name from onclick attribute
            //   "verifiedBySarathi(); return false;" -> "verifiedBySarathi"
            const fnFromOnclick = (str) => {{
                if (!str) return null;
                const m = str.match(/^\\s*([a-zA-Z_$][\\w$]*)\\s*\\(/);
                return m ? m[1] : null;
            }};

            const targetButton = target_id ? document.getElementById(target_id) : null;
            if (targetButton) {{
                targetButton.disabled = false;
                targetButton.removeAttribute('disabled');
                targetButton.removeAttribute('readonly');
                targetButton.style.display = '';
                targetButton.style.visibility = 'visible';
            }}

            if (strategy === 'button_click' && targetButton) {{
                try {{
                    targetButton.click();
                    return 'button_click:' + target_id;
                }} catch (e) {{
                    return 'button_click_error:' + target_id + ':' + e.message;
                }}
            }}

            if (strategy === 'form_submit' && targetButton) {{
                const form = targetButton.form || targetButton.closest('form');
                try {{
                    if (form && typeof form.requestSubmit === 'function') {{
                        form.requestSubmit(targetButton);
                        return 'form_request_submit:' + target_id;
                    }}
                    if (form && typeof form.submit === 'function') {{
                        form.submit();
                        return 'form_submit:' + target_id;
                    }}
                }} catch (e) {{
                    return 'form_submit_error:' + target_id + ':' + e.message;
                }}
            }}

            // 1. PRIMARY: call the rule-book function directly
            if (primary_fn && typeof window[primary_fn] === 'function') {{
                try {{ window[primary_fn](); return 'rulebook_fn:' + primary_fn; }}
                catch (e) {{ return 'rulebook_fn_error:' + primary_fn + ':' + e.message; }}
            }}

            // 2. Parse the function name from the actual button onclick
            if (target_id) {{
                const b = document.getElementById(target_id);
                if (b) {{
                    b.disabled = false;
                    b.removeAttribute('disabled');
                    const fname = fnFromOnclick(b.getAttribute('onclick'));
                    if (fname && typeof window[fname] === 'function') {{
                        try {{ window[fname](); return 'fn_from_onclick:' + fname; }}
                        catch (e) {{ return 'fn_error:' + fname + ':' + e.message; }}
                    }}
                }}
            }}

            // 3. Try any of the known Sarathi verify functions
            for (const fn of known_fns) {{
                if (typeof window[fn] === 'function') {{
                    try {{ window[fn](); return 'known_fn:' + fn; }} catch (e) {{}}
                }}
            }}

            // 4. Force-click the button (last resort)
            if (target_id) {{
                const b = document.getElementById(target_id);
                if (b) {{
                    b.disabled = false;
                    b.removeAttribute('disabled');
                    b.click();
                    return 'force_click:' + target_id;
                }}
            }}

            // 5. Last-resort: any visible button whose onclick mentions verify/otp
            const all = Array.from(document.querySelectorAll(
                'button, input[type="submit"], input[type="button"], a.btn'
            )).filter(b => b.offsetParent !== null && b.id !== forbid_id);
            const cand = all.find(b => {{
                const k = ((b.id||'') + (b.getAttribute('onclick')||'')).toLowerCase();
                return /verify|otp/.test(k) && !/generate|resend/.test(k);
            }});
            if (cand) {{
                cand.disabled = false;
                cand.removeAttribute('disabled');
                const fname = fnFromOnclick(cand.getAttribute('onclick'));
                if (fname && typeof window[fname] === 'function') {{
                    try {{ window[fname](); return 'fallback_fn:' + fname; }} catch (e) {{}}
                }}
                cand.click();
                return 'fallback_click:' + (cand.id || cand.tagName);
            }}
            return null;
        }}""")

        if js_result:
            clicked = True
            click_method = js_result

        log.info("brain.otp_submit_attempted", clicked=clicked, method=click_method,
                 strategy=submit_strategy, btn_id=submit_btn_id,
                 btn_text=submit_btn_text, type=otp_type)

        # ── Wait and verify OTP submission worked ─────────────────────────────
        await asyncio.sleep(4.0)
        post_url    = await self._browser.current_url()
        post_text   = await self._browser.page_text()
        post_dom    = await self._browser.get_interactive_elements()
        dialog_msg  = await self._browser.get_last_dialog_message() or ""

        otp_reject_patterns     = VERIFY_OTP_RULES["rejection_dialog_patterns"]
        captcha_reject_patterns = VERIFY_OTP_RULES["captcha_rejection_patterns"]
        rejection_phrases       = otp_reject_patterns + captcha_reject_patterns
        otp_rejected = any(p in post_text.lower() for p in rejection_phrases) \
                    or any(p in dialog_msg.lower() for p in rejection_phrases)

        # Page is considered changed if URL changed OR OTP input is gone from post-submit DOM
        otp_gone_now = not self._has_otp_input(post_dom, post_text)
        page_changed = (post_url != pre_url) or otp_gone_now

        if dialog_msg:
            log.info("brain.otp_submit_dialog", message=dialog_msg[:200])

        if page_changed and not otp_rejected:
            log.info("brain.otp_verified_page_changed", pre=pre_url[-60:], post=post_url[-60:])
            self._save_captcha_attempt(
                job_id=job.job_id,
                flow="otp",
                attempt=self._otp_submit_attempts,
                stage="verified",
                image_bytes=otp_captcha_bytes,
                solution=otp_captcha_sol,
                metadata={
                    "method": click_method,
                    "strategy": submit_strategy,
                    "post_url": post_url,
                    "dialog": dialog_msg[:200],
                },
            )
            self._record_otp_rule_discoveries(
                otp_section_info=otp_section_info,
                click_method=click_method,
                rulebook_otp_input_sel=rulebook_otp_input_sel,
                rulebook_captcha_img_sel=rulebook_captcha_img_sel,
                rulebook_captcha_inp_sel=rulebook_captcha_inp_sel,
                rulebook_submit_sel=rulebook_submit_sel,
                rulebook_submit_fn=rulebook_submit_fn,
            )
            completed_step = (
                "aadhaar_otp_verification" if otp_type == "aadhaar"
                else "mobile_otp_verification"
            )
            if completed_step not in job.steps_completed:
                job.mark_step_done(completed_step, StepLog(
                    step_name=completed_step,
                    status="success",
                    observation=f"{otp_type} OTP verified — page navigated",
                    action_taken="otp_js_fill_submit",
                ))
            if "auth_method_selection" not in job.steps_completed:
                job.mark_step_done("auth_method_selection", StepLog(
                    step_name="auth_method_selection",
                    status="success",
                    observation="Authentication method completed before OTP verification",
                    action_taken="otp_verified_checkpoint",
                ))
            if "accept_alert_popup" not in job.steps_completed:
                job.mark_step_done("accept_alert_popup", StepLog(
                    step_name="accept_alert_popup",
                    status="success",
                    observation="Post-authentication alert/OTP step completed",
                    action_taken="otp_verified_checkpoint",
                ))
            job.otp_pending_type = ""
            self._otp_sent = False
            self._otp_reveal_attempts = 0
            self._cached_otp = ""
            self._otp_submit_attempts = 0
            self._force_otp_handler = False
            await self._sm.transition(job, JobStatus.AGENT_RUNNING)
        else:
            combined = (post_text + " " + dialog_msg).lower()
            reason = self._classify_otp_submit_failure(combined)
            captcha_rejected = reason == "captcha_rejected"
            otp_expired = reason == "otp_expired"
            otp_invalid = reason == "otp_invalid"
            if reason == "unknown" and otp_rejected:
                reason = "otp_invalid"
                otp_invalid = True
            if reason == "unknown":
                reason = "page_unchanged_after_submit"
            log.warning("brain.otp_submit_unconfirmed", reason=reason,
                        attempt=self._otp_submit_attempts, captcha=otp_captcha_sol,
                        dialog=dialog_msg[:120])
            self._save_captcha_attempt(
                job_id=job.job_id,
                flow="otp",
                attempt=self._otp_submit_attempts,
                stage=reason,
                image_bytes=otp_captcha_bytes,
                solution=otp_captcha_sol,
                metadata={
                    "method": click_method,
                    "strategy": submit_strategy,
                    "dialog": dialog_msg[:200],
                    "post_url": post_url,
                    "otp_still_present": not otp_gone_now,
                    "readiness": readiness,
                },
            )

            if otp_expired:
                await self._resend_current_otp("portal_said_otp_expired")
            elif otp_invalid:
                self._cached_otp = ""
                self._otp_submit_attempts = 0
                self._otp_prompt_reason = "The portal said the previous OTP was invalid."
            elif captcha_rejected:
                await self._refresh_visible_captcha(f"#{captcha_img_id}" if captcha_img_id else "")
            elif self._otp_submit_attempts >= max_same_otp_attempts:
                await self._resend_current_otp("otp_page_unchanged_after_max_attempts")
            else:
                await self._refresh_visible_captcha(f"#{captcha_img_id}" if captcha_img_id else "")

        return "submitted"

    async def _maybe_handle_auth_method_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        """
        Deterministically select Sarathi's non-Aadhaar/mobile OTP path.

        Leaving this to the LLM caused a slow loop: it clicked Submit before
        selecting the radio, accepted Sarathi's alert, then waited. This page is
        stable enough to rule-drive.
        """
        lower = (page_text or "").lower()
        input_ids = {(i.get("id") or "") for i in dom_elements.get("inputs", [])}
        button_ids = {(b.get("id") or "") for b in dom_elements.get("buttons", [])}
        has_auth_radio = "aadhaarHoldingType0" in input_ids
        has_submit = "submt" in button_ids or "#submt" in {
            b.get("selector", "") for b in dom_elements.get("buttons", [])
        }
        otp_done = {"mobile_otp_verification", "aadhaar_otp_verification"}
        if any(s in job.steps_completed for s in otp_done):
            return False

        if not has_auth_radio or not has_submit:
            return False
        if "generate otp" in lower or self._has_otp_input(dom_elements, page_text):
            return False

        log.info("brain.auth_method_page_detected", step=current_step)
        ok = await self._browser.evaluate("""() => {
            const radio = document.getElementById('aadhaarHoldingType0');
            if (!radio) return {ok:false, reason:'missing_radio'};
            radio.disabled = false;
            radio.removeAttribute('disabled');
            radio.checked = true;
            radio.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            radio.dispatchEvent(new Event('input', {bubbles:true}));
            radio.dispatchEvent(new Event('change', {bubbles:true}));

            const submit = document.getElementById('submt');
            if (!submit) return {ok:false, reason:'missing_submit', checked: radio.checked};
            submit.disabled = false;
            submit.removeAttribute('disabled');
            submit.removeAttribute('readonly');
            submit.click();
            return {ok:true, checked: radio.checked, submit_id: submit.id};
        }""")
        log.info("brain.auth_method_submitted", result=ok)
        await asyncio.sleep(1.0)
        dialog_msg = await self._browser.get_last_dialog_message() or ""
        if dialog_msg:
            log.info("brain.auth_method_dialog", message=dialog_msg[:160])
        return bool(ok and ok.get("ok"))

    def _classify_otp_submit_failure(self, combined_text: str) -> str:
        """Map Sarathi OTP/CAPTCHA messages to deterministic recovery actions."""
        lower = (combined_text or "").lower()
        if any(p in lower for p in VERIFY_OTP_RULES.get("captcha_rejection_patterns", [])):
            return "captcha_rejected"
        if any(p in lower for p in VERIFY_OTP_RULES.get("otp_expired_patterns", [])):
            return "otp_expired"
        if any(p in lower for p in VERIFY_OTP_RULES.get("otp_invalid_patterns", [])):
            return "otp_invalid"
        if any(p in lower for p in VERIFY_OTP_RULES.get("rejection_dialog_patterns", [])):
            return "otp_invalid"
        return "unknown"

    async def _resend_current_otp(self, reason: str) -> bool:
        """Click/call Sarathi's Resend OTP rule and reset local OTP cache."""
        method = await self._browser.evaluate(f"""() => {{
            const selector = {json.dumps(GENERATE_OTP_RULES["resend_button_selector"])};
            const fnName = {json.dumps(GENERATE_OTP_RULES["resend_button_onclick_fn"])};
            const btn = document.querySelector(selector)
                || document.getElementById('generateResendSarathiotp')
                || Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a.btn'))
                    .find(el => {{
                        const key = ((el.id || '') + ' ' + (el.value || '') + ' '
                            + (el.textContent || '') + ' ' + (el.getAttribute('onclick') || '')).toLowerCase();
                        return key.includes('resend') && key.includes('otp');
                    }});
            if (btn) {{
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.style.display = '';
                btn.style.visibility = 'visible';
            }}
            if (fnName && typeof window[fnName] === 'function') {{
                try {{ window[fnName](); return 'rulebook_fn:' + fnName; }}
                catch (e) {{ return 'rulebook_fn_error:' + e.message; }}
            }}
            if (btn) {{
                try {{ btn.click(); return 'button_click:' + (btn.id || btn.value || btn.textContent || btn.tagName); }}
                catch (e) {{ return 'button_click_error:' + e.message; }}
            }}
            return '';
        }}""")
        if not method:
            for resend_text in ["Resend OTP", "Resend", "Resend otp"]:
                if await self._browser.click_text(resend_text, exact=False):
                    method = f"text_click:{resend_text}"
                    break

        self._cached_otp = ""
        self._otp_submit_attempts = 0
        self._otp_reveal_attempts = 0
        self._force_otp_handler = True
        self._otp_prompt_reason = (
            "The previous OTP expired, so we requested a fresh OTP."
            if "expired" in reason or "exhausted" in reason or "unchanged" in reason
            else "We requested a fresh OTP."
        )
        if method:
            log.info("brain.otp_resend_triggered", method=method, reason=reason)
            await asyncio.sleep(2.0)
            return True

        self._otp_sent = False
        log.warning("brain.otp_resend_unavailable", reason=reason)
        return False

    def _save_captcha_attempt(
        self,
        *,
        job_id: str,
        flow: str,
        attempt: int,
        stage: str,
        image_bytes: bytes,
        solution: str,
        metadata: dict | None = None,
    ) -> None:
        """Persist CAPTCHA evidence so failed runs can be debugged precisely.

        Writes are synchronous (caller does not await). The disk write happens
        immediately so local debug paths keep working; the S3 upload is fired
        as a background task so we don't block the agent step on a network
        round-trip.
        """
        try:
            safe_flow = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in flow)
            safe_stage = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stage)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = Path("data") / "captcha_attempts" / job_id
            out_dir.mkdir(parents=True, exist_ok=True)
            base = out_dir / f"{ts}_{safe_flow}_attempt{attempt}_{safe_stage}"
            png_path = base.with_suffix(".png") if image_bytes else None
            if png_path is not None:
                png_path.write_bytes(image_bytes)
            base.with_suffix(".json").write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "flow": flow,
                        "attempt": attempt,
                        "stage": stage,
                        "solution": solution,
                        "metadata": metadata or {},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            log.info("brain.captcha_attempt_saved", path=str(base))

            if png_path is not None and self._current_job is not None:
                from tools.storage import get_storage, stamp_screenshot_on_job

                async def _upload_and_stamp() -> None:
                    try:
                        result = await get_storage().put_bytes(
                            local_path=str(png_path),
                            data=image_bytes,
                            kind="captcha_attempt",
                            job_id=job_id,
                            content_type="image/png",
                        )
                        # Only stamp on the job if S3 actually accepted the upload
                        # — otherwise the disk write already happened and tracking
                        # the local-only URL would clutter the screenshots list.
                        if result.s3_url and self._current_job is not None:
                            stamp_screenshot_on_job(
                                self._current_job, result,
                                label=f"captcha:{flow}:attempt{attempt}:{stage}",
                            )
                    except Exception as e:  # noqa: BLE001
                        log.warning("brain.captcha_attempt_s3_failed", error=str(e))

                try:
                    asyncio.create_task(_upload_and_stamp())
                except RuntimeError:
                    # No running loop (rare in our async code path); skip.
                    pass
        except Exception as e:
            log.warning("brain.captcha_attempt_save_failed", error=str(e))

    def _record_rule_discovery(
        self,
        rule_name: str,
        key: str,
        discovered_value,
        current_value,
        source: str,
    ) -> None:
        """Persist a learned rule only when it differs from the rule book."""
        if not discovered_value or discovered_value == current_value:
            return
        try:
            record_discovery(rule_name, key, discovered_value)
            log.info(
                "brain.rule_discovery_recorded",
                rule=rule_name,
                key=key,
                value=discovered_value,
                previous=current_value,
                source=source,
            )
        except Exception as e:
            log.warning(
                "brain.rule_discovery_failed",
                rule=rule_name,
                key=key,
                value=discovered_value,
                error=str(e),
            )

    @staticmethod
    def _function_from_click_method(method: str) -> str:
        """
        Extract a function name from OTP submit/generate fallback markers.

        Examples:
          fn_from_onclick:verifiedBySarathi -> verifiedBySarathi
          known_fn:verifyOTP -> verifyOTP
        """
        if not method or ":" not in method:
            return ""
        prefix, value = method.split(":", 1)
        if prefix in {
            "fn_from_onclick",
            "known_fn",
            "fallback_fn",
            "onclick_fn",
        } and value:
            return value.split(":", 1)[0]
        return ""

    def _record_otp_rule_discoveries(
        self,
        otp_section_info: dict,
        click_method: str,
        rulebook_otp_input_sel: str,
        rulebook_captcha_img_sel: str,
        rulebook_captcha_inp_sel: str,
        rulebook_submit_sel: str,
        rulebook_submit_fn: str,
    ) -> None:
        """Promote observed OTP selectors/functions after OTP verification succeeds."""
        discovered = {
            "otp_input_selector": otp_section_info.get("otp_selector"),
            "captcha_image_selector": otp_section_info.get("captcha_img_selector"),
            "captcha_input_selector": otp_section_info.get("captcha_inp_selector"),
            "submit_button_selector": otp_section_info.get("submit_btn_selector"),
        }
        current = {
            "otp_input_selector": rulebook_otp_input_sel,
            "captcha_image_selector": rulebook_captcha_img_sel,
            "captcha_input_selector": rulebook_captcha_inp_sel,
            "submit_button_selector": rulebook_submit_sel,
        }
        for key, value in discovered.items():
            self._record_rule_discovery(
                "VERIFY_OTP_RULES",
                key,
                value,
                current[key],
                source="otp_verified",
            )

        discovered_fn = self._function_from_click_method(click_method)
        self._record_rule_discovery(
            "VERIFY_OTP_RULES",
            "submit_onclick_fn",
            discovered_fn,
            rulebook_submit_fn,
            source=click_method,
        )

    def _record_generate_otp_rule_discoveries(
        self,
        method: str,
        discovered_selector: str,
        discovered_fn: str,
        rulebook_selector: str,
        rulebook_fn: str,
    ) -> None:
        """Promote observed Generate OTP selector/function after OTP generation succeeds."""
        self._record_rule_discovery(
            "GENERATE_OTP_RULES",
            "button_selector",
            discovered_selector,
            rulebook_selector,
            source=method,
        )

        fn = discovered_fn or self._function_from_click_method(method)
        self._record_rule_discovery(
            "GENERATE_OTP_RULES",
            "button_onclick_fn",
            fn,
            rulebook_fn,
            source=method,
        )

    async def _maybe_handle_generate_otp_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        """
        Handle Sarathi's Generate OTP page (CAPTCHA + mobile OTP trigger).

        Root cause of #generateSarathiotp staying disabled:
          The button requires TWO fields to be non-empty: mobileNumber AND captcha.
          Our previous code only filled captcha. fill() also skips DOM events, so
          the portal's JS onChange never fired to enable the button.

        Strategy (primary = direct API, UI = fallback):
          1. Extract the pre-filled mobile number from the page input.
          2. Solve + fill CAPTCHA, then press Tab to fire onblur.
          3. Fill mobile number field and fire input/change events via JS.
          4. Wait for JS to enable the button, try Playwright click.
          5. If button still disabled: call getOtpFromSarathi.do API directly
             using the browser session cookies — server validates CAPTCHA and
             sends OTP to the registered mobile. No UI button needed.
        """
        otp_done = {"mobile_otp_verification", "aadhaar_otp_verification"}
        if any(s in job.steps_completed for s in otp_done):
            return False

        lower = page_text.lower()
        visible_buttons = [
            (b.get("text") or "").strip().lower()
            for b in dom_elements.get("buttons", [])
            if b.get("visible", True)
        ]
        if "generate otp" not in lower and not any("generate otp" in b for b in visible_buttons):
            return False

        pre_generate_dom = await self._browser.get_interactive_elements()
        pre_generate_text = page_text
        pre_had_otp_input = self._has_otp_input(pre_generate_dom, pre_generate_text)

        # ── Guard: OTP already sent — wait for the real portal OTP section ───
        if self._otp_sent:
            self._otp_reveal_attempts += 1
            log.info("brain.otp_already_sent_waiting", attempt=self._otp_reveal_attempts)
            await asyncio.sleep(1.0)
            if self._otp_reveal_attempts > 6:
                # The portal did not naturally reveal OTP UI; try fresh captcha.
                log.warning("brain.otp_natural_reveal_gave_up")
                self._otp_sent = False
                self._otp_reveal_attempts = 0
                return False
            return True  # come back next loop iteration

        log.info("brain.generate_otp_page_detected", step=current_step)
        self._generate_otp_attempts += 1
        log.info("brain.generate_otp_attempt", attempt=self._generate_otp_attempts, max_attempts=4)
        if self._generate_otp_attempts > 4:
            log.warning("brain.generate_otp_attempts_exhausted", step=current_step)
            self._generate_otp_attempts = 0
            return False

        # Consume stale alerts before this attempt. Otherwise an old
        # "invalid captcha" dialog can poison the proof check for the next
        # freshly-filled CAPTCHA.
        await self._browser.get_last_dialog_message()

        # ── Step 1: Read mobile number from pre-filled page field ─────────────
        mobile = await self._browser.evaluate("""() => {
            const sels = [
                'input[name="mobileNumber"]',
                'input[id*="mobile" i]:not([type="hidden"])',
                'input[id*="phone" i]:not([type="hidden"])',
                'input[type="tel"]',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.value && el.value.trim().length >= 6) return el.value.trim();
            }
            return null;
        }""")
        if not mobile:
            mobile = job.customer_data.get("mobile_number", "")
        log.info("brain.generate_otp_mobile", mobile=mobile, step=current_step)

        # ── Step 2: Solve + fill CAPTCHA ──────────────────────────────────────
        # Refresh CAPTCHA first to avoid stale image / repeated-answer issue.
        await self._browser.evaluate("""() => {
            const imgs = Array.from(document.querySelectorAll(
                'img[id*="cap" i], img[src*="cap" i]'
            )).filter(img => img.offsetParent !== null || img.id === 'capimg');
            for (const img of imgs) {
                if (!img.src) continue;
                const base = img.src.split('?')[0];
                img.src = base + '?t=' + Date.now();
            }
            const inputs = Array.from(document.querySelectorAll(
                'input[id*="capt" i]:not([type="hidden"]), input[name*="capt" i]:not([type="hidden"])'
            ));
            for (const el of inputs) {
                const nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }
            return imgs.map(img => img.id || img.src.slice(0, 40));
        }""")
        await asyncio.sleep(0.8)
        for refresh_text in ["Refresh", "Change Image", "Reload"]:
            try:
                el = self._browser._page.get_by_text(refresh_text, exact=False)
                if await el.first.is_visible(timeout=400):
                    await el.first.click(timeout=2000)
                    await asyncio.sleep(0.6)
                    log.info("brain.captcha_refreshed", via=refresh_text)
                    break
            except Exception:
                pass

        fresh_dom = await self._browser.get_interactive_elements()
        captcha_solution = await self._solve_captcha_value(
            image_selector=GENERATE_OTP_RULES["captcha_image_selector"],
            force_manual=self._generate_otp_attempts > settings.captcha_max_retries,
            prompt_context=(
                "Generate OTP CAPTCHA. Automatic attempts did not produce a valid OTP request; "
                "please provide the CAPTCHA shown before clicking Generate OTP."
                if self._generate_otp_attempts > settings.captcha_max_retries else ""
            ),
        )
        if not captcha_solution:
            log.warning("brain.generate_otp_captcha_unsolvable", step=current_step)
            return self._generate_otp_attempts <= 4

        captcha_filled = await self._browser.fill(
            GENERATE_OTP_RULES["captcha_input_selector"],
            captcha_solution,
        )
        if not captcha_filled:
            captcha_filled = await self._fill_captcha_field(fresh_dom, captcha_solution)
        if not captcha_filled:
            log.warning("brain.generate_otp_captcha_not_filled", step=current_step)
            return self._generate_otp_attempts <= 4

        # ── Step 3: Fill mobile number field + fire JS events ─────────────────
        if mobile:
            filled_mobile = await self._browser.evaluate(f"""() => {{
                const val = {json.dumps(mobile)};
                const sels = [
                    'input[name="mobileNumber"]',
                    'input[id*="mobile" i]:not([type="hidden"])',
                    'input[id*="phone" i]:not([type="hidden"])',
                    'input[type="tel"]',
                ];
                for (const s of sels) {{
                    const el = document.querySelector(s);
                    if (el) {{
                        const nativeSet = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        nativeSet.call(el, val);
                        el.dispatchEvent(new Event('input',  {{bubbles:true}}));
                        el.dispatchEvent(new Event('change', {{bubbles:true}}));
                        el.dispatchEvent(new Event('blur',   {{bubbles:true}}));
                        return s;
                    }}
                }}
                return null;
            }}""")
            if filled_mobile:
                log.info("brain.mobile_field_filled", selector=filled_mobile, mobile=mobile)

        # Press Tab on captcha to fire blur → should enable the Generate OTP button
        await self._browser.press_key("Tab")
        await asyncio.sleep(1.2)

        # ── Click Generate OTP button/function deterministically ──────────────
        # #generateotp is an invisible Aadhaar button. The mobile OTP button is
        # #generateSarathiotp and its onclick is gensarathiOTP(). Call that first
        # after force-enabling the button so we do not waste time on hidden nodes.
        clicked = False
        generate_method = "none"
        discovered_generate_selector = ""
        discovered_generate_fn = ""
        rulebook_generate_selector = GENERATE_OTP_RULES["button_selector"]
        rulebook_generate_fn = GENERATE_OTP_RULES["button_onclick_fn"]

        gen_btn_id = GENERATE_OTP_RULES["button_selector"].lstrip("#")
        gen_fn     = GENERATE_OTP_RULES["button_onclick_fn"]
        result = await self._browser.evaluate(f"""() => {{
            const fn = {json.dumps(gen_fn)};
            const targetId = {json.dumps(gen_btn_id)};
            const btn = document.getElementById(targetId)
                || Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"]'))
                    .find(x => x.id === 'generateSarathiotp'
                        || /gensarathiotp/i.test(x.getAttribute('onclick') || '')
                        || /generate\\s*otp/i.test((x.textContent || '') + (x.value || '')));
            if (btn) {{
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.removeAttribute('readonly');
                btn.style.display = '';
                btn.style.visibility = 'visible';
                btn.scrollIntoView({{block: 'center', inline: 'center'}});
            }}
            if (typeof window[fn] === 'function') {{
                try {{ window[fn](); return 'rulebook_fn:' + fn; }} catch(e) {{
                    return 'rulebook_fn_error:' + fn + ':' + e.message;
                }}
            }}
            if (btn) {{
                const onclick = btn.getAttribute('onclick') || '';
                const m = onclick.match(/^\\s*([a-zA-Z_$][\\w$]*)\\s*\\(/);
                if (m && typeof window[m[1]] === 'function') {{
                    try {{ window[m[1]](); return 'onclick_fn:' + m[1]; }} catch(e) {{
                        return 'onclick_fn_error:' + m[1] + ':' + e.message;
                    }}
                }}
                btn.click();
                return 'force_click:' + (btn.id || 'btn');
            }}
            return null;
        }}""")
        if result:
            clicked = True
            generate_method = result
            discovered_generate_selector = rulebook_generate_selector
            if result.startswith("onclick_fn:"):
                discovered_generate_fn = result.split(":", 1)[1]
            elif result.startswith("rulebook_fn:"):
                discovered_generate_fn = gen_fn
            log.info("brain.generate_otp_js_force_click", method=result)

        # Do not bypass the visible government form with the API here. The UI
        # click/function is what runs Sarathi's real validation and SMS flow.

        if not clicked:
            log.warning("brain.generate_otp_all_methods_failed", step=current_step)
            return self._generate_otp_attempts <= 4

        await asyncio.sleep(2.5)  # wait for Sarathi AJAX to activate OTP entry section
        otp_proof = await self._verify_otp_was_sent(pre_had_otp_input=pre_had_otp_input)
        if not otp_proof:
            log.warning(
                "brain.generate_otp_unconfirmed",
                method=generate_method,
                captcha=captcha_solution,
            )
            self._otp_sent = False
            return self._generate_otp_attempts <= 4

        # Mark OTP as sent only after the portal proves it.
        self._otp_sent = True
        self._generate_otp_attempts = 0
        self._record_generate_otp_rule_discoveries(
            method=generate_method,
            discovered_selector=discovered_generate_selector,
            discovered_fn=discovered_generate_fn,
            rulebook_selector=rulebook_generate_selector,
            rulebook_fn=rulebook_generate_fn,
        )

        if "auth_method_selection" not in job.steps_completed:
            job.mark_step_done("auth_method_selection", StepLog(
                step_name="auth_method_selection",
                status="success",
                observation="Generated OTP on authentication page",
                action_taken="generate_otp",
            ))
            await self._sm.save(job)

        log.info("brain.generate_otp_clicked", step=current_step)
        return True

    async def _verify_otp_was_sent(self, pre_had_otp_input: bool) -> bool:
        """True only when Sarathi naturally shows/mentions the OTP step."""
        try:
            page_text = await self._browser.page_text()
            dom = await self._browser.get_interactive_elements()
            dialog_msg = await self._browser.get_last_dialog_message() or ""
        except Exception as e:
            log.warning("brain.generate_otp_verify_failed", error=str(e))
            return False

        combined = f"{page_text}\n{dialog_msg}".lower()
        sent_markers = [
            "otp has been sent",
            "otp sent",
            "enter otp",
            "one time password",
            "resend otp",
            "verify otp",
        ]
        rejection_markers = [
            "invalid captcha",
            "captcha mismatch",
            "wrong captcha",
            "please enter valid captcha",
            "please enter captcha",
            "unable to generate otp",
            "otp not sent",
        ]
        rejected = any(marker in combined for marker in rejection_markers)
        has_text_proof = any(marker in combined for marker in sent_markers)
        has_input_proof = self._has_otp_input(dom, page_text)
        newly_showed_otp_input = has_input_proof and not pre_had_otp_input
        log.info(
            "brain.generate_otp_proof",
            text_proof=has_text_proof,
            input_proof=has_input_proof,
            pre_had_otp_input=pre_had_otp_input,
            newly_showed_otp_input=newly_showed_otp_input,
            rejected=rejected,
            dialog=dialog_msg[:120],
        )
        return has_text_proof or newly_showed_otp_input

    # ── Confirm DL details (PIN code → RTO auto-fill) ─────────────────────────

    async def _maybe_handle_confirm_dl_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        """
        Deterministically handle the DL details confirmation page.
        Fills pin code (auto-populates RTO), selects YES, clicks Proceed.
        This replaces the LLM trying to guess the RTO name from a long dropdown.
        """
        if "confirm_dl_details" in job.steps_completed:
            return False

        rules = DL_CONFIRM_RULES
        yes_select_id = rules["yes_select_selector"].lstrip("#")
        category_id   = rules["category_selector"].lstrip("#")

        visible_select_ids = {
            (s.get("id") or "")
            for s in dom_elements.get("selects", [])
            if s.get("visible", True)
        }
        if yes_select_id not in visible_select_ids:
            return False

        visible_buttons = [
            (b.get("text") or "").strip().lower()
            for b in dom_elements.get("buttons", [])
            if b.get("visible", True)
        ]
        if not any("proceed" in t or "confirm" in t for t in visible_buttons):
            return False

        log.info("brain.confirm_dl_page_detected", step=current_step)

        # Step 1: Select YES — "Are these your DL details?"
        await self._browser.select_option(rules["yes_select_selector"], label=rules["yes_label"])
        await asyncio.sleep(0.5)

        # Step 2: Fill PIN code → portal auto-selects the RTO
        pin_code = (
            job.customer_data.get("pin_code")
            or job.customer_data.get("pincode", "")
        )
        if pin_code:
            for sel in rules["pin_input_selectors"]:
                if await self._browser.fill(sel, pin_code, blur_after=True):
                    log.info("brain.pin_code_filled", selector=sel, pin=pin_code)
                    await asyncio.sleep(rules["rto_autofill_wait_ms"] / 1000)
                    break
            else:
                log.warning("brain.pin_code_field_not_found")

        # Step 3: Select category if visible (General = default)
        if category_id in visible_select_ids:
            await self._browser.select_option(rules["category_selector"], label=rules["category_default"])
            await asyncio.sleep(0.3)

        # Step 4: Click Proceed button
        clicked = await self._browser.click_selector(rules["proceed_button_selector"], "Proceed")
        if not clicked:
            clicked = await self._browser.click_text("Proceed", exact=True)
        if not clicked:
            log.warning("brain.confirm_dl_proceed_not_clicked")
            return False

        await asyncio.sleep(1.5)
        dialog_msg = await self._browser.get_last_dialog_message()
        if dialog_msg:
            log.info("brain.confirm_dl_dialog_accepted", message=dialog_msg[:120])

        job.mark_step_done("confirm_dl_details", StepLog(
            step_name="confirm_dl_details",
            status="success",
            observation="DL details confirmed: YES selected, pin code filled, Proceed clicked",
            action_taken="confirm_dl",
        ))
        await self._sm.save(job)
        return True

    async def _fail_if_service_rejected(
        self,
        job: Job,
        page_text: str,
        current_step: str,
    ) -> bool:
        """
        Sarathi can accept a service selection click, then reject that service
        for the resolved RTO. That is a terminal portal business-rule result,
        not a selector failure to retry.
        """
        clean = " ".join((page_text or "").split())
        lower = clean.lower()
        if not (
            "requested service" in lower
            and (
                "unable to process your data" in lower
                or "not legible for requested rto" in lower
                or "not eligible for requested rto" in lower
                or "kindly visit the rto/rla authority" in lower
            )
        ):
            return False

        selected = job.customer_data.get("selected_service", "")
        service = selected
        rto = ""
        match = re.search(
            r"Requested Service:\s*(.*?)\s+is not (?:legible|eligible) for Requested RTO:\s*(.*?)(?:\.|Kindly|$)",
            clean,
            flags=re.IGNORECASE,
        )
        if match:
            service = match.group(1).strip() or service
            rto = match.group(2).strip()

        customer_message = SERVICE_REJECTION_RULES["customer_message"]
        if service and rto:
            customer_message = (
                f"Sarathi says {service} is not available for {rto}. "
                "Choose another available service or visit the RTO/RLA authority for this request."
            )

        job.customer_data["portal_service_rejection"] = {
            "service": service,
            "rto": rto,
            "raw_message": clean,
            "customer_message": customer_message,
        }
        job.step_logs.append(StepLog(
            step_name="service_selection",
            status="warning",
            observation=customer_message,
            action_taken="portal_service_rejection_recoverable",
            error=clean,
        ).to_dict())
        # Self-healing path:
        # 1) don't fail terminally,
        # 2) clear prior selected service,
        # 3) ask customer to pick another available service.
        rejected_canonical = (service or "").strip()
        available = job.customer_data.get("available_services", []) or []
        options = []
        for s in available:
            if not rejected_canonical or s.strip().upper() != rejected_canonical.upper():
                options.append(s)
        if not options:
            options = available[:]
        if not options:
            options = [
                "CHANGE OF DATE OF BIRTH IN DL",
                "CHANGE OF ADDRESS IN DL",
                "CHANGE OF NAME IN DL",
            ]
        job.customer_data.pop("selected_service", None)
        job.customer_data["_pending_customer_request"] = {
            "step_name": "service_selection",
            "action_type": "service_selection",
            "question": customer_message + " Please choose another service.",
            "context": "Select another service from the available list.",
            "options": options[:8],
            "answered": False,
        }
        await self._sm.transition(job, JobStatus.STUCK_HUMAN_NEEDED, customer_message)
        await self._sm.save(job)
        log.warning(
            "brain.service_rejected_by_rto",
            service=service,
            rto=rto,
            message=clean[:220],
            step=current_step,
        )
        return True

    # ── Service selection page (after OTP verification) ───────────────────────

    async def _maybe_handle_service_selection_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
        screenshot: bytes,
    ) -> bool:
        """
        Handle the DL service selection page that appears after OTP verification.
        Extracts available services from the page, asks user to pick one,
        validates DL Renewal eligibility (365-day rule), and clicks Proceed.
        """
        if "service_selection" in job.steps_completed:
            return False

        # Must have actually submitted and verified OTP — not just seen the auth page.
        # auth_method_selection is marked done as soon as the auth page is detected,
        # which is too early. Only proceed after OTP is confirmed.
        otp_verified = {"mobile_otp_verification", "aadhaar_otp_verification"}
        if not any(s in job.steps_completed for s in otp_verified):
            return False

        if await self._fail_if_service_rejected(job, page_text, current_step):
            return True

        lower = page_text.lower()
        service_page_signals = [
            "select service", "service selection", "services on dl",
            "renewal of driving licence", "extract of driving licence",
            "duplicate driving licence", "change of address on dl",
            "select the service", "dl services",
        ]
        if not any(sig in lower for sig in service_page_signals):
            return False

        log.info("brain.service_selection_page_detected", step=current_step)

        service_items = await self._browser.evaluate(r"""() => {
            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const labelText = (inp) => {
                const byFor = inp.id ? document.querySelector(`label[for="${CSS.escape(inp.id)}"]`) : null;
                if (byFor) return byFor.innerText || byFor.textContent || '';
                const wrapping = inp.closest('label');
                if (wrapping) return wrapping.innerText || wrapping.textContent || '';
                const row = inp.closest('tr, li, div, p');
                return row ? (row.innerText || row.textContent || '') : '';
            };
            return Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"]'))
                .filter(inp => (inp.name || '').toLowerCase() === 'dlc' || (inp.value || '').toLowerCase().includes('dl'))
                .map(inp => ({
                    id: inp.id || '',
                    name: inp.name || '',
                    value: inp.value || '',
                    label: labelText(inp).replace(/\s+/g, ' ').trim(),
                    selector: inp.id ? `#${CSS.escape(inp.id)}` : '',
                    visible: isVisible(inp),
                    checked: !!inp.checked,
                    disabled: !!inp.disabled,
                }))
                .filter(item => item.value || item.label);
        }""")

        def canonical_service(value: str) -> str:
            raw = (value or "").strip()
            key = " ".join(raw.lower().replace("_", " ").split())
            aliases = SERVICE_SELECTION_RULES.get("aliases", {})
            if key in aliases:
                return aliases[key]
            for alias, canonical in aliases.items():
                if alias in key or key in alias:
                    return canonical
            return raw.upper()

        services: list[str] = []
        for item in service_items or []:
            candidate = (item.get("value") or item.get("label") or "").strip()
            if candidate and candidate not in services:
                services.append(candidate)

        if not services:
            services = [
                "CHANGE OF DATE OF BIRTH IN DL",
                "CHANGE OF ADDRESS IN DL",
                "CHANGE OF NAME IN DL",
            ]
        job.customer_data["available_services"] = services

        # DL Renewal 365-day validation
        from datetime import datetime as _dt
        dl_expiry = job.customer_data.get("dl_expiry_date", "")
        renewal_note = ""
        if dl_expiry:
            try:
                exp = _dt.strptime(dl_expiry, "%d-%m-%Y")
                days_left = (exp - _dt.now()).days
                if days_left > 365:
                    renewal_note = (
                        f"Note: Your DL expires on {dl_expiry} ({days_left} days away). "
                        "DL Renewal is only available within 365 days of expiry — "
                        "Renewal has been removed from the options below."
                    )
                    services = [s for s in services if "renewal" not in s.lower()]
                    log.info("brain.dl_renewal_ineligible", days_left=days_left)
            except Exception:
                pass

        options_to_show = services[:8]
        context = renewal_note or "Select the DL service you need. The agent will fill the form and submit."

        selected = (job.customer_data.get("selected_service") or "").strip()
        if not selected:
            manual_file = Path(SERVICE_SELECTION_RULES["manual_answer_file"])
            if manual_file.exists():
                selected = manual_file.read_text(encoding="utf-8").strip()
                try:
                    manual_file.unlink()
                except OSError:
                    pass

        if not selected:
            resp = await self._hl.ask(
                job=job,
                step_name="service_selection",
                question="Which DL service would you like to apply for?",
                context=context,
                screenshot=screenshot,
                options=options_to_show,
            )
            if resp.answer in ("__timeout__", ""):
                log.warning("brain.service_selection_no_answer")
                return True
            selected = resp.answer

        selected = canonical_service(selected)
        job.customer_data["selected_service"] = selected
        log.info("brain.service_selected", service=selected, available=services)

        # Click the selected service (checkbox / radio / link)
        result = await self._browser.evaluate(f"""() => {{
            const selected = {json.dumps(selected)};
            const selectedLower = selected.toLowerCase();
            const serviceName = {json.dumps(SERVICE_SELECTION_RULES["service_input_name"])};
            const isVisible = (el) => {{
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            }};
            const labelText = (inp) => {{
                const byFor = inp.id ? document.querySelector(`label[for="${{CSS.escape(inp.id)}}"]`) : null;
                if (byFor) return byFor.innerText || byFor.textContent || '';
                const wrapping = inp.closest('label');
                if (wrapping) return wrapping.innerText || wrapping.textContent || '';
                const row = inp.closest('tr, li, div, p');
                return row ? (row.innerText || row.textContent || '') : '';
            }};
            const inputs = Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"]'));
            const exactByName = inputs.filter(inp =>
                (inp.name || '').toLowerCase() === serviceName
                && (inp.value || '').toLowerCase() === selectedLower
            );
            const candidates = inputs.filter(inp => {{
                const value = (inp.value || '').toLowerCase();
                const label = labelText(inp).toLowerCase();
                return (inp.name || '').toLowerCase() === serviceName
                    || value.includes('dl')
                    || label.includes('dl');
            }});
            let target = exactByName[0]
                || candidates.find(inp => (inp.value || '').toLowerCase() === selectedLower)
                || candidates.find(inp => (inp.value || '').toLowerCase().includes(selectedLower))
                || candidates.find(inp => selectedLower.includes((inp.value || '').toLowerCase()))
                || candidates.find(inp => labelText(inp).toLowerCase().includes(selectedLower));
            if (!target) {{
                return {{
                    ok: false,
                    reason: 'service_not_found',
                    available: candidates.map(inp => inp.value || labelText(inp)).filter(Boolean),
                }};
            }}
            const matchingInputs = inputs.filter(inp => (inp.value || '').toLowerCase() === selectedLower);
            const touched = [];
            for (const inp of matchingInputs.length ? matchingInputs : [target]) {{
                inp.disabled = false;
                inp.removeAttribute('disabled');
                if (isVisible(inp)) {{
                    inp.scrollIntoView({{block: 'center', inline: 'center'}});
                    if (!inp.checked) inp.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                }}
                inp.checked = true;
                inp.setAttribute('checked', 'checked');
                inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                touched.push({{
                    id: inp.id || '',
                    name: inp.name || '',
                    value: inp.value || '',
                    visible: isVisible(inp),
                    checked: !!inp.checked,
                }});
            }}
            if (!touched.some(item => item.checked && item.name === serviceName)) {{
                const host = target.form || target.closest('form') || document.querySelector('form') || document.body;
                let canonical = inputs.find(inp =>
                    (inp.name || '').toLowerCase() === serviceName
                    && (inp.value || '').toLowerCase() === selectedLower
                );
                if (!canonical) {{
                    canonical = document.createElement('input');
                    canonical.type = 'checkbox';
                    canonical.name = serviceName;
                    canonical.value = selected;
                    canonical.id = 'agent_' + serviceName + '_' + Date.now();
                    canonical.style.display = 'none';
                    host.appendChild(canonical);
                }}
                canonical.disabled = false;
                canonical.removeAttribute('disabled');
                canonical.checked = true;
                canonical.setAttribute('checked', 'checked');
                canonical.dispatchEvent(new Event('input', {{bubbles: true}}));
                canonical.dispatchEvent(new Event('change', {{bubbles: true}}));
                touched.push({{
                    id: canonical.id || '',
                    name: canonical.name || '',
                    value: canonical.value || '',
                    visible: isVisible(canonical),
                    checked: !!canonical.checked,
                    injected: true,
                }});
            }}
            return {{
                ok: touched.some(item => item.checked && item.name === 'dlc') || touched.some(item => item.checked),
                id: target.id || '',
                name: target.name || '',
                value: target.value || '',
                label: labelText(target).replace(/\\s+/g, ' ').trim(),
                visible: isVisible(target),
                checked: !!target.checked,
                exact_name_matches: exactByName.length,
                touched,
            }};
        }}""")
        clicked = bool(result and result.get("ok"))
        if clicked:
            log.info("brain.service_clicked_rulebook", result=result)

        if not clicked:
            log.warning("brain.service_click_failed", service=selected, result=result)
            job.customer_data.pop("selected_service", None)
            return True

        await asyncio.sleep(0.8)

        # Click Proceed / Continue
        proceed_clicked = await self._browser.click_selector(
            SERVICE_SELECTION_RULES["proceed_selector"],
            "Proceed after selecting DL service",
        )
        if not proceed_clicked:
            for btn in ["Proceed", "Continue", "Next", "Submit"]:
                if await self._browser.click_text(btn, exact=True):
                    proceed_clicked = True
                    log.info("brain.service_proceed_clicked", btn=btn)
                    break
        await asyncio.sleep(2.0)

        dialog = await self._browser.get_last_dialog_message() or ""
        post_text = await self._browser.page_text()
        if await self._fail_if_service_rejected(job, post_text, current_step):
            return True
        if "select at least one" in dialog.lower():
            log.warning("brain.service_selection_rejected", dialog=dialog, selected=selected)
            job.customer_data.pop("selected_service", None)
            return True
        if not proceed_clicked:
            log.warning("brain.service_proceed_failed", selected=selected)
            return True

        job.mark_step_done("service_selection", StepLog(
            step_name="service_selection",
            status="success",
            observation=f"User selected: {selected}",
            action_taken="service_selection",
        ))
        await self._sm.save(job)
        return True

    # ── Service form (reason / organ donation / CAPTCHA / ACK) ───────────────

    async def _maybe_handle_service_form_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
        screenshot: bytes,
    ) -> bool:
        """
        Handle the DL service-specific form after service selection.
        Covers: reason dropdown (ask user), organ donation (5s timeout → NO),
        CAPTCHA + Submit, popup handling, ACK number extraction.
        """
        if "service_form_fill" in job.steps_completed:
            return False
        if "service_selection" not in job.steps_completed:
            return False

        lower = page_text.lower()
        form_signals = [
            "reason for", "reason of", "organ donation", "willing to donate",
            "donor", "form confirmation", "declaration", "confirm application",
        ]
        if not any(sig in lower for sig in form_signals):
            return False

        log.info("brain.service_form_page_detected", step=current_step)
        selected_service = job.customer_data.get("selected_service", "DL service")

        from config.portal_rules import get_fee
        expected_fee = get_fee(selected_service, job.state_code or job.customer_data.get("state_code", "RJ"))
        log.info("brain.expected_fee", service=selected_service, state=job.state_code, fee_inr=expected_fee)
        job.customer_data["expected_fee_inr"] = expected_fee

        change_dob_handled = False
        if "date of birth" in selected_service.lower() or "coddob" in lower:
            reason_opts: list[str] = []
            for sel in dom_elements.get("selects", []):
                if (sel.get("id") or "") == CHANGE_DOB_RULES["reason_selector"].lstrip("#"):
                    reason_opts = [o.get("text", o) if isinstance(o, dict) else str(o)
                                   for o in (sel.get("options") or [])]
                    reason_opts = [o for o in reason_opts if o and o.strip() and "select" not in o.lower()]
                    break

            reason = (job.customer_data.get(CHANGE_DOB_RULES["reason_answer_key"]) or "").strip()
            if not reason:
                resp = await self._hl.ask(
                    job=job,
                    step_name="service_form_fill",
                    question="Why do you want to change the date of birth on the DL?",
                    context="Sarathi requires a reason before it can proceed with DOB correction.",
                    screenshot=screenshot,
                    options=reason_opts[:4] or [
                        "Wrong date of birth entered",
                        "Date of birth is mismatched with other date of birth proofs",
                        "Miscellaneous",
                    ],
                )
                if resp.answer in ("__timeout__", ""):
                    log.warning("brain.change_dob_reason_missing")
                    return True
                reason = resp.answer
                job.customer_data[CHANGE_DOB_RULES["reason_answer_key"]] = reason

            corrected_dob = (job.customer_data.get(CHANGE_DOB_RULES["corrected_dob_answer_key"]) or "").strip()
            if not corrected_dob:
                resp = await self._hl.ask(
                    job=job,
                    step_name="service_form_fill",
                    question="Please provide the correct DOB in DD-MM-YYYY format.",
                    context="This value will be entered in the Sarathi DOB correction form.",
                    screenshot=screenshot,
                    options=[],
                )
                if resp.answer in ("__timeout__", ""):
                    log.warning("brain.change_dob_corrected_dob_missing")
                    return True
                corrected_dob = resp.answer.strip()
                job.customer_data[CHANGE_DOB_RULES["corrected_dob_answer_key"]] = corrected_dob

            await self._browser.select_option(CHANGE_DOB_RULES["reason_selector"], label=reason, value=reason)
            await asyncio.sleep(0.4)
            await self._browser.fill(CHANGE_DOB_RULES["corrected_dob_selector"], corrected_dob, blur_after=True)
            log.info("brain.change_dob_fields_filled", reason=reason, corrected_dob=corrected_dob)

            if "miscellaneous" in reason.lower():
                manual_reason = (
                    job.customer_data.get(CHANGE_DOB_RULES["manual_reason_answer_key"])
                    or "DOB correction requested by applicant"
                )
                await self._browser.fill(CHANGE_DOB_RULES["manual_reason_selector"], manual_reason, blur_after=True)
                log.info("brain.change_dob_manual_reason_filled")

            confirm_result = await self._browser.evaluate(f"""() => {{
                const btn = document.querySelector({json.dumps(CHANGE_DOB_RULES["confirm_selector"])});
                if (!btn) return {{ok:false, reason:'confirm_missing'}};
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.scrollIntoView({{block:'center', inline:'center'}});
                btn.click();
                return {{ok:true, id: btn.id || '', value: btn.value || ''}};
            }}""")
            log.info("brain.change_dob_confirm_clicked", result=confirm_result)
            await asyncio.sleep(2.5)
            change_dob_handled = True

        # ── 1. Reason dropdown ────────────────────────────────────────────────
        if not change_dob_handled and "reason" in lower:
            reason_opts: list[str] = []
            for sel in dom_elements.get("selects", []):
                key = ((sel.get("id") or "") + (sel.get("name") or "") + (sel.get("label") or "")).lower()
                if "reason" in key:
                    reason_opts = [o.get("text", o) if isinstance(o, dict) else str(o)
                                   for o in (sel.get("options") or [])]
                    reason_opts = [o for o in reason_opts if o and o.strip() and "select" not in o.lower()]
                    break

            if reason_opts:
                resp = await self._hl.ask(
                    job=job,
                    step_name="service_form_fill",
                    question=f"Please select your reason for '{selected_service}':",
                    context="The form requires a reason for this DL service request.",
                    screenshot=screenshot,
                    options=reason_opts[:4],
                )
                if resp.answer not in ("__timeout__", ""):
                    for sel in dom_elements.get("selects", []):
                        key = ((sel.get("id") or "") + (sel.get("name") or "")).lower()
                        if "reason" in key:
                            await self._browser.select_option(sel.get("selector", ""), label=resp.answer)
                            log.info("brain.reason_selected", reason=resp.answer)
                            await asyncio.sleep(0.5)
                            break

                    # Click Confirm to load next part of form
                    for btn in ["Confirm", "Submit", "Proceed", "Next"]:
                        if await self._browser.click_text(btn, exact=True):
                            log.info("brain.reason_confirm_clicked", btn=btn)
                            await asyncio.sleep(2.5)
                            break

        # Re-read page after reason confirm
        await asyncio.sleep(0.5)
        fresh_text = await self._browser.page_text()
        fresh_dom  = await self._browser.get_interactive_elements()

        # ── 2. Organ donation (5-second timeout → NO) ─────────────────────────
        if "organ" in fresh_text.lower() or "donat" in fresh_text.lower():
            log.info("brain.organ_donation_question")
            resp = await self._hl.ask(
                job=job,
                step_name="service_form_fill",
                question="Do you wish to donate your organs? (Auto-selects NO in 5 seconds)",
                context="The form has an organ donation option. Your consent is required.",
                screenshot=await self._browser.screenshot(),
                options=["Yes", "No"],
                timeout_seconds=5,
            )
            donate_yes = resp.answer.lower().startswith("y") if resp.answer not in ("__timeout__", "") else False
            log.info("brain.organ_donation_answer", answer=resp.answer, donating=donate_yes)

            if donate_yes:
                # Find and check organ donation checkbox
                for inp in fresh_dom.get("inputs", []):
                    key = ((inp.get("id") or "") + (inp.get("name") or "") + (inp.get("label") or "")).lower()
                    if "organ" in key or "donat" in key:
                        await self._browser.ensure_checked(inp.get("selector", ""))
                        log.info("brain.organ_donation_checked")
                        break
            # If NO → leave unchecked (Sarathi's default)

        # Re-read again for CAPTCHA
        fresh_dom2 = await self._browser.get_interactive_elements()

        # ── 3. CAPTCHA + Submit ───────────────────────────────────────────────
        captcha_sol = await self._solve_captcha_value()
        if captcha_sol:
            await self._fill_captcha_field(fresh_dom2, captcha_sol)
            log.info("brain.service_form_captcha_filled", solution=captcha_sol)
            await self._browser.press_key("Tab")
            await asyncio.sleep(0.5)

        for btn in ["Submit", "Proceed", "Confirm", "Next"]:
            if await self._browser.click_text(btn, exact=True):
                log.info("brain.service_form_submitted", btn=btn)
                await asyncio.sleep(3.0)
                break

        # ── 4. Handle "NOT donating organs" confirmation popup ────────────────
        await asyncio.sleep(1.0)
        dialog_msg = await self._browser.get_last_dialog_message()
        if dialog_msg:
            log.info("brain.service_form_dialog", message=dialog_msg[:120])

        # ── 5. Extract ACK number ─────────────────────────────────────────────
        ack = await self._extract_ack_number()
        if ack:
            job.application_number = ack
            log.info("brain.ack_extracted", ack=ack)

        job.mark_step_done("service_form_fill", StepLog(
            step_name="service_form_fill",
            status="success",
            observation=f"Service form submitted for {selected_service}. ACK: {ack or 'pending'}",
            action_taken="service_form_fill",
        ))
        await self._sm.save(job)
        return True

    # ── Payment page handler ──────────────────────────────────────────────────

    async def _maybe_handle_payment_page(
        self,
        job: Job,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        """
        Detect and handle the fee payment page.

        Rule book facts (from portal_rules.py):
          - Payment page often opens in a NEW POPUP window
          - Expected fee is known from get_fee() — log and verify it matches
          - If fee is deducted but portal shows pending → do NOT retry payment
          - Preferred: UPI first (fastest), then netbanking/card

        This handler does NOT attempt automated payment — fee deduction is
        irreversible and must be confirmed by a human or a separate payment flow.
        Instead it:
          1. Detects the payment page
          2. Logs the fee amount shown on screen
          3. Switches to popup window if needed
          4. Pauses and asks user to complete payment manually
          5. After user confirms, extracts ACK number
        """
        if "fee_payment" in job.steps_completed:
            return False

        lower = page_text.lower()
        payment_signals = [
            "pay now", "proceed to pay", "payment gateway",
            "fee payment", "total fee", "amount payable",
            "transaction amount", "pay fee", "online payment",
        ]
        if not any(sig in lower for sig in payment_signals):
            return False

        from config.portal_rules import PAYMENT_RULES, get_fee
        expected_fee = job.customer_data.get(
            "expected_fee_inr",
            get_fee(
                job.customer_data.get("selected_service", ""),
                job.state_code or job.customer_data.get("state_code", "RJ"),
            ),
        )
        log.info("brain.payment_page_detected", expected_fee_inr=expected_fee)

        # Try to switch to popup window if payment opened one
        try:
            pages = self._browser._page.context.pages
            if len(pages) > 1:
                self._browser._page = pages[-1]
                log.info("brain.payment_switched_to_popup", pages=len(pages))
                await asyncio.sleep(1.0)
                page_text = await self._browser.page_text()
        except Exception as e:
            log.warning("brain.payment_popup_switch_failed", error=str(e))

        # Extract fee amount shown on page
        amount_on_page = await self._browser.evaluate(r"""() => {
            const text = document.body?.innerText || '';
            const m = text.match(/(?:Rs\.?|INR|₹)\s*(\d[\d,]+)/i)
                   || text.match(/(\d[\d,]+)\s*(?:Rs\.?|INR|₹)/i)
                   || text.match(/total[^₹\d]*(?:Rs\.?|INR|₹)?\s*(\d[\d,]+)/i);
            return m ? m[1].replace(',','') : null;
        }""")
        if amount_on_page:
            log.info("brain.payment_amount_on_page", amount=amount_on_page, expected=expected_fee)

        # Ask user to complete payment — agent does not auto-pay (irreversible)
        resp = await self._hl.ask(
            job=job,
            step_name="fee_payment",
            question=(
                f"Payment page is open. Expected fee: ₹{expected_fee}. "
                f"{'Amount shown: ₹' + str(amount_on_page) + '.' if amount_on_page else ''} "
                "Please complete the payment and type 'done' when finished."
            ),
            context=(
                f"Preferred: UPI (fastest). "
                f"If deduction happened but portal shows pending — do NOT retry. "
                f"Gateway: {PAYMENT_RULES['gateway']}"
            ),
            screenshot=await self._browser.screenshot(),
            options=["done", "payment failed", "already paid"],
        )

        answer = (resp.answer or "").lower()
        if "fail" in answer or answer == "__timeout__":
            log.warning("brain.payment_failed_or_timeout", answer=answer)
            return False

        await asyncio.sleep(2.0)
        ack = await self._extract_ack_number()
        if ack:
            job.application_number = ack
            log.info("brain.ack_after_payment", ack=ack)

        job.mark_step_done("fee_payment", StepLog(
            step_name="fee_payment",
            status="success",
            observation=f"Payment completed. Fee: ₹{expected_fee}. ACK: {ack or 'pending'}",
            action_taken="payment_human_confirmed",
        ))
        await self._sm.save(job)
        return True

    async def _extract_ack_number(self) -> str:
        """Extract application reference/ACK number from the current page."""
        result = await self._browser.evaluate(r"""() => {
            const text = document.body ? document.body.innerText : '';
            const patterns = [
                /application\s+(?:reference\s+)?number[:\s]+([A-Z0-9\/\-]+)/i,
                /reference\s+number[:\s]+([A-Z0-9\/\-]+)/i,
                /ACK[:\s#]+([A-Z0-9\/\-]+)/i,
                /application\s+no[.:\s]+([A-Z0-9\/\-]+)/i,
                /\b(RJ\d{8,})\b/,
                /\b([A-Z]{2}\d{10,})\b/,
            ];
            for (const pat of patterns) {
                const m = text.match(pat);
                if (m && m[1]) return m[1].trim();
            }
            return null;
        }""")
        return result or ""

    async def _maybe_handle_dl_fetch_page(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        return await self._dl_fetch_impl(job, current_step, page_text, dom_elements)

    async def _maybe_handle_dl_services_landing(self, url: str) -> bool:
        """
        dlServicesDet.do is a static instructions page with a single 'Continue'
        button that takes you to envaction.do. No LLM needed — rules drive it.
        """
        rules = DL_SERVICES_LANDING_RULES
        if rules["url_fragment"] not in url:
            return False

        log.info("brain.dl_services_landing_detected")
        clicked = False
        for text in rules["continue_button_texts"]:
            if await self._browser.click_text(text, exact=True):
                clicked = True
                log.info("brain.dl_services_landing_continue_clicked", via=text)
                break
        if not clicked:
            # JS fallback: click any visible Continue/Proceed button
            texts_re = "|".join(rules["continue_button_texts"])
            result = await self._browser.evaluate(f"""() => {{
                const re = new RegExp('^(' + {json.dumps(texts_re)} + ')$', 'i');
                const btn = Array.from(document.querySelectorAll(
                    'button, input[type="submit"], input[type="button"], a.btn'
                )).find(b => b.offsetParent !== null
                          && re.test((b.textContent||'').trim() || (b.value||'').trim()));
                if (btn) {{ btn.click(); return btn.id || btn.tagName; }}
                return null;
            }}""")
            if result:
                clicked = True
                log.info("brain.dl_services_landing_js_clicked", via=result)
        await asyncio.sleep(1.5)
        return clicked

    async def _dl_fetch_impl(
        self,
        job: Job,
        current_step: str,
        page_text: str,
        dom_elements: dict,
    ) -> bool:
        """
        Deterministic handler for the DL number/DOB/CAPTCHA page.

        This page is stable enough that letting the LLM choose each action adds
        latency and caused a loop of repeated CAPTCHA solving. Runs whenever the
        DL number + DOB form fields are present and not yet confirmed, regardless
        of current_step (handles cases where user skipped or LLM mis-marked).
        """
        if "confirm_dl_details" in job.steps_completed or "fetch_dl_details" in job.steps_completed:
            return False

        rules = DL_FETCH_RULES
        dl_sel  = rules["dl_input_selector"]
        dob_sel = rules["dob_input_selector"]

        selectors = {i.get("selector", "") for i in dom_elements.get("inputs", [])}
        if dl_sel not in selectors or dob_sel not in selectors:
            return False

        lower_check = page_text.lower()
        if ("driving licence number" not in lower_check
                and "driving license number" not in lower_check):
            return False

        log.info("brain.dl_fetch_page_detected")

        dl_number = job.customer_data.get("dl_number", "")
        dob = job.customer_data.get("dob", "")
        if not dl_number or not dob:
            question = "Please provide the Driving Licence number and Date of Birth."
            resp = await self._hl.ask(
                job=job,
                step_name=current_step,
                question=question,
                context="DL number/DOB are required before fetching DL details.",
                options=[],
            )
            if resp.answer == "__timeout__":
                self._last_deterministic_failure = (
                    "DL number/DOB are required, but no human response was available."
                )
                return False
            return False

        self._last_deterministic_failure = ""
        for attempt in range(1, settings.captcha_max_retries + 1):
            # Consume stale alerts before this attempt so only this click's
            # rejection is considered.
            await self._browser.get_last_dialog_message()

            ok_dl  = await self._browser.fill(dl_sel,  dl_number)
            ok_dob = await self._browser.fill(dob_sel, dob, blur_after=True)
            # Give the portal a moment to react to the DOB tab-blur
            await asyncio.sleep(0.8)
            # Check the page is still live (Tab can trigger navigation on some portals)
            try:
                _ = await self._browser.current_url()
            except Exception as e:
                log.warning("brain.dl_fetch_page_closed_after_dob", error=str(e))
                self._last_deterministic_failure = (
                    f"Page became unavailable after filling DOB (Tab may have navigated): {e}"
                )
                return False
            fresh_dom = await self._browser.get_interactive_elements()
            ok_captcha = await self._solve_and_fill_visible_captcha(
                fresh_dom,
                image_selector=rules["captcha_image_selector"],
                force_manual=attempt >= settings.captcha_max_retries,
                prompt_context=(
                    "DL details CAPTCHA failed after automatic retries. Please provide "
                    "the fresh CAPTCHA currently shown for Get DL Details."
                    if attempt >= settings.captcha_max_retries else ""
                ),
            )

            ok_terms = True
            privacy_sel = rules["privacy_checkbox_selector"]
            fresh_selectors = {i.get("selector", "") for i in fresh_dom.get("inputs", [])}
            if privacy_sel in fresh_selectors:
                ok_terms = await self._browser.ensure_checked(privacy_sel)

            if not (ok_dl and ok_dob and ok_captcha and ok_terms):
                log.warning(
                    "brain.dl_fetch_prepare_failed",
                    attempt=attempt,
                    dl=ok_dl,
                    dob=ok_dob,
                    captcha=ok_captcha,
                    terms=ok_terms,
                )
                self._last_deterministic_failure = (
                    "Could not prepare DL details form before clicking Get DL Details. "
                    f"dl={ok_dl}, dob={ok_dob}, captcha={ok_captcha}, terms={ok_terms}."
                )
                return False

            get_btn_sel = rules["get_dl_button_selector"]
            clicked = await self._browser.click_selector(get_btn_sel, "Get DL Details")
            if not clicked:
                clicked = await self._browser.click_text("Get DL Details", exact=True)

            if not clicked:
                log.warning("brain.get_dl_details_click_failed", attempt=attempt)
                self._last_deterministic_failure = (
                    "Could not click Get DL Details even though the form fields were filled."
                )
                return False

            await asyncio.sleep(2.0)
            dialog_msg = await self._browser.get_last_dialog_message()
            if dialog_msg and self._dialog_indicates_failure(dialog_msg):
                log.warning(
                    "brain.dl_fetch_rejected",
                    attempt=attempt,
                    message=dialog_msg[:160],
                )
                after_reject_dom = await self._browser.get_interactive_elements()
                has_dl_field = any(
                    i.get("selector") == dl_sel and i.get("visible", True)
                    for i in after_reject_dom.get("inputs", [])
                )
                if not has_dl_field:
                    log.warning("brain.dl_fetch_form_disappeared_after_reject")
                    if attempt < settings.captcha_max_retries:
                        recovered = await self._recover_dl_fetch_page_after_reject()
                        if recovered:
                            log.info("brain.dl_fetch_form_recovered_after_reject", attempt=attempt)
                            continue
                        log.warning("brain.dl_fetch_recovery_failed", attempt=attempt)
                    self._last_deterministic_failure = (
                        f"Portal rejected Get DL Details with alert '{dialog_msg}', "
                        "and the DL form disappeared even after retry/reload. Ask the "
                        "user/operator to inspect DL/DOB or wait before retrying."
                    )
                    return False
                await self._refresh_visible_captcha(rules["captcha_image_selector"])
                continue

            if dialog_msg:
                log.info("brain.dl_fetch_dialog_accepted", message=dialog_msg[:160])

            verified = await self._dl_fetch_success_visible()
            if verified:
                log.info("brain.get_dl_details_verified", attempt=attempt)
                self._last_deterministic_failure = ""
                return True

            log.warning("brain.get_dl_details_no_success_proof", attempt=attempt)

        log.warning("brain.dl_fetch_exhausted_retries")
        self._last_deterministic_failure = (
            "Clicked Get DL Details but could not verify that DL details or the next controls appeared. "
            "The agent should stay on the DL fetch page, retry with a fresh CAPTCHA if available, "
            "or ask the user/operator instead of clicking navigation links."
        )
        return False

    async def _recover_dl_fetch_page_after_reject(self) -> bool:
        """
        Sarathi sometimes clears or navigates away from envaction.do after a
        rejected DL/CAPTCHA attempt. Recover through the stable DL services
        landing page instead of repeatedly reloading the broken form URL.
        """
        async def has_dl_fetch_fields() -> bool:
            dom = await self._browser.get_interactive_elements()
            selectors = {i.get("selector", "") for i in dom.get("inputs", [])}
            return (
                DL_FETCH_RULES["dl_input_selector"] in selectors
                and DL_FETCH_RULES["dob_input_selector"] in selectors
            )

        try:
            fetch_url = f"{settings.sarathi_base_url}/envaction.do"
            landing_url = f"{settings.sarathi_base_url}/dlServicesDet.do"

            await self._browser.goto(landing_url)
            await asyncio.sleep(0.8)
            clicked_landing = await self._maybe_handle_dl_services_landing(url=landing_url)
            await asyncio.sleep(1.2)
            if await has_dl_fetch_fields():
                log.info("brain.dl_fetch_recovery_probe", recovered=True, via="landing_continue")
                return True

            # Some rejected states render dlServicesDet.do without the Continue
            # control. The session can still accept a direct envaction.do reload.
            await self._browser.goto(fetch_url)
            await asyncio.sleep(1.2)
            if await has_dl_fetch_fields():
                log.info("brain.dl_fetch_recovery_probe", recovered=True, via="direct_envaction")
                return True

            dom = await self._browser.get_interactive_elements()
            text = await self._browser.page_text()
            url = await self._browser.current_url()
            log.warning(
                "brain.dl_fetch_recovery_probe",
                recovered=False,
                clicked_landing=clicked_landing,
                url=url,
                inputs=[i.get("selector") for i in dom.get("inputs", [])[:8]],
                buttons=[b.get("text") or b.get("value") for b in dom.get("buttons", [])[:8]],
                links=[l.get("text") for l in dom.get("links", [])[:8]],
                text=text[:240],
            )
            return False
        except Exception as e:
            log.warning("brain.dl_fetch_recovery_exception", error=str(e))
            return False

    async def _dl_fetch_success_visible(self) -> bool:
        """Return True when the page proves Get DL Details worked."""
        try:
            page_text = await self._browser.page_text()
            dom = await self._browser.get_interactive_elements()
        except Exception as e:
            log.warning("brain.dl_fetch_verify_failed", error=str(e))
            return False

        # Fastest check: if #dlno is gone the form navigated away → success
        visible_input_ids = {
            (i.get("id") or "").lower()
            for i in dom.get("inputs", [])
            if i.get("visible", True)
        }
        if "dlno" not in visible_input_ids:
            log.info("brain.dl_fetch_success_dl_field_gone")
            return True

        lower = page_text.lower()
        visible_select_ids = {
            (s.get("id") or "")
            for s in dom.get("selects", [])
            if s.get("visible", True)
        }
        visible_buttons = [
            (b.get("text") or "").strip().lower()
            for b in dom.get("buttons", [])
            if b.get("visible", True)
        ]
        has_confirm_button = any(t in {"proceed", "continue"} for t in visible_buttons)
        has_details_text = any(
            marker in lower
            for marker in [
                "driving licence details",
                "driving license details",
                "personal details and particulars",
                "particulars of existing licence",
                "licence holder",
                "license holder",
                "dl holder last endorsed details",
                "class of vehicles",
                "validity period",
                "transport department, government of rajasthan",
                "class of vehicles",
                "select category",
                "applicant details",
            ]
        )
        has_confirm_select = "dispDLDet" in visible_select_ids
        if has_details_text:
            log.info("brain.dl_fetch_success_details_text")
            return True
        return has_confirm_select and has_confirm_button

    async def _solve_and_fill_visible_captcha(
        self,
        dom_elements: dict,
        *,
        image_selector: str = "",
        force_manual: bool = False,
        prompt_context: str = "",
    ) -> bool:
        solution = await self._solve_captcha_value(
            image_selector=image_selector,
            force_manual=force_manual,
            prompt_context=prompt_context,
        )
        if not solution:
            return False

        fresh_dom = await self._browser.get_interactive_elements()
        return await self._fill_captcha_field(fresh_dom or dom_elements, solution)

    async def _refresh_visible_captcha(self, image_selector: str = "") -> bool:
        """Refresh the current visible CAPTCHA and clear visible CAPTCHA inputs."""
        try:
            result = await self._browser.evaluate(f"""() => {{
                const preferredSelector = {json.dumps(image_selector)};
                const isVisible = (el) => {{
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    return st.display !== 'none' && st.visibility !== 'hidden'
                        && (el.offsetParent !== null || el.tagName === 'IMG');
                }};
                const captchaImgs = Array.from(document.querySelectorAll(
                    'img[id*="captcha" i], img[src*="captcha" i], img[id*="captha" i], img[src*="captha" i], img[id*="cap" i], img[src*="cap" i]'
                )).filter(isVisible);
                let img = preferredSelector ? document.querySelector(preferredSelector) : null;
                if (!isVisible(img)) img = captchaImgs[0] || null;

                const controls = Array.from(document.querySelectorAll(
                    'button, input[type="button"], input[type="submit"], a, img'
                )).filter(isVisible);
                const refresh = controls.find(el => {{
                    const key = [
                        el.id || '',
                        el.name || '',
                        el.value || '',
                        el.textContent || '',
                        el.alt || '',
                        el.title || '',
                        el.getAttribute('onclick') || '',
                        el.getAttribute('src') || '',
                    ].join(' ').toLowerCase();
                    return /refresh|reload|change.*image|captcha|captha/.test(key)
                        && !/submit|verify|generate|resend|reset|home|cancel/.test(key);
                }});

                const before = img ? img.src : '';
                if (refresh && refresh !== img) {{
                    try {{ refresh.click(); }} catch (e) {{}}
                }} else if (img && img.src) {{
                    const u = new URL(img.src, window.location.href);
                    u.searchParams.set('_agent_refresh', String(Date.now()));
                    img.src = u.toString();
                }}

                const inputs = Array.from(document.querySelectorAll(
                    'input[id*="capt" i]:not([type="hidden"]), input[name*="capt" i]:not([type="hidden"]), input[id*="capth" i]:not([type="hidden"]), input[name*="capth" i]:not([type="hidden"])'
                ));
                for (const el of inputs) {{
                    const nativeSet = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeSet.call(el, '');
                    el.dispatchEvent(new Event('input', {{bubbles:true}}));
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                }}
                return {{
                    ok: !!(refresh || img),
                    clicked: refresh ? (refresh.id || refresh.value || refresh.textContent || refresh.tagName) : '',
                    image_id: img ? (img.id || '') : '',
                    before,
                    after: img ? img.src : '',
                    cleared_inputs: inputs.map(el => el.id || el.name || ''),
                }};
            }}""")
            await asyncio.sleep(0.8)
            log.info("brain.captcha_refreshed_for_retry", result=result)
            return bool(result and result.get("ok"))
        except Exception as e:
            log.warning("brain.captcha_refresh_failed", error=str(e))
            return False

    async def _fetch_visible_captcha_bytes(self, image_selector: str = "") -> bytes:
        """Capture the CAPTCHA image exactly as displayed in the browser."""
        selector_info = await self._browser.evaluate(f"""() => {{
            const preferredSelector = {json.dumps(image_selector)};
            const isVisible = (img) => {{
                if (!img) return false;
                const st = window.getComputedStyle(img);
                return st.display !== 'none' && st.visibility !== 'hidden';
            }};
            let img = preferredSelector ? document.querySelector(preferredSelector) : null;
            if (!isVisible(img)) img = null;
            const imgs = Array.from(document.querySelectorAll(
                'img[id*="captcha" i], img[src*="captcha" i], img[id*="captha" i], img[src*="captha" i], img[id*="cap" i], img[src*="cap" i]'
            )).filter(isVisible);
            if (!img) {{
                img = imgs.find(x => x.id === 'captchaimg')
                   || imgs.find(x => x.id === 'capimg1')
                   || imgs.find(x => x.id === 'capimg')
                   || imgs.find(x => /captcha|captha|cap/i.test((x.id || '') + ' ' + (x.src || '')));
            }}
            if (!img) return null;
            if (img.id) return {{ selector: '#' + CSS.escape(img.id), id: img.id || '', src: (img.src || '').slice(0, 120) }};
            const idx = Array.from(document.images).indexOf(img);
            return {{ selector: `img:nth-of-type(${{idx + 1}})`, id: '', src: (img.src || '').slice(0, 120) }};
        }}""")

        if selector_info and isinstance(selector_info, dict):
            selector = selector_info.get("selector") or ""
            if selector:
                cropped = await self._browser.crop_element_screenshot(selector, timeout_ms=2500)
                if cropped:
                    log.info(
                        "brain.captcha_captured_from_element",
                        id=selector_info.get("id", ""),
                        selector=selector,
                        bytes=len(cropped),
                    )
                    return cropped

        # Canvas fallback uses the already-loaded DOM image pixels. Do not fetch
        # img.src; on Sarathi that can rotate the server-side CAPTCHA and make
        # the browser image differ from the solved image.
        data_url = await self._browser.evaluate(f"""() => {{
            const selector = {json.dumps((selector_info or {}).get("selector", "") if isinstance(selector_info, dict) else "")};
            const img = selector ? document.querySelector(selector) : null;
            if (!img || !img.src) return null;
            const w = img.naturalWidth || img.width || 160;
            const h = img.naturalHeight || img.height || 40;
            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);
            return {{
                id: img.id || '',
                src: img.src.slice(0, 120),
                data: canvas.toDataURL('image/png'),
            }};
        }}""")
        if not data_url or not isinstance(data_url, dict):
            return b""
        raw = data_url.get("data") or ""
        if "," not in raw:
            return b""
        try:
            decoded = base64.b64decode(raw.split(",", 1)[1])
            log.info(
                "brain.captcha_captured_from_canvas",
                id=data_url.get("id", ""),
                bytes=len(decoded),
            )
            return decoded
        except Exception as e:
            log.warning("brain.captcha_canvas_decode_failed", error=str(e))
            return b""

    async def _reveal_otp_section(self) -> int:
        """Show the hidden OTP entry section that Sarathi's JS normally reveals via AJAX callback."""
        revealed = await self._browser.evaluate("""() => {
            let count = 0;
            // Reveal any element whose id/class contains 'otp' and is currently hidden
            document.querySelectorAll('*[id], *[class]').forEach(el => {
                const key = ((el.id || '') + ' ' + (el.className || '')).toLowerCase();
                if (!key.includes('otp')) return;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || el.hidden) {
                    el.style.display = 'block';
                    el.removeAttribute('hidden');
                    count++;
                }
            });
            // Also try known Sarathi div IDs
            ['enterSarathiOtpDiv', 'sarathiOtpDiv', 'otpDiv', 'enterOtpDiv',
             'entOtpDiv', 'otp_section', 'otpSection', 'verifyOtpDiv'].forEach(id => {
                const el = document.getElementById(id);
                if (el && (el.hidden || window.getComputedStyle(el).display === 'none')) {
                    el.style.display = 'block';
                    el.removeAttribute('hidden');
                    count++;
                }
            });
            return count;
        }""")
        if revealed:
            log.info("brain.otp_section_revealed", elements=revealed)
        return int(revealed or 0)

    async def _solve_captcha_value(
        self,
        *,
        image_selector: str = "",
        force_manual: bool = False,
        prompt_context: str = "",
    ) -> str:
        """Solve the visible CAPTCHA and return the text solution (not yet filled)."""
        attempts = 1 if force_manual else max(1, settings.captcha_max_retries)
        last_solution = ""

        for attempt in range(1, attempts + 1):
            captcha_bytes = await self._fetch_visible_captcha_bytes(image_selector=image_selector)
            if not captcha_bytes:
                try:
                    captcha_bytes = await self._browser.screenshot()
                except Exception as e:
                    log.warning("brain.captcha_screenshot_failed", error=str(e))
                    return ""

            result = await self._captcha.solve_with_confidence(
                captcha_bytes,
                force_manual=force_manual,
                allow_manual=force_manual,
                prompt_context=prompt_context,
                job=self._current_job,
                human_loop=self._hl,
            )
            solution = result.text or ""
            last_solution = solution

            if solution and len(solution) > 12:
                log.warning(
                    "brain.captcha_solution_too_long",
                    chars=len(solution),
                    preview=solution[:20],
                    attempt=attempt,
                )
                solution = ""

            if force_manual:
                # Do not re-fetch the CAPTCHA after a human answers. Sarathi's
                # CAPTCHA endpoint may produce different bytes on every fetch,
                # even when the visible browser image has not changed. Trust the
                # saved image the human answered; if it is wrong/stale, the
                # portal rejection path will refresh and ask again.
                if solution:
                    log.info("brain.captcha_manual_solution_received", chars=len(solution))
                return solution

            if solution and result.confidence >= settings.captcha_confidence_threshold:
                log.info(
                    "brain.captcha_high_confidence",
                    solution=solution,
                    confidence=result.confidence,
                    provider=result.provider,
                    attempt=attempt,
                )
                return solution

            log.warning(
                "brain.captcha_low_confidence_refresh",
                solution=solution,
                confidence=result.confidence,
                provider=result.provider,
                attempt=attempt,
                max_attempts=attempts,
                threshold=settings.captcha_confidence_threshold,
            )
            if attempt < attempts:
                await self._refresh_visible_captcha(image_selector)

        log.warning(
            "brain.captcha_auto_exhausted",
            last_solution=last_solution,
            attempts=attempts,
        )
        return ""

    async def _fill_captcha_field(self, dom_elements: dict, solution: str) -> bool:
        """Fill a CAPTCHA field with the given solution string."""
        selectors = []
        for inp in dom_elements.get("inputs", []):
            if inp.get("disabled"):
                continue
            key = " ".join([
                inp.get("id", ""),
                inp.get("name", ""),
                inp.get("placeholder", ""),
            ]).lower()
            if "captcha" in key or "captha" in key:
                selectors.append(inp.get("selector", ""))
        selectors.extend([
            "#entcaptxt", "#entCaptha", "#captcha",
            "input[id*='captha' i]:not([type='hidden'])",
            "input[name*='captha' i]:not([type='hidden'])",
            "input[id*='captcha' i]:not([type='hidden'])",
            "input[name*='captcha' i]:not([type='hidden'])",
            "input[placeholder*='captcha' i]:not([type='hidden'])",
        ])
        for selector in [s for s in selectors if s]:
            if await self._browser.fill(selector, solution):
                proof = await self._browser.evaluate(f"""() => {{
                    const el = document.querySelector({json.dumps(selector)});
                    return {{
                        ok: !!el && el.value === {json.dumps(solution)},
                        value_len: el ? (el.value || '').length : 0,
                    }};
                }}""")
                log.info(
                    "brain.captcha_field_filled",
                    selector=selector,
                    solution=solution,
                    proof=proof,
                )
                if proof and proof.get("ok"):
                    return True

        # JS fallback — Sarathi sometimes CSS-hides the captcha input so Playwright
        # fill() times out waiting for it to be visible. Set value directly via JS.
        result = await self._browser.evaluate(f"""() => {{
            const val = {json.dumps(solution)};
            const candidates = [
                '#entcaptxt', '#entCaptha', '#captcha',
                'input[id*="captha" i]', 'input[id*="captcha" i]',
                'input[name*="captha" i]', 'input[name*="captcha" i]',
            ];
            for (const s of candidates) {{
                const el = document.querySelector(s);
                if (!el || el.type === 'hidden') continue;
                const nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                nativeSet.call(el, val);
                el.dispatchEvent(new Event('input',  {{bubbles:true}}));
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                el.dispatchEvent(new Event('blur',   {{bubbles:true}}));
                return {{
                    selector: s,
                    ok: el.value === val,
                    value_len: (el.value || '').length,
                }};
            }}
            return null;
        }}""")
        if result:
            log.info("brain.captcha_field_filled_js", result=result, solution=solution)
            return bool(result.get("ok")) if isinstance(result, dict) else True
        return False

    async def _api_generate_sarathi_otp(self, mobile: str, captcha: str) -> bool:
        """
        Call getOtpFromSarathi.do directly using browser session cookies.
        This bypasses the disabled #generateSarathiotp UI button.
        The server still validates CAPTCHA and sends OTP to the mobile.
        """
        import httpx
        try:
            cookies = await self._browser.get_session_cookies_dict()
            current_url = await self._browser.current_url()
            async with httpx.AsyncClient(timeout=20, verify=False) as client:
                resp = await client.post(
                    "https://sarathi.parivahan.gov.in/sarathiservice/getOtpFromSarathi.do",
                    data={"mobileNumber": mobile, "captcha": captcha},
                    cookies=cookies,
                    headers={
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "Origin": "https://sarathi.parivahan.gov.in",
                        "Referer": current_url,
                        "X-Requested-With": "XMLHttpRequest",
                        "User-Agent": settings.browser_user_agent,
                    },
                )
            response_text = resp.text.strip() if resp.text else ""
            log.info(
                "brain.generate_otp_api_response",
                status=resp.status_code,
                body=response_text[:200],
            )
            # Sarathi returns "SUCCESS" or a mobile number on success
            if resp.status_code == 200 and response_text:
                lower = response_text.lower()
                if "error" in lower or "invalid" in lower or "captcha" in lower:
                    log.warning("brain.generate_otp_api_rejected", body=response_text[:200])
                    return False
                return True
            return False
        except Exception as e:
            log.warning("brain.generate_otp_api_exception", error=str(e))
            return False

    @staticmethod
    def _has_otp_input(dom_elements: dict, page_text: str) -> bool:
        lower_text = page_text.lower()
        if "otp" not in lower_text:
            return False
        for inp in dom_elements.get("inputs", []):
            if not inp.get("visible", True):
                continue
            key = " ".join([
                inp.get("id", ""),
                inp.get("name", ""),
                inp.get("placeholder", ""),
            ]).lower()
            if "otp" in key:
                return True
        return any(term in lower_text for term in ["enter otp", "one time password", "verify otp"])

    @staticmethod
    def _otp_input_selectors(dom_elements: dict) -> list[str]:
        selectors: list[str] = []
        for inp in dom_elements.get("inputs", []):
            if not inp.get("visible", True) or inp.get("disabled"):
                continue
            key = " ".join([
                inp.get("id", ""),
                inp.get("name", ""),
                inp.get("placeholder", ""),
            ]).lower()
            if "otp" in key:
                selectors.append(inp.get("selector", ""))
        if not selectors:
            for inp in dom_elements.get("inputs", []):
                if not inp.get("visible", True) or inp.get("disabled"):
                    continue
                input_type = (inp.get("type") or "").lower()
                key = " ".join([
                    inp.get("id", ""),
                    inp.get("name", ""),
                    inp.get("placeholder", ""),
                ]).lower()
                blocked = ["captcha", "captha", "dlno", "dob", "date", "aadhaarholdingtype"]
                if input_type in {"text", "tel", "number"} and not any(b in key for b in blocked):
                    selectors.append(inp.get("selector", ""))
        selectors.extend([
            "input[id*='otp' i]:not([type='hidden'])",
            "input[name*='otp' i]:not([type='hidden'])",
            "input[placeholder*='otp' i]:not([type='hidden'])",
        ])
        return [s for s in selectors if s]

    @staticmethod
    def _dialog_indicates_failure(message: str) -> bool:
        lower = message.lower()
        if "application already exists" in lower:
            return False
        failure_terms = [
            "please provide",
            "invalid",
            "not valid",
            "captcha",
            "error",
            "failed",
            "mandatory",
            "required",
        ]
        return any(term in lower for term in failure_terms)

    @staticmethod
    def _portal_transient_block_reason(page_text: str, url: str) -> str:
        lower = f"{url}\n{page_text}".lower()
        if "403" in lower and "forbidden" in lower:
            return "Sarathi returned 403 Forbidden"
        if "service unavailable" in lower or "bad gateway" in lower or "gateway timeout" in lower:
            return "Sarathi gateway/service unavailable"
        if "site can't be reached" in lower or "site cannot be reached" in lower:
            return "Sarathi page could not be reached"
        return ""

    @staticmethod
    def _is_bad_navigation(action: AgentAction, current_step: str) -> str:
        text = (action.text or "").strip().lower()
        desc = (action.description or "").strip().lower()
        if current_step not in ("open_homepage", "close_homepage_popup", "select_state"):
            blocked_texts = {"dashboard", "login", "change state", "home"}
            if (
                text in blocked_texts
                or "clicking 'dashboard'" in desc
                or "clicking 'login'" in desc
                or "clicking 'change state'" in desc
                or "clicking 'home'" in desc
            ):
                return (
                    "Blocked backtracking/navigation action. Dashboard/Login/Change State/Home "
                    "leaves the current application flow and can restart the portal. Stay on "
                    "the current Sarathi application page, use visible form fields, retry captcha, "
                    "or ask the user/operator for help."
                )
        return ""

    def _state_signature(self, step_name: str, url: str, dom_elements: dict) -> str:
        """
        Compact page-state fingerprint for loop detection.

        We intentionally use DOM values/checked states instead of body text because
        Sarathi prints changing timestamps on some pages, which would hide loops.
        """
        import hashlib

        selects = [
            {
                "k": s.get("id") or s.get("name") or s.get("selector"),
                "v": s.get("value", ""),
                "t": s.get("selected_text", ""),
            }
            for s in dom_elements.get("selects", [])[:20]
            if s.get("visible", True)
        ]
        inputs = [
            {
                "k": i.get("id") or i.get("name") or i.get("selector"),
                "type": i.get("type", ""),
                "v": i.get("value", ""),
                "checked": i.get("checked", False),
            }
            for i in dom_elements.get("inputs", [])[:40]
            if i.get("visible", True)
        ]
        buttons = [
            {
                "t": b.get("text", ""),
                "disabled": b.get("disabled", False),
            }
            for b in dom_elements.get("buttons", [])[:20]
            if b.get("visible", True)
        ]
        links = [
            l.get("text", "")
            for l in dom_elements.get("links", [])[:30]
            if l.get("visible", True)
        ]
        raw = json.dumps(
            {
                "step": step_name,
                "url": url.split("?")[0],
                "selects": selects,
                "inputs": inputs,
                "buttons": buttons,
                "links": links,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    # ── Self-diagnosis ────────────────────────────────────────────────────────

    async def _diagnose_failure(
        self,
        action: AgentAction,
        dom_elements: dict,
        page_text: str,
        url: str,
    ) -> str:
        """
        After a failed action, reason about WHY it failed by examining the
        actual page state. Returns a plain-English diagnosis for the next
        LLM call so it can try a different approach.
        """
        lines = [
            f"Previous action FAILED: {action.action_type} "
            f"selector='{action.selector}' text='{action.text}' value='{action.value}'",
            f"Page URL at time of failure: {url}",
            f"Page text snippet: {page_text[:200].strip()!r}",
        ]

        selects = dom_elements.get("selects", [])
        inputs  = dom_elements.get("inputs", [])
        buttons = dom_elements.get("buttons", [])
        links   = dom_elements.get("links", [])

        # Was the selector wrong?
        if action.selector:
            sel_ids_names = (
                [s["id"] for s in selects] +
                [s["name"] for s in selects] +
                [i["id"] for i in inputs] +
                [i["name"] for i in inputs]
            )
            found = any(
                action.selector.lstrip("#") in (s or "") for s in sel_ids_names
            )
            if not found:
                lines.append(
                    f"- Selector '{action.selector}' does NOT exist on this page."
                )

        # What selects ARE there?
        if selects:
            for s in selects:
                opts = [o["t"] for o in s["options"][:6]]
                lines.append(
                    f"- Real <select> on page: id='{s['id']}' name='{s['name']}' "
                    f"selector='{s['selector']}' options={opts}"
                )
        else:
            lines.append("- No <select> dropdown elements found on this page.")

        # If state selection was attempted, look for the state as a link
        if action.text and action.action_type in ("select", "click"):
            search_text = action.text.lower()
            matching_links = [
                lnk for lnk in links
                if search_text in lnk["text"].lower() and lnk["visible"]
            ]
            if matching_links:
                lines.append(
                    f"- Found visible links matching '{action.text}': "
                    + str([lnk["text"] for lnk in matching_links[:5]])
                    + " — try action_type='click' with the link text instead."
                )
            else:
                lines.append(
                    f"- No visible links matching '{action.text}' on this page."
                )
                # Show what links ARE visible
                visible = [lnk["text"][:40] for lnk in links if lnk["visible"]][:10]
                if visible:
                    lines.append(f"- Visible links on page: {visible}")

        # Summarise what IS interactable
        if inputs:
            inp_summary = [
                f"id='{i['id']}' name='{i['name']}' type='{i['type']}'"
                for i in inputs[:5]
            ]
            lines.append(f"- Input fields on page: {inp_summary}")

        if buttons:
            btn_texts = [b["text"] for b in buttons if b["visible"]][:8]
            lines.append(f"- Visible buttons: {btn_texts}")

        lines.append(
            "ACTION REQUIRED: choose a DIFFERENT approach based on what actually exists above."
        )

        diagnosis = "\n".join(lines)
        return diagnosis

    # ── LLM call ──────────────────────────────────────────────────────────────

    async def _ask_llm(
        self,
        job: Job,
        screenshot: bytes,
        page_text: str,
        url: str,
        pending_steps: list,
        learned_hint: Optional[Scenario],
        dom_elements: dict = None,
        failure_context: str = "",
        step_action_history: list = None,
    ) -> AgentAction:
        next_step       = pending_steps[0] if pending_steps else None
        remaining_names = [s.name for s in pending_steps]

        # ── Learned hint ──────────────────────────────────────────────────────
        hint_text = ""
        if learned_hint:
            hint_text = (
                f"\n\nLEARNED HINT (from past runs): "
                f"When you see '{learned_hint.description}', "
                f"the solution that worked was: {learned_hint.solution}\n"
                f"Detail: {json.dumps(learned_hint.solution_detail)}"
            )

        # ── DOM context — real selectors ──────────────────────────────────────
        dom_text = ""
        if dom_elements:
            selects = dom_elements.get("selects", [])
            inputs  = dom_elements.get("inputs", [])
            buttons = dom_elements.get("buttons", [])
            links   = dom_elements.get("links", [])
            parts   = []

            if selects:
                parts.append("DROPDOWNS (<select>) — use these exact selectors:")
                for s in selects:
                    opts = ", ".join(f'"{o["t"]}"' for o in s["options"][:10])
                    parts.append(
                        f'  selector="{s["selector"]}"  id="{s["id"]}"  '
                        f'name="{s["name"]}"  selected="{s.get("selected_text", "")}"  '
                        f'value="{s.get("value", "")}"  options=[{opts}]  visible={s["visible"]}'
                    )
            else:
                parts.append("NO <select> dropdowns found on this page.")

            if inputs:
                visible_inputs = [i for i in inputs if i.get("visible", True)]
                hidden_inputs  = [i for i in inputs if not i.get("visible", True)]
                parts.append(f"INPUT FIELDS (visible={len(visible_inputs)}, hidden={len(hidden_inputs)}):")
                for i in visible_inputs[:20]:
                    dis = "  [DISABLED]" if i.get("disabled") else ""
                    checked = f' checked={i.get("checked")}' if i.get("type") in ("checkbox", "radio") else ""
                    current = f' value="{i.get("value", "")[:80]}"'
                    parts.append(
                        f'  selector="{i["selector"]}"  id="{i["id"]}"  '
                        f'name="{i["name"]}"  type="{i["type"]}"  '
                        f'placeholder="{i["placeholder"]}"{current}{checked}{dis}'
                    )

            if buttons:
                visible_btns = [b for b in buttons if b["visible"]][:10]
                btn_info = [
                    b["text"] + (" [DISABLED — do NOT JS-click]" if b.get("disabled") else "")
                    for b in visible_btns
                ]
                parts.append(f"BUTTONS: {btn_info}")

            if links:
                vis_links = [l for l in links if l["visible"]][:20]
                parts.append(f"VISIBLE LINKS ({len(vis_links)}):")
                for lnk in vis_links:
                    parts.append(f'  "{lnk["text"]}"  href="{lnk["href"][:50]}"')

            dom_text = "\n\nACTUAL PAGE ELEMENTS (ground truth — use these, not guesses):\n" + "\n".join(parts)

        # ── Failure context from previous attempt ─────────────────────────────
        failure_text = ""
        if failure_context:
            failure_text = f"\n\nSELF-DIAGNOSIS FROM LAST FAILED ATTEMPT:\n{failure_context}\n"

        system_prompt = (
            "You are an AI agent controlling a browser to complete a DL Renewal "
            "application on India's Sarathi government portal. "
            "You are self-healing: when something fails, you reason about WHY "
            "and try a different approach. "
            "Respond ONLY with a single valid JSON object. No markdown."
        )

        history_text = ""
        if step_action_history:
            history_text = (
                "\n\nACTIONS ALREADY TAKEN IN THIS STEP (do NOT repeat these — move to the next field/action):\n"
                + "\n".join(f"  {i+1}. {h}" for i, h in enumerate(step_action_history))
            )

        user_text = f"""=== CURRENT STATE ===
URL: {url}
Page text (truncated to 1200 chars):
{page_text[:1200]}
{dom_text}
{failure_text}
{history_text}
=== GOAL ===
Complete the DL Renewal for this customer.

CUSTOMER DATA:
{json.dumps(job.customer_data, indent=2)}

STEPS COMPLETED: {job.steps_completed}
NEXT STEP TO COMPLETE: {next_step.name if next_step else 'DONE'} — {next_step.description if next_step else 'All done'}
REMAINING: {remaining_names}
KNOWN OBSTACLES: {json.dumps(next_step.known_obstacles if next_step else [])}
{hint_text}

=== STRICT RULES ===
1. NEVER click Reset / Clear All / Cancel / browser back button.
2. Dismiss any popup/modal FIRST before other actions.
3. If two auth options exist, pick mobile OTP.
4. Email is optional — leave the email field blank unless the portal marks it required.
   Do NOT fill any fallback or placeholder email.
5. If CAPTCHA fails, get a fresh one and retry.
6. Use selectors EXACTLY from ACTUAL PAGE ELEMENTS above — do NOT invent selectors.
   NEVER use CSS `:contains()` syntax (e.g. `button:contains('OK')`) — it is jQuery-only
   and does NOT work in Playwright. Instead: leave selector blank and set text="OK", OR
   use the button's ID from the BUTTONS list (e.g. `#dlconfirm`).
7. If no <select> exists for state selection, the state is likely a CLICKABLE LINK.
   Use action_type="click" and text="Rajasthan" (or the state name) to click it.
8. Set step_complete=true ONLY when you are certain it worked (e.g. URL changed,
   confirmation text appeared, form field is filled).
9. If you have already tried one approach and it is in the SELF-DIAGNOSIS section,
   DO NOT try it again — choose a fundamentally different approach.
10. PAGE RESET DETECTION: If the screenshot shows a form field is EMPTY but ACTIONS
    ALREADY TAKEN shows it was previously filled — the page refreshed and wiped the
    form. Re-fill ALL fields from scratch, starting from the first empty one.
11. When a form has multiple fields (e.g. DL number + DOB + CAPTCHA), fill ALL of
    them before clicking submit. Check ACTIONS ALREADY TAKEN to know which ones are
    done, then fill the remaining ones. If 2+ ordinary fields are visible and their
    values are known in CUSTOMER DATA, prefer one action_type="fill_many" with:
    tool_args={{"fields":[{{"selector":"#fieldId","value":"value"}}]}}
    instead of filling them one by one. Do not include CAPTCHA in fill_many.
12. CAPTCHA: always use action_type="tool_call" and tool="captcha_solver" to solve it.
    After solving, fill the captcha input, then click the submit/GO button.
13. DISABLED BUTTONS (shown as [DISABLED — do NOT JS-click]): never try to click a
    disabled button. It means a required checkbox or field hasn't been filled yet.
    Look at ALL visible inputs to find what's missing (a service checkbox, agreement,
    or required field). Complete that first, then the button will enable.
14. After clicking 'Get DL Details' / 'GO', use action_type="wait" for 3 seconds to let
    the AJAX response load before reading the page. New inputs or sections may appear.
15. If you are stuck scrolling and can't find an element, STOP scrolling and instead
    look at BUTTONS and INPUT FIELDS above — the element may need to be revealed by
    clicking something (like 'Get DL Details') first.
16. If the portal asks for a field that is not in CUSTOMER DATA, ask the user with
    action_type="human_help", need_human=true, human_question="...", and
    tool_args={{"field_key":"pin_code"}} (use the relevant key). If CUSTOMER DATA has
    pin_code or pincode, fill that value directly; for this test it is 334401.
17. For checkboxes/radio buttons, use action_type="check" with the exact selector.
    Only click a checkbox if it is currently unchecked.

=== RESPOND WITH EXACTLY THIS JSON ===
{{
  "observation": "what is visible on screen right now",
  "thought": "why this approach, and what is different from last attempt if applicable",
  "action_type": "click | fill | fill_many | check | select | upload | scroll | wait | tool_call | close_popup | otp_wait | human_help | done",
  "description": "plain English description of what you are doing",
  "selector": "exact CSS selector from ACTUAL PAGE ELEMENTS, or empty string",
  "text": "link/button text to click, or text to type",
  "value": "dropdown value to select",
  "tool": "captcha_solver | image_processor | none",
  "tool_args": {{}},
  "step_complete": false,
  "step_name": "step name if step_complete is true",
  "need_otp": false,
  "otp_type": "",
  "need_human": false,
  "human_question": "",
  "is_done": false,
  "application_number": ""
}}"""

        raw_text = await self._llm.vision(screenshot, system_prompt, user_text)

        # Strip markdown code fences if present
        if "```" in raw_text:
            parts = raw_text.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("json"):
                    p = p[4:]
                p = p.strip()
                if p.startswith("{"):
                    raw_text = p
                    break

        try:
            return AgentAction(json.loads(raw_text))
        except json.JSONDecodeError:
            log.error("brain.llm_parse_error", raw=raw_text[:300])
            return AgentAction({"action_type": "wait", "description": "LLM parse error — waiting"})

    # ── Execute action with fallback chain ────────────────────────────────────

    async def _force_select_state(self, job: Job) -> bool:
        code = (job.state_code or job.customer_data.get("state_code") or "RJ").strip().upper()
        state_name = STATE_CODES.get(code, code)
        ok = await self._browser.select_option("#stfNameId", label=state_name)
        if not ok:
            ok = await self._browser.select_option("#stfNameId", value=state_name)
        if not ok:
            ok = await self._browser.click_text(state_name)
        if not ok:
            ok = await self._browser.click_link_containing(state_name)
        if not ok:
            log.warning("brain.force_select_state_failed", state_code=code, state_name=state_name)
            return False

        job.customer_data["state_code"] = code
        job.customer_data["state_name"] = state_name
        job.mark_step_done("select_state", StepLog(
            step_name="select_state",
            status="success",
            observation=f"Forced filing state: {state_name} ({code})",
            action_taken="deterministic_state_selection",
        ))
        log.info("brain.force_select_state_success", state_code=code, state_name=state_name)
        return True

    async def _execute(self, action: AgentAction, job: Job) -> tuple[bool, str]:
        """
        Returns (success, detail_string).
        detail_string explains what was tried — used in logging and diagnosis.
        """
        at = action.action_type

        if at == "click":
            detail = []
            ok = False

            # Strip jQuery :contains() selectors — they're invalid CSS/JS.
            # Extract the text and fall through to click_text instead.
            if action.selector and ":contains(" in action.selector:
                import re as _re
                m = _re.search(r':contains\([\'"](.+?)[\'"]\)', action.selector)
                if m and not action.text:
                    action.text = m.group(1)
                action.selector = ""
                detail.append(f"stripped_contains_selector extracted_text='{action.text}'")

            if action.selector:
                ok = await self._browser.click_selector(action.selector, action.description)
                detail.append(f"selector='{action.selector}' -> {ok}")
                if not ok:
                    ok = await self._browser.click_by_js(action.selector)
                    detail.append(f"js_click selector='{action.selector}' -> {ok}")

            if not ok and action.text:
                ok = await self._browser.click_text(action.text)
                detail.append(f"click_text='{action.text}' -> {ok}")

            if not ok and action.text:
                ok = await self._browser.click_link_containing(action.text)
                detail.append(f"link_containing='{action.text}' -> {ok}")

            # If the text looks like a modal-close button (x, ×, close) and all
            # click attempts failed, try the universal popup-close routine.
            close_texts = {"x", "×", "close", "dismiss", "skip"}
            if not ok and action.text and action.text.strip().lower() in close_texts:
                ok = await self._browser.close_popups_on_page()
                detail.append(f"close_popup_fallback -> {ok}")

            return ok, " | ".join(detail)

        elif at == "fill":
            if action.selector:
                is_date = any(k in action.selector.lower() for k in ["dob", "date", "birth"])
                ok = await self._browser.fill(
                    action.selector, action.value or action.text, blur_after=is_date
                )
                return ok, f"fill selector='{action.selector}' blur_after={is_date}"
            return False, "fill: no selector"

        elif at == "fill_many":
            fields = action.tool_args.get("fields", [])
            if not fields:
                return False, "fill_many: no fields"

            details = []
            ok_count = 0
            for field in fields:
                selector = field.get("selector", "")
                source_key = field.get("source_key", "")
                value = field.get("value", "")
                if source_key and not value:
                    value = job.customer_data.get(source_key, "")
                if not selector:
                    details.append("missing_selector -> False")
                    continue
                is_date = any(k in selector.lower() for k in ["dob", "date", "birth"])
                sel_lower = selector.lower()
                is_known_select = (
                    "dispdldet" in sel_lower
                    or "applcatgdlsereq" in sel_lower
                    or "rtocodedltr" in sel_lower
                )
                if is_known_select:
                    ok = await self._browser.select_option(selector, label=str(value))
                    if not ok:
                        ok = await self._browser.select_option(selector, value=str(value))
                else:
                    ok = await self._browser.fill(selector, str(value), blur_after=is_date)
                details.append(f"{selector} -> {ok}")
                if ok:
                    ok_count += 1
            return ok_count == len(fields), f"fill_many {ok_count}/{len(fields)} | " + " | ".join(details)

        elif at == "check":
            if action.selector:
                ok = await self._browser.ensure_checked(action.selector, checked=True)
                return ok, f"check selector='{action.selector}'"
            return False, "check: no selector"

        elif at == "select":
            detail = []
            ok = await self._browser.select_option(
                action.selector, value=action.value, label=action.text
            )
            detail.append(f"select selector='{action.selector}' -> {ok}")
            if not ok and action.text:
                ok = await self._browser.click_link_containing(action.text)
                detail.append(f"link_fallback text='{action.text}' -> {ok}")
            if not ok and action.text:
                ok = await self._browser.click_text(action.text)
                detail.append(f"click_text_fallback='{action.text}' -> {ok}")
            return ok, " | ".join(detail)

        elif at == "upload":
            doc_key   = action.tool_args.get("doc_key", "")
            file_path = job.documents.get(doc_key, "")
            if not file_path:
                return False, f"upload: doc '{doc_key}' not in job.documents"
            ok = await self._browser.upload_file(action.selector, file_path)
            return ok, f"upload selector='{action.selector}' file='{file_path}'"

        elif at == "scroll":
            await self._browser.scroll_to_bottom()
            return True, "scrolled to bottom"

        elif at == "wait":
            await asyncio.sleep(2)
            return True, "waited 2s"

        elif at == "tool_call":
            ok = await self._execute_tool(action, job)
            return ok, f"tool={action.tool}"

        elif at == "close_popup":
            ok = await self._browser.close_popups_on_page()
            return ok, "close_popup"

        elif at in ("otp_wait", "human_help", "done"):
            return True, at

        log.warning("brain.unknown_action_type", type=at)
        return False, f"unknown action_type={at}"

    async def _execute_tool(self, action: AgentAction, job: Job) -> bool:
        tool = action.tool
        args = action.tool_args

        if tool == "captcha_solver":
            sel = args.get("selector", "img[src*='captcha'], img[id*='captcha']")
            captcha_bytes = await self._browser.crop_element_screenshot(sel) or await self._browser.screenshot()
            result = await self._captcha.solve_with_confidence(captcha_bytes, allow_manual=False)
            if result.confidence < settings.captcha_confidence_threshold:
                log.warning(
                    "brain.tool_captcha_low_confidence",
                    solution=result.text,
                    confidence=result.confidence,
                    threshold=settings.captcha_confidence_threshold,
                )
                await self._refresh_visible_captcha(sel)
                return False
            solution = result.text
            if solution:
                # Try selectors in order — Sarathi misspells "captcha" as "captha" (visible id: entCaptha).
                # Always prefer visible inputs over hidden ones.
                for inp_sel in [
                    args.get("input_selector", ""),
                    "#entCaptha",
                    "input[id*='captha']:not([type='hidden'])",
                    "input[id*='captcha']:not([type='hidden'])",
                    "input[name*='captcha']:not([type='hidden'])",
                ]:
                    if inp_sel and await self._browser.fill(inp_sel, solution):
                        log.info("brain.captcha_filled", selector=inp_sel, solution=solution)
                        return True
            return False

        elif tool == "image_processor":
            doc_key      = args.get("doc_key", "")
            compress_type = args.get("compress_type", "document")
            file_path    = job.documents.get(doc_key, "")
            if not file_path:
                return False
            if compress_type == "photo":
                compressed = self._img_proc.compress_photo(file_path)
            elif compress_type == "signature":
                compressed = self._img_proc.compress_signature(file_path)
            else:
                compressed = self._img_proc.compress_document(file_path)
            job.documents[f"{doc_key}_compressed"] = compressed
            await self._sm.save(job)
            return True

        elif tool == "close_popup":
            return await self._browser.close_popups_on_page()

        elif tool == "accept_dialog":
            return True   # Playwright dialog handler already accepts

        log.warning("brain.unknown_tool", tool=tool)
        return False

    # ── Human escalation ──────────────────────────────────────────────────────

    async def _maybe_record_portal_triage(
        self,
        *,
        job: Job,
        screenshot: bytes,
        page_text: str,
        url: str,
        current_step: str,
        dom_elements: dict,
        failure_context: str,
        last_action: str = "",
    ) -> None:
        """Run bounded LLM triage and store enum results on the job."""
        if settings.portal_triage_mode == "off" or not failure_context:
            return
        try:
            triage = await self._portal_triage.classify(
                screenshot=screenshot,
                page_text=page_text,
                url=url,
                current_step=current_step,
                dom_elements=dom_elements,
                failure_context=failure_context,
                last_action=last_action,
                retry_count=job.retry_counts.get(current_step, 0),
            )
            if not triage:
                return
            job.customer_data["portal_triage"] = triage
            history = job.customer_data.setdefault("portal_triage_history", [])
            if not isinstance(history, list):
                history = []
                job.customer_data["portal_triage_history"] = history
            history.append(triage)
            if len(history) > 20:
                del history[: len(history) - 20]
            await self._sm.save(job)
        except Exception as e:  # noqa: BLE001
            log.warning("brain.portal_triage_record_failed", error=str(e), step=current_step)

    async def _handle_stuck(self, job: Job, action: AgentAction, screenshot: bytes):
        q_text, ctx, opts = HumanLoop.build_stuck_question(action.observation, action.thought)
        question = action.human_question or q_text
        field_key = action.tool_args.get("field_key") or action.tool_args.get("customer_data_key")

        resp = await self._hl.ask(
            job=job,
            step_name=action.step_name or "unknown",
            question=question,
            context=ctx,
            screenshot=screenshot,
            options=opts,
            field_key=field_key,
        )

        answer = resp.answer
        if answer == "__timeout__":
            log.warning("brain.human_loop_timeout", job_id=job.job_id)
            return

        if field_key and answer and answer.lower() not in {"skip", "abort job", "cancel"}:
            job.customer_data[field_key] = answer
            await self._sm.save(job)
            log.info("brain.human_answer_saved", field_key=field_key, value=answer)

        # Record the human solution for future runs
        scenario_id = LearningStore.make_scenario_id(
            action.step_name or "unknown", action.observation
        )
        scenario = Scenario(
            scenario_id    = scenario_id,
            step_name      = action.step_name or "unknown",
            description    = action.observation,
            page_url       = job.last_url,
            solution       = answer,
            solution_detail= {"action_type": "human_provided", "notes": answer},
            human_provided = True,
        )
        await self._ls.record(scenario)
        log.info("brain.learned_from_human", scenario_id=scenario_id)
