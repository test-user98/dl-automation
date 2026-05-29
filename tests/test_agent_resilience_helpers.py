"""Fast regression tests for agent resilience decision helpers.

These do not drive the live Sarathi portal. They lock the small deterministic
decisions that prevent the browser agent from looping, backtracking, or asking
the customer the wrong thing when OTP/CAPTCHA pages behave oddly.
"""

from agent.brain import AgentAction, AgentBrain
import config.portal_rules as portal_rules


def _brain():
    return object.__new__(AgentBrain)


def test_otp_submit_failure_classification():
    brain = _brain()

    assert brain._classify_otp_submit_failure("Please enter valid captcha") == "captcha_rejected"
    assert brain._classify_otp_submit_failure("OTP has expired, resend OTP") == "otp_expired"
    assert brain._classify_otp_submit_failure("Invalid OTP entered") == "otp_invalid"
    assert brain._classify_otp_submit_failure("Still waiting on same page") == "unknown"


def test_otp_input_detection_requires_visible_otp_context():
    dom = {
        "inputs": [
            {"id": "entcaptxt1", "name": "entcaptxt1", "placeholder": "Captcha", "visible": True},
            {"id": "otpNumberSarathi", "name": "otpNumberSarathi", "placeholder": "", "visible": True},
        ]
    }
    assert AgentBrain._has_otp_input(dom, "Validate OTP")

    hidden_only = {"inputs": [{"id": "otpNumber", "name": "otpNumber", "visible": False}]}
    assert not AgentBrain._has_otp_input(hidden_only, "Validate OTP")


def test_otp_input_selector_candidates_exclude_captcha_and_dob():
    dom = {
        "inputs": [
            {"selector": "#entcaptxt1", "id": "entcaptxt1", "name": "entcaptxt1", "type": "text", "visible": True},
            {"selector": "#dob", "id": "dob", "name": "dob", "type": "text", "visible": True},
            {"selector": "#otpNumberSarathi", "id": "otpNumberSarathi", "name": "otpNumberSarathi", "type": "text", "visible": True},
        ]
    }

    selectors = AgentBrain._otp_input_selectors(dom)

    assert selectors[0] == "#otpNumberSarathi"
    assert "#entcaptxt1" not in selectors[:1]
    assert "#dob" not in selectors


def test_bad_navigation_guard_blocks_flow_reset_mid_application():
    action = AgentAction({"action_type": "click", "text": "Dashboard", "description": "Clicking 'Dashboard'"})

    assert AgentBrain._is_bad_navigation(action, "fill_personal_details")
    assert AgentBrain._is_bad_navigation(action, "open_homepage") == ""


def test_dialog_failure_detection_for_portal_errors():
    assert AgentBrain._dialog_indicates_failure("Please enter valid captcha")
    assert AgentBrain._dialog_indicates_failure("Invalid OTP")
    assert not AgentBrain._dialog_indicates_failure("OTP sent successfully")


def test_state_signature_ignores_query_string_and_tracks_form_values():
    brain = _brain()
    dom = {
        "inputs": [
            {"id": "otpNumber", "type": "text", "value": "123456", "checked": False, "visible": True},
            {"id": "otpCheckbox", "type": "checkbox", "value": "true", "checked": True, "visible": True},
        ],
        "buttons": [{"text": "Submit OTP", "disabled": False, "visible": True}],
        "selects": [],
        "links": [],
    }

    sig1 = brain._state_signature("mobile_otp_verification", "https://x/envaction.do?a=1", dom)
    sig2 = brain._state_signature("mobile_otp_verification", "https://x/envaction.do?a=2", dom)
    assert sig1 == sig2

    changed = {**dom, "inputs": [{**dom["inputs"][0], "value": "654321"}, dom["inputs"][1]]}
    assert brain._state_signature("mobile_otp_verification", "https://x/envaction.do", changed) != sig1


def test_rule_discovery_persists_overlay_and_applies_live(tmp_path, monkeypatch):
    overlay_path = tmp_path / "discovered_rules.json"
    monkeypatch.setattr(portal_rules, "_OVERLAY_PATH", overlay_path)

    original = portal_rules.VERIFY_OTP_RULES["submit_button_selector"]
    try:
        portal_rules.record_discovery("VERIFY_OTP_RULES", "submit_button_selector", "#verifyOtpNew")

        assert portal_rules.VERIFY_OTP_RULES["submit_button_selector"] == "#verifyOtpNew"
        assert '"submit_button_selector": "#verifyOtpNew"' in overlay_path.read_text(encoding="utf-8")
    finally:
        portal_rules.VERIFY_OTP_RULES["submit_button_selector"] = original
