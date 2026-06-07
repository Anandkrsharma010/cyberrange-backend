"""Best-effort workshop invite email delivery (log, SMTP, or disabled)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

from backend.config import get_settings

log = logging.getLogger("email_delivery")


async def send_workshop_invite_email(
    *,
    to_addr: str,
    workshop_title: str,
    invite_url: str,
) -> tuple[bool, str | None]:
    """
    Returns (ok, error_message). Never raises — callers record last_email_error.
    """
    settings = get_settings()
    subject = f"You're invited — {workshop_title}"
    body = (
        f"You have been invited to the workshop cohort: {workshop_title}\n\n"
        f"Open this link to review the invitation and sign in with the same email "
        f"this message was sent to:\n{invite_url}\n\n"
        f"If you did not expect this message, you can ignore it.\n"
    )

    backend = (settings.EMAIL_BACKEND or "log").strip().lower()
    if backend == "none":
        log.info(
            "EMAIL_BACKEND=none: skipping send for workshop invite to=%s url=%s",
            to_addr,
            invite_url,
        )
        return True, None

    if backend == "log":
        log.info(
            "WORKSHOP INVITE EMAIL (log backend)\n  To: %s\n  Subject: %s\n  URL: %s",
            to_addr,
            subject,
            invite_url,
        )
        return True, None

    if backend != "smtp":
        return False, f"Unknown EMAIL_BACKEND={backend!r}"

    if not settings.SMTP_HOST or not settings.SMTP_FROM:
        return False, "SMTP_HOST or SMTP_FROM not configured"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)

    def _send_sync() -> None:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(_send_sync)
        return True, None
    except Exception as exc:
        log.warning("SMTP send failed for workshop invite to=%s: %s", to_addr, exc)
        return False, str(exc)


async def send_aws_verification_code_email(
    *,
    to_addr: str,
    code: str,
) -> tuple[bool, str | None]:
    """
    Returns (ok, error_message).
    """
    settings = get_settings()
    subject = "Your AWS Security Labs Access Code"
    body = (
        f"Here is your temporary access code for AWS Security Labs:\n\n"
        f"Code: {code}\n\n"
        f"This code will expire in 5 minutes. Enter this code on your dashboard to unlock access.\n"
    )

    backend = (settings.EMAIL_BACKEND or "log").strip().lower()
    if backend == "none":
        log.info(
            "EMAIL_BACKEND=none: skipping send for AWS verification code to=%s",
            to_addr,
        )
        return True, None

    if backend == "log":
        log.info(
            "AWS VERIFICATION EMAIL (log backend)\n  To: %s\n  Subject: %s\n  Code: %s",
            to_addr,
            subject,
            code,
        )
        return True, None

    if backend != "smtp":
        return False, f"Unknown EMAIL_BACKEND={backend!r}"

    if not settings.SMTP_HOST or not settings.SMTP_FROM:
        return False, "SMTP_HOST or SMTP_FROM not configured"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)

    def _send_sync() -> None:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
            if settings.SMTP_USE_TLS:
                smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(msg)

    try:
        await asyncio.to_thread(_send_sync)
        return True, None
    except Exception as exc:
        log.warning("SMTP send failed for AWS verification to=%s: %s", to_addr, exc)
        return False, str(exc)

