"""
Sarathi Portal Rule Book — hardcoded facts that never need LLM/runtime discovery.

These are known portal behaviors, fee structures, and deterministic rules.
All handlers check this first before attempting anything dynamic.

Self-evolving overlay:
  At import time, this module loads `data/discovered_rules.json` and overlays
  any keys onto the rule dicts below. When a deterministic handler's selector
  stops working and the LLM/scoring logic finds a NEW working selector, the
  agent calls `record_discovery()` to persist it. Next run picks it up
  automatically. Human can review the JSON and merge proven entries back into
  this Python file.
"""

import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

# ── Fee structure by state + service ─────────────────────────────────────────
# Amounts in INR. Source: Sarathi/MoRTH fee schedule.
# Marked UNKNOWN where the exact fee varies by RTO sub-category.

DL_SERVICE_FEES: dict[str, dict[str, int]] = {
    "DL_RENEWAL": {
        "RJ": 200,   # Rajasthan — non-transport LMV/MCWG
        "DL": 200,   # Delhi
        "MH": 200,   # Maharashtra
        "KA": 200,   # Karnataka
        "TN": 200,   # Tamil Nadu
        "UP": 200,   # Uttar Pradesh
        "GJ": 200,   # Gujarat
        "HR": 200,   # Haryana
        "WB": 200,   # West Bengal
        "default": 200,
    },
    "DL_EXTRACT": {
        "RJ": 50,
        "default": 50,
    },
    "DUPLICATE_DL": {
        "RJ": 300,
        "default": 300,
    },
    "CHANGE_OF_ADDRESS": {
        "default": 200,
    },
    "ADDITION_OF_CLASS": {
        "default": 500,
    },
}


def get_fee(service: str, state_code: str) -> int:
    """Return the expected fee in INR for a service in a state."""
    service_map = {
        "renewal of driving licence": "DL_RENEWAL",
        "extract of driving licence": "DL_EXTRACT",
        "duplicate driving licence":  "DUPLICATE_DL",
        "change of address":          "CHANGE_OF_ADDRESS",
        "addition of class":          "ADDITION_OF_CLASS",
    }
    key = service_map.get(service.lower().strip(), service.upper().replace(" ", "_"))
    fees = DL_SERVICE_FEES.get(key, {})
    return fees.get(state_code.upper(), fees.get("default", 200))


# ── Payment page rules ────────────────────────────────────────────────────────

PAYMENT_RULES = {
    # Sarathi payment page opens in a NEW POPUP WINDOW — agent must switch context
    "opens_in_popup": True,

    # Preferred payment methods in order (fastest / most reliable)
    "preferred_methods": ["UPI", "Net Banking", "Debit Card", "Credit Card"],

    # If payment is deducted but portal shows "pending" — do NOT retry payment
    "on_pending_after_deduction": "wait_and_reload",

    # Payment gateway used by Sarathi
    "gateway": "SBI ePay / PayGov",

    # Typical payment confirmation text patterns
    "success_patterns": [
        "payment successful",
        "transaction successful",
        "payment received",
        "your payment of",
        "receipt no",
        "acknowledgement",
    ],
    "failure_patterns": [
        "payment failed",
        "transaction failed",
        "payment declined",
        "insufficient funds",
        "try again",
    ],
}


# ── Known disabled/hidden inputs — always use JS fill ─────────────────────────

JS_FILL_REQUIRED_SELECTORS = {
    "#otpNumber",       # OTP input — Sarathi disables until EnableDisableOtp() runs
    "#otpNumberSarathi",
    "#entCaptha",       # CAPTCHA input — CSS-hidden (display:none), not type=hidden
    "#entcaptxt",       # Alternate CAPTCHA field — also CSS-hidden in some flows
    "#entcaptxt1",
}

# ── Generate OTP rules ────────────────────────────────────────────────────────

GENERATE_OTP_RULES = {
    # Click the UI button first — the API call works but bypasses Sarathi's AJAX
    # callback that activates the OTP entry section + reveals Submit OTP button.
    "primary_method": "ui_click",
    "api_endpoint": "/sarathiservice/getOtpFromSarathi.do",
    "button_selector": "#generateSarathiotp",
    "button_onclick_fn": "gensarathiOTP",       # Sarathi JS function on the button
    "captcha_image_selector": "#capimg",
    "captcha_input_selector": "#entcaptxt",
    "resend_button_selector": "#generateResendSarathiotp",
    "resend_button_onclick_fn": "genResendsarathiOTP",
    # After click, Sarathi's AJAX reveals the OTP entry section automatically.
    "reveal_otp_section_after_api": True,
    "known_otp_div_ids": [
        "enterSarathiOtpDiv", "sarathiOtpDiv", "otpDiv", "enterOtpDiv",
        "entOtpDiv", "otp_section", "otpSection", "verifyOtpDiv",
    ],
}

# ── Verify OTP (Submit OTP) rules — the trap that loops the agent ────────────
#
# Sarathi keeps the Submit OTP button `disabled="true"` until its keyup listener
# sees a 6-digit OTP + non-empty captcha. Setting `value` via JS does NOT fire
# that listener. Clicking after removeAttribute('disabled') runs onclick but the
# handler reads internal state and silently aborts.
#
# The fix that works: call `verifiedBySarathi()` directly via window['fn']() —
# bypasses the disabled state entirely. Fill OTP with per-character keydown +
# input + keyup events (with proper keyCode) so the listener sees real typing.
VERIFY_OTP_RULES = {
    "otp_input_selector":      "#otpNumberSarathi",
    "otp_input_selectors":     ["#otpNumberSarathi", "#otpNumber"],
    "captcha_image_selector":  "#capimg1",
    "captcha_image_selectors": ["#capimg1", "#capimg"],
    "captcha_input_selector":  "#entcaptxt1",
    "captcha_input_selectors": ["#entcaptxt1", "#entcaptxt"],
    "submit_button_selector":  "#verifySarathi",
    "submit_onclick_fn":       "verifiedBySarathi",
    # The auth-method Submit at the top — NEVER click this for OTP verification.
    "forbidden_submit_selector": "#submt",
    # Known function names tried as fallback if onclick can't be parsed
    "known_verify_fns": [
        "verifiedBySarathi", "verifySarathiOtp", "verifySarathiOTP",
        "verifyOtp", "verifyOTP", "validateOtp", "validateOTP",
        "sarathiOtpVerify", "submitOtp", "sarathiotpverify",
    ],
    # Must fire per-character keystroke events — Sarathi reads keyCode/which.
    "fill_strategy": "per_character_keystrokes",
    # Sarathi shows OTP error via JS alert (auto-dismissed by our handler)
    "rejection_dialog_patterns": [
        "invalid otp", "otp expired", "wrong otp", "incorrect otp",
        "otp mismatch", "please enter valid otp", "enter valid otp",
        "otp verification failed", "otp time", "otp timeout",
        "otp timed out", "otp has expired", "otp is expired",
    ],
    "otp_expired_patterns": [
        "otp expired", "otp has expired", "otp is expired", "otp time",
        "otp timeout", "otp timed out", "time expired", "session expired",
        "otp validity", "validity expired", "resend otp",
    ],
    "otp_invalid_patterns": [
        "invalid otp", "wrong otp", "incorrect otp", "otp mismatch",
        "please enter valid otp", "enter valid otp", "otp verification failed",
    ],
    "captcha_rejection_patterns": [
        "invalid captcha", "captcha mismatch", "wrong captcha", "captcha not match",
        "captcha failed", "captcha verification failed", "captcha invalid",
        "please enter valid captcha", "enter valid captcha", "security code",
    ],
    # Recovery policy: do not ask for OTP again for CAPTCHA failures. Refresh
    # CAPTCHA and retry with the same OTP; if the OTP itself expired, click
    # Resend OTP and ask for the fresh code.
    "max_same_otp_submit_attempts": 3,
    "on_captcha_reject": "keep_otp_refresh_captcha",
    "on_otp_expired": "resend_then_ask_new_otp",
    "on_otp_invalid": "ask_new_otp_without_resend",
}

# ── DL fetch page (envaction.do — DL number + DOB + captcha) ─────────────────

DL_FETCH_RULES = {
    "dl_input_selector":        "#dlno",
    "dob_input_selector":       "#dob",
    "captcha_image_selector":   "#captchaimg",
    "captcha_input_selector":   "#entCaptha",
    "privacy_checkbox_selector":"#PrivacyPolicyTermsofService",
    "get_dl_button_selector":   "#GetDLDetails",
    "proceed_button_selector":  "#dlconfirm",
    "rejection_dialog_patterns": [
        "valid driving licence", "valid dl", "invalid captcha", "captcha",
    ],
}

# ── DL confirm page (PIN code → RTO auto-fill) ───────────────────────────────

DL_CONFIRM_RULES = {
    "yes_select_selector":   "#dispDLDet",
    "yes_label":             "YES",
    "category_selector":     "#applcatgDLserReq",
    "category_default":      "General",
    "pin_input_selectors": [
        "input[placeholder*='pin' i]:not([type='hidden'])",
        "input[placeholder*='Pin' i]:not([type='hidden'])",
        "#pinCodeDLTr", "#pinCode", "#rtoPinCode",
        "input[name='pinCodeDLTr']", "input[name='pinCode']",
    ],
    "proceed_button_selector": "#dlconfirm",
    # Pin code → AJAX → RTO auto-fill takes ~1.5s
    "rto_autofill_wait_ms": 1500,
    "existing_app_dialog_pattern": "application already exists",
}

# ── DL services landing page (dlServicesDet.do) ──────────────────────────────
#
# Static instructions page. Only one action needed: click Continue → envaction.do
DL_SERVICES_LANDING_RULES = {
    "url_fragment": "dlServicesDet.do",
    "continue_button_texts": ["Continue", "Proceed", "Next"],
    "navigates_to": "envaction.do",
}

SERVICE_SELECTION_RULES = {
    "manual_answer_file": "data/manual_service.txt",
    "default_test_service": "CHANGE OF DATE OF BIRTH IN DL",
    "proceed_selector": "#trsaction_enve_proceed",
    "service_input_name": "dlc",
    "aliases": {
        "dob update": "CHANGE OF DATE OF BIRTH IN DL",
        "date of birth update": "CHANGE OF DATE OF BIRTH IN DL",
        "change dob": "CHANGE OF DATE OF BIRTH IN DL",
        "change of dob": "CHANGE OF DATE OF BIRTH IN DL",
        "change of date of birth": "CHANGE OF DATE OF BIRTH IN DL",
        "change of date of birth in dl": "CHANGE OF DATE OF BIRTH IN DL",
        "address change": "CHANGE OF ADDRESS IN DL",
        "change of address": "CHANGE OF ADDRESS IN DL",
        "name change": "CHANGE OF NAME IN DL",
        "change of name": "CHANGE OF NAME IN DL",
    },
}

SERVICE_REJECTION_RULES = {
    "rto_service_ineligible_patterns": [
        "unable to process your data",
        "holder requested service",
        "requested service:",
        "is not legible for requested rto",
        "kindly visit the rto/rla authority",
    ],
    "customer_title": "This service is not available at your RTO",
    "customer_message": (
        "Sarathi says the selected DL service is not available for the RTO linked "
        "to this licence. Choose another available service or visit the RTO/RLA "
        "authority for this request."
    ),
}

# ── DL central repository unavailable (terminal business rule) ───────────────
#
# Sarathi reports that a DL is not present in the national/central repository,
# so the licence cannot be processed online — the customer must visit the
# issuing RTO/RLA. This is the single source of truth for the phrases both the
# agent (raw portal dialog) and the status layer (customer copy) match against,
# plus the canonical marker the agent writes into Job.error_message so the
# status layer can recognise the reason without re-parsing portal prose.
DL_CENTRAL_REPO_UNAVAILABLE_RULES = {
    "dialog_patterns": [
        "details of given dl number not available",
        "not available in the central repository",
        "licence data not available in central repository",
        "license data not available in central repository",
    ],
    "error_marker": "dl central record unavailable",
    "customer_title": "DL record not available online",
    "customer_message": (
        "Sarathi could not find this DL in its online records. Online application "
        "cannot continue for this licence; please contact the issuing RTO/RLA authority."
    ),
    "agent_error_message": (
        "DL central record unavailable: Sarathi says this licence is not available "
        "for online applications and requires RTO/RLA handling."
    ),
}


def text_indicates_dl_central_repo_unavailable(text: str) -> bool:
    """True for a raw Sarathi dialog/page OR our own canonical error marker.

    Used by both the agent (matching raw portal text) and the status layer
    (matching Job.error_message prose), so the phrase list never drifts.
    """
    lower = (text or "").lower()
    if DL_CENTRAL_REPO_UNAVAILABLE_RULES["error_marker"] in lower:
        return True
    if any(p in lower for p in DL_CENTRAL_REPO_UNAVAILABLE_RULES["dialog_patterns"]):
        return True
    return "central repository" in lower and ("rto / rla" in lower or "rto/rla" in lower)


# ── Terminal job reasons (one source of truth for agent + status layer) ──────
#
# When the agent stops a job for good it stamps job.customer_data
# ["portal_terminal_reason"] with one of these keys. The status layer reads the
# *key* (not re-parsed prose) to render the customer message — so the agent's
# decision and the customer's copy can never drift. `retryable` here means the
# customer may sensibly start over later (e.g. portal was down), not that the
# closed job auto-retries.
TERMINAL_REASONS = {
    "dl_not_in_central_repository": {
        "title": DL_CENTRAL_REPO_UNAVAILABLE_RULES["customer_title"],
        "message": DL_CENTRAL_REPO_UNAVAILABLE_RULES["customer_message"],
        "error_message": DL_CENTRAL_REPO_UNAVAILABLE_RULES["agent_error_message"],
        "retryable": False,
    },
    "service_unavailable_for_rto": {
        "title": SERVICE_REJECTION_RULES["customer_title"],
        "message": SERVICE_REJECTION_RULES["customer_message"],
        "error_message": "Service unavailable for RTO: " + SERVICE_REJECTION_RULES["customer_message"],
        "retryable": False,
    },
    "dl_not_found": {
        "title": "We couldn't find your driving licence",
        "message": (
            "Sarathi could not fetch this driving licence with the number and date of "
            "birth provided. Please double-check the DL number and try again. If the "
            "details are correct and it still can't be found, the licence may not be "
            "available for online services — please contact your RTO/RLA authority."
        ),
        "error_message": (
            "DL not found: Sarathi could not fetch the licence for the DL number/DOB "
            "provided, and the customer did not supply a usable correction."
        ),
        "retryable": True,
    },
    "portal_unavailable": {
        "title": "The government portal isn't responding",
        "message": (
            "We tried several times but the government Sarathi portal did not respond. "
            "Your details are saved — please try again in a little while."
        ),
        "error_message": "Portal unavailable: Sarathi did not respond after the maximum retries.",
        "retryable": True,
    },
}


def terminal_reason_view(reason: str) -> dict | None:
    """Return the (title, message, retryable) view for a terminal reason key."""
    return TERMINAL_REASONS.get((reason or "").strip().lower())


CHANGE_DOB_RULES = {
    "service_value": "CHANGE OF DATE OF BIRTH IN DL",
    "reason_selector": "#codreasoncd",
    "manual_reason_selector": "#codreasondesc",
    "corrected_dob_selector": "#coddob",
    "confirm_selector": "#codconfirm",
    "reason_answer_key": "dob_change_reason",
    "corrected_dob_answer_key": "corrected_dob",
    "manual_reason_answer_key": "dob_change_manual_reason",
}

# ── Portal navigation rules ───────────────────────────────────────────────────

NAVIGATION_RULES = {
    # Never use browser back — Sarathi session breaks
    "never_use_back": True,
    # Never click these in the middle of an application flow
    "never_click": ["Dashboard", "Login", "Change State", "Reset", "Clear All"],
    # After CAPTCHA rejection the form is cleared — re-fill ALL fields from scratch
    "form_clears_on_captcha_rejection": True,
    # DL Renewal eligibility
    "renewal_max_days_before_expiry": 365,
}

# ── Organ donation default ─────────────────────────────────────────────────────

ORGAN_DONATION_DEFAULT = "NO"   # default if user doesn't respond within timeout
ORGAN_DONATION_TIMEOUT_SECONDS = 5

# ── Step → expected page URL fragment mapping ─────────────────────────────────

STEP_URL_HINTS = {
    "open_homepage":         "sarathiHomePublic.do",
    "select_state":          "stateSelectBean.do",
    "navigate_to_dl_services": "dlServicesDet.do",
    "fetch_dl_details":      "envaction.do",
    "confirm_dl_details":    "envaction.do",
    "auth_method_selection": "envaction.do",
    "mobile_otp_verification": "envaction.do",
    "service_selection":     "envaction.do",
    "service_form_fill":     "envaction.do",
    "fee_payment":           "payment",     # payment gateway — different domain
    "download_acknowledgment": "envaction.do",
}


# ── Self-evolving overlay (discovered rules persisted across runs) ───────────
#
# `data/discovered_rules.json` is a JSON file the agent writes when it discovers
# a working selector/function that differs from this file. At import time, the
# overlay is merged on top of the rule dicts above. To promote a discovery
# permanently, copy the entry from the JSON into the matching dict in this file.

_OVERLAY_PATH = Path(__file__).resolve().parent.parent / "data" / "discovered_rules.json"

# Names of rule dicts that may be overlaid / written to
_OVERLAY_TARGETS = {
    "VERIFY_OTP_RULES":         VERIFY_OTP_RULES,
    "GENERATE_OTP_RULES":       GENERATE_OTP_RULES,
    "DL_FETCH_RULES":           DL_FETCH_RULES,
    "DL_CONFIRM_RULES":         DL_CONFIRM_RULES,
    "DL_SERVICES_LANDING_RULES":DL_SERVICES_LANDING_RULES,
    "SERVICE_SELECTION_RULES":  SERVICE_SELECTION_RULES,
    "SERVICE_REJECTION_RULES":  SERVICE_REJECTION_RULES,
    "CHANGE_DOB_RULES":         CHANGE_DOB_RULES,
    "NAVIGATION_RULES":         NAVIGATION_RULES,
    "PAYMENT_RULES":            PAYMENT_RULES,
}


def _load_overlay() -> dict:
    if not _OVERLAY_PATH.exists():
        return {}
    try:
        return json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        _log.warning("portal_rules.overlay_load_failed error=%s", e)
        return {}


def _apply_overlay() -> None:
    """At import: merge overlay JSON onto the rule dicts."""
    overlay = _load_overlay()
    for rule_name, updates in overlay.items():
        target = _OVERLAY_TARGETS.get(rule_name)
        if isinstance(target, dict) and isinstance(updates, dict):
            target.update(updates)
            _log.info("portal_rules.overlay_applied rule=%s keys=%s",
                      rule_name, list(updates.keys()))


def record_discovery(rule_name: str, key: str, value) -> None:
    """
    Persist a discovered rule update to data/discovered_rules.json AND apply
    it live so the rest of this run benefits immediately.

    Called by deterministic handlers when the rule-book value didn't work but a
    different value did (e.g. Sarathi changed a button ID).
    """
    if rule_name not in _OVERLAY_TARGETS:
        _log.warning("portal_rules.unknown_rule_name name=%s", rule_name)
        return

    overlay = _load_overlay()
    bucket = overlay.setdefault(rule_name, {})
    # Skip if we already have the same discovery
    if bucket.get(key) == value:
        return

    bucket[key] = value
    _OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OVERLAY_PATH.write_text(json.dumps(overlay, indent=2), encoding="utf-8")

    # Apply to live in-memory dict
    _OVERLAY_TARGETS[rule_name][key] = value
    _log.info("portal_rules.discovery_recorded rule=%s key=%s value=%s",
              rule_name, key, value)


# Apply overlay at import so any caller of these dicts sees the discovered values.
_apply_overlay()
