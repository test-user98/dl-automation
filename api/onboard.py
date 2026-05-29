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
from tools.dl_normalizer import DLNormalizer, DL_FORMAT_HINT, DL_LOCATION_HINT

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

@router.post("/extract-dl-image")
async def extract_dl_image(file: UploadFile = File(...)):
    """
    Customer uploads a photo of their DL.
    We OCR it, extract dl_number and other fields.
    Returns pre-filled form data for the confirmation screen.
    """
    upload_dir = Path("./uploads")
    upload_dir.mkdir(exist_ok=True)
    file_path = str(upload_dir / f"dl_{file.filename}")
    with open(file_path, "wb") as f:
        f.write(await file.read())

    extracted = {}
    for attempt in range(1, settings.ocr_max_attempts + 1):
        extracted = await _ocr.extract_driving_license(file_path)
        if extracted.get("dl_number") or extracted.get("dob"):
            break

    # Normalise the DL number if OCR found one
    dl_raw = extracted.get("dl_number", "")
    normalised = _normalizer.normalize(dl_raw) if dl_raw else {"valid": False}
    missing_fields = [
        label for label, key in [
            ("DL number", "dl_number"),
            ("date of birth", "dob"),
        ]
        if not extracted.get(key)
    ]
    confidence = 0.95
    if missing_fields:
        confidence -= 0.3 * len(missing_fields)
    if not normalised.get("valid"):
        confidence -= 0.25
    confidence = max(0.0, min(0.99, confidence))
    needs_manual_review = bool(missing_fields) or not normalised.get("valid")

    return {
        "ocr_success":    bool(extracted) and not needs_manual_review,
        "extracted":      extracted,
        "dl_normalised":  normalised,
        "display":        _normalizer.format_for_display(normalised.get("normalized", "")) if normalised.get("valid") else "",
        "confidence":     round(confidence, 2),
        "missing_fields": missing_fields,
        "needs_manual_review": needs_manual_review,
        # Path passed through to /onboard/confirm-and-start so we can persist
        # a Document record once we know the customer_id.
        "dl_image_path":  file_path,
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
    state_code:    str = "RJ"
    rto_code:      str = ""

    # Document paths (set by server after upload)
    photo_path:     str = ""
    signature_path: str = ""
    dl_image_path:  str = ""             # returned by /onboard/extract-dl-image
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
        "state_code":    dl_result["state_code"] or req.state_code,
        "state_name":    dl_result["state_name"],
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

    # Persist the OCR'd DL image as a Document if we have one.
    if req.dl_image_path:
        try:
            await _store.add_document(
                customer_id=cust.customer_id,
                app_id=application.app_id,
                doc_type="driving_license",
                file_path=req.dl_image_path,
                mime_type="image/jpeg",
                ocr_data=req.ocr_data or {},
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
            "state":   dl_result["state_name"],
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
