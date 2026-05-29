"""Central structlog setup with redaction.

Redacts API keys, OTPs, mobile/DL/Aadhaar numbers, and any value whose
log key matches a known sensitive name. Imported by api/server.py and
run_agent.py so the same rules apply to API logs and agent logs.
"""

import logging
import re
import sys

import structlog


_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),         "sk-ant-***"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"),             "sk-***"),
    (re.compile(r"\bAKIA[A-Z0-9]{16}\b"),              "AKIA***"),
    (re.compile(r"\b(?i:bearer)\s+[A-Za-z0-9._-]+"),   "Bearer ***"),
    (re.compile(r"\b\d{12}\b"),                        "************"),   # Aadhaar
    (re.compile(r"\b\d{10}\b"),                        "**********"),     # mobile
    (re.compile(r"(?<!\d)\d{6}(?!\d)"),                "******"),         # OTP / PIN-like
]

_SENSITIVE_KEYS: set[str] = {
    "api_key", "openai_api_key", "anthropic_api_key",
    "api_secret_key", "x_secret", "secret", "password",
    "token", "access_token", "refresh_token",
    "otp", "captcha", "captcha_text",
    "dl_number", "aadhaar_number", "aadhaar",
    "mobile_number", "mobile", "phone", "dob",
    "aws_access_key_id", "aws_secret_access_key",
}


def _mask_secret(value: str) -> str:
    if len(value) <= 4:
        return "***"
    return f"{value[:2]}***{value[-2:]}"


def _redact_value(value):
    if isinstance(value, str):
        out = value
        for pat, repl in _PATTERNS:
            out = pat.sub(repl, out)
        return out
    if isinstance(value, dict):
        return {k: _redact_pair(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        cls = type(value)
        return cls(_redact_value(v) for v in value)
    return value


def _redact_pair(key, value):
    if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS:
        if isinstance(value, str):
            return _mask_secret(value)
        return "***"
    return _redact_value(value)


def redaction_processor(_logger, _method, event_dict):
    return {k: _redact_pair(k, v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotent. Safe to call from multiple entrypoints."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(stream=sys.stdout, level=log_level, format="%(message)s")

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        processors=[
            structlog.processors.add_log_level,
            # Redact BEFORE the timestamp is added so the 6-digit pattern
            # cannot match microsecond fields.
            redaction_processor,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
