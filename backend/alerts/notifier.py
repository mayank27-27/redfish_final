"""
alerts/notifier.py
===================
Sends a "ticket" email for every newly-raised CRITICAL alert.

Design choices, deliberately conservative:
- Never raises. A broken/unset SMTP config must never take down a poll
  cycle - every call is wrapped in try/except and just logs on failure.
- Only fires for brand-new alerts, never for the same open alert being
  bumped again on a later poll (that's decided by the caller in
  engine.py, which only passes newly-created Alert rows here).
- Fully inert until SMTP_USERNAME + SMTP_PASSWORD are set in .env -
  config.SMTP_ENABLED gates every call so this is safe to leave wired
  up before credentials exist.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)


def _server_label(server) -> str:
    if server is None:
        return "Unknown server"
    name = server.display_name or server.hostname or "Unknown server"
    return f"{name} ({server.ip_address})" if getattr(server, "ip_address", None) else name


def _build_message(config, alert, server) -> MIMEMultipart:
    label = _server_label(server)
    subject = f"[CRITICAL] {label} - {alert.category}: {alert.message}"

    dashboard_url = (config.get("PUBLIC_WEBHOOK_BASE_URL", "") or "").rstrip("/")
    link_line = f"\nDashboard: {dashboard_url}\n" if dashboard_url else ""

    body = (
        f"A new CRITICAL alert was raised.\n\n"
        f"Ticket ID:   {alert.id}\n"
        f"Server:      {label}\n"
        f"Vendor:      {(server.vendor if server else None) or 'Unknown'} "
        f"{(server.model if server else None) or ''}\n"
        f"Category:    {alert.category}\n"
        f"Message:     {alert.message}\n"
        f"First seen:  {alert.first_occurred_at} UTC\n"
        f"{link_line}\n"
        f"This is an automated notification from Navigator Systems."
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    msg["To"] = config.ALERT_EMAIL_TO
    msg.attach(MIMEText(body, "plain"))
    return msg


def send_critical_alert_email(config, alert, server=None):
    """Best-effort send. Swallows and logs every failure - a mail
    outage must never break polling or alert evaluation."""
    if not config:
        return False
    smtp_enabled = config.get("SMTP_ENABLED", False) if isinstance(config, dict) else getattr(config, "SMTP_ENABLED", False)
    if not smtp_enabled:
        logger.debug(
            "Email alerts disabled (SMTP_USERNAME/SMTP_PASSWORD not set) - "
            "skipping ticket email for alert %s", alert.id,
        )
        return False
    alert_to = config.get("ALERT_EMAIL_TO") if isinstance(config, dict) else getattr(config, "ALERT_EMAIL_TO", "")
    if not alert_to:
        logger.warning("SMTP is configured but ALERT_EMAIL_TO is empty - skipping ticket email for alert %s", alert.id)
        return False

    try:
        msg = _build_message(config, alert, server)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.sendmail(config.SMTP_FROM, [config.ALERT_EMAIL_TO], msg.as_string())
        logger.info("Sent critical-alert ticket email for alert %s to %s", alert.id, config.ALERT_EMAIL_TO)
        return True
    except Exception:
        logger.exception("Failed to send critical-alert ticket email for alert %s", alert.id)
        return False