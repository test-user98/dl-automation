"""Optional SMTP email notifications for customer/RTO status updates."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage

import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)


async def send_email(to_email: str, subject: str, body: str) -> dict:
    """Send a plain-text email when SMTP is configured; otherwise no-op."""
    settings = get_settings()
    to_email = (to_email or "").strip()
    if not to_email:
        return {"sent": False, "reason": "missing_recipient"}
    if not settings.email_notifications_enabled:
        return {"sent": False, "reason": "disabled"}
    if not settings.smtp_host or not settings.smtp_username or not settings.smtp_password:
        return {"sent": False, "reason": "smtp_not_configured"}

    sender = settings.smtp_from or settings.smtp_username
    try:
        await asyncio.to_thread(_send_sync, sender, to_email, subject, body)
        log.info("email.sent", to=to_email, subject=subject)
        return {"sent": True}
    except Exception as e:
        log.warning("email.failed", to=to_email, error=str(e))
        return {"sent": False, "reason": str(e)}


def _send_sync(sender: str, to_email: str, subject: str, body: str) -> None:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
