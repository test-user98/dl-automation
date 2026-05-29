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
