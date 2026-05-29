"""Customer-facing status/error mapping.

The agent and Sarathi portal produce technical errors. This module converts
them into short product messages that a customer can understand.
"""

from agent.state_manager import Job, JobStatus


STEP_LABELS = {
    "open_homepage": "Opening the government portal",
    "close_homepage_popup": "Preparing the portal session",
    "select_state": "Selecting your state",
    "close_state_popup": "Handling portal notices",
    "navigate_to_dl_services": "Opening the DL renewal service",
    "fetch_dl_details": "Fetching your DL details",
    "confirm_dl_details": "Confirming your DL information",
    "select_renewal_service": "Selecting renewal service",
    "auth_method_selection": "Setting up verification",
    "fill_personal_details": "Filling your application",
    "accept_alert_popup": "Handling portal confirmation",
    "mobile_otp_verification": "Verifying your OTP",
    "aadhaar_otp_verification": "Verifying Aadhaar OTP",
    "upload_documents": "Uploading documents",
    "upload_photo_signature": "Uploading photo and signature",
    "fee_payment": "Preparing payment",
    "download_acknowledgment": "Collecting acknowledgement",
}


def customer_job_view(job: Job) -> dict:
    """Return a stable, user-safe status payload for frontend/mobile clients."""
    raw = _raw_context(job)
    lower = raw.lower()
    last_step = job.steps_completed[-1] if job.steps_completed else ""
    step_label = STEP_LABELS.get(last_step, "Working on your application")

    action_required = False
    action_type = ""
    title = "Application in progress"
    message = step_label
    severity = "info"
    retryable = True

    if job.status == JobStatus.WAITING_OTP:
        action_required = True
        action_type = "otp"
        title = "OTP needed"
        message = "Enter the OTP sent by the government portal so we can continue."
        severity = "action"
    elif job.status == JobStatus.STUCK_HUMAN_NEEDED:
        action_required = True
        action_type = "otp" if "otp" in lower else "human_response"
        title = "Action needed"
        message = _human_message(lower) or "We need one detail from you to continue safely."
        severity = "action"
    elif job.status in {JobStatus.SUBMITTED, JobStatus.COMPLETED}:
        title = "Application submitted"
        message = "Your application was submitted on the government portal."
        severity = "success"
        retryable = False
    elif job.status == JobStatus.FAILED:
        title, message, retryable = _failure_message(lower)
        severity = "error"
    elif job.status == JobStatus.PAYMENT_PENDING:
        title = "Payment pending"
        message = "The portal is checking payment status. Do not retry payment yet."
        severity = "warning"
    elif "captcha" in lower:
        title = "Portal verification retry"
        message = "The government portal asked for a fresh verification code. We are retrying safely."
        severity = "warning"
    elif "403" in lower or "forbidden" in lower:
        title = "Portal temporarily unavailable"
        message = "The government portal refused this request for now. We will retry without changing your details."
        severity = "warning"

    return {
        "title": title,
        "message": message,
        "severity": severity,
        "action_required": action_required,
        "action_type": action_type,
        "retryable": retryable,
        "last_step_label": step_label,
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


def _failure_message(lower: str) -> tuple[str, str, bool]:
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
        "Could not complete application",
        "The government portal did not complete the request. Your details are saved and you can retry.",
        True,
    )
