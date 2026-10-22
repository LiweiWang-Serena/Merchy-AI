"""
Tells the team when a customer in the shop chat asks for a person.

Set either or both in backend/.env:
  SLACK_WEBHOOK_URL   a Slack "Incoming Webhook" URL (Slack > Apps > Incoming Webhooks)
  NOTIFY_EMAIL_TO     where to send the email, together with SMTP_HOST,
                      SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD and
                      optionally NOTIFY_EMAIL_FROM. Works with Gmail (use an
                      app password), Outlook, or an email service's SMTP.
  SITE_URL            the site's address, for the link in the message
                      (default http://127.0.0.1:5050)

Sending happens in the background, so the customer's chat isn't slowed
down, and a failure is only logged: the conversation is still flagged on
the Support page either way.
"""

import logging
import os
import smtplib
import threading
from email.message import EmailMessage

log = logging.getLogger(__name__)
BACKGROUND = True      # tests turn this off to send synchronously


def channels():
    return {
        "slack": bool(os.environ.get("SLACK_WEBHOOK_URL", "").strip()),
        "email": bool(os.environ.get("NOTIFY_EMAIL_TO", "").strip() and os.environ.get("SMTP_HOST", "").strip()),
    }


def site_url():
    return os.environ.get("SITE_URL", "").strip().rstrip("/") or "http://127.0.0.1:5050"


def compose(conversation_id, headline, details):
    link = f"{site_url()}/admin/support?c={conversation_id}"
    subject = f"Shop chat: {headline}"
    body = "\n".join([headline, *[f"{label}: {value}" for label, value in details if value],
                      f"Reply in the chat: {link}"])
    return subject, body


def notify(conversation_id, headline, details):
    """Sends to every configured channel. Returns the channel names used."""
    subject, body = compose(conversation_id, headline, details)
    used = [name for name, on in channels().items() if on]
    for name in used:
        job = (lambda: send_slack(body)) if name == "slack" else (lambda: send_email(subject, body))
        if BACKGROUND:
            threading.Thread(target=_safely, args=(job, name), daemon=True).start()
        else:
            _safely(job, name)
    return used


def _safely(job, name):
    try:
        job()
    except Exception as e:
        log.warning("Couldn't send the %s notification: %s", name, e)


def send_slack(text):
    import requests
    response = requests.post(os.environ["SLACK_WEBHOOK_URL"].strip(), json={"text": text}, timeout=10)
    response.raise_for_status()


def send_email(subject, text):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("NOTIFY_EMAIL_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()
    message["To"] = os.environ["NOTIFY_EMAIL_TO"].strip()
    message.set_content(text)
    with smtplib.SMTP(os.environ["SMTP_HOST"].strip(), int(os.environ.get("SMTP_PORT", "587")), timeout=15) as smtp:
        smtp.starttls()
        if os.environ.get("SMTP_USER", "").strip():
            smtp.login(os.environ["SMTP_USER"].strip(), os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(message)
