from datetime import datetime, timezone

from agent.state_manager import Job, JobStatus, StepLog
from api.status_messages import customer_job_view


def _job(status: JobStatus = JobStatus.AGENT_RUNNING) -> Job:
    job = Job(
        job_id="job-1",
        customer_id="cust-1",
        service="DL_RENEWAL",
        customer_data={"mobile_number": "9876544163"},
        documents={},
    )
    job.status = status
    return job


def test_customer_view_exposes_service_selection_options():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.customer_data["_pending_customer_request"] = {
        "step_name": "service_selection",
        "question": "Which DL service would you like to apply for?",
        "context": "Select the DL service you need.",
        "options": ["CHANGE OF ADDRESS IN DL", "DL EXTRACT"],
        "action_type": "service_selection",
    }

    view = customer_job_view(job)

    assert view["phase"] == "waiting"
    assert view["action_required"] is True
    assert view["action_type"] == "service_selection"
    assert view["headline"] == "Choose a DL service"
    assert view["customer_request"]["options"] == ["CHANGE OF ADDRESS IN DL", "DL EXTRACT"]
    assert view["customer_request"]["context"] == "Select the DL service you need."


def test_customer_view_exposes_text_question():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.customer_data["_pending_customer_request"] = {
        "step_name": "change_dob_reason",
        "question": "Why do you want to change the DOB?",
        "context": "",
        "options": [],
        "action_type": "text",
    }

    view = customer_job_view(job)

    assert view["action_required"] is True
    assert view["action_type"] == "text"
    assert view["subline"] == "Why do you want to change the DOB?"
    assert view["customer_request"]["question"] == "Why do you want to change the DOB?"


def test_customer_view_exposes_field_key_when_agent_needs_specific_detail():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.customer_data["_pending_customer_request"] = {
        "step_name": "fill_personal_details",
        "question": "",
        "context": "",
        "options": [],
        "action_type": "text",
        "field_key": "pin_code",
    }

    view = customer_job_view(job)

    assert view["action_required"] is True
    assert view["headline"] == "Confirm your PIN code"
    assert view["subline"] == "Please enter your PIN code so we can continue."
    assert view["customer_request"]["field_key"] == "pin_code"


def test_customer_view_does_not_ask_generic_question_without_pending_request():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)

    view = customer_job_view(job)

    assert view["action_required"] is False
    assert view["action_type"] == ""
    assert view["customer_request"] == {}
    assert view["headline"] == "Checking portal requirement"
    assert "Please confirm one detail" not in view["subline"]


def test_customer_view_exposes_confirmation_request():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.customer_data["_pending_customer_request"] = {
        "step_name": "confirm_details",
        "question": "Please verify these details are correct.",
        "context": "Name: Test User\nDOB: 04-09-1998\nDL: RJ07...",
        "options": ["Yes, details are correct"],
        "action_type": "confirmation",
    }

    view = customer_job_view(job)

    assert view["action_required"] is True
    assert view["action_type"] == "confirmation"
    assert view["headline"] == "Please review and confirm"
    assert "Name: Test User" in view["customer_request"]["context"]


def test_customer_view_maps_service_rto_rejection_as_terminal_message():
    job = _job(JobStatus.FAILED)
    job.error_message = (
        "Sorry for inconvenience, Unable to Process your Data. DL Holder "
        "Requested Service: CHANGE OF DATE OF BIRTH IN DL is not legible for "
        "Requested RTO: DTO, LONGDING . Kindly visit the RTO/RLA Authority"
    )
    job.customer_data["available_services"] = ["CHANGE OF ADDRESS IN DL", "DL EXTRACT"]

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["headline"] == "This service is not available at your RTO"
    assert view["retryable"] is False
    assert view["action_required"] is False
    assert view["available_services"] == ["CHANGE OF ADDRESS IN DL", "DL EXTRACT"]


def test_customer_view_maps_central_repository_rejection_as_terminal_message():
    job = _job(JobStatus.FAILED)
    job.error_message = (
        "DL central record unavailable: Sarathi says this licence is not available "
        "for online applications and requires RTO/RLA handling."
    )

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["headline"] == "DL record not available online"
    assert "online application cannot continue" in view["subline"].lower()
    assert view["retryable"] is False
    assert view["action_required"] is False


def test_customer_view_maps_portal_down_to_retrying():
    job = _job(JobStatus.FAILED_RETRYING)
    job.step_logs.append(StepLog(
        step_name="fetch_dl_details",
        status="retrying",
        observation="503 Service Unavailable",
        action_taken="retry",
    ).to_dict())

    view = customer_job_view(job)

    assert view["phase"] == "retrying"
    assert "retry" in view["subline"].lower()
    assert view["severity"] == "warning"


def test_customer_view_maps_otp_wait_to_otp_action():
    job = _job(JobStatus.WAITING_OTP)
    job.steps_completed.append("mobile_otp_verification")

    view = customer_job_view(job)

    assert view["phase"] == "waiting"
    assert view["action_required"] is True
    assert view["action_type"] == "otp"
    assert view["mobile_suffix"].endswith("63")


def test_answered_customer_request_does_not_keep_prompting():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.customer_data["_pending_customer_request"] = {
        "step_name": "service_selection",
        "question": "Which DL service would you like to apply for?",
        "context": "Select the DL service you need.",
        "options": ["DL EXTRACT"],
        "action_type": "service_selection",
        "answered": True,
    }

    view = customer_job_view(job)

    assert view["phase"] == "waiting"
    assert view["action_required"] is False
    assert view["customer_request"] == {}


def test_customer_view_maps_otp_expired_to_fresh_otp_prompt():
    job = _job(JobStatus.WAITING_OTP)
    job.error_message = "OTP expired. Fresh OTP requested via resend."

    view = customer_job_view(job)

    assert view["headline"] == "Enter the fresh OTP"
    assert "fresh OTP" in view["subline"]
    assert view["action_required"] is True


def test_customer_view_maps_invalid_otp_to_check_otp_prompt():
    job = _job(JobStatus.WAITING_OTP)
    job.error_message = "Invalid OTP entered"

    view = customer_job_view(job)

    assert view["headline"] == "Check the OTP"
    assert "did not accept" in view["subline"]
    assert view["action_required"] is True


def test_customer_view_maps_forbidden_to_customer_safe_retry():
    job = _job(JobStatus.AGENT_RUNNING)
    job.step_logs.append(StepLog(
        step_name="fill_personal_details",
        status="failed",
        observation="403 Forbidden",
        action_taken="retry",
    ).to_dict())

    view = customer_job_view(job)

    assert view["phase"] == "retrying"
    assert view["headline"] == "Government portal is slow right now"
    assert "retry" in view["subline"].lower()


def test_customer_view_hides_forbidden_even_when_agent_asked_human():
    job = _job(JobStatus.STUCK_HUMAN_NEEDED)
    job.error_message = "The page is showing a 403 Forbidden error."
    job.customer_data["_pending_customer_request"] = {
        "step_name": "unknown",
        "question": "The page is showing a 403 Forbidden error.",
        "context": "The agent is currently seeing: 403 Forbidden error page is displayed.",
        "options": ["Try again from this step", "Skip this step if optional"],
        "action_type": "confirmation",
    }

    view = customer_job_view(job)

    assert view["phase"] == "retrying"
    assert view["headline"] == "Government portal is slow right now"
    assert view["action_required"] is False
    assert view["action_type"] == ""
    assert view["customer_request"] == {}
    assert "403" not in view["subline"]
    assert "Forbidden" not in view["subline"]


def test_customer_view_maps_browser_session_interruption_to_retryable_failure():
    job = _job(JobStatus.FAILED)
    job.error_message = "Target page, context or browser has been closed"

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["headline"] == "Portal session interrupted"
    assert view["retryable"] is True


def test_customer_view_maps_max_retry_exit_to_clear_failure():
    job = _job(JobStatus.FAILED)
    job.error_message = (
        "Stopped after 4 repeated attempts on step 'fetch_dl_details'. "
        "The government portal did not move forward after safe retries."
    )

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["headline"] == "Portal is stuck right now"
    assert "safe retries" in view["subline"]
    assert view["action_required"] is False
    assert view["retryable"] is True


def test_customer_view_uses_high_confidence_portal_triage(monkeypatch):
    import api.status_messages as status_messages

    monkeypatch.setattr(status_messages.settings, "portal_triage_mode", "assist")
    monkeypatch.setattr(status_messages.settings, "portal_triage_min_confidence", 0.70)
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = {
        "issue_type": "validation_rejected",
        "confidence": 0.91,
        "recommended_next_action": "refill_required_fields",
        "field_key": "",
        "at": datetime.now(timezone.utc).isoformat(),
        "internal_diagnosis": "Portal alert rejected fields with raw technical text.",
        "reasoning_summary": "Alert text appeared after submit.",
    }

    view = customer_job_view(job)

    assert view["phase"] == "retrying"
    assert view["headline"] == "Checking the details again"
    assert "portal did not accept" in view["subline"].lower()
    assert view["portal_triage"]["issue_type"] == "validation_rejected"
    assert "raw technical" not in view["subline"]


def test_customer_view_maps_security_code_triage_without_generic_detail_copy(monkeypatch):
    import api.status_messages as status_messages

    monkeypatch.setattr(status_messages.settings, "portal_triage_mode", "assist")
    monkeypatch.setattr(status_messages.settings, "portal_triage_min_confidence", 0.70)
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = {
        "issue_type": "missing_customer_data",
        "confidence": 0.91,
        "recommended_next_action": "solve_captcha",
        "field_key": "",
        "evidence": ["Captcha field is empty", "Terms checkbox is unchecked"],
        "at": datetime.now(timezone.utc).isoformat(),
    }

    view = customer_job_view(job)

    assert view["action_required"] is False
    assert view["headline"] == "Working through the portal verification"
    assert "one detail" not in view["headline"].lower()
    assert "additional detail" not in view["subline"].lower()


def test_customer_view_ignores_low_confidence_portal_triage(monkeypatch):
    import api.status_messages as status_messages

    monkeypatch.setattr(status_messages.settings, "portal_triage_mode", "assist")
    monkeypatch.setattr(status_messages.settings, "portal_triage_min_confidence", 0.70)
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = {
        "issue_type": "validation_rejected",
        "confidence": 0.40,
        "recommended_next_action": "refill_required_fields",
        "at": datetime.now(timezone.utc).isoformat(),
    }

    view = customer_job_view(job)

    assert view["phase"] == "filling"
    assert view["headline"] == "Filling your application"
    assert view["portal_triage"] == {}


def test_customer_view_shadow_triage_does_not_override_copy(monkeypatch):
    import api.status_messages as status_messages

    monkeypatch.setattr(status_messages.settings, "portal_triage_mode", "shadow")
    monkeypatch.setattr(status_messages.settings, "portal_triage_min_confidence", 0.70)
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = {
        "issue_type": "portal_slow",
        "confidence": 0.95,
        "recommended_next_action": "restart_portal_session",
        "at": datetime.now(timezone.utc).isoformat(),
    }

    view = customer_job_view(job)

    assert view["phase"] == "filling"
    assert view["headline"] == "Filling your application"
    assert view["portal_triage"]["mode"] == "shadow"


# ── Structured terminal reasons + terminal confidence gate ──────────────────

def _fresh_triage(issue_type: str, confidence: float) -> dict:
    return {
        "issue_type": issue_type,
        "confidence": confidence,
        "recommended_next_action": "stop_terminal",
        "evidence": [],
        "at": datetime.now(timezone.utc).isoformat(),
    }


def test_portal_unavailable_terminal_closes_loop_not_retry():
    """A FAILED job stamped portal_unavailable must NOT say 'we'll keep retrying'."""
    job = _job(JobStatus.FAILED)
    job.customer_data["portal_terminal_reason"] = "portal_unavailable"
    # Error prose contains portal-down keywords that previously triggered the
    # calm "we are retrying automatically" message even though the job is closed.
    job.error_message = "Portal unavailable: government portal timed out / unavailable."

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert "isn't responding" in view["headline"]
    assert "retrying" not in view["subline"].lower()
    assert view["action_required"] is False


def test_dl_not_found_terminal_message():
    job = _job(JobStatus.FAILED)
    job.customer_data["portal_terminal_reason"] = "dl_not_found"

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["headline"] == "We couldn't find your driving licence"
    assert view["retryable"] is True


def test_low_confidence_terminal_triage_downgraded_to_retry():
    """A low-confidence terminal guess must not tell the customer to give up."""
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = _fresh_triage("dl_not_in_central_repository", 0.75)

    view = customer_job_view(job)

    assert view["phase"] == "retrying"
    assert view["retryable"] is True


def test_high_confidence_terminal_triage_shows_terminal():
    job = _job(JobStatus.AGENT_RUNNING)
    job.customer_data["portal_triage"] = _fresh_triage("dl_not_in_central_repository", 0.90)

    view = customer_job_view(job)

    assert view["phase"] == "failed"
    assert view["retryable"] is False
