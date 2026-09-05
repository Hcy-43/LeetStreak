"""Sending mail.

Two backends. **SMTP** is the default and what the README sets up, using a Gmail App
Password: no domain to buy, no signup, and it delivers to anyone. **Resend** is there
for later, when you own a domain and want proper transactional deliverability.

With neither configured we log the message and warn loudly. That keeps the test suite
and a first `uv run` working, but it means nobody receives their code.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import Settings

log = logging.getLogger("leetstreak.mail")


class MailError(RuntimeError):
    pass


def _build(settings: Settings, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def _send_smtp(settings: Settings, message: EmailMessage) -> None:
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
        with server:
            server.ehlo()
            if settings.smtp_starttls and settings.smtp_port != 465:
                server.starttls()
                server.ehlo()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"Could not send the email: {exc}") from exc


async def _send_resend(settings: Settings, to: str, subject: str, body: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                json={"from": settings.mail_from, "to": [to], "subject": subject, "text": body},
            )
    except httpx.HTTPError as exc:
        raise MailError(f"Could not reach Resend: {exc}") from exc

    if response.status_code >= 400:
        # Almost always an unverified sending domain; say so rather than "HTTP 403".
        raise MailError(f"Resend refused the message (HTTP {response.status_code}).")


async def send(settings: Settings, to: str, subject: str, body: str) -> None:
    """Deliver a message. Raises MailError if a configured provider refuses it."""
    backend = settings.mail_backend

    if backend == "console":
        log.warning(
            "No mail provider configured, so this email was NOT sent. To: %s\nSubject: %s\n%s",
            to,
            subject,
            body,
        )
        return

    if backend == "resend":
        await _send_resend(settings, to, subject, body)
        return

    # smtplib is blocking, and this runs inside the request.
    await asyncio.to_thread(_send_smtp, settings, _build(settings, to, subject, body))


def verification_body(code: str, minutes: int) -> str:
    return (
        f"Your LeetStreak verification code is:\n\n"
        f"    {code}\n\n"
        f"It expires in {minutes} minutes. If you did not try to sign up, you can ignore\n"
        f"this email - nobody can use this code without your inbox.\n"
    )
