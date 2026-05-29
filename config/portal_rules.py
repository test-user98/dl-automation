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
    "#entCaptha",       # CAPTCHA input — CSS-hidden (display:none), not type=hidden
    "#entcaptxt",       # Alternate CAPTCHA field — also CSS-hidden in some flows
}

# ── Generate OTP rules ────────────────────────────────────────────────────────

GENERATE_OTP_RULES = {
    # Click the UI button first — the API call works but bypasses Sarathi's AJAX
    # callback that activates the OTP entry section + reveals Submit OTP button.
    "primary_method": "ui_click",
    "api_endpoint": "/sarathiservice/getOtpFromSarathi.do",
    "button_selector": "#generateSarathiotp",
    "button_onclick_fn": "gensarathiOTP",       # Sarathi JS function on the button
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
    "otp_input_selector":      "#otpNumber",
    "captcha_image_selector":  "#capimg",
    "captcha_input_selector":  "#entcaptxt",
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
        "otp mismatch", "please enter valid otp",
    ],
    "captcha_rejection_patterns": [
        "invalid captcha", "captcha mismatch", "wrong captcha", "captcha not match",
    ],
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
