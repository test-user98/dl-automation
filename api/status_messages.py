"""Customer-facing status/error mapping.

The agent and Sarathi portal produce technical errors. This module converts
them into short product messages that a customer can understand.

Design rule: at any moment the customer is in one of these phases:
  - connecting  → "Connecting to the government portal"
  - filling     → "Filling your application"
  - waiting     → "Waiting on you" (OTP / human-needed)
  - submitting  → "Submitting your application"
  - retrying    → portal/network hiccup, auto retry (e.g. portal is down)
  - done        → submitted/completed
  - failed      → terminal, but still retryable from customer's side

The portal-down case (5xx / "service unavailable" / "bad gateway") is
treated as a transient retry, not a hard failure, so the customer sees
a calm "we will retry" message instead of an error screen.
"""

import re
from datetime import datetime, timezone

from agent.state_manager import Job, JobStatus
from config.settings import get_settings


settings = get_settings()


PHASE_CONNECTING = "connecting"
PHASE_FILLING    = "filling"
PHASE_WAITING    = "waiting"
PHASE_SUBMITTING = "submitting"
PHASE_RETRYING   = "retrying"
PHASE_DONE       = "done"
PHASE_FAILED     = "failed"


# Map each known agent step to (phase, short customer label).
# Anything missing falls back to ("filling", "Working on your application").
#
# Connecting = pre-portal: we're still loading the Sarathi homepage.
# Once any popup is closed or the state dropdown is touched, the agent is
# verifiably on the portal, so we advance to "filling". This matters because
# the customer reads a stale "Connecting…" headline as the agent being stuck.
STEP_TO_PHASE: dict[str, tuple[str, str]] = {
    "open_homepage":            (PHASE_CONNECTING, "Connecting to the government portal"),
    "close_homepage_popup":     (PHASE_FILLING,    "On the portal — preparing your request"),
    "select_state":             (PHASE_FILLING,    "Selecting your state"),
    "close_state_popup":        (PHASE_FILLING,    "Opening the DL renewal section"),
    "navigate_to_dl_services":  (PHASE_FILLING,    "Opening the DL renewal page"),
    "fetch_dl_details":         (PHASE_FILLING,    "Looking up your DL on the portal"),
    "confirm_dl_details":       (PHASE_FILLING,    "Confirming your details with the portal"),
    "select_renewal_service":   (PHASE_FILLING,    "Filling your application"),
    "auth_method_selection":    (PHASE_FILLING,    "Setting up verification"),
    "fill_personal_details":    (PHASE_FILLING,    "Filling your application"),
    "accept_alert_popup":       (PHASE_FILLING,    "Filling your application"),
    "mobile_otp_verification":  (PHASE_WAITING,    "Waiting for the OTP"),
    "aadhaar_otp_verification": (PHASE_WAITING,    "Waiting for the OTP"),
    "upload_documents":         (PHASE_SUBMITTING, "Uploading documents"),
    "upload_photo_signature":   (PHASE_SUBMITTING, "Uploading photo and signature"),
    "fee_payment":              (PHASE_SUBMITTING, "Preparing the government fee"),
    "download_acknowledgment":  (PHASE_SUBMITTING, "Collecting your acknowledgement"),
}

# Legacy shape — kept so existing UI code that reads STEP_LABELS still works.
STEP_LABELS = {k: v[1] for k, v in STEP_TO_PHASE.items()}


_PORTAL_DOWN_PATTERNS = (
    "503", "502", "504", "403", "forbidden",
    "service unavailable", "bad gateway",
    "gateway timeout", "site can't be reached", "site cannot be reached",
    "err_connection", "name not resolved", "net::err",
)

# Phrases that mean Sarathi actually marked the OTP as expired/needing resend
# (not just our own UI offering a "Resend OTP" option). Must be specific enough
# that the option label "Resend OTP" alone does NOT trip the expired branch.
_OTP_EXPIRED_PATTERNS = (
    "otp expired",
    "otp has expired",
    "fresh otp",
    "expired otp",
    "request a fresh",
    "request fresh otp",
    "resend otp option",  # the agent explicitly chose the resend path
)
_OTP_INVALID_PATTERNS = (
    "invalid otp", "wrong otp", "incorrect otp", "otp invalid",
    "otp incorrect", "otp does not match",
)


def _otp_message(lower: str, mobile_suffix: str) -> tuple[str, str]:
    """Pick the right OTP customer message based on portal signals.

    Order matters: invalid is more specific than expired (an expired OTP can
    also be rejected as invalid by some portals).
    """
    if any(p in lower for p in _OTP_INVALID_PATTERNS):
        return (
            "Check the OTP",
            "The government portal did not accept the previous OTP. "
            f"Please enter the latest OTP sent to {mobile_suffix}.",
        )
    if any(p in lower for p in _OTP_EXPIRED_PATTERNS):
        return (
            "Enter the fresh OTP",
            f"The previous OTP expired. We requested a fresh OTP on {mobile_suffix}. "
            "Enter the new code here so we can continue.",
        )
    return (
        "Enter the OTP",
        f"The government portal just sent an OTP to {mobile_suffix}. "
        "Enter it here so we can submit your application.",
    )


def _otp_in_question(pending_request: dict, lower: str) -> bool:
    """True iff the pending human-loop request is genuinely about an OTP.

    Looks at the pending request's own text first (step_name / question /
    context) before falling back to the broader job context, because the
    job log can contain OTP-related noise from earlier steps.
    """
    parts = []
    if isinstance(pending_request, dict):
        parts.extend([
            str(pending_request.get("step_name", "")),
            str(pending_request.get("question", "")),
            str(pending_request.get("context", "")),
        ])
    if any("otp" in p.lower() for p in parts):
        return True
    return "otp" in lower


def _mask_mobile(mobile: str) -> str:
    """+91 XX*****63 — last 2 digits visible, rest masked."""
    digits = re.sub(r"\D", "", mobile or "")
    if len(digits) < 4:
        return "your registered mobile"
    last2 = digits[-2:]
    first2 = digits[-10:-8] if len(digits) >= 10 else digits[:2]
    return f"+91 {first2}******{last2}"


def customer_job_view(job: Job) -> dict:
    """Return a stable, user-safe status payload for frontend/mobile clients."""
    raw = _raw_context(job)
    lower = raw.lower()
    last_step = job.steps_completed[-1] if job.steps_completed else ""
    phase, step_label = STEP_TO_PHASE.get(
        last_step, (PHASE_FILLING, "Working on your application")
    )

    mobile_suffix = _mask_mobile(job.customer_data.get("mobile_number", ""))
    pending_request = job.customer_data.get("_pending_customer_request") or {}
    if not isinstance(pending_request, dict):
        pending_request = {}
    pending_answered = bool(pending_request.get("answered"))

    action_required = False
    action_type = ""
    title = {
        PHASE_CONNECTING: "Connecting to the portal",
        PHASE_FILLING:    "Filling your application",
        PHASE_WAITING:    "Waiting on you",
        PHASE_SUBMITTING: "Submitting your application",
    }.get(phase, "Working on your application")
    message = step_label
    severity = "info"
    retryable = True
    portal_down = _is_portal_down(lower)
    central_repo_unavailable = _is_central_repo_unavailable(lower)
    service_rejected = _is_service_rejection(lower)

    # Portal-down beats most other states — show the calm retry message.
    # Important: it also beats a generic STUCK_HUMAN_NEEDED prompt. The LLM
    # may ask the customer/operator what to do with a raw 403/Forbidden page,
    # but that is not actionable customer input; it is a portal retry state.
    if portal_down and job.status not in {
        JobStatus.SUBMITTED, JobStatus.COMPLETED, JobStatus.WAITING_OTP,
    }:
        phase = PHASE_RETRYING
        title = "Government portal is slow right now"
        message = (
            "The government Sarathi portal is temporarily unavailable. "
            "Your details are saved — we'll retry automatically. "
            "You don't need to do anything."
        )
        severity = "warning"
        action_required = False
        action_type = ""

    elif central_repo_unavailable:
        phase = PHASE_FAILED
        title, message, retryable = _central_repo_unavailable_message()
        action_required = False
        action_type = ""
        severity = "error"

    elif service_rejected:
        phase = PHASE_FAILED
        title, message, retryable = _service_rejection_message(lower)
        action_required = False
        action_type = ""
        severity = "error"

    elif job.status == JobStatus.WAITING_OTP:
        phase = PHASE_WAITING
        action_required = True
        action_type = "otp"
        title, message = _otp_message(lower, mobile_suffix)
        step_label = "Waiting for the OTP"
        severity = "action"

    elif job.status == JobStatus.STUCK_HUMAN_NEEDED:
        phase = PHASE_WAITING
        has_customer_prompt = _has_customer_prompt(pending_request)
        action_required = bool(has_customer_prompt and not pending_answered)
        action_type = ""
        if has_customer_prompt:
            action_type = (
                pending_request.get("action_type")
                or ("otp" if _otp_in_question(pending_request, lower) else "human_response")
            )
        if action_type == "otp":
            title, message = _otp_message(lower, mobile_suffix)
            step_label = "Waiting for the OTP"
        elif action_type == "captcha":
            title = "Help us read the security code"
            message = (
                pending_request.get("question")
                or "Type the characters shown in the image so we can continue."
            )
            step_label = "Help with security code"
        elif has_customer_prompt:
            title = _title_for_customer_request(pending_request)
            message = (
                pending_request.get("question")
                or pending_request.get("context")
                or _field_question(pending_request.get("field_key", ""))
            )
            step_label = title
        else:
            title = "Checking portal requirement"
            message = _human_message(lower) or (
                "The portal paused on a step that needs review. "
                "You do not need to type anything yet."
            )
            step_label = title
            action_required = False
            action_type = ""
        severity = "action" if action_required else "info"

    elif job.status in {JobStatus.SUBMITTED, JobStatus.COMPLETED}:
        phase = PHASE_DONE
        title = "Application submitted"
        message = "Your application was submitted on the government portal."
        severity = "success"
        retryable = False

    elif job.status == JobStatus.FAILED:
        phase = PHASE_FAILED
        title, message, retryable = _failure_message(lower)
        severity = "error"

    elif job.status == JobStatus.PAYMENT_PENDING:
        phase = PHASE_SUBMITTING
        title = "Confirming payment"
        message = "The portal is confirming your payment. Please don't retry — we'll update you in a moment."
        severity = "warning"

    elif job.status == JobStatus.FAILED_RETRYING:
        phase = PHASE_RETRYING
        title = "Retrying"
        message = "The portal didn't respond cleanly. We're retrying — your details are safe."
        severity = "warning"

    elif "captcha" in lower:
        # CAPTCHA churn is internal — hide it behind the phase message.
        title = "Working through the portal verification"
        message = step_label
        severity = "info"

    elif "403" in lower or "forbidden" in lower:
        phase = PHASE_RETRYING
        title = "Government portal is slow right now"
        message = (
            "The portal refused this request for a moment. "
            "We'll retry without changing your details."
        )
        severity = "warning"

    # Latest portal-side error/alert text the agent saw (e.g. "Please provide
    # valid Driving Licence data / Captcha"). Surfaces to the customer as a
    # passive banner so they can see what Sarathi is complaining about while
    # the agent is still working. Only set when non-stale (last 5 minutes).
    portal_message = ""
    last_portal = job.customer_data.get("last_portal_message") or {}
    if isinstance(last_portal, dict):
        portal_message_text = (last_portal.get("text") or "").strip()
        if portal_message_text:
            try:
                at_iso = last_portal.get("at") or ""
                # Parse as UTC; tolerate both naive and tz-aware ISO strings.
                if at_iso:
                    dt = datetime.fromisoformat(at_iso.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - dt).total_seconds()
                    if age <= 300:
                        portal_message = portal_message_text
            except Exception:
                portal_message = portal_message_text  # if parse fails, show it anyway

    triage = _fresh_portal_triage(job)
    if (
        triage
        and settings.portal_triage_mode == "assist"
        and not action_required
        and job.status not in {JobStatus.SUBMITTED, JobStatus.COMPLETED}
    ):
        overlay = _triage_status_overlay(triage)
        if overlay:
            phase = overlay["phase"]
            title = overlay["title"]
            message = overlay["message"]
            severity = overlay["severity"]
            retryable = overlay["retryable"]
            step_label = overlay.get("step_label", step_label)

    return {
        # New phase-based fields
        "phase":           phase,
        "headline":        title,
        "subline":         message,
        "mobile_suffix":   mobile_suffix if action_type == "otp" else "",
        "portal_message":  portal_message,
        # Legacy fields — kept so the current frontend still works
        "title":            title,
        "message":          message,
        "severity":         severity,
        "action_required":  action_required,
        "action_type":      action_type,
        "retryable":        retryable,
        "last_step_label":  step_label,
        "portal_triage":    _customer_triage_payload(triage),
        "customer_request": _customer_request_payload(pending_request, action_required),
        "available_services": job.customer_data.get("available_services", []),
    }


def _raw_context(job: Job) -> str:
    parts = [job.error_message or "", job.last_url or ""]
    for item in job.step_logs[-5:]:
        parts.extend([
            item.get("step_name", ""),
            item.get("status", ""),
            item.get("observation", ""),
            item.get("action_taken", ""),
            item.get("error", ""),
        ])
    return "\n".join(parts)


def _fresh_portal_triage(job: Job) -> dict:
    triage = job.customer_data.get("portal_triage") or {}
    if not isinstance(triage, dict):
        return {}
    try:
        confidence = float(triage.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if confidence < settings.portal_triage_min_confidence:
        return {}
    at_iso = triage.get("at") or ""
    if at_iso:
        try:
            dt = datetime.fromisoformat(str(at_iso).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt).total_seconds() > 300:
                return {}
        except Exception:
            return {}
    return triage


def _triage_status_overlay(triage: dict) -> dict:
    issue_type = (triage.get("issue_type") or "unknown").strip().lower()
    recommended = (triage.get("recommended_next_action") or "").strip().lower()
    evidence_blob = " ".join(
        str(item or "") for item in (triage.get("evidence") or [])
    ).lower()
    if issue_type == "missing_customer_data" and (
        "captcha" in recommended or "captcha" in evidence_blob
    ):
        issue_type = "captcha_required"
    templates = {
        "portal_slow": {
            "phase": PHASE_RETRYING,
            "title": "Government portal is slow right now",
            "message": "The portal is not responding cleanly. Your details are saved and we are retrying.",
            "severity": "warning",
            "retryable": True,
            "step_label": "Retrying the portal",
        },
        "retryable_portal_error": {
            "phase": PHASE_RETRYING,
            "title": "Retrying",
            "message": "The portal did not respond cleanly. We are retrying without changing your details.",
            "severity": "warning",
            "retryable": True,
            "step_label": "Retrying the portal",
        },
        "validation_rejected": {
            "phase": PHASE_RETRYING,
            "title": "Checking the details again",
            "message": "The portal did not accept the last attempt. We are checking the fields and retrying.",
            "severity": "warning",
            "retryable": True,
            "step_label": "Checking the portal response",
        },
        "missing_customer_data": {
            "phase": PHASE_FILLING,
            "title": "Checking one detail",
            "message": "The portal is asking for an additional detail. We are checking what is needed.",
            "severity": "info",
            "retryable": True,
            "step_label": "Checking portal requirements",
        },
        "captcha_required": {
            "phase": PHASE_FILLING,
            "title": "Working through the portal verification",
            "message": "We are completing the portal verification step.",
            "severity": "info",
            "retryable": True,
            "step_label": "Portal verification",
        },
        "service_unavailable_for_rto": {
            "phase": PHASE_FAILED,
            "title": "This service is not available at your RTO",
            "message": "Sarathi says this DL service is not available for the RTO linked to this licence.",
            "severity": "error",
            "retryable": False,
            "step_label": "Service unavailable",
        },
        "dl_not_in_central_repository": {
            "phase": PHASE_FAILED,
            "title": "DL record not available online",
            "message": (
                "Sarathi could not find this DL in its online records. Online application "
                "cannot continue for this licence; please contact the issuing RTO/RLA authority."
            ),
            "severity": "error",
            "retryable": False,
            "step_label": "DL lookup stopped",
        },
        "payment_pending": {
            "phase": PHASE_SUBMITTING,
            "title": "Confirming payment",
            "message": "The portal is confirming your payment. Please do not retry yet.",
            "severity": "warning",
            "retryable": True,
            "step_label": "Confirming payment",
        },
    }
    return templates.get(issue_type, {})


def _customer_triage_payload(triage: dict) -> dict:
    if not triage:
        return {}
    return {
        "issue_type": triage.get("issue_type", "unknown"),
        "confidence": triage.get("confidence", 0),
        "recommended_next_action": triage.get("recommended_next_action", "unknown"),
        "field_key": triage.get("field_key", ""),
        "mode": settings.portal_triage_mode,
    }


def _is_portal_down(lower: str) -> bool:
    return any(p in lower for p in _PORTAL_DOWN_PATTERNS)


def _is_service_rejection(lower: str) -> bool:
    return (
        "requested service" in lower
        and (
            "unable to process your data" in lower
            or "not legible for requested rto" in lower
            or "not eligible for requested rto" in lower
            or "kindly visit the rto/rla authority" in lower
        )
    )


def _is_central_repo_unavailable(lower: str) -> bool:
    return (
        "dl central record unavailable" in lower
        or "details of given dl number not available" in lower
        or "not available in the central repository" in lower
        or "licence data not available in central repository" in lower
        or "license data not available in central repository" in lower
    )


def _central_repo_unavailable_message() -> tuple[str, str, bool]:
    return (
        "DL record not available online",
        "Sarathi could not find this DL in its online records. Online application "
        "cannot continue for this licence; please contact the issuing RTO/RLA authority.",
        False,
    )


def _service_rejection_message(lower: str) -> tuple[str, str, bool]:
    return (
        "This service is not available at your RTO",
        "Sarathi says the selected DL service is not available for the RTO linked "
        "to this licence. Choose another available service or visit the RTO/RLA "
        "authority for this request.",
        False,
    )


def _human_message(lower: str) -> str:
    if "otp" in lower:
        return "Enter the OTP sent to your registered mobile number."
    if "captcha" in lower:
        return "The portal verification image was unclear. Please help us read it."
    if "dl number" in lower or "date of birth" in lower:
        return "Please confirm your DL number and date of birth."
    if "pin" in lower or "address" in lower:
        return "Please confirm your present address PIN code."
    return ""


def _title_for_customer_request(request: dict) -> str:
    action_type = request.get("action_type", "")
    step_name = request.get("step_name", "")
    field_label = _friendly_field_label(request.get("field_key", ""))
    if field_label:
        return f"Confirm your {field_label}"
    if action_type == "service_selection" or step_name == "service_selection":
        return "Choose a DL service"
    if action_type == "confirmation":
        return "Please review and confirm"
    if action_type == "choice" or request.get("options"):
        return "Choose an option"
    return "We need one detail"


def _customer_request_payload(request: dict, action_required: bool) -> dict:
    if not request or not action_required:
        return {}
    if not _has_customer_prompt(request):
        return {}
    options = request.get("options") or []
    payload = {
        "step_name": request.get("step_name", ""),
        "question": request.get("question", ""),
        "context": request.get("context", ""),
        "options": options if isinstance(options, list) else [],
        "action_type": request.get("action_type", "human_response"),
        "field_key": request.get("field_key", ""),
    }
    # CAPTCHA — frontend renders the embedded image so the customer can read it.
    image_b64 = request.get("image_b64")
    if image_b64:
        payload["image_b64"] = image_b64
    return payload


def _has_customer_prompt(request: dict) -> bool:
    if not isinstance(request, dict) or not request:
        return False
    options = request.get("options") or []
    return bool(
        str(request.get("question", "")).strip()
        or str(request.get("context", "")).strip()
        or str(request.get("field_key", "")).strip()
        or request.get("image_b64")
        or (isinstance(options, list) and options)
    )


def _friendly_field_label(field_key: str) -> str:
    key = str(field_key or "").strip().lower()
    labels = {
        "dl_number": "DL number",
        "dob": "date of birth",
        "pin_code": "PIN code",
        "mobile_number": "mobile number",
        "email": "email address",
        "address": "address",
        "state_code": "state",
        "rto_code": "RTO",
    }
    return labels.get(key, key.replace("_", " ") if key else "")


def _field_question(field_key: str) -> str:
    label = _friendly_field_label(field_key)
    return f"Please enter your {label} so we can continue." if label else ""


def _failure_message(lower: str) -> tuple[str, str, bool]:
    if _is_central_repo_unavailable(lower):
        return _central_repo_unavailable_message()
    if _is_service_rejection(lower):
        return _service_rejection_message(lower)
    if "stopped after" in lower and "repeated attempts" in lower:
        return (
            "Portal is stuck right now",
            "The government portal did not move forward after several safe retries. "
            "Your details are saved and you can retry.",
            True,
        )
    if _is_portal_down(lower):
        return (
            "Government portal is unavailable",
            "The Sarathi portal is down right now. Your details are saved — "
            "please try again in a few minutes.",
            True,
        )
    if "403" in lower or "forbidden" in lower:
        return (
            "Portal temporarily unavailable",
            "The government portal refused this request. Please try again after some time.",
            True,
        )
    if "target page" in lower or "browser" in lower or "context" in lower:
        return (
            "Portal session interrupted",
            "The government portal session closed unexpectedly. You can safely retry.",
            True,
        )
    if "captcha" in lower:
        return (
            "Verification failed",
            "The portal verification code could not be completed. Please retry.",
            True,
        )
    if "otp" in lower:
        return (
            "OTP verification failed",
            "The OTP could not be verified. Please request a fresh OTP and try again.",
            True,
        )
    return (
        "Couldn't complete the application",
        "The government portal did not complete the request. Your details are saved and you can retry.",
        True,
    )
