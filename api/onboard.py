"""
Customer onboarding router — the step-by-step data collection flow.

Steps:
  1. Customer enters service type + state
  2. Customer enters mobile number (OTP destination), DOB, DL number
     → DL number normalised and validated
     → If OCR available: auto-fill name, address etc from DL image
  3. Customer sees a confirmation card with ALL extracted data
     → Reviews, edits if wrong, clicks Confirm
  4. Job is created and agent starts
  5. Customer is shown real-time step progress
  6. When OTP is needed: customer enters it in the OTP screen
  7. Customer receives application number at the end
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional
from pathlib import Path

from config.settings import get_settings, get_settings as _gs
from config.portal_rules import get_fee
from api.deps import state_manager, ocr_service, orchestrator, customer_store
from tools.dl_normalizer import DLNormalizer, DL_FORMAT_HINT, DL_LOCATION_HINT, STATE_CODES

router = APIRouter(prefix="/onboard", tags=["onboarding"])
settings = get_settings()

_normalizer    = DLNormalizer()
_ocr           = ocr_service
_state_manager = state_manager
_orchestrator  = orchestrator
_store         = customer_store

import asyncio


# ── Step 1: Validate DL number ─────────────────────────────────────────────────

class DLValidateRequest(BaseModel):
    dl_number: str
    state_code: str = ""

@router.post("/validate-dl")
async def validate_dl(req: DLValidateRequest):
    """
    Customer types their DL number in any format.
    Returns normalised form + state info + any error with hints.
    """
    result = _normalizer.normalize(req.dl_number)
    return {
        **result,
        "display":         _normalizer.format_for_display(result["normalized"]) if result["valid"] else "",
        "format_hint":     DL_FORMAT_HINT,
        "location_hint":   DL_LOCATION_HINT,
    }


# ── Step 2: OCR fallback — upload DL photo to extract number ───────────────────

_MAX_UPLOAD_BYTES = 8 * 1024 * 1024              # 8 MB
_ACCEPTED_UPLOAD_TYPES = {
    "image/jpeg", "image/jpg", "image/png", "image/heic", "image/webp",
    "application/pdf",
}

_UPLOAD_REJECTION_COPY = {
    "invalid_dl_number": (
        "Check the licence number",
        "We read a licence number, but it does not match the expected format. Please check it before continuing.",
    ),
    "missing_required": (
        "Some details were not readable",
        "We need the DL number and date of birth. You can retake the photo or type them.",
    ),
    "unreadable": (
        "Upload a clearer photo",
        "We could not read the licence clearly. Try a well-lit, uncropped photo.",
    ),
    "not_dl": (
        "Upload a driving licence photo",
        "That image does not look like a driving licence. Please upload the front side of your DL.",
    ),
    "wrong_side": (
        "Upload the front side",
        "We need the side that shows your DL number and date of birth.",
    ),
    "screenshot": (
        "Upload the document photo",
        "This looks like a screen capture. Please upload a clear photo of the physical licence.",
    ),
    "low_confidence": (
        "Check the uploaded photo",
        "The details were not clear enough to use automatically. Please retake the photo or type them.",
    ),
    "unsupported": (
        "Upload a supported document",
        "Please upload a clear photo or PDF of your Indian driving licence.",
    ),
    "parse_error": (
        "We could not read this upload",
        "Please try again with a clearer photo or continue by typing your details.",
    ),
    "model_error": (
        "We could not read this upload",
        "Please try again with a clearer photo or continue by typing your details.",
    ),
}


def _safe_filename(name: str) -> str:
    """Strip path separators and dangerous characters; preserve the extension."""
    import re as _re
    name = (name or "upload").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return _re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-120:] or "upload"


def _upload_rejection_copy(reason: str) -> tuple[str, str]:
    return _UPLOAD_REJECTION_COPY.get(
        reason,
        (
            "We could not read this upload",
            "Please try again with a clearer photo or continue by typing your details.",
        ),
    )


def _unique_labels(values: list) -> list[str]:
    seen = set()
    labels = []
    for value in values or []:
        label = str(value or "").strip()
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def _required_upload_missing_labels(values: list) -> list[str]:
    labels = []
    for value in values or []:
        label = _required_upload_missing_label(value)
        if not label:
            continue
        if label not in labels:
            labels.append(label)
    return labels


def _required_upload_missing_label(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", " ")
    if not text:
        return ""
    if ("dl" in text or "licence" in text or "license" in text) and "number" in text:
        return "DL number"
    if text in {"dob", "date of birth", "birth date"} or (
        "date" in text and "birth" in text
    ):
        return "date of birth"
    return ""


@router.post("/extract-dl-image")
async def extract_dl_image(file: UploadFile = File(...), attempt: int = Form(1)):
    """
    Customer uploads a photo of their DL.
    We OCR it, extract dl_number and other fields.
    Returns pre-filled form data for the confirmation screen.
    """
    # Reject obviously-wrong content types early
    if file.content_type and file.content_type.lower() not in _ACCEPTED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {file.content_type}. "
                   "Please upload a JPG, PNG, HEIC, WEBP, or PDF.",
        )

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(data) / (1024*1024):.1f} MB). "
                   f"Maximum is {_MAX_UPLOAD_BYTES / (1024*1024):.0f} MB.",
        )
    if not data:
        raise HTTPException(status_code=400, detail="Empty file.")

    upload_dir = Path("./uploads")
    upload_dir.mkdir(exist_ok=True)
    safe_name = _safe_filename(file.filename)
    file_path = str(upload_dir / f"dl_{safe_name}")

    # Write to disk + push to S3 in one step. Disk write is always durable
    # for the in-process OCR call below; S3 URL goes onto the response and is
    # later persisted with the Document record by /onboard/confirm-and-start.
    from tools.storage import get_storage
    storage_result = await get_storage().put_bytes(
        local_path=file_path,
        data=data,
        kind="dl_upload",
        content_type=file.content_type or "image/jpeg",
    )

    assessment = await _ocr.classify_and_extract_driving_license(file_path)
    extracted = assessment.get("extracted") or {}

    # Normalise the DL number if OCR found one
    dl_raw = extracted.get("dl_number", "")
    normalised = _normalizer.normalize(dl_raw) if dl_raw else {"valid": False}
    raw_missing_fields = _unique_labels(assessment.get("missing_fields") or [])
    missing_fields = _required_upload_missing_labels(raw_missing_fields)
    optional_missing_fields = _unique_labels(
        [
            item for item in raw_missing_fields if not _required_upload_missing_label(item)
        ]
        + (assessment.get("optional_missing_fields") or [])
    )
    is_driving_license = bool(assessment.get("is_driving_license"))
    if is_driving_license:
        missing_fields = _unique_labels(
            missing_fields
            + [
                label for label, key in [
                    ("DL number", "dl_number"),
                    ("date of birth", "dob"),
                ]
                if not extracted.get(key)
            ]
        )

    confidence = assessment.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(0.99, confidence))

    rejection_reason = (assessment.get("rejection_reason") or "").strip()
    if assessment.get("accepted") and not normalised.get("valid"):
        rejection_reason = "invalid_dl_number"
    elif rejection_reason == "missing_required" and not missing_fields and normalised.get("valid"):
        rejection_reason = ""
    elif not rejection_reason and (missing_fields or not normalised.get("valid")):
        rejection_reason = "missing_required"
    elif not rejection_reason and not extracted:
        rejection_reason = "unreadable"

    rejection_title = assessment.get("rejection_title") or ""
    rejection_message = assessment.get("rejection_message") or ""
    if rejection_reason:
        rejection_title, rejection_message = _upload_rejection_copy(rejection_reason)

    needs_manual_review = bool(rejection_reason) or bool(missing_fields) or not normalised.get("valid")
    ocr_success = is_driving_license and not needs_manual_review

    retake_attempt = max(1, int(attempt or 1))
    retake_budget = max(0, int(getattr(settings, "ocr_retake_budget", 2)))
    retakes_remaining = 0 if ocr_success else max(0, retake_budget - max(0, retake_attempt - 1))
    response_extracted = extracted
    extracted = response_extracted if ocr_success else {}

    return {
        "ocr_success":    ocr_success,
        "extracted":      response_extracted,
        "dl_normalised":  normalised,
        "display":        _normalizer.format_for_display(normalised.get("normalized", "")) if normalised.get("valid") else "",
        "confidence":     round(confidence, 2),
        "missing_fields": missing_fields,
        "optional_missing_fields": optional_missing_fields,
        "needs_manual_review": needs_manual_review,
        "is_driving_license": is_driving_license,
        "document_type": assessment.get("document_type", "unknown"),
        "image_quality": assessment.get("image_quality", "unknown"),
        "rejection_reason": rejection_reason,
        "rejection_title": rejection_title,
        "rejection_message": rejection_message,
        "retake_attempt": retake_attempt,
        "retake_budget": retake_budget,
        "retakes_remaining": retakes_remaining,
        "can_retake": bool(not ocr_success and retakes_remaining > 0),
        # Path passed through to /onboard/confirm-and-start so we can persist
        # a Document record once we know the customer_id.
        "dl_image_path":  file_path,
        "dl_image_s3_url": storage_result.s3_url or "",
        "dl_image_s3_key": storage_result.s3_key or "",
        "storage_backend": storage_result.backend,
        "message":        "Details extracted from your DL image" if extracted else "Could not read DL — please enter details manually",
    }


# ── Step 3: Confirm details + start job ────────────────────────────────────────

class ConfirmAndStartRequest(BaseModel):
    # What the customer confirmed
    dl_number:     str
    dob:           str            # DD-MM-YYYY
    mobile_number: str
    name:          str = ""
    email:         str = ""       # optional
    address:       str = ""
    pin_code:      str = ""
    blood_group:   str = ""
    gender:        str = ""
    state_code:    str = ""
    rto_code:      str = ""

    # Document paths (set by server after upload)
    photo_path:     str = ""
    signature_path: str = ""
    dl_image_path:  str = ""             # returned by /onboard/extract-dl-image
    dl_image_s3_url: str = ""            # also returned by /onboard/extract-dl-image
    dl_image_s3_key: str = ""
    ocr_data:       dict = {}            # what the OCR step extracted
    ocr_confidence: float = 0.0

    service: str = "DL_RENEWAL"

@router.post("/confirm-and-start")
async def confirm_and_start(req: ConfirmAndStartRequest):
    """
    Customer has reviewed their details and clicked Confirm.
    Normalise DL number, create job, start agent in background.
    """
    # Normalise DL number
    dl_result = _normalizer.normalize(req.dl_number)
    if not dl_result["valid"]:
        raise HTTPException(status_code=400, detail=dl_result["error"])

    # State is derived from the DL number whenever it can be. The client only
    # sends an explicit state_code when the customer picked one because we
    # could not detect it (or to override) — so trust the client's value if
    # present, otherwise fall back to the DL-derived state. An empty result
    # means we genuinely could not detect a state and the customer must choose.
    requested_state_code = (req.state_code or "").strip().upper()
    dl_state_code = (dl_result.get("state_code") or "").strip().upper()
    selected_state_code = requested_state_code or dl_state_code
    if not selected_state_code:
        raise HTTPException(status_code=400, detail="Please select the filing state before starting.")
    selected_state_name = STATE_CODES.get(
        selected_state_code,
        {"TG": "Telangana"}.get(selected_state_code, selected_state_code),
    )

    customer_data = {
        "dl_number":     dl_result["normalized"],
        "dob":           req.dob,
        "name":          req.name,
        "mobile_number": req.mobile_number,
        "email":         req.email,
        "address":       req.address,
        "pin_code":      req.pin_code,
        "blood_group":   req.blood_group,
        "gender":        req.gender,
        "state_code":    selected_state_code,
        "state_name":    selected_state_name,
        "rto_code":      req.rto_code or dl_result["rto_code"],
    }

    documents = {}
    if req.photo_path:
        documents["photo"] = req.photo_path
    if req.signature_path:
        documents["signature"] = req.signature_path

    job = _state_manager.new_job(
        customer_id   = req.mobile_number,    # mobile as customer ID
        service       = req.service,
        state_code    = customer_data["state_code"],
        customer_data = customer_data,
        documents     = documents,
    )
    await _state_manager.save(job)

    # Persist durable customer + application records so the operator
    # dashboard sees this customer and the agent can write status updates.
    cust = await _store.upsert_customer(
        phone=req.mobile_number, name=req.name or "", email=req.email or "",
    )
    fee_inr = get_fee(req.service.lower().replace("_", " "), customer_data["state_code"])
    application = await _store.create_application(
        customer_id    = cust.customer_id,
        service_type   = req.service,
        state_code     = customer_data["state_code"],
        fee_inr        = fee_inr,
        current_job_id = job.job_id,
        metadata       = {
            "dl_number":  customer_data["dl_number"],
            "dob":        customer_data["dob"],
            "pin_code":   customer_data.get("pin_code", ""),
            "rto_code":   customer_data.get("rto_code", ""),
            "state_name": customer_data.get("state_name", ""),
        },
    )

    # Persist the OCR'd DL image as a Document if we have one. ocr_data gets
    # a non-destructive `s3_url`/`s3_key` pair so the operator UI can deep-link
    # to the durable copy even after the container disk is wiped on redeploy.
    if req.dl_image_path:
        try:
            doc_ocr_data = dict(req.ocr_data or {})
            if req.dl_image_s3_url:
                doc_ocr_data["s3_url"] = req.dl_image_s3_url
            if req.dl_image_s3_key:
                doc_ocr_data["s3_key"] = req.dl_image_s3_key
            await _store.add_document(
                customer_id=cust.customer_id,
                app_id=application.app_id,
                doc_type="driving_license",
                file_path=req.dl_image_path,
                mime_type="image/jpeg",
                ocr_data=doc_ocr_data,
                confidence=req.ocr_confidence or 0.0,
            )
        except Exception as e:  # don't fail the application start if doc record fails
            import structlog
            structlog.get_logger(__name__).warning(
                "onboard.add_document_failed",
                error=str(e), app_id=application.app_id,
            )

    # Start agent in background
    asyncio.create_task(_orchestrator.run_job(job.job_id))

    return {
        "job_id":          job.job_id,
        "app_id":          application.app_id,
        "customer_id":     cust.customer_id,
        "fee_inr":         fee_inr,
        "status":          job.status.value,
        "dl_display":      _normalizer.format_for_display(dl_result["normalized"]),
        "customer_summary": {
            "name":    req.name or "—",
            "mobile":  req.mobile_number,
            "dl":      _normalizer.format_for_display(dl_result["normalized"]),
            "dob":     req.dob,
            "state":   selected_state_name,
        },
        "message": "Your application has started. We'll notify you when we need your OTP.",
    }


# ── Document upload endpoints ──────────────────────────────────────────────────

@router.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...)):
    from tools.image_processor import ImageProcessor
    upload_dir = Path("./uploads")
    upload_dir.mkdir(exist_ok=True)
    raw_path = str(upload_dir / f"photo_{file.filename}")
    with open(raw_path, "wb") as f:
        f.write(await file.read())
    compressed = ImageProcessor().compress_photo(raw_path)
    return {"path": compressed, "message": "Photo uploaded and compressed"}


@router.post("/upload-signature")
async def upload_signature(file: UploadFile = File(...)):
    from tools.image_processor import ImageProcessor
    upload_dir = Path("./uploads")
    upload_dir.mkdir(exist_ok=True)
    raw_path = str(upload_dir / f"sig_{file.filename}")
    with open(raw_path, "wb") as f:
        f.write(await file.read())
    compressed = ImageProcessor().compress_signature(raw_path)
    return {"path": compressed, "message": "Signature uploaded and compressed"}
