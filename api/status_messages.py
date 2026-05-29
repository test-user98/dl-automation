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
from agent.state_manager import Job, JobStatus


PHASE_CONNECTING = "connecting"
PHASE_FILLING    = "filling"
PHASE_WAITING    = "waiting"
PHASE_SUBMITTING = "submitting"
PHASE_RETRYING   = "retrying"
PHASE_DONE       = "done"
PHASE_FAILED     = "failed"


# Map each known agent step to (phase, short customer label).
# Anything missing falls back to ("filling", "Working on your application").
STEP_TO_PHASE: dict[str, tuple[str, str]] = {
    "open_homepage":            (PHASE_CONNECTING, "Connecting to the government portal"),
    "close_homepage_popup":     (PHASE_CONNECTING, "Connecting to the government portal"),
    "select_state":             (PHASE_CONNECTING, "Selecting your state"),
    "close_state_popup":        (PHASE_CONNECTING, "Selecting your state"),
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
    "503", "502", "504", "service unavailable", "bad gateway",
    "gateway timeout", "site can't be reached", "site cannot be reached",
    "err_connection", "name not resolved", "net::err",
)


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
    service_rejected = _is_service_rejection(lower)

    # Portal-down beats most other states — show the calm retry message.
    if portal_down and job.status not in {
        JobStatus.SUBMITTED, JobStatus.COMPLETED,
        JobStatus.WAITING_OTP, JobStatus.STUCK_HUMAN_NEEDED,
    }:
        phase = PHASE_RETRYING
        title = "Government portal is slow right now"
        message = (
            "The government Sarathi portal is temporarily unavailable. "
            "Your details are saved — we'll retry automatically. "
            "You don't need to do anything."
        )
        severity = "warning"

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
        if "expired" in lower or "fresh otp" in lower or "resend" in lower:
            title = "Enter the fresh OTP"
            message = (
                f"The previous OTP expired. We requested a fresh OTP on {mobile_suffix}. "
                "Enter the new code here so we can continue."
            )
        elif "invalid otp" in lower or "wrong otp" in lower or "incorrect otp" in lower:
            title = "Check the OTP"
            message = (
                "The government portal did not accept the previous OTP. "
                f"Please enter the latest OTP sent to {mobile_suffix}."
            )
        else:
            title = "Enter the OTP"
            message = (
                f"The government portal just sent an OTP to {mobile_suffix}. "
                "Enter it here so we can submit your application."
            )
        severity = "action"

    elif job.status == JobStatus.STUCK_HUMAN_NEEDED:
        phase = PHASE_WAITING
        action_required = not pending_answered
        action_type = (
            pending_request.get("action_type")
            or ("otp" if "otp" in lower else "human_response")
        )
        if action_type == "otp":
            if "expired" in lower or "fresh otp" in lower or "resend" in lower:
                title = "Enter the fresh OTP"
                message = (
                    f"The previous OTP expired. We requested a fresh OTP on {mobile_suffix}. "
                    "Enter the new code here so we can continue."
                )
            elif "invalid otp" in lower or "wrong otp" in lower or "incorrect otp" in lower:
                title = "Check the OTP"
                message = (
                    "The government portal did not accept the previous OTP. "
                    f"Please enter the latest OTP sent to {mobile_suffix}."
                )
            else:
                title = "Enter the OTP"
                message = (
                    f"The government portal just sent an OTP to {mobile_suffix}. "
                    "Enter it here so we can submit your application."
                )
        elif pending_request:
            title = _title_for_customer_request(pending_request)
            message = (
                pending_request.get("question")
                or pending_request.get("context")
                or "Please confirm one detail so we can continue."
            )
        else:
            title = "We need one detail"
            message = _human_message(lower) or "Please confirm one detail so we can continue."
        severity = "action"

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

    return {
        # New phase-based fields
        "phase":           phase,
        "headline":        title,
        "subline":         message,
        "mobile_suffix":   mobile_suffix if action_type == "otp" else "",
        # Legacy fields — kept so the current frontend still works
        "title":            title,
        "message":          message,
        "severity":         severity,
        "action_required":  action_required,
        "action_type":      action_type,
        "retryable":        retryable,
        "last_step_label":  step_label,
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
    if action_type == "service_selection" or step_name == "service_selection":
        return "Choose a DL service"
    if action_type == "choice" or request.get("options"):
        return "Choose an option"
    return "We need one detail"


def _customer_request_payload(request: dict, action_required: bool) -> dict:
    if not request or not action_required:
        return {}
    options = request.get("options") or []
    return {
        "step_name": request.get("step_name", ""),
        "question": request.get("question", ""),
        "context": request.get("context", ""),
        "options": options if isinstance(options, list) else [],
        "action_type": request.get("action_type", "human_response"),
    }


def _failure_message(lower: str) -> tuple[str, str, bool]:
    if _is_service_rejection(lower):
        return _service_rejection_message(lower)
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
