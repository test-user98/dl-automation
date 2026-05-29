"""LLM-backed portal state triage.

This module interprets unfamiliar Sarathi page states, but it does not write
customer-facing copy. It returns a small enum payload that deterministic
status templates can safely consume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from agent.llm_client import get_llm_client
from config.settings import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()


ISSUE_TYPES = {
    "normal_progress",
    "portal_slow",
    "retryable_portal_error",
    "validation_rejected",
    "missing_customer_data",
    "otp_required",
    "captcha_required",
    "service_unavailable_for_rto",
    "payment_pending",
    "unknown",
}

NEXT_ACTIONS = {
    "continue",
    "retry_same_step",
    "restart_portal_session",
    "refill_required_fields",
    "ask_customer",
    "wait_for_otp",
    "solve_captcha",
    "stop_terminal",
    "unknown",
}

FIELD_KEYS = {
    "",
    "dl_number",
    "dob",
    "mobile_number",
    "pin_code",
    "address",
    "name",
    "email",
    "state_code",
    "rto_code",
    "otp",
}


class PortalTriageService:
    """Classify hard portal states into safe enums for status sync."""

    def __init__(self) -> None:
        self._llm = None

    async def classify(
        self,
        *,
        screenshot: bytes,
        page_text: str,
        url: str,
        current_step: str,
        dom_elements: dict | None = None,
        failure_context: str = "",
        last_action: str = "",
        retry_count: int = 0,
    ) -> dict:
        if settings.portal_triage_mode == "off":
            return {}

        reasoning_field = (
            '"reasoning_summary": "one sentence with visible evidence, no hidden chain-of-thought",'
            if settings.portal_triage_reasoning_mode == "summary"
            else '"reasoning_summary": "",'
        )
        system_prompt = (
            "You classify Sarathi portal states for an automation system. "
            "Return only strict JSON. Do not write customer-facing copy. "
            "Do not include hidden chain-of-thought; use a short evidence summary only."
        )
        user_text = f"""Classify this portal state.

Allowed issue_type values:
{sorted(ISSUE_TYPES)}

Allowed recommended_next_action values:
{sorted(NEXT_ACTIONS)}

Allowed field_key values:
{sorted(FIELD_KEYS)}

Current step: {current_step}
URL: {url}
Retry count for this step: {retry_count}
Last action/failure:
{last_action[:800]}

Failure context:
{failure_context[:1200]}

Page text:
{page_text[:1800]}

DOM summary:
{self._summarise_dom(dom_elements or {})}

Return exactly:
{{
  "issue_type": "unknown",
  "customer_action_required": false,
  "field_key": "",
  "recommended_next_action": "unknown",
  "confidence": 0.0,
  "internal_diagnosis": "one short technical sentence",
  {reasoning_field}
  "evidence": ["short visible signal 1", "short visible signal 2"]
}}

Rules:
- Use missing_customer_data only when the page clearly asks for data not already available.
- Use validation_rejected when Sarathi rejected submitted fields or reset a form.
- Use retryable_portal_error or portal_slow for gateway, timeout, blocked, or unavailable states.
- Use service_unavailable_for_rto only when Sarathi says the requested service is not available for the RTO.
- Use unknown with confidence below 0.60 when the page state is ambiguous.
"""
        try:
            llm = self._llm or get_llm_client()
            self._llm = llm
            raw = await llm.vision(screenshot, system_prompt, user_text)
            parsed = self._parse_json_object(raw)
            triage = self.normalise(parsed, current_step=current_step, url=url)
            log.info(
                "portal_triage.classified",
                issue_type=triage.get("issue_type"),
                confidence=triage.get("confidence"),
                step=current_step,
            )
            return triage
        except Exception as e:  # noqa: BLE001
            log.warning("portal_triage.failed", error=str(e), step=current_step)
            return {}

    @staticmethod
    def normalise(data: dict, *, current_step: str = "", url: str = "") -> dict:
        if not isinstance(data, dict):
            data = {}

        issue_type = str(data.get("issue_type") or "unknown").strip().lower()
        if issue_type not in ISSUE_TYPES:
            issue_type = "unknown"

        next_action = str(data.get("recommended_next_action") or "unknown").strip().lower()
        if next_action not in NEXT_ACTIONS:
            next_action = "unknown"

        field_key = str(data.get("field_key") or "").strip().lower()
        if field_key not in FIELD_KEYS:
            field_key = ""

        confidence = PortalTriageService._normalise_confidence(data.get("confidence"))
        evidence = data.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]

        return {
            "source": "llm_portal_triage",
            "issue_type": issue_type,
            "customer_action_required": PortalTriageService._as_bool(
                data.get("customer_action_required")
            ),
            "field_key": field_key,
            "recommended_next_action": next_action,
            "confidence": round(confidence, 2),
            "internal_diagnosis": str(data.get("internal_diagnosis") or "")[:500],
            "reasoning_summary": str(data.get("reasoning_summary") or "")[:500],
            "evidence": [str(item)[:180] for item in evidence[:4]],
            "step_name": current_step,
            "url": url[:500],
            "at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _summarise_dom(dom_elements: dict) -> str:
        parts: list[str] = []
        for key in ("inputs", "selects", "buttons", "links"):
            items = dom_elements.get(key) or []
            summary = []
            for item in items[:10]:
                if key == "inputs":
                    summary.append(
                        {
                            "selector": item.get("selector", ""),
                            "type": item.get("type", ""),
                            "name": item.get("name", ""),
                            "value": item.get("value", ""),
                            "visible": item.get("visible", True),
                            "disabled": item.get("disabled", False),
                        }
                    )
                elif key == "selects":
                    summary.append(
                        {
                            "selector": item.get("selector", ""),
                            "selected": item.get("selected_text", ""),
                            "visible": item.get("visible", True),
                        }
                    )
                else:
                    summary.append(
                        {
                            "text": item.get("text", ""),
                            "selector": item.get("selector", ""),
                            "visible": item.get("visible", True),
                            "disabled": item.get("disabled", False),
                        }
                    )
            parts.append(f"{key}={json.dumps(summary, ensure_ascii=True)}")
        return "\n".join(parts)

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
    def _normalise_confidence(value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            return 0.0
        if confidence > 1 and confidence <= 100:
            confidence = confidence / 100
        return max(0.0, min(0.99, confidence))

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "y"}
