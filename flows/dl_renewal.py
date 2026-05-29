"""
DL Renewal flow definition.

This is the ground truth for what the agent is trying to accomplish on
the Sarathi portal for driving license renewal. It defines:

  - The ordered steps the agent should move through
  - What data is needed at each step (sourced from customer_data)
  - What documents must be uploaded at each step
  - Which steps are skippable / conditional
  - Known popups and obstacles at each step

The agent's brain reads this to build its goal context.
It does NOT blindly follow this as a script — it uses it as a map.
If the portal looks different, the agent reasons from the screenshot.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FlowStep:
    name: str                          # internal step identifier
    description: str                   # human-readable description
    required_data: list[str]           # keys from customer_data
    documents_to_upload: list[str]     # keys from job.documents
    is_conditional: bool = False       # True = might be skipped depending on portal state
    condition_description: str = ""    # when this step applies
    known_obstacles: list[str] = field(default_factory=list)
    otp_expected: bool = False
    otp_type: str = ""                 # "mobile" | "aadhaar"


DL_RENEWAL_STEPS: list[FlowStep] = [
    FlowStep(
        name="open_homepage",
        description="Navigate to Sarathi portal homepage",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=["mobile number update popup appears immediately"],
    ),
    FlowStep(
        name="close_homepage_popup",
        description="Close the mobile number update modal popup",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=["modal has X button or press Escape"],
    ),
    FlowStep(
        name="select_state",
        description="Select the customer's state from the dropdown",
        required_data=["state_code"],
        documents_to_upload=[],
        known_obstacles=["second popup may appear after state selection — close it too"],
    ),
    FlowStep(
        name="close_state_popup",
        description="Close any popup that appears after state selection",
        required_data=[],
        documents_to_upload=[],
        is_conditional=True,
        condition_description="Only if a second popup/modal opens after state selection",
        known_obstacles=["OK button or X button to close"],
    ),
    FlowStep(
        name="navigate_to_dl_services",
        description="Click 'Services on DL (Renewal/Duplicate/Others)' from the menu",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=["menu may be under 'Driving Licence' section — look carefully"],
    ),
    FlowStep(
        name="fetch_dl_details",
        description="Enter DL number and date of birth, solve CAPTCHA, click GO",
        required_data=["dl_number", "dob"],
        documents_to_upload=[],
        known_obstacles=["CAPTCHA must be solved", "DOB format may need DD/MM/YYYY"],
    ),
    FlowStep(
        name="confirm_dl_details",
        description="Verify the fetched DL details are correct, select RTO, click Confirm",
        required_data=["state_code", "rto_code"],
        documents_to_upload=[],
        known_obstacles=["RTO dropdown — pick closest match to customer's RTO"],
    ),
    FlowStep(
        name="select_renewal_service",
        description="Click/select 'Renewal of Driving Licence' option",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=["may be a checkbox, radio button, or link — look for 'Renewal'"],
    ),
    FlowStep(
        name="auth_method_selection",
        description="If asked to choose authentication method, select mobile OTP",
        required_data=["mobile_number"],
        documents_to_upload=[],
        is_conditional=True,
        condition_description="Only if portal shows an auth method selection screen",
        known_obstacles=["prefer mobile OTP over Aadhaar OTP — simpler and faster"],
    ),
    FlowStep(
        name="fill_personal_details",
        description="Fill personal details form — name, address, blood group, etc.",
        required_data=[
            "name", "dob", "address", "pin_code", "state",
            "mobile_number", "email", "blood_group", "gender",
        ],
        documents_to_upload=[],
        known_obstacles=[
            "email is optional — fill if available, skip if not",
            "alert popup may appear after submitting — click OK",
            "additional info section at bottom of page — scroll down and fill",
        ],
    ),
    FlowStep(
        name="accept_alert_popup",
        description="Accept any alert/confirmation dialog that appears",
        required_data=[],
        documents_to_upload=[],
        is_conditional=True,
        condition_description="Only when a JS alert or confirmation modal appears",
        known_obstacles=["always click OK unless it says cancel/reset/delete"],
    ),
    FlowStep(
        name="upload_documents",
        description="Upload address proof and other required documents",
        required_data=[],
        documents_to_upload=["address_proof", "form1_self_declaration"],
        known_obstacles=[
            "select doc type from dropdown first, then upload file",
            "file size must be under 200KB — compress if needed",
            "Form 1 may need to be downloaded, filled, and re-uploaded",
        ],
    ),
    FlowStep(
        name="upload_photo_signature",
        description="Upload passport photo and signature",
        required_data=[],
        documents_to_upload=["photo", "signature"],
        known_obstacles=[
            "photo max 20KB — compress before uploading",
            "signature max 10KB — compress before uploading",
            "dimensions: ~200x200 for photo",
        ],
    ),
    FlowStep(
        name="mobile_otp_verification",
        description="Enter OTP sent to the customer's DL-registered mobile number",
        required_data=["mobile_number"],
        documents_to_upload=[],
        otp_expected=True,
        otp_type="mobile",
        known_obstacles=[
            "agent PAUSES here — customer receives OTP and enters it in the app",
            "OTP expires in ~10 minutes",
            "resend link available if OTP not received",
        ],
    ),
    FlowStep(
        name="aadhaar_otp_verification",
        description="Enter OTP sent to the customer's Aadhaar-linked mobile number",
        required_data=["aadhaar_number"],
        documents_to_upload=[],
        is_conditional=True,
        condition_description="Only if Aadhaar e-KYC is required by the portal",
        otp_expected=True,
        otp_type="aadhaar",
        known_obstacles=[
            "agent PAUSES here — customer receives OTP and enters it in the app",
            "OTP expires in ~10 minutes",
        ],
    ),
    FlowStep(
        name="service_selection",
        description="Select the DL service from options shown after OTP (Renewal, Extract, etc.)",
        required_data=[],
        documents_to_upload=[],
        is_conditional=True,
        condition_description="Shown after OTP verification — user picks which DL service they need",
        known_obstacles=[
            "DL Renewal is only available if DL expires within 365 days",
            "page may show checkboxes or radio buttons for service choice",
        ],
    ),
    FlowStep(
        name="service_form_fill",
        description="Fill the selected service form — reason dropdown, organ donation, CAPTCHA",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=[
            "reason for extract/duplicate — must ask user, agent cannot decide",
            "organ donation checkbox — ask user with 5-second timeout, default NO",
            "success popup shows ACK number — extract and save",
        ],
    ),
    FlowStep(
        name="fee_payment",
        description="Pay the renewal fee (₹200-500 depending on state) via UPI/card/netbanking",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=[
            "payment page opens in a POPUP WINDOW — switch browser context",
            "amount varies by state",
            "if payment deducted but portal shows pending — wait, do not retry",
            "UPI is usually fastest",
        ],
    ),
    FlowStep(
        name="download_acknowledgment",
        description="Download the application acknowledgment PDF and extract application number",
        required_data=[],
        documents_to_upload=[],
        known_obstacles=[
            "PDF viewer opens — find and click the download button",
            "application number shown on acknowledgment — extract and save",
        ],
    ),
]

# Flat set of step names for quick lookup
DL_RENEWAL_STEP_NAMES = {s.name for s in DL_RENEWAL_STEPS}

# Data fields required across the entire DL renewal flow
DL_RENEWAL_REQUIRED_FIELDS = [
    "dl_number",
    "dob",
    "name",
    "mobile_number",
    "state_code",
]

DL_RENEWAL_OPTIONAL_FIELDS = [
    "email",
    "address",
    "pin_code",
    "blood_group",
    "gender",
    "rto_code",
    "aadhaar_number",
]

# Documents needed
DL_RENEWAL_DOCUMENTS = {
    "photo":                  "Passport-size photo (JPEG, max 20KB)",
    "signature":              "Signature on white paper (JPEG, max 10KB)",
    "address_proof":          "Aadhaar / Voter ID / Passport / Utility Bill (JPEG/PDF, max 200KB)",
    "form1_self_declaration": "Filled Form 1 — self declaration of fitness (PDF)",
}

# What to show the customer in the app for each step
CUSTOMER_STATUS_MESSAGES = {
    "open_homepage":            "Connecting to Sarathi portal...",
    "close_homepage_popup":     "Navigating portal...",
    "select_state":             "Selecting your state...",
    "close_state_popup":        "Navigating portal...",
    "navigate_to_dl_services":  "Finding DL renewal service...",
    "fetch_dl_details":         "Fetching your DL details...",
    "confirm_dl_details":       "Confirming your DL information...",
    "select_renewal_service":   "Selecting renewal service...",
    "auth_method_selection":    "Setting up verification...",
    "fill_personal_details":    "Filling in your details...",
    "accept_alert_popup":       "Confirming form...",
    "upload_documents":         "Uploading your documents...",
    "upload_photo_signature":   "Uploading photo and signature...",
    "mobile_otp_verification":  "Waiting for your OTP...",
    "aadhaar_otp_verification":  "Waiting for your Aadhaar OTP...",
    "service_selection":        "Waiting for your service selection...",
    "service_form_fill":        "Filling service form...",
    "fee_payment":              "Processing fee payment...",
    "download_acknowledgment":  "Getting your application reference...",
}


def get_step(name: str) -> Optional[FlowStep]:
    for s in DL_RENEWAL_STEPS:
        if s.name == name:
            return s
    return None


def steps_after(completed: list[str]) -> list[FlowStep]:
    """Return ordered steps not yet in completed list."""
    return [s for s in DL_RENEWAL_STEPS if s.name not in completed]
