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
import structlog
from typing import Optional

from config.settings import get_settings
from agent.llm_client import get_llm_client
from agent.state_manager import Job, JobStatus, StateManager, StepLog, FORBIDDEN_ACTIONS
from agent.learning_store import LearningStore, Scenario
from agent.human_loop import HumanLoop
from browser.controller import BrowserController
from tools.captcha_solver import CaptchaSolver
from tools.otp_relay import OTPRelay
from tools.image_processor import ImageProcessor
from flows.dl_renewal import DL_RENEWAL_STEPS, steps_after

log = structlog.get_logger(__name__)
settings = get_settings()

SARATHI_HOME = f"{settings.sarathi_base_url}/sarathiHomePublic.do"


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
        self._llm      = get_llm_client()

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self, job: Job) -> Job:
        await self._sm.transition(job, JobStatus.AGENT_RUNNING)
        log.info("brain.run_started", job_id=job.job_id, service=job.service)

        step_count   = 0
        step_failures: dict[str, int] = {}   # step_name -> consecutive fail count
        failure_context = ""                  # diagnosis from last failed action

        while step_count < settings.max_steps_per_job:
            step_count += 1

            screenshot   = await self._browser.screenshot()
            url          = await self._browser.current_url()
            page_text    = await self._browser.page_text()
            dom_elements = await self._browser.get_interactive_elements()

            log.info(
                "brain.observe",
                step=step_count,
                url=url[:80],
                selects=len(dom_elements.get("selects", [])),
                inputs=len(dom_elements.get("inputs", [])),
                links=len(dom_elements.get("links", [])),
            )

            # ── Build pending steps ────────────────────────────────────────────
            pending = steps_after(job.steps_completed)
            next_step = pending[0] if pending else None

            if not pending:
                log.info("brain.all_steps_done", job_id=job.job_id)
                break

            current_step = next_step.name if next_step else "unknown"
            fails        = step_failures.get(current_step, 0)

            # ── Escalate to human after too many self-healing attempts ─────────
            if fails >= settings.max_consecutive_step_failures:
                log.warning(
                    "brain.escalating_to_human",
                    step=current_step,
                    consecutive_fails=fails,
                    url=url,
                    last_diagnosis=failure_context[:200],
                )
                resp = await self._hl.ask(
                    job=job,
                    step_name=current_step,
                    question=(
                        f"Agent is stuck on '{current_step}' after {fails} attempts.\n"
                        f"URL: {url}\n"
                        f"Diagnosis: {failure_context}\n\n"
                        f"What should it try?"
                    ),
                    context=f"Page text: {page_text[:400]}",
                    screenshot=screenshot,
                    options=["Retry fresh", "Skip this step", "Abort job"],
                )
                answer = (resp.answer or "").lower()
                if "skip" in answer:
                    job.mark_step_done(current_step, StepLog(
                        step_name=current_step,
                        status="skipped",
                        observation="Skipped by human after repeated failures",
                        action_taken="human_skip",
                    ))
                    await self._sm.save(job)
                    step_failures.pop(current_step, None)
                    failure_context = ""
                    log.info("brain.step_skipped_by_human", step=current_step)
                elif "abort" in answer:
                    log.info("brain.aborted_by_human", step=current_step)
                    break
                else:
                    step_failures[current_step] = 0
                    failure_context = ""
                    # Record human hint in learning store
                    if resp.answer and len(resp.answer) > 5:
                        await self._ls.record(Scenario(
                            scenario_id=LearningStore.make_scenario_id(current_step, failure_context),
                            step_name=current_step,
                            description=failure_context[:200],
                            page_url=url,
                            solution=resp.answer,
                            solution_detail={"source": "human_escalation"},
                            human_provided=True,
                        ))
                continue

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
            )

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

            # ── If action failed: self-diagnose and prepare next iteration ─────
            if not success and action.action_type not in ("wait", "scroll"):
                step_failures[current_step] = fails + 1
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
                    diagnosis=failure_context,
                )
                # Record failure in learning store so future runs avoid this
                await self._ls.record_failure(
                    step_name=current_step,
                    observation=failure_context,
                    page_url=url,
                    failed_approach=f"{action.action_type} selector={action.selector} text={action.text}",
                )
            else:
                # Successful action — reset failure state
                if success:
                    step_failures.pop(current_step, None)
                    failure_context = ""

            # ── Handle special signals ─────────────────────────────────────────
            if action.is_done:
                if action.application_number:
                    job.application_number = action.application_number
                log.info("brain.completed", job_id=job.job_id, app_no=job.application_number)
                break

            if action.need_otp:
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

        await self._sm.transition(
            job,
            JobStatus.SUBMITTED if job.application_number else JobStatus.FAILED,
        )
        return job

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
                        f'name="{s["name"]}"  options=[{opts}]  visible={s["visible"]}'
                    )
            else:
                parts.append("NO <select> dropdowns found on this page.")

            if inputs:
                parts.append("INPUT FIELDS:")
                for i in inputs[:8]:
                    parts.append(
                        f'  selector="{i["selector"]}"  id="{i["id"]}"  '
                        f'name="{i["name"]}"  type="{i["type"]}"  '
                        f'placeholder="{i["placeholder"]}"  visible={i["visible"]}'
                    )

            if buttons:
                visible_btns = [b for b in buttons if b["visible"]][:10]
                parts.append(f"BUTTONS: {[b['text'] for b in visible_btns]}")

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

        user_text = f"""=== CURRENT STATE ===
URL: {url}
Page text (truncated to 1200 chars):
{page_text[:1200]}
{dom_text}
{failure_text}
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
4. Email is optional — skip if not in customer data.
5. If CAPTCHA fails, get a fresh one and retry.
6. Use selectors EXACTLY from ACTUAL PAGE ELEMENTS above — do NOT invent selectors.
7. If no <select> exists for state selection, the state is likely a CLICKABLE LINK.
   Use action_type="click" and text="Rajasthan" (or the state name) to click it.
8. Set step_complete=true ONLY when you are certain it worked (e.g. URL changed,
   confirmation text appeared, form field is filled).
9. If you have already tried one approach and it is in the SELF-DIAGNOSIS section,
   DO NOT try it again — choose a fundamentally different approach.

=== RESPOND WITH EXACTLY THIS JSON ===
{{
  "observation": "what is visible on screen right now",
  "thought": "why this approach, and what is different from last attempt if applicable",
  "action_type": "click | fill | select | upload | scroll | wait | tool_call | close_popup | otp_wait | human_help | done",
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

    async def _execute(self, action: AgentAction, job: Job) -> tuple[bool, str]:
        """
        Returns (success, detail_string).
        detail_string explains what was tried — used in logging and diagnosis.
        """
        at = action.action_type

        if at == "click":
            detail = []
            ok = False

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

            return ok, " | ".join(detail)

        elif at == "fill":
            if action.selector:
                ok = await self._browser.fill(action.selector, action.value or action.text)
                return ok, f"fill selector='{action.selector}'"
            return False, "fill: no selector"

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
            solution = await self._captcha.solve(captcha_bytes)
            if solution:
                inp = args.get("input_selector", "input[name*='captcha'], input[id*='captcha']")
                return await self._browser.fill(inp, solution)
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

    async def _handle_stuck(self, job: Job, action: AgentAction, screenshot: bytes):
        q_text, ctx, opts = HumanLoop.build_stuck_question(action.observation, action.thought)
        question = action.human_question or q_text

        resp = await self._hl.ask(
            job=job,
            step_name=action.step_name or "unknown",
            question=question,
            context=ctx,
            screenshot=screenshot,
            options=opts,
        )

        answer = resp.answer
        if answer == "__timeout__":
            log.warning("brain.human_loop_timeout", job_id=job.job_id)
            return

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
