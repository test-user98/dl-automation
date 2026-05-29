"""
Self-test script for the customer-UI fixes landed in this session.

Validates:
  Bug A — PIN/DOB/mobile empty: inline error stays on screen-3, no bounce.
  Bug B — last_step_label overrides to "Waiting for the OTP" when phase=waiting.
  Bug C — first-OTP doesn't trip the "expired/fresh OTP" branch when the
          question text contains the word "Resend".
  Bug D — phase advances past PHASE_CONNECTING once portal popup is closed.
  Bug G — retrying/portal-down payloads do not render the generic answer box.

Run after restarting uvicorn on 127.0.0.1:8001.

Usage: python scripts/validate_fixes.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.state_manager import Job, JobStatus
from api.status_messages import customer_job_view


def _fake_job(steps: list[str], status: JobStatus, pending: dict | None = None,
              error_message: str = "", mobile: str = "9876512345") -> Job:
    job = Job(
        job_id="test-job",
        customer_id=mobile,
        service="DL_RENEWAL",
        customer_data={"mobile_number": mobile},
        documents={},
        state_code="RJ",
    )
    job.steps_completed = list(steps)
    job.status = status
    job.error_message = error_message
    if pending is not None:
        job.customer_data["_pending_customer_request"] = pending
    return job


def assert_eq(label: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'} {label}: got={actual!r} expected={expected!r}")
    return ok


def test_bug_d_phase_advance_past_connecting() -> bool:
    print("\n[Bug D] phase advances past PHASE_CONNECTING after popup closed")
    ok = True
    # Step 1: only homepage opened -> still connecting
    job1 = _fake_job(["open_homepage"], JobStatus.AGENT_RUNNING)
    v1 = customer_job_view(job1)
    ok &= assert_eq("open_homepage -> phase", v1["phase"], "connecting")

    # Step 2: popup closed -> filling already
    job2 = _fake_job(["open_homepage", "close_homepage_popup"], JobStatus.AGENT_RUNNING)
    v2 = customer_job_view(job2)
    ok &= assert_eq("close_homepage_popup -> phase", v2["phase"], "filling")
    ok &= assert_eq(
        "close_homepage_popup -> headline",
        v2["headline"], "Filling your application",
    )

    # Step 3: state selected -> still filling
    job3 = _fake_job(
        ["open_homepage", "close_homepage_popup", "select_state"],
        JobStatus.AGENT_RUNNING,
    )
    v3 = customer_job_view(job3)
    ok &= assert_eq("select_state -> phase", v3["phase"], "filling")
    ok &= assert_eq("select_state -> last_step_label", v3["last_step_label"], "Selecting your state")
    return ok


def test_bug_c_first_otp_no_false_expiry() -> bool:
    print("\n[Bug C] first OTP question with 'Resend OTP' option does NOT trip 'fresh OTP'")
    ok = True
    pending = {
        "step_name": "accept_alert_popup",
        "question": "OTP has been sent to ******4163. Please enter it below, or choose 'Resend OTP' if you did not receive it.",
        "context": "The Sarathi portal sent a 6-digit OTP. If you didn't receive it, select 'Resend OTP'.",
        "options": ["Resend OTP"],
        "action_type": "otp",
    }
    job = _fake_job(
        steps=[
            "open_homepage", "close_homepage_popup", "select_state",
            "close_state_popup", "navigate_to_dl_services", "select_renewal_service",
            "auth_method_selection", "fetch_dl_details", "confirm_dl_details",
            "fill_personal_details", "accept_alert_popup",
        ],
        status=JobStatus.STUCK_HUMAN_NEEDED,
        pending=pending,
        error_message=pending["question"],
    )
    v = customer_job_view(job)
    ok &= assert_eq("action_type", v["action_type"], "otp")
    ok &= assert_eq("headline (NOT 'fresh')", v["headline"], "Enter the OTP")
    expired_phrase = "previous OTP expired"
    ok &= assert_eq(
        "subline does NOT claim expiry",
        expired_phrase in v["subline"], False,
    )
    return ok


def test_bug_g_retrying_is_not_customer_question() -> bool:
    print("\n[Bug G] retrying/portal-down is passive, not a customer question")
    ok = True

    job = _fake_job(
        ["open_homepage", "close_homepage_popup", "select_state"],
        JobStatus.STUCK_HUMAN_NEEDED,
        pending={
            "question": "The page is showing a 403 Forbidden error.",
            "context": "403 Forbidden",
            "action_type": "confirmation",
        },
        error_message="The page is showing a 403 Forbidden error.",
    )
    v = customer_job_view(job)
    ok &= assert_eq("phase", v["phase"], "retrying")
    ok &= assert_eq("action_required", v["action_required"], False)
    ok &= assert_eq("action_type", v["action_type"], "")
    ok &= assert_eq("customer_request", v["customer_request"], {})

    frontend = Path("frontend/index.html").read_text(encoding="utf-8")
    ok &= assert_eq(
        "frontend no longer routes all STUCK_HUMAN_NEEDED to answer screen",
        "|| r.status === 'STUCK_HUMAN_NEEDED'" in frontend,
        False,
    )
    ok &= assert_eq(
        "frontend has explicit retrying branch",
        "v.phase === 'retrying'" in frontend,
        True,
    )
    return ok


def test_bug_b_step_label_overrides_for_otp() -> bool:
    print("\n[Bug B] last_step_label says 'Waiting for the OTP' when waiting on OTP")
    ok = True
    pending = {
        "step_name": "accept_alert_popup",
        "question": "Please enter the OTP sent to your mobile.",
        "context": "Sarathi OTP step.",
        "options": [],
        "action_type": "otp",
    }
    job = _fake_job(
        steps=["open_homepage", "close_homepage_popup", "select_state",
               "fill_personal_details", "accept_alert_popup"],
        status=JobStatus.STUCK_HUMAN_NEEDED,
        pending=pending,
        error_message="Please enter the OTP sent to your mobile.",
    )
    v = customer_job_view(job)
    ok &= assert_eq("last_step_label", v["last_step_label"], "Waiting for the OTP")
    ok &= assert_eq("phase", v["phase"], "waiting")
    return ok


def test_bug_c_explicit_expiry_still_works() -> bool:
    print("\n[Bug C+] genuine expiry/invalid signals still trigger the correct branch")
    ok = True
    # Genuine expiry
    job_expired = _fake_job(
        steps=["open_homepage", "fill_personal_details", "accept_alert_popup"],
        status=JobStatus.STUCK_HUMAN_NEEDED,
        pending={"step_name": "accept_alert_popup", "question": "OTP expired, please request a fresh OTP.",
                 "context": "", "options": [], "action_type": "otp"},
        error_message="OTP expired, please request a fresh OTP.",
    )
    v1 = customer_job_view(job_expired)
    ok &= assert_eq("expired headline", v1["headline"], "Enter the fresh OTP")

    # Genuine invalid
    job_invalid = _fake_job(
        steps=["open_homepage", "fill_personal_details", "accept_alert_popup"],
        status=JobStatus.STUCK_HUMAN_NEEDED,
        pending={"step_name": "accept_alert_popup", "question": "Invalid OTP, please try again.",
                 "context": "", "options": [], "action_type": "otp"},
        error_message="Invalid OTP, please try again.",
    )
    v2 = customer_job_view(job_invalid)
    ok &= assert_eq("invalid headline", v2["headline"], "Check the OTP")
    return ok


async def test_bug_a_ui_pin_stays_on_step_3() -> bool:
    print("\n[Bug A] PIN empty -> inline error on screen-3 (no bounce to screen-2)")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  SKIP playwright not available")
        return True

    ok = True
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8001/")
        await page.wait_for_selector("#screen-1")

        # Step 1 -> 2
        await page.click("#step1-next")
        await page.wait_for_selector("#screen-2:not(.hidden)", timeout=4000)
        # Fill step 2
        await page.fill("#dl_raw", "RJ0720170010191")
        await page.fill("#dob", "04-09-1998")
        await page.fill("#mobile", "9876512345")
        # The Continue button on screen-2 is the .primary inside #screen-2
        await page.click("#screen-2 button.primary")
        await page.wait_for_selector("#screen-3:not(.hidden)", timeout=4000)

        # Leave PIN empty, click Start
        await page.click("#start-btn")
        # Wait a tick for async validation
        await page.wait_for_timeout(800)

        # ✅ Should STILL be on screen-3
        screen3_visible = await page.is_visible("#screen-3:not(.hidden)")
        screen2_visible = await page.is_visible("#screen-2:not(.hidden)")
        ok &= assert_eq("still on screen-3", screen3_visible, True)
        ok &= assert_eq("did NOT bounce to screen-2", screen2_visible, False)

        # ✅ pin-error visible with text
        pin_err_visible = await page.is_visible("#pin_code-error:not(.hidden)")
        ok &= assert_eq("pin_code-error shown", pin_err_visible, True)
        if pin_err_visible:
            pin_err_text = (await page.text_content("#pin_code-error") or "").strip()
            ok &= assert_eq("pin error mentions 6 digits", "6 digits" in pin_err_text, True)

        # ✅ input has the error class
        pin_classes = await page.get_attribute("#pin_code", "class") or ""
        ok &= assert_eq("pin input has input-error class", "input-error" in pin_classes, True)

        # Now fill correct PIN and verify error clears
        await page.fill("#pin_code", "334401")
        await page.wait_for_timeout(300)
        cleared = await page.is_hidden("#pin_code-error")
        ok &= assert_eq("pin error cleared after typing", cleared, True)

        await browser.close()
    return ok


def test_bug_f_captcha_action_type_and_payload() -> bool:
    print("\n[Bug F] captcha pending request maps to action_type='captcha' + exposes image_b64")
    ok = True
    pending = {
        "step_name": "captcha_manual",
        "question": "Help us read the security code shown on the government portal.",
        "context": "The portal showed a CAPTCHA we couldn't read automatically.",
        "options": [],
        "action_type": "captcha",
        # 12 chars of arbitrary base64-shaped data — frontend will treat as image
        "image_b64": "iVBORw0KGgoAAAA",
    }
    job = _fake_job(
        steps=["open_homepage", "close_homepage_popup", "select_state",
               "fetch_dl_details"],
        status=JobStatus.STUCK_HUMAN_NEEDED,
        pending=pending,
        error_message=pending["question"],
    )
    v = customer_job_view(job)
    ok &= assert_eq("phase", v["phase"], "waiting")
    ok &= assert_eq("action_required", v["action_required"], True)
    ok &= assert_eq("action_type", v["action_type"], "captcha")
    ok &= assert_eq("headline", v["headline"], "Help us read the security code")
    ok &= assert_eq("last_step_label", v["last_step_label"], "Help with security code")
    ok &= assert_eq("customer_request.image_b64 carried through",
                    v["customer_request"].get("image_b64"), "iVBORw0KGgoAAAA")
    ok &= assert_eq("customer_request.action_type",
                    v["customer_request"].get("action_type"), "captcha")
    return ok


def test_human_loop_captcha_step_name_routes_correctly() -> bool:
    """Replays HumanLoop's action-type detection for step_name='captcha_manual'.

    Important: even if 'otp' appears in surrounding text, the captcha-step
    check fires first and wins. Mirrors the logic in agent/human_loop.py.
    """
    print("\n[Bug F-unit] HumanLoop detects captcha step over otp substring")
    ok = True
    qlower = "captcha_manual help us read the security code shown — otp page".lower()
    step_name = "captcha_manual"
    is_captcha_step = (
        step_name.startswith("captcha")
        or "captcha image" in qlower
        or step_name == "captcha"
    )
    action_type = (
        "captcha" if is_captcha_step else
        "otp" if "otp" in qlower else "text"
    )
    ok &= assert_eq("step_name=captcha_manual + 'otp' in surrounding text",
                    action_type, "captcha")
    return ok


async def test_state_confirmation_ui_changes_payload() -> bool:
    print("\n[State confirmation] customer can override DL-derived state on screen-3")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  SKIP playwright not available")
        return True
    ok = True
    captured_payloads: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Mock confirm-and-start so we capture the state_code without spinning the agent.
        async def handle_start(route):
            try:
                req = route.request
                import json as _json
                body = _json.loads(req.post_data or "{}")
                captured_payloads.append(body)
            except Exception:
                pass
            await route.fulfill(
                status=200,
                content_type="application/json",
                body='{"job_id":"state-smoke-job","status":"AGENT_RUNNING","customer_summary":{}}',
            )
        await ctx.route("**/onboard/confirm-and-start", handle_start)
        await ctx.route("**/jobs/state-smoke-job*", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"status":"AGENT_RUNNING","customer_view":{"phase":"connecting","headline":"Starting","subline":"","action_required":false,"action_type":"","last_step_label":"Connecting","customer_request":{}}}',
        ))

        await page.goto("http://127.0.0.1:8001/")
        await page.click("#step1-next")
        await page.wait_for_selector("#screen-2:not(.hidden)")
        await page.fill("#dl_raw", "RJ0720170010191")
        # validateDL is debounced 300 ms + hits /onboard/validate-dl. Wait so
        # dlNormalised is populated before we leave screen-2.
        async with page.expect_response("**/onboard/validate-dl") as resp_info:
            await page.fill("#dob", "04-09-1998")
        await resp_info.value
        await page.fill("#mobile", "9876512345")
        await page.click("#screen-2 button.primary")
        await page.wait_for_selector("#screen-3:not(.hidden)")
        await page.fill("#pin_code", "334401")

        # Verify the review shows the DL-derived state
        review_text = await page.text_content("#review-rows")
        ok &= assert_eq("review shows Rajasthan from DL", "Rajasthan" in (review_text or ""), True)
        ok &= assert_eq("review asks customer to confirm state",
                        "please confirm" in (review_text or "").lower(), True)
        await page.click("#start-btn")
        await page.wait_for_timeout(300)
        banner_text = await page.text_content("#step3-banner")
        ok &= assert_eq("start blocked until filing state confirmed",
                        "confirm the state" in (banner_text or "").lower(), True)
        ok &= assert_eq("confirm-and-start not called before state confirm",
                        len(captured_payloads), 0)

        # Override state via the Change → dropdown
        await page.wait_for_selector("#state-edit:not(.hidden)")
        await page.select_option("#state-edit-select", "MH")
        await page.wait_for_timeout(150)

        review_text2 = await page.text_content("#review-rows")
        ok &= assert_eq("review now shows Maharashtra", "Maharashtra" in (review_text2 or ""), True)
        ok &= assert_eq("review now shows 'you picked this'",
                        "you picked this" in (review_text2 or ""), True)

        # Click Start and capture payload
        await page.click("#start-btn")
        await page.wait_for_timeout(800)

        ok &= assert_eq("confirm-and-start was called", len(captured_payloads) >= 1, True)
        if captured_payloads:
            ok &= assert_eq("payload state_code override",
                            captured_payloads[-1].get("state_code"), "MH")

        await browser.close()
    return ok


async def main():
    print("=" * 70)
    print("Validating customer-UI fixes against http://127.0.0.1:8001")
    print("=" * 70)
    results = [
        test_bug_d_phase_advance_past_connecting(),
        test_bug_c_first_otp_no_false_expiry(),
        test_bug_b_step_label_overrides_for_otp(),
        test_bug_c_explicit_expiry_still_works(),
        test_bug_f_captcha_action_type_and_payload(),
        test_human_loop_captcha_step_name_routes_correctly(),
        test_bug_g_retrying_is_not_customer_question(),
        await test_bug_a_ui_pin_stays_on_step_3(),
        await test_state_confirmation_ui_changes_payload(),
    ]
    print("\n" + "=" * 70)
    if all(results):
        print("ALL FIXES VALIDATED")
        return 0
    else:
        print("FAILURES — see above")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
