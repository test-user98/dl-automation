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
from typing import Optional

from config.settings import get_settings
from agent.llm_client import get_llm_client

log = structlog.get_logger(__name__)
settings = get_settings()


class OCRService:
    def __init__(self):
        self._llm = None

    async def extract_driving_license(self, image_path: str) -> dict:
        """
        Extract structured data from a driving license image.
        Returns dict with: dl_number, name, dob, address, vehicle_classes,
                           issue_date, expiry_date, blood_group, state_code
        """
        prompt = """
        This is an Indian Driving License. Extract ALL visible information and return
        a JSON object with these exact keys (use empty string if not visible):

        {
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
        }

        Return ONLY the JSON, no explanation.
        """
        return await self._extract(image_path, prompt, "driving_license")

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

    async def _extract(self, image_path: str, prompt: str, doc_type: str) -> dict:
        try:
            image_bytes = Path(image_path).read_bytes()
            llm = self._llm or get_llm_client()
            self._llm = llm
            raw = await llm.vision(
                image_bytes,
                "You extract structured data from Indian identity documents. Return only valid JSON.",
                prompt,
            )
            # Strip markdown code blocks if model wraps JSON in them
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            data = json.loads(raw)
            log.info("ocr.extracted", doc_type=doc_type, fields=list(data.keys()))
            return data

        except json.JSONDecodeError as e:
            log.error("ocr.json_parse_failed", doc_type=doc_type, error=str(e))
            return {}
        except Exception as e:
            log.error("ocr.failed", doc_type=doc_type, error=str(e))
            return {}

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
