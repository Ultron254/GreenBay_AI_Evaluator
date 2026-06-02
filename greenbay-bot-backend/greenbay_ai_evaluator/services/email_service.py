"""
Team email notifications via AWS SES (issue #13).

Sends an email to the GreenBay team whenever an evaluation's price is accepted,
summarising the item, model, age, condition/issues, the customer's asking price
and the AI price.

Transport: AWS SES (reuses the existing AWS credentials used for S3).
Delivery: async / non-blocking via a daemon thread. Never raises.

Config (app.config.Settings):
  - ses_sender_email          — verified SES "From" address (required)
  - ses_region                — SES region (falls back to aws_region)
  - team_notification_emails  — comma-separated recipient list
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger


def _recipients() -> list[str]:
    from app.config import get_settings
    s = get_settings()
    raw = getattr(s, "team_notification_emails", "") or ""
    return [e.strip() for e in raw.split(",") if e.strip()]


def _format_money(currency: str, amount: float | None) -> str:
    try:
        return f"{currency} {float(amount or 0):,.0f}"
    except (TypeError, ValueError):
        return f"{currency} 0"


def _summarise_issues(defects: Any, condition_grade: str | None) -> str:
    """Build a readable 'use & issue report' from stored defects + grade."""
    parts: list[str] = []
    if condition_grade:
        parts.append(f"Condition grade: {condition_grade}")
    items = []
    if isinstance(defects, list):
        for d in defects:
            if isinstance(d, dict):
                desc = d.get("description") or d.get("type") or ""
                sev = d.get("severity")
                items.append(f"{desc}{f' ({sev})' if sev else ''}".strip())
            elif d:
                items.append(str(d))
    if items:
        parts.append("Reported issues: " + "; ".join(i for i in items if i))
    else:
        parts.append("Reported issues: none recorded")
    return " | ".join(parts)


def snapshot_session(vs: Any) -> dict[str, Any]:
    """Extract a plain dict from a ValuationSession while its DB session is open.

    Must be called in the request thread (not the email worker) to avoid
    SQLAlchemy DetachedInstanceError.
    """
    return {
        "id": str(getattr(vs, "id", "") or ""),
        "brand": getattr(vs, "brand", "") or "",
        "model": getattr(vs, "model", "") or "",
        "category": getattr(vs, "category", "") or "",
        "age_years": getattr(vs, "age_years", None),
        "condition_grade": getattr(vs, "condition_grade", None),
        "defects": getattr(vs, "defects", None),
        "seller_name": getattr(vs, "seller_name", "") or "",
        "seller_phone": getattr(vs, "seller_phone", "") or "",
        "seller_asking_price": getattr(vs, "seller_asking_price", None),
        "opening_offer": getattr(vs, "opening_offer", None),
        "final_offer": getattr(vs, "final_offer", None),
        "confidence_score": getattr(vs, "confidence_score", None),
        "currency_code": getattr(vs, "currency_code", None) or "KES",
        "size_value": getattr(vs, "size_value", None),
        "size_unit": getattr(vs, "size_unit", None),
    }


def build_accepted_email(vs: dict[str, Any]) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for an accepted evaluation."""
    currency = vs.get("currency_code") or "KES"
    product = " ".join(
        p for p in [
            (vs.get("brand") or "").strip(),
            (vs.get("model") or "").strip(),
            (vs.get("category") or "").strip(),
        ] if p
    ) or "Appliance"

    size_str = ""
    sv, su = vs.get("size_value"), vs.get("size_unit")
    if sv and su:
        size_str = f"{sv:g} {su}"

    age = vs.get("age_years")
    ai_price = vs.get("final_offer") or vs.get("opening_offer")
    asking = vs.get("seller_asking_price")
    issues = _summarise_issues(vs.get("defects"), vs.get("condition_grade"))
    confidence = vs.get("confidence_score")

    subject = f"[GreenBay] Offer ACCEPTED — {product} — {_format_money(currency, ai_price)}"

    rows = [
        ("Item", product),
        ("Model number", vs.get("model") or "—"),
        ("Size", size_str or "—"),
        ("Age", f"{age:g} years" if age is not None else "—"),
        ("Use & issue report", issues),
        ("Customer asking price", _format_money(currency, asking) if asking else "Not provided"),
        ("AI price (accepted)", _format_money(currency, ai_price)),
        ("AI confidence", f"{confidence:.0f}%" if confidence is not None else "—"),
        ("Customer", vs.get("seller_name") or "—"),
        ("Phone", vs.get("seller_phone") or "—"),
        ("Session", str(vs.get("id") or "")[:8]),
    ]

    text_body = "GreenBay trade-in offer ACCEPTED\n\n" + "\n".join(
        f"{label}: {value}" for label, value in rows
    )

    html_rows = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:600;color:#14532d;'>{label}</td>"
        f"<td style='padding:6px 12px;'>{value}</td></tr>"
        for label, value in rows
    )
    html_body = (
        "<div style='font-family:Arial,sans-serif;max-width:600px;'>"
        "<h2 style='color:#14532d;'>Trade-in offer ACCEPTED</h2>"
        "<table style='border-collapse:collapse;width:100%;'>"
        f"{html_rows}</table>"
        "<p style='color:#666;font-size:12px;margin-top:16px;'>"
        "Automated notification from the GreenBay AI Evaluator.</p></div>"
    )
    return subject, text_body, html_body


def _send_smtp(subject: str, text_body: str, html_body: str) -> bool:
    """Send via plain SMTP (e.g. Google Workspace / Gmail app password).

    Used when SMTP_HOST is configured — simpler than SES (no identity
    verification or sandbox). Returns True on success.
    """
    from app.config import get_settings
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    s = get_settings()
    host = getattr(s, "smtp_host", "") or ""
    if not host:
        return False
    recipients = _recipients()
    if not recipients:
        logger.warning("Email: no TEAM_NOTIFICATION_EMAILS configured — skipping")
        return False
    sender = getattr(s, "smtp_from", "") or getattr(s, "smtp_user", "") or ""
    if not sender:
        logger.warning("Email: SMTP_FROM / SMTP_USER not configured — skipping")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        port = int(getattr(s, "smtp_port", 587) or 587)
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPException:
                pass  # server may not support STARTTLS (e.g. port 465 wrappers)
            user = getattr(s, "smtp_user", "") or ""
            pwd = getattr(s, "smtp_password", "") or ""
            if user and pwd:
                server.login(user, pwd)
            server.sendmail(sender, recipients, msg.as_string())
        logger.info(f"Email: sent via SMTP to {recipients}")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email: SMTP send failed: {e}")
        return False


def _send_ses(subject: str, text_body: str, html_body: str) -> bool:
    """Deliver email. Prefers SMTP when SMTP_HOST is set, else uses AWS SES."""
    from app.config import get_settings
    s = get_settings()

    # Simpler path first: SMTP (Google Workspace / Gmail / any provider).
    if getattr(s, "smtp_host", ""):
        return _send_smtp(subject, text_body, html_body)

    sender = getattr(s, "ses_sender_email", "") or ""
    recipients = _recipients()
    if not sender:
        logger.warning("Email: no SMTP_HOST and no SES_SENDER_EMAIL — email is OFF")
        return False
    if not recipients:
        logger.warning("Email: no TEAM_NOTIFICATION_EMAILS configured — skipping")
        return False
    if not (s.aws_access_key_id and s.aws_secret_access_key):
        logger.warning("Email: AWS credentials not configured — skipping")
        return False

    try:
        import boto3
        region = getattr(s, "ses_region", "") or s.aws_region
        client = boto3.client(
            "ses",
            aws_access_key_id=s.aws_access_key_id,
            aws_secret_access_key=s.aws_secret_access_key,
            region_name=region,
        )
        client.send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        logger.info(f"Email: notification sent via SES to {recipients}")
        return True
    except Exception as e:
        logger.warning(f"Email: SES send failed: {e}")
        return False


def send_alert_email(subject: str, body: str) -> bool:
    """Send an ops alert email to the team (synchronous). Returns success."""
    html = (
        "<div style='font-family:Arial,sans-serif;max-width:600px;'>"
        "<h2 style='color:#b91c1c;'>GreenBay Evaluator Alert</h2>"
        f"<pre style='white-space:pre-wrap;font-size:14px;'>{body}</pre></div>"
    )
    return _send_ses(f"[GreenBay ALERT] {subject}", body, html)


def send_evaluation_accepted_email(snapshot: dict[str, Any]) -> None:
    """Fire-and-forget: email the team that an offer was accepted. Never raises.

    *snapshot* must be a plain dict (use ``snapshot_session(vs)`` in the request
    thread) so the email worker never touches a detached ORM object.
    """
    def _worker():
        try:
            subject, text_body, html_body = build_accepted_email(snapshot)
            _send_ses(subject, text_body, html_body)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Email: accepted-offer worker failed: {e}")

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email: could not start notification thread: {e}")
