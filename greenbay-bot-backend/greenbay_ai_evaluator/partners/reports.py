"""Read-side queries for partner trade-in: funnel, leads, demand, credits,
QR performance and settlement.

Every function takes ``partner_id`` (or ``None`` only where an internal,
admin-keyed caller asks for all partners) and filters by it in SQL. The
``portal_*`` shapers build partner-facing payloads from an allow-list of
fields: first names only, masked phones, no staff names, no engine
internals.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
from greenbay_ai_evaluator.models.partner_models import (
    PartnerCredit,
    PartnerCreditEvent,
    PartnerFunnelEvent,
    PartnerQrCode,
)
from greenbay_ai_evaluator.partners import ledger
from greenbay_ai_evaluator.partners.config import PartnerConfig, get_partner

FUNNEL = ("scan", "start", "complete", "offer", "accept", "validated", "issued", "redeemed")

# Credit statuses that still hold money GreenBay owes the partner on use.
OUTSTANDING = frozenset({ledger.ISSUED, ledger.PARTIALLY_REDEEMED})
# Leads the team still has to act on.
AWAITING = frozenset({ledger.ACCEPTED, ledger.PENDING_VALIDATION, ledger.VALIDATED})


def now() -> datetime:
    return datetime.now(timezone.utc)


def since(days: int | None) -> datetime | None:
    if not days:
        return None
    return now() - timedelta(days=max(1, min(int(days), 3650)))


def _iso(dt: datetime | None) -> str | None:
    dt = ledger._aware(dt)
    return dt.isoformat() if dt else None


def mask_phone(phone: str | None) -> str | None:
    """254712345678 -> '0712 *** 678'. Enough for staff to match a customer
    who reads their number out; not enough to call them."""
    p = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(p) < 7:
        return None
    if p.startswith("254") and len(p) == 12:
        local = "0" + p[3:]
        return f"{local[:4]} *** {local[-3:]}"
    return f"{p[:4]} *** {p[-3:]}"


def first_name(name: str | None) -> str | None:
    n = (name or "").strip().split()
    return n[0][:40] if n else None


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------
def funnel(db: Session, partner_id: str | None, *, days: int | None = None,
           qr_code: str | None = None) -> dict[str, int]:
    q = db.query(PartnerFunnelEvent.event_type, PartnerFunnelEvent.qr_code)
    if partner_id:
        q = q.filter(PartnerFunnelEvent.partner_id == partner_id)
    start = since(days)
    if start:
        q = q.filter(PartnerFunnelEvent.created_at >= start)
    if qr_code:
        q = q.filter(PartnerFunnelEvent.qr_code == qr_code)
    counts = Counter(row[0] for row in q.all())
    return {k: int(counts.get(k, 0)) for k in FUNNEL}


def funnel_by_code(db: Session, partner_id: str, *, days: int | None = None) -> dict[str, dict[str, Any]]:
    q = db.query(PartnerFunnelEvent).filter(PartnerFunnelEvent.partner_id == partner_id)
    start = since(days)
    if start:
        q = q.filter(PartnerFunnelEvent.created_at >= start)
    out: dict[str, dict[str, Any]] = defaultdict(lambda: {"funnel": Counter(), "redeemed_amount": 0.0})
    for ev in q.all():
        key = ev.qr_code or ""
        out[key]["funnel"][ev.event_type] += 1
        if ev.event_type == "redeemed" and ev.amount:
            out[key]["redeemed_amount"] += float(ev.amount)
    return out


# ---------------------------------------------------------------------------
# credits
# ---------------------------------------------------------------------------
def credits(db: Session, partner_id: str | None, *, statuses: Iterable[str] | None = None,
            days: int | None = None, limit: int = 200, search: str | None = None) -> list[PartnerCredit]:
    q = db.query(PartnerCredit)
    if partner_id:
        q = q.filter(PartnerCredit.partner_id == partner_id)
    statuses = [s for s in (statuses or []) if s in ledger.STATUSES]
    if statuses:
        q = q.filter(PartnerCredit.status.in_(statuses))
    start = since(days)
    if start:
        q = q.filter(PartnerCredit.created_at >= start)
    if search:
        s = search.strip().upper()
        from greenbay_ai_evaluator.api.schemas import normalise_phone

        phone = normalise_phone(search, "KE")
        conds = [PartnerCredit.reference == s, PartnerCredit.voucher_code == s]
        if phone:
            conds.append(PartnerCredit.customer_phone == phone)
        from sqlalchemy import or_

        q = q.filter(or_(*conds))
    return q.order_by(PartnerCredit.created_at.desc()).limit(max(1, min(int(limit), 1000))).all()


def credit_totals(db: Session, partner_id: str | None) -> dict[str, Any]:
    q = db.query(PartnerCredit)
    if partner_id:
        q = q.filter(PartnerCredit.partner_id == partner_id)
    issued_total = redeemed_total = outstanding = expired_lapsed = 0.0
    by_status: Counter = Counter()
    for c in q.all():
        by_status[c.status] += 1
        if c.issued_amount is not None:
            issued_total += float(c.issued_amount)
            redeemed_total += float(c.redeemed_amount or 0)
            if c.status in OUTSTANDING:
                outstanding += c.balance
            if c.status == ledger.EXPIRED:
                expired_lapsed += c.balance
    return {
        "issued_total": round(issued_total, 2),
        "redeemed_total": round(redeemed_total, 2),
        "outstanding": round(outstanding, 2),
        "lapsed": round(expired_lapsed, 2),
        "by_status": {s: int(by_status.get(s, 0)) for s in ledger.STATUSES},
        "issued_count": int(sum(by_status.get(s, 0) for s in (ledger.ISSUED, ledger.PARTIALLY_REDEEMED,
                                                               ledger.REDEEMED, ledger.EXPIRED))),
        "awaiting_inspection": int(sum(by_status.get(s, 0) for s in AWAITING)),
    }


# ---------------------------------------------------------------------------
# leads (every partner evaluation, with or without a credit)
# ---------------------------------------------------------------------------
def leads(db: Session, partner_id: str | None, *, days: int | None = 90, limit: int = 200,
          only_review: bool = False) -> list[tuple[ValuationSession, PartnerCredit | None]]:
    q = db.query(ValuationSession).filter(ValuationSession.partner_id.isnot(None))
    if partner_id:
        q = q.filter(ValuationSession.partner_id == partner_id)
    start = since(days)
    if start:
        q = q.filter(ValuationSession.created_at >= start)
    rows = q.order_by(ValuationSession.created_at.desc()).limit(max(1, min(int(limit), 1000))).all()
    by_session = {}
    if rows:
        cq = db.query(PartnerCredit).filter(PartnerCredit.valuation_session_id.in_([r.id for r in rows]))
        if partner_id:
            cq = cq.filter(PartnerCredit.partner_id == partner_id)
        by_session = {c.valuation_session_id: c for c in cq.all()}
    out = [(vs, by_session.get(vs.id)) for vs in rows]
    if only_review:
        out = [(vs, c) for vs, c in out if c is None and (vs.final_decision or "") != "accepted"]
    return out


def demand(db: Session, cfg: PartnerConfig, *, days: int | None = 90) -> dict[str, Any]:
    q = db.query(ValuationSession.buy_intent_category, ValuationSession.buy_intent_model).filter(
        ValuationSession.partner_id == cfg.slug, ValuationSession.buy_intent_category.isnot(None),
    )
    start = since(days)
    if start:
        q = q.filter(ValuationSession.created_at >= start)
    counts: Counter = Counter()
    models: dict[str, Counter] = defaultdict(Counter)
    for cat, model in q.all():
        counts[cat] += 1
        if model:
            models[cat][model.strip()[:80]] += 1
    # Labels from config; the stored ids are slugged ("cooker-oven").
    labels = {b.id.replace("_", "-"): b.label for b in cfg.buy_categories}
    items = [
        {"category": cat, "label": labels.get(cat, cat.replace("-", " ").capitalize()), "count": n,
         "models": [m for m, _ in models[cat].most_common(5)]}
        for cat, n in counts.most_common()
    ]
    return {"total": int(sum(counts.values())), "items": items}


# ---------------------------------------------------------------------------
# QR codes (config placements plus admin-made codes)
# ---------------------------------------------------------------------------
def qr_codes(db: Session, cfg: PartnerConfig) -> list[dict[str, Any]]:
    out = {p.code: {"code": p.code, "label": p.label, "channel": p.channel, "shop": p.shop,
                    "placement": p.placement, "active": True, "source": "config"} for p in cfg.placements}
    for row in db.query(PartnerQrCode).filter(PartnerQrCode.partner_id == cfg.slug).all():
        out[row.code] = {"code": row.code, "label": row.label or "", "channel": row.channel, "shop": row.shop,
                         "placement": row.placement, "active": bool(row.active), "source": "admin"}
    return sorted(out.values(), key=lambda r: r["code"])


# ---------------------------------------------------------------------------
# settlement: what GreenBay owes the partner for credit spent, per period
# ---------------------------------------------------------------------------
def settlement(db: Session, partner_id: str, *, start: datetime, end: datetime) -> dict[str, Any]:
    """Issued, redeemed, lapsed and outstanding for one partner and period.

    GreenBay pays the partner for credit actually redeemed in the period
    (the ``redeemed`` total is the amount due). Outstanding is what customers
    still hold at the end of the period.
    """
    start, end = ledger._aware(start), ledger._aware(end)
    events = (
        db.query(PartnerCreditEvent)
        .filter(PartnerCreditEvent.partner_id == partner_id,
                PartnerCreditEvent.event_type.in_(("issued", "redeemed", "expired", "cancelled")))
        .order_by(PartnerCreditEvent.id.asc())
        .all()
    )
    in_period = [e for e in events if start <= ledger._aware(e.created_at) < end]
    issued = sum(float(e.amount or 0) for e in in_period if e.event_type == "issued")
    redeemed = sum(float(e.amount or 0) for e in in_period if e.event_type == "redeemed")
    lapsed = sum(float(e.amount or 0) for e in in_period if e.event_type == "expired")

    # Outstanding at the end of the period, replayed from the trail.
    held: dict[str, float] = defaultdict(float)
    for e in events:
        if ledger._aware(e.created_at) >= end:
            break
        if e.event_type == "issued":
            held[e.credit_id] += float(e.amount or 0)
        elif e.event_type in ("redeemed", "expired"):
            held[e.credit_id] -= float(e.amount or 0)
        elif e.event_type == "cancelled":
            held[e.credit_id] = 0.0
    outstanding = sum(v for v in held.values() if v > 0.004)

    credit_ids = {e.credit_id for e in in_period if e.event_type == "redeemed"}
    lines = []
    if credit_ids:
        refs = {c.id: c for c in db.query(PartnerCredit).filter(PartnerCredit.id.in_(credit_ids),
                                                                PartnerCredit.partner_id == partner_id)}
        for e in in_period:
            if e.event_type != "redeemed":
                continue
            c = refs.get(e.credit_id)
            lines.append({
                "date": _iso(e.created_at), "voucher_code": c.voucher_code if c else None,
                "reference": c.reference if c else None, "amount": round(float(e.amount or 0), 2),
                "note": e.note or "", "recorded_by": e.actor,
            })
    cfg = get_partner(partner_id)
    return {
        "partner": partner_id,
        "currency": cfg.currency if cfg else "KES",
        "from": _iso(start), "to": _iso(end),
        "issued": round(issued, 2),
        "redeemed": round(redeemed, 2),
        "amount_due": round(redeemed, 2),
        "lapsed": round(lapsed, 2),
        "outstanding_at_end": round(outstanding, 2),
        "redemptions": lines,
    }


# ---------------------------------------------------------------------------
# partner-facing shapes (allow-listed fields only)
# ---------------------------------------------------------------------------
def portal_customer(name: str | None, phone: str | None) -> dict[str, Any]:
    return {"first_name": first_name(name), "phone_masked": mask_phone(phone)}


def portal_credit(c: PartnerCredit, *, with_history: list[PartnerCreditEvent] | None = None) -> dict[str, Any]:
    out = {
        "reference": c.reference,
        "voucher_code": c.voucher_code if c.issued_at else None,
        "status": c.status,
        "currency": c.currency,
        "issued_amount": c.issued_amount,
        "redeemed_amount": round(float(c.redeemed_amount or 0), 2),
        "balance": c.balance,
        "issued_at": _iso(c.issued_at),
        "expires_at": _iso(c.expires_at),
        "customer": portal_customer(c.customer_name, c.customer_phone),
    }
    if with_history is not None:
        out["history"] = [
            {"event": e.event_type, "at": _iso(e.created_at), "amount": e.amount}
            for e in with_history
            if e.event_type in ("offered", "accepted", "validated", "issued", "redeemed", "expired", "cancelled",
                                "inspection_failed")
        ]
    return out


def portal_lead(vs: ValuationSession, c: PartnerCredit | None) -> dict[str, Any]:
    return {
        "ref": c.reference if c else None,
        "created_at": _iso(vs.created_at),
        "category": vs.category,
        "brand": vs.brand,
        "model": vs.model or None,
        "channel": vs.partner_channel,
        "shop": vs.partner_shop,
        "qr_code": vs.partner_qr_code,
        "source": vs.partner_source,
        "status": c.status if c else "review",
        "credit_offer": (c.offered_amount if c else vs.partner_offer),
        "buy_intent": {"category": vs.buy_intent_category, "model": vs.buy_intent_model},
        "customer": portal_customer(vs.seller_name, vs.seller_phone),
    }
