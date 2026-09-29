"""
Team email notifications via SMTP or AWS SES (issue #13).

Sends an email to the GreenBay team whenever a customer accepts *or* rejects an
AI offer, summarising the item, model, age, condition/issues, the customer's
asking price, the AI price and links to the submitted photos.

Transport: SMTP when SMTP_HOST is set, otherwise AWS SES.
Delivery: async / non-blocking via a daemon thread. Never raises.

Config (app.config.Settings):
  - smtp_host / smtp_user / smtp_password / smtp_from — SMTP transport
  - ses_sender_email          — verified SES "From" address (SES transport)
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


def presign_session_images(vs: Any, expires_in_seconds: int = 7 * 24 * 3600) -> list[str]:
    """Re-presign the session's stored S3 photo keys. Returns [] when S3 is off."""
    keys = getattr(vs, "image_s3_keys", None)
    if not isinstance(keys, list) or not keys:
        return []
    try:
        from app.services.s3_service import _ensure_s3_client, settings as s3_settings
        client = _ensure_s3_client()
        bucket = getattr(s3_settings, "aws_s3_bucket", None)
        if not client or not bucket:
            return []
        urls: list[str] = []
        for key in [k for k in keys if k][:8]:
            urls.append(
                client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=expires_in_seconds,
                )
            )
        return urls
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email: could not presign session images: {e}")
        return []


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
        "decision_reason": getattr(vs, "decision_reason", "") or "",
        "image_urls": presign_session_images(vs),
    }


def build_outcome_email(
    vs: dict[str, Any],
    outcome: str,
    extra: str = "",
) -> tuple[str, str, str]:
    """Return (subject, text_body, html_body) for an accepted/rejected evaluation.

    *outcome* is a short uppercase label such as "ACCEPTED" or "REJECTED".
    """
    outcome = (outcome or "").upper() or "UPDATED"
    accent = "#14532d" if outcome == "ACCEPTED" else "#b45309"

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
    image_urls = [u for u in (vs.get("image_urls") or []) if u]

    subject = (
        f"[GreenBay] Offer {outcome} — {product} — {_format_money(currency, ai_price)}"
    )

    rows = [
        ("Outcome", outcome + (f" — {extra}" if extra else "")),
        ("Item", product),
        ("Model number", vs.get("model") or "—"),
        ("Size", size_str or "—"),
        ("Age", f"{age:g} years" if age is not None else "—"),
        ("Use & issue report", issues),
        ("Customer asking price", _format_money(currency, asking) if asking else "Not provided"),
        (f"AI price ({outcome.lower()})", _format_money(currency, ai_price)),
        ("AI confidence", f"{confidence:.0f}%" if confidence is not None else "—"),
        ("Customer", vs.get("seller_name") or "—"),
        ("Phone", vs.get("seller_phone") or "—"),
        ("Photos", f"{len(image_urls)} attached below" if image_urls else "none stored"),
        ("Session", str(vs.get("id") or "")[:8]),
    ]

    text_body = f"GreenBay trade-in offer {outcome}\n\n" + "\n".join(
        f"{label}: {value}" for label, value in rows
    )
    if image_urls:
        text_body += "\n\nPhotos (links valid 7 days):\n" + "\n".join(image_urls)

    html_rows = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:600;color:{accent};'>{label}</td>"
        f"<td style='padding:6px 12px;'>{value}</td></tr>"
        for label, value in rows
    )
    html_images = ""
    if image_urls:
        thumbs = "".join(
            f"<a href='{u}'><img src='{u}' alt='photo' "
            "style='width:150px;height:150px;object-fit:cover;"
            "border-radius:6px;margin:4px;border:1px solid #ddd;'></a>"
            for u in image_urls
        )
        html_images = (
            "<h3 style='margin-top:20px;color:#334155;font-size:15px;'>"
            "Customer photos <span style='font-weight:400;color:#888;'>"
            "(links valid 7 days)</span></h3>"
            f"<div>{thumbs}</div>"
        )

    html_body = (
        "<div style='font-family:Arial,sans-serif;max-width:700px;'>"
        f"<h2 style='color:{accent};'>Trade-in offer {outcome}</h2>"
        "<table style='border-collapse:collapse;width:100%;'>"
        f"{html_rows}</table>"
        f"{html_images}"
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


def send_evaluation_outcome_email(
    snapshot: dict[str, Any],
    outcome: str,
    extra: str = "",
) -> None:
    """Fire-and-forget: email the team an accepted/rejected outcome. Never raises.

    *snapshot* must be a plain dict (use ``snapshot_session(vs)`` in the request
    thread) so the email worker never touches a detached ORM object.
    """
    def _worker():
        try:
            subject, text_body, html_body = build_outcome_email(snapshot, outcome, extra)
            _send_ses(subject, text_body, html_body)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Email: {outcome} worker failed: {e}")

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email: could not start notification thread: {e}")


def qualify_lead(vs: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether an evaluation is worth the CX team phoning back.

    Returns (is_qualified, reason). A lead is only useful if somebody can
    actually be called and the item is worth the trip, so the bar is a
    reachable phone plus an offer above ``LEAD_MIN_OFFER_KES``.
    """
    import os

    phone = (vs.get("seller_phone") or "").strip()
    if not phone:
        return False, "no phone captured"

    offer = vs.get("final_offer") or vs.get("opening_offer") or 0
    try:
        offer = float(offer)
    except (TypeError, ValueError):
        offer = 0.0
    try:
        floor = float(os.environ.get("LEAD_MIN_OFFER_KES", "5000"))
    except ValueError:
        floor = 5000.0
    if offer < floor:
        return False, f"offer {offer:,.0f} below {floor:,.0f} floor"

    return True, f"contactable, offer {offer:,.0f}"


def send_qualified_lead_email(snapshot: dict[str, Any], outcome: str) -> None:
    """Fire-and-forget: tell CX about a lead worth calling. Never raises."""
    qualified, reason = qualify_lead(snapshot)
    if not qualified:
        logger.info(f"Lead not qualified for CX outreach ({reason})")
        return

    def _worker():
        try:
            subject, text_body, html_body = build_outcome_email(snapshot, outcome)
            subject = f"[LEAD] {subject}"
            note = f"Qualified lead — {reason}. Call {snapshot.get('seller_phone')}."
            text_body = note + "\n\n" + text_body
            html_body = (
                f"<p style='font-size:15px;font-weight:600;color:#14532d;'>{note}</p>"
                + html_body
            )
            _send_ses(subject, text_body, html_body)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Email: qualified-lead worker failed: {e}")

    try:
        threading.Thread(target=_worker, daemon=True).start()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Email: could not start lead thread: {e}")


def send_evaluation_accepted_email(snapshot: dict[str, Any]) -> None:
    """Backwards-compatible wrapper for the accepted-offer email."""
    send_evaluation_outcome_email(snapshot, "ACCEPTED")
