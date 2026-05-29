"""
OCR service — extracts structured data from uploaded documents
(Aadhaar, Driving License, address proof) using Claude vision.

Output is a normalised dict that feeds directly into the agent's
customer_data, which it uses to fill Sarathi forms.
"""

import base64
import json
import structlog
from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings
from agent.llm_client import get_llm_client

log = structlog.get_logger(__name__)
settings = get_settings()

DL_EXTRACTED_FIELDS = (
    "dl_number",
    "name",
    "dob",
    "father_or_husband_name",
    "address",
    "pin_code",
    "state",
    "state_code",
    "rto_code",
    "issue_date",
    "expiry_date",
    "vehicle_classes",
    "blood_group",
    "gender",
    "badge_number",
)

DL_REQUIRED_FIELDS = {
    "dl_number": "DL number",
    "dob": "date of birth",
}

DL_REJECTION_COPY = {
    "not_dl": (
        "Upload a driving licence photo",
        "That image does not look like a driving licence. Please upload the front side of your DL.",
    ),
    "unreadable": (
        "Upload a clearer photo",
        "We could not read the licence clearly. Try a well-lit, uncropped photo.",
    ),
    "wrong_side": (
        "Upload the front side",
        "We need the side that shows your DL number and date of birth.",
    ),
    "screenshot": (
        "Upload the document photo",
        "This looks like a screen capture. Please upload a clear photo of the physical licence.",
    ),
    "missing_required": (
        "Some details were not readable",
        "We need the DL number and date of birth. You can retake the photo or type them.",
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


class OCRService:
    def __init__(self):
        self._llm = None

    async def extract_driving_license(self, image_path: str) -> dict:
        """
        Extract structured data from a driving license image.
        Returns dict with: dl_number, name, dob, address, vehicle_classes,
                           issue_date, expiry_date, blood_group, state_code
        """
        assessment = await self.classify_and_extract_driving_license(image_path)
        return assessment.get("extracted", {})

    async def classify_and_extract_driving_license(self, image_path: str) -> dict:
        """
        Single vision pass for customer DL uploads.

        The model must classify the upload and extract visible fields in the
        same response so callers can decide whether to accept, ask for a
        retake, or continue with manual entry.
        """
        prompt = """
        Inspect this customer upload for an Indian driving licence renewal flow.
        In one pass:
        1. Classify what the image/PDF appears to be.
        2. Decide if it is acceptable for automatic DL data capture.
        3. Extract every visible DL field.

        Return ONLY one valid JSON object with this exact schema:

        {
          "is_driving_license": false,
          "document_type": "driving_license | aadhaar | pan | passport | vehicle_rc | other_document | not_a_document | unknown",
          "image_quality": "clear | blurry | dark | glare | cropped | too_small | screenshot | unknown",
          "confidence": 0.0,
          "rejection_reason": "not_dl | unreadable | wrong_side | screenshot | missing_required | low_confidence | unsupported | ",
          "extracted": {
            "dl_number": "",
            "name": "",
            "dob": "DD-MM-YYYY format",
            "father_or_husband_name": "",
            "address": "",
            "pin_code": "",
            "state": "",
            "state_code": "",
            "rto_code": "",
            "issue_date": "DD-MM-YYYY format",
            "expiry_date": "DD-MM-YYYY format",
            "vehicle_classes": ["LMV", "MCWG"],
            "blood_group": "",
            "gender": "M or F",
            "badge_number": ""
          },
          "missing_fields": [],
          "optional_missing_fields": [],
          "notes": ""
        }

        Rules:
        - Set is_driving_license true only for an Indian driving licence or a clearly visible DL PDF.
        - If this is not a DL, set rejection_reason to not_dl and keep extracted values empty.
        - If it looks like a screen capture rather than the document/photo/PDF itself, use screenshot.
        - If the back side is shown and DL number or DOB is absent, use wrong_side.
        - If the image is too blurry, cropped, dark, tiny, or glared to read required fields, use unreadable.
        - A DL upload is acceptable when both dl_number and dob are readable.
        - missing_fields may contain ONLY dl_number or dob. Do not include optional
          fields like address, pin_code, vehicle_classes, gender, or badge_number.
        - Put unread optional fields in optional_missing_fields.
        - If a DL is visible but dl_number or dob is missing, use missing_required.
        - Use low_confidence when fields are guessed instead of clearly read.
        - Leave rejection_reason empty only when the DL is acceptable.
        - Do not invent values. Use empty strings for fields that are not visible.
        """
        data = await self._extract(
            image_path,
            prompt,
            "driving_license_classify_extract",
            system_prompt=(
                "You are a strict document intake checker for an Indian driving "
                "licence renewal service. Return only valid JSON."
            ),
        )
        return self._normalise_dl_assessment(data)

    async def extract_aadhaar(self, image_path: str) -> dict:
        """
        Extract structured data from an Aadhaar card (front or back).
        Returns dict with: aadhaar_number (last 4 visible), name, dob,
                           gender, address, pin_code
        """
        prompt = """
        This is an Indian Aadhaar card. Extract ALL visible information and return
        a JSON object with these exact keys (use empty string if not visible):

        {
          "aadhaar_number": "only last 4 digits — XXXX XXXX 1234 format",
          "name": "",
          "dob": "DD-MM-YYYY format",
          "gender": "Male or Female",
          "address": "",
          "district": "",
          "state": "",
          "pin_code": "",
          "mobile_linked": ""
        }

        If the Aadhaar number is fully masked (XXXX XXXX XXXX), note it as masked.
        Return ONLY the JSON, no explanation.
        """
        return await self._extract(image_path, prompt, "aadhaar")

    async def extract_address_proof(self, image_path: str) -> dict:
        """
        Extract address from any address proof document.
        Returns dict with: address, district, state, pin_code, doc_type
        """
        prompt = """
        This is an Indian address proof document. Identify the document type and
        extract the address. Return a JSON object:

        {
          "doc_type": "Aadhaar / Voter ID / Passport / Utility Bill / Bank Statement",
          "name": "",
          "address": "full address as printed",
          "district": "",
          "state": "",
          "pin_code": ""
        }

        Return ONLY the JSON, no explanation.
        """
        return await self._extract(image_path, prompt, "address_proof")

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _extract(
        self,
        image_path: str,
        prompt: str,
        doc_type: str,
        system_prompt: str | None = None,
    ) -> dict:
        try:
            image_bytes = Path(image_path).read_bytes()
            llm = self._llm or get_llm_client()
            self._llm = llm
            raw = await llm.vision(
                image_bytes,
                system_prompt or "You extract structured data from Indian identity documents. Return only valid JSON.",
                prompt,
            )
            data = self._parse_json_object(raw)
            fields = list(data.keys()) if isinstance(data, dict) else []
            log.info("ocr.extracted", doc_type=doc_type, fields=fields)
            return data

        except json.JSONDecodeError as e:
            log.error("ocr.json_parse_failed", doc_type=doc_type, error=str(e))
            return {}
        except Exception as e:
            log.error("ocr.failed", doc_type=doc_type, error=str(e))
            return {}

    def _normalise_dl_assessment(self, data: dict) -> dict:
        if not isinstance(data, dict) or not data:
            return self._dl_failure("parse_error")

        extracted_raw = data.get("extracted")
        if not isinstance(extracted_raw, dict):
            extracted_raw = {key: data.get(key, "") for key in DL_EXTRACTED_FIELDS}
        extracted = self._ensure_dl_fields(extracted_raw)

        doc_type = str(data.get("document_type") or "").strip().lower()
        quality = str(data.get("image_quality") or data.get("quality") or "").strip().lower()
        is_dl = self._as_bool(data.get("is_driving_license"))
        if doc_type in {"driving_license", "driving_licence", "driver_license", "driver_licence", "dl"}:
            is_dl = True
        if extracted.get("dl_number"):
            is_dl = True

        confidence = self._normalise_confidence(data.get("confidence"))
        if confidence == 0.0 and any(v for v in extracted.values() if v):
            confidence = 0.75

        raw_missing_fields = self._as_list(data.get("missing_fields"))
        missing_fields = self._required_missing_labels(raw_missing_fields)
        optional_missing_fields = [
            item for item in raw_missing_fields if not self._required_missing_label(item)
        ]
        optional_missing_fields.extend(self._as_list(data.get("optional_missing_fields")))
        optional_missing_fields = list(dict.fromkeys(optional_missing_fields))

        for key, label in DL_REQUIRED_FIELDS.items():
            if not extracted.get(key) and label not in missing_fields:
                missing_fields.append(label)

        reason = str(data.get("rejection_reason") or "").strip().lower()
        if reason in {"none", "null", "n/a", "na"}:
            reason = ""
        if reason and reason not in DL_REJECTION_COPY:
            reason = "unsupported"

        if not is_dl:
            if quality == "screenshot":
                reason = "screenshot"
            elif not reason:
                reason = "unreadable" if doc_type in {"", "unknown", "not_a_document"} else "not_dl"
        elif reason == "missing_required" and not missing_fields:
            reason = ""
        elif not reason and missing_fields:
            reason = "missing_required"
        elif not reason and confidence < 0.5:
            reason = "low_confidence"

        title, message = DL_REJECTION_COPY.get(reason, ("", ""))
        return {
            "accepted": bool(is_dl and not reason),
            "is_driving_license": is_dl,
            "document_type": doc_type or "unknown",
            "image_quality": quality or "unknown",
            "confidence": round(confidence, 2),
            "rejection_reason": reason,
            "rejection_title": title,
            "rejection_message": message,
            "extracted": extracted,
            "missing_fields": missing_fields if is_dl else [],
            "optional_missing_fields": optional_missing_fields if is_dl else [],
            "needs_manual_review": bool(reason),
            "notes": str(data.get("notes") or "")[:500],
        }

    @staticmethod
    def _parse_json_object(raw: str) -> Any:
        text = (raw or "").strip()
        if text.startswith("```"):
            parts = text.split("```")
            if len(parts) >= 2:
                text = parts[1].strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
            raise

    @staticmethod
    def _ensure_dl_fields(extracted: dict) -> dict:
        clean = {}
        for field in DL_EXTRACTED_FIELDS:
            value = extracted.get(field, "")
            if field == "vehicle_classes":
                clean[field] = OCRService._as_list(value)
            elif value is None:
                clean[field] = ""
            else:
                clean[field] = str(value).strip()
        return clean

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, tuple):
            return [str(v).strip() for v in value if str(v).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]

    @staticmethod
    def _normalise_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        if confidence > 1 and confidence <= 100:
            confidence = confidence / 100
        return max(0.0, min(0.99, confidence))

    @staticmethod
    def _required_missing_labels(values: list[str]) -> list[str]:
        labels = []
        for value in values:
            label = OCRService._required_missing_label(value)
            if not label:
                continue
            if label not in labels:
                labels.append(label)
        return labels

    @staticmethod
    def _required_missing_label(value: str) -> str:
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

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y"}

    @staticmethod
    def _dl_failure(reason: str) -> dict:
        title, message = DL_REJECTION_COPY.get(reason, DL_REJECTION_COPY["parse_error"])
        return {
            "accepted": False,
            "is_driving_license": False,
            "document_type": "unknown",
            "image_quality": "unknown",
            "confidence": 0.0,
            "rejection_reason": reason,
            "rejection_title": title,
            "rejection_message": message,
            "extracted": OCRService._ensure_dl_fields({}),
            "missing_fields": [],
            "optional_missing_fields": [],
            "needs_manual_review": True,
            "notes": "",
        }

    @staticmethod
    def _load_image(image_path: str) -> tuple[str, str]:
        suffix = Path(image_path).suffix.lower()
        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".pdf": "application/pdf",
        }
        media_type = media_types.get(suffix, "image/jpeg")
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return b64, media_type

    @staticmethod
    def merge_customer_data(*extractions: dict) -> dict:
        """
        Merge OCR results from multiple documents into one customer profile.
        Later sources override earlier ones for the same key.
        """
        merged = {}
        for d in extractions:
            for k, v in d.items():
                if v:  # only override if non-empty
                    merged[k] = v
        return merged
