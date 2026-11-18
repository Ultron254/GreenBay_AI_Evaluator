"""Emails for partner swap events, to the people named in the partner config.

Events (``notify`` in each partner's config file names who gets which):

* ``accepted``          a customer accepted a swap offer: book the check
* ``needs_validation``  a check was booked: the technician or shop is up
* ``credit_issued``     the item passed: call the customer with the code
* ``credit_used``       CX recorded a redemption: the settlement grew

Sending never blocks or breaks the action that triggered it: it runs on a
background thread, every failure is logged and swallowed, and the transport
is the evaluator's existing one (SMTP when SMTP_HOST is set, else SES).
Demo and test records (ZZTEST, ZZSMOKE, ...) are never emailed.

The WhatsApp alert for a newly accepted swap goes through the existing ops
line (internal_notification_service) from the accept route, labelled with
the partner and the customer's reference.
"""

from __future__ import annotations

import threading
from html import escape
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.partners.config import NOTIFY_EVENTS, get_partner

OPS_PAGE = "https://evaluate.greenbay.market/app/partners-ops.html"

_TITLES = {
    "accepted": "Swap accepted, book the check",
    "needs_validation": "Check booked",
    "credit_issued": "Credit issued, call the customer",
    "credit_used": "Credit used",
}

_METHOD = {"technician": "Technician visit", "drop_greenbay": "Drop-off at a GreenBay shop",
           "drop_partner": "Drop-off at a partner shop"}


def _is_test(snapshot: dict[str, Any]) -> bool:
    try:
        from greenbay_ai_evaluator.services.reference_data_service import TEST_ROW_PATTERN
    except Exception:  # noqa: BLE001
        return False
    text = " ".join(str(snapshot.get(k) or "") for k in ("model", "customer_name", "brand"))
    return bool(TEST_ROW_PATTERN.search(text))


def snapshot(credit, **extra: Any) -> dict[str, Any]:
    """Plain values copied off the credit row, safe to use on another thread."""
    out = {
        "partner": credit.partner_id, "reference": credit.reference, "voucher_code": credit.voucher_code,
        "status": credit.status, "currency": credit.currency or "KES",
        "offered_amount": credit.offered_amount, "agreed_amount": credit.agreed_amount,
        "issued_amount": credit.issued_amount, "balance": credit.balance,
        "customer_name": credit.customer_name, "customer_phone": credit.customer_phone,
        "brand": credit.brand, "model": credit.model, "category": credit.category,
        "source": credit.source_tag, "qr_code": credit.qr_code,
        "buy_intent": " ".join(p for p in (credit.buy_intent_category, credit.buy_intent_model) if p),
        "method": credit.validation_method, "bulky": credit.bulky,
        "expires_at": credit.expires_at.strftime("%d %b %Y") if credit.expires_at else None,
    }
    out.update(extra)
    return out


def _money(cur: str, n: Any) -> str:
    try:
        return f"{cur} {float(n):,.0f}"
    except (TypeError, ValueError):
        return ""


def build(event: str, s: dict[str, Any]) -> tuple[str, str, str]:
    """(subject, text, html) for one event. Pure, so it is tested directly."""
    cfg = get_partner(s.get("partner"))
    name = cfg.name if cfg else str(s.get("partner"))
    cur = s.get("currency") or "KES"
    item = " ".join(p for p in (s.get("brand"), s.get("model"), (s.get("category") or "").replace("_", " ")) if p)
    rows: list[tuple[str, str]] = [("Reference", s.get("reference") or ""), ("Item", item)]
    if event == "accepted":
        amount = s.get("offered_amount")
        rows += [("Credit offered", _money(cur, amount)),
                 ("Suggested check", "Technician visit" if s.get("bulky") else "Drop-off at a GreenBay shop")]
        lead = f"{s.get('customer_name') or 'A customer'} accepted a {name} swap. Call them to book the check."
    elif event == "needs_validation":
        amount = s.get("offered_amount")
        rows += [("Credit offered", _money(cur, amount)), ("Check", _METHOD.get(s.get("method") or "", "Booked"))]
        lead = f"A check is booked for this {name} swap. Record what it finds on the ops page."
    elif event == "credit_issued":
        amount = s.get("issued_amount")
        rows += [("Voucher code", s.get("voucher_code") or ""), ("Credit", _money(cur, amount)),
                 ("Expires", s.get("expires_at") or "")]
        lead = f"The item passed. Call {s.get('customer_name') or 'the customer'} with their {name} credit code."
    elif event == "credit_used":
        amount = s.get("amount")
        rows += [("Voucher code", s.get("voucher_code") or ""), ("Used now", _money(cur, amount)),
                 ("Left", _money(cur, s.get("balance"))), ("Partner receipt", s.get("note") or "")]
        lead = f"{name} credit was used. It is now part of {name}'s settlement."
    else:
        raise ValueError(f"unknown event {event}")
    rows += [("Customer", f"{s.get('customer_name') or ''} {s.get('customer_phone') or ''}".strip()),
             ("Came from", " ".join(p for p in (s.get("source"), f"({s['qr_code']})" if s.get("qr_code") else "") if p)),
             ("Wants to buy", s.get("buy_intent") or "Not said")]
    if s.get("actor"):
        rows.append(("Recorded by", s["actor"]))
    rows = [(k, v) for k, v in rows if v]

    subject = f"[{name} swap] {_TITLES[event]}: {item}, {_money(cur, amount)} ({s.get('reference')})"
    text = lead + "\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nOps page: {OPS_PAGE}\n"
    html = (
        "<div style='font-family:Arial,sans-serif;max-width:600px;color:#102A1E'>"
        f"<p style='font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#0A7F3F;margin:0 0 6px'>"
        f"GreenBay x {escape(name)} swap</p>"
        f"<h2 style='margin:0 0 10px;font-size:20px'>{escape(_TITLES[event])}</h2>"
        f"<p style='margin:0 0 16px;font-size:15px'>{escape(lead)}</p>"
        "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
        + "".join(
            f"<tr><td style='padding:6px 12px 6px 0;color:#5B625E;border-bottom:1px solid #E3DCCD;width:150px'>{escape(k)}</td>"
            f"<td style='padding:6px 0;border-bottom:1px solid #E3DCCD'><b>{escape(str(v))}</b></td></tr>"
            for k, v in rows
        )
        + "</table>"
        f"<p style='margin:18px 0 0'><a href='{OPS_PAGE}' style='color:#075C2F'>Open the partner swaps page</a></p>"
        "</div>"
    )
    return subject, text, html


def recipients(event: str, partner: str | None) -> list[str]:
    cfg = get_partner(partner)
    if cfg is None or event not in NOTIFY_EVENTS:
        return []
    return [r for r in (cfg.notify.get(event) or []) if "@" in r]


def _send(event: str, s: dict[str, Any]) -> None:
    try:
        to = recipients(event, s.get("partner"))
        if not to:
            return
        subject, text, html = build(event, s)
        from greenbay_ai_evaluator.services.email_service import _send_ses

        ok = _send_ses(subject, text, html, to)
        logger.info(f"Partner {event} email for {s.get('reference')}: {'sent' if ok else 'not sent'}")
    except Exception as e:  # noqa: BLE001, a notification never breaks an action
        logger.warning(f"Partner {event} email failed for {s.get('reference')}: {e}")


def notify(event: str, credit, **extra: Any) -> None:
    """Fire and forget. Call after the database commit."""
    try:
        s = snapshot(credit, **extra)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Partner {event} notification skipped: {e}")
        return
    if _is_test(s):
        logger.info(f"Partner {event} email skipped for test record {s.get('reference')}")
        return
    threading.Thread(target=lambda: _send(event, s), daemon=True).start()


def whatsapp_payload(credit, base: dict[str, Any]) -> dict[str, Any]:
    """The ops-line WhatsApp alert for a newly accepted partner swap."""
    cfg = get_partner(credit.partner_id)
    name = cfg.name if cfg else credit.partner_id
    wants = " ".join(p for p in (credit.buy_intent_category, credit.buy_intent_model) if p)
    out = dict(base)
    out["outcome"] = f"ACCEPTED, {name} swap"
    out["ai_price"] = credit.offered_amount
    out["extra"] = (f"{name} credit, ref {credit.reference}. Book the check"
                    f" ({'technician' if credit.bulky else 'drop-off'})."
                    + (f" Wants: {wants}." if wants else "")
                    + (f" From: {credit.source_tag}." if credit.source_tag else ""))
    return out
