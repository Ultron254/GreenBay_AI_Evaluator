"""Partner trade-in data routes, mounted under /tradein.

Two routers, two keys, nothing shared:

* ``partner_portal_router``  GET only, read-only key (or admin). What Pulse's
  partner portal reads for ONE partner: summary, leads, demand, credits,
  credit lookup, QR performance. Pulse puts the partner slug from its own
  signed-in session in the path; every query here filters by it, and every
  payload is built from an allow-list (first names, masked phones, no staff
  names). The credit lookup query travels in the X-Lookup-Query header, never
  in a URL, so a phone number never reaches an access log.

* ``partner_admin_router``   admin key only. GreenBay's own work: the
  all-partner overview, inspection and credit approvals, recording a
  redemption (CX), cancelling, settlement per partner, leads waiting for a
  person. Every write names the person doing it; the ledger refuses one
  without a name and keeps it in the audit trail.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.database.db import get_db
from greenbay_ai_evaluator.api.security import verify_admin_key, verify_readonly_or_admin_key
from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
from greenbay_ai_evaluator.models.partner_models import PartnerCredit
from greenbay_ai_evaluator.partners import ledger, notify, reports, rules
from greenbay_ai_evaluator.partners.config import PartnerConfig, all_partners, get_partner

partner_portal_router = APIRouter()
partner_admin_router = APIRouter()


def _partner(slug: str) -> PartnerConfig:
    cfg = get_partner(slug)
    if cfg is None:
        raise HTTPException(404, "Unknown partner")
    return cfg


def _days(days: int) -> int:
    return max(1, min(int(days or 30), 3650))


# ===========================================================================
# Pulse partner portal (read-only key)
# ===========================================================================
@partner_portal_router.get("/partners/{slug}/portal/summary")
def portal_summary(slug: str, days: int = Query(30, ge=1, le=3650), db: Session = Depends(get_db),
                   _: bool = Depends(verify_readonly_or_admin_key)):
    cfg = _partner(slug)
    ledger.expire_due(db, partner_id=cfg.slug)
    db.commit()
    totals = reports.credit_totals(db, cfg.slug)
    return {
        "partner": cfg.slug, "name": cfg.name, "currency": cfg.currency, "days": _days(days),
        "funnel": reports.funnel(db, cfg.slug, days=_days(days)),
        "credits": {k: totals[k] for k in ("issued_total", "redeemed_total", "outstanding",
                                           "issued_count", "awaiting_inspection")},
        "generated_at": reports.now().isoformat(),
    }


@partner_portal_router.get("/partners/{slug}/portal/leads")
def portal_leads(slug: str, days: int = Query(90, ge=1, le=3650), limit: int = Query(200, ge=1, le=1000),
                 db: Session = Depends(get_db), _: bool = Depends(verify_readonly_or_admin_key)):
    cfg = _partner(slug)
    return {"items": [reports.portal_lead(vs, c) for vs, c in reports.leads(db, cfg.slug, days=days, limit=limit)]}


@partner_portal_router.get("/partners/{slug}/portal/demand")
def portal_demand(slug: str, days: int = Query(90, ge=1, le=3650), db: Session = Depends(get_db),
                  _: bool = Depends(verify_readonly_or_admin_key)):
    return reports.demand(db, _partner(slug), days=days)


@partner_portal_router.get("/partners/{slug}/portal/credits")
def portal_credits(slug: str, status: str = Query("", max_length=40), limit: int = Query(200, ge=1, le=1000),
                   db: Session = Depends(get_db), _: bool = Depends(verify_readonly_or_admin_key)):
    cfg = _partner(slug)
    ledger.expire_due(db, partner_id=cfg.slug)
    db.commit()
    statuses = [s.strip() for s in status.split(",") if s.strip()] or None
    return {"items": [reports.portal_credit(c) for c in reports.credits(db, cfg.slug, statuses=statuses, limit=limit)]}


@partner_portal_router.get("/partners/{slug}/portal/credit-lookup")
def portal_credit_lookup(slug: str, x_lookup_query: str = Header("", alias="X-Lookup-Query", max_length=60),
                         db: Session = Depends(get_db), _: bool = Depends(verify_readonly_or_admin_key)):
    cfg = _partner(slug)
    q = (x_lookup_query or "").strip()
    if len(q) < 4:
        raise HTTPException(422, "Send a voucher code, reference or phone number in X-Lookup-Query.")
    ledger.expire_due(db, partner_id=cfg.slug)
    db.commit()
    found = ledger.find_for_partner(db, cfg.slug, query=q)
    return {"items": [reports.portal_credit(c, with_history=list(ledger.history(db, c))) for c in found[:10]]}


@partner_portal_router.get("/partners/{slug}/portal/qr")
def portal_qr(slug: str, days: int = Query(90, ge=1, le=3650), db: Session = Depends(get_db),
              _: bool = Depends(verify_readonly_or_admin_key)):
    cfg = _partner(slug)
    stats = reports.funnel_by_code(db, cfg.slug, days=days)
    items = []
    for code in reports.qr_codes(db, cfg):
        s = stats.get(code["code"], {"funnel": {}, "redeemed_amount": 0.0})
        items.append({
            "code": code["code"], "label": code["label"], "channel": code["channel"], "shop": code["shop"],
            "placement": code["placement"], "active": code["active"],
            "url": f"https://evaluate.greenbay.market/q/{code['code']}",
            "funnel": {k: int(s["funnel"].get(k, 0)) for k in reports.FUNNEL},
            "redeemed_amount": round(float(s["redeemed_amount"]), 2),
        })
    return {"items": items}


# ===========================================================================
# GreenBay internal (admin key)
# ===========================================================================
class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(..., min_length=2, max_length=100, description="Who is doing this")
    note: str | None = Field(None, max_length=1000)


class StartValidation(_Action):
    method: str = Field(..., description="technician | drop_greenbay | drop_partner")


class Validate(_Action):
    agreed_amount: float | None = Field(None, gt=0, le=50_000_000)


class Redeem(_Action):
    partner: str = Field(..., max_length=40)
    voucher_code: str = Field(..., max_length=24)
    amount: float = Field(..., gt=0, le=50_000_000)


class FromReview(_Action):
    amount: float = Field(..., gt=0, le=50_000_000)


def _credit(db: Session, credit_id: str) -> PartnerCredit:
    c = db.query(PartnerCredit).filter(PartnerCredit.id == credit_id).first()
    if c is None:
        raise HTTPException(404, "No such credit")
    return c


def _admin_credit(c: PartnerCredit, *, history: bool = False, db: Session | None = None) -> dict[str, Any]:
    out = {
        "id": c.id, "partner": c.partner_id, "reference": c.reference, "voucher_code": c.voucher_code,
        "status": c.status, "currency": c.currency,
        "offered_amount": c.offered_amount, "agreed_amount": c.agreed_amount,
        "issued_amount": c.issued_amount, "redeemed_amount": c.redeemed_amount, "balance": c.balance,
        "customer_name": c.customer_name, "customer_phone": c.customer_phone,
        "category": c.category, "brand": c.brand, "model": c.model,
        "is_nominal": c.is_nominal, "bulky": c.bulky,
        "channel": c.channel, "shop": c.shop, "qr_code": c.qr_code, "source": c.source_tag,
        "buy_intent": {"category": c.buy_intent_category, "model": c.buy_intent_model},
        "validation_method": c.validation_method,
        "suggested_method": "technician" if c.bulky else "drop_greenbay",
        "session_ref": (c.valuation_session_id or "")[:8],
        "offered_at": reports._iso(c.offered_at), "accepted_at": reports._iso(c.accepted_at),
        "validated_at": reports._iso(c.validated_at), "issued_at": reports._iso(c.issued_at),
        "expires_at": reports._iso(c.expires_at), "created_at": reports._iso(c.created_at),
    }
    if history and db is not None:
        out["history"] = [
            {"event": e.event_type, "from": e.from_status, "to": e.to_status, "amount": e.amount,
             "actor": e.actor, "note": e.note, "at": reports._iso(e.created_at)}
            for e in ledger.history(db, c)
        ]
    return out


def _run(db: Session, fn, *args, **kwargs):
    try:
        out = fn(*args, **kwargs)
        db.commit()
        return out
    except ledger.LedgerError as e:
        db.rollback()
        raise HTTPException(409, {"code": e.code, "message": e.message})


@partner_admin_router.get("/partners/admin/overview")
def admin_overview(days: int = Query(30, ge=1, le=3650), db: Session = Depends(get_db),
                   _: bool = Depends(verify_admin_key)):
    ledger.expire_due(db)
    db.commit()
    out = []
    for cfg in all_partners():
        totals = reports.credit_totals(db, cfg.slug)
        out.append({
            "partner": cfg.slug, "name": cfg.name, "currency": cfg.currency, "path": cfg.path,
            "funnel": reports.funnel(db, cfg.slug, days=_days(days)),
            "credits": totals,
            "review_leads": len(reports.leads(db, cfg.slug, days=days, only_review=True)),
        })
    return {"days": _days(days), "partners": out, "generated_at": reports.now().isoformat()}


@partner_admin_router.get("/partners/admin/credits")
def admin_credits(partner: str = Query("", max_length=40), status: str = Query("", max_length=200),
                  limit: int = Query(200, ge=1, le=1000),
                  x_lookup_query: str = Header("", alias="X-Lookup-Query", max_length=60),
                  db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    """Search (code, reference or phone) travels in X-Lookup-Query, not the URL."""
    pid = _partner(partner).slug if partner else None
    statuses = [s.strip() for s in status.split(",") if s.strip()] or None
    q = (x_lookup_query or "").strip()
    rows = reports.credits(db, pid, statuses=statuses, limit=limit, search=q or None)
    return {"items": [_admin_credit(c) for c in rows]}


@partner_admin_router.get("/partners/admin/credits/{credit_id}")
def admin_credit_detail(credit_id: str, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    return _admin_credit(_credit(db, credit_id), history=True, db=db)


@partner_admin_router.post("/partners/admin/credits/{credit_id}/start-validation")
def admin_start_validation(credit_id: str, body: StartValidation, db: Session = Depends(get_db),
                           _: bool = Depends(verify_admin_key)):
    c = _credit(db, credit_id)
    _run(db, ledger.start_validation, db, c, method=body.method, actor=body.actor, note=body.note)
    notify.notify("needs_validation", c, actor=body.actor)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.post("/partners/admin/credits/{credit_id}/validate")
def admin_validate(credit_id: str, body: Validate, db: Session = Depends(get_db),
                   _: bool = Depends(verify_admin_key)):
    c = _credit(db, credit_id)
    _run(db, ledger.validate, db, c, actor=body.actor, agreed_amount=body.agreed_amount, note=body.note)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.post("/partners/admin/credits/{credit_id}/fail-inspection")
def admin_fail_inspection(credit_id: str, body: _Action, db: Session = Depends(get_db),
                          _: bool = Depends(verify_admin_key)):
    c = _credit(db, credit_id)
    _run(db, ledger.reject_inspection, db, c, actor=body.actor, note=body.note)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.post("/partners/admin/credits/{credit_id}/issue")
def admin_issue(credit_id: str, body: _Action, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    c = _credit(db, credit_id)
    _run(db, ledger.issue, db, c, actor=body.actor)
    notify.notify("credit_issued", c, actor=body.actor)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.post("/partners/admin/credits/{credit_id}/cancel")
def admin_cancel(credit_id: str, body: _Action, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    c = _credit(db, credit_id)
    _run(db, ledger.cancel, db, c, actor=body.actor, note=body.note)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.post("/partners/admin/redeem")
def admin_redeem(body: Redeem, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    cfg = _partner(body.partner)
    c = _run(db, ledger.redeem, db, partner_id=cfg.slug, voucher_code=body.voucher_code,
             amount=body.amount, actor=body.actor, note=body.note)
    notify.notify("credit_used", c, actor=body.actor, amount=body.amount, note=body.note)
    return _admin_credit(c, history=True, db=db)


@partner_admin_router.get("/partners/admin/review-leads")
def admin_review_leads(partner: str = Query("", max_length=40), days: int = Query(90, ge=1, le=3650),
                       db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    pid = _partner(partner).slug if partner else None
    out = []
    for vs, _c in reports.leads(db, pid, days=days, only_review=True):
        out.append({
            "session_id": vs.id, "session_ref": vs.id[:8], "partner": vs.partner_id,
            "created_at": reports._iso(vs.created_at), "category": vs.category, "brand": vs.brand,
            "model": vs.model, "age_years": vs.age_years, "condition_grade": vs.condition_grade,
            "engine_offer": vs.opening_offer, "confidence": vs.confidence_score, "risk": vs.risk_score,
            "customer_name": vs.seller_name, "customer_phone": vs.seller_phone, "source": vs.partner_source,
            "buy_intent": {"category": vs.buy_intent_category, "model": vs.buy_intent_model},
        })
    return {"items": out}


@partner_admin_router.post("/partners/admin/review-leads/{session_id}/offer")
def admin_offer_from_review(session_id: str, body: FromReview, db: Session = Depends(get_db),
                            _: bool = Depends(verify_admin_key)):
    """A person priced a review-routed partner lead and the customer agreed on
    the phone: open the credit at that figure, already accepted."""
    vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
    if vs is None or not vs.partner_id:
        raise HTTPException(404, "No such partner trade-in")
    cfg = _partner(vs.partner_id)
    if ledger.for_session(db, vs.id):
        raise HTTPException(409, {"code": "exists", "message": "This trade-in already has a credit."})
    amount = rules.credit_for_accept(cfg, accepted_amount=body.amount, offered_credit=body.amount,
                                     age_years=vs.age_years)

    def _open():
        vs.partner_offer = amount
        credit = ledger.create_offered(db, cfg, vs, amount=amount)
        ledger.accept(db, credit, amount=amount, actor=body.actor)
        vs.final_decision = "accepted"
        vs.final_offer = amount
        return credit

    credit = _run(db, _open)
    notify.notify("accepted", credit, actor=body.actor)
    return _admin_credit(credit, history=True, db=db)


def _period(month: str | None, start: str | None, end: str | None) -> tuple[datetime, datetime]:
    try:
        if month:
            y, m = (int(x) for x in month.split("-"))
            a = datetime(y, m, 1, tzinfo=timezone.utc)
            b = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
            return a, b
        if start and end:
            a = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            b = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) + timedelta(days=1)
            return a, b
    except (ValueError, TypeError):
        raise HTTPException(422, "Use month=YYYY-MM, or from=YYYY-MM-DD&to=YYYY-MM-DD.")
    now = reports.now()
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc), now + timedelta(seconds=1)


@partner_admin_router.get("/partners/admin/settlement")
def admin_settlement(partner: str = Query(..., max_length=40), month: str = Query("", max_length=7),
                     start: str = Query("", alias="from", max_length=10), end: str = Query("", alias="to", max_length=10),
                     format: str = Query("json", pattern="^(json|csv)$"),
                     db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    cfg = _partner(partner)
    a, b = _period(month or None, start or None, end or None)
    ledger.expire_due(db, partner_id=cfg.slug)
    db.commit()
    rep = reports.settlement(db, cfg.slug, start=a, end=b)
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Partner", cfg.name])
        w.writerow(["Period", rep["from"][:10], (b - timedelta(seconds=1)).date().isoformat()])
        w.writerow(["Currency", rep["currency"]])
        for label, key in (("Credit issued", "issued"), ("Credit redeemed (amount due to partner)", "redeemed"),
                           ("Credit lapsed", "lapsed"), ("Outstanding at period end", "outstanding_at_end")):
            w.writerow([label, f"{rep[key]:.2f}"])
        w.writerow([])
        w.writerow(["Date", "Voucher code", "Reference", "Amount", "Note", "Recorded by"])
        for line in rep["redemptions"]:
            w.writerow([line["date"][:10], line["voucher_code"], line["reference"], f"{line['amount']:.2f}",
                        line["note"], line["recorded_by"]])
        name = f"settlement-{cfg.slug}-{rep['from'][:7]}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return rep


@partner_admin_router.get("/partners/admin/partners")
def admin_partner_list(_: bool = Depends(verify_admin_key)):
    return {"items": [{"slug": c.slug, "name": c.name, "path": c.path, "currency": c.currency,
                       "expiry_days": c.credit.expiry_days} for c in all_partners()]}


# ---------------------------------------------------------------------------
# QR codes and sticker sheets (admin key)
# ---------------------------------------------------------------------------
class NewQr(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(..., min_length=2, max_length=100)
    partner: str = Field(..., max_length=40)
    channel: str = Field(..., max_length=20, description="shop | website | social | print | staff | event")
    shop: str | None = Field(None, max_length=60, description="Shop name, for shop stickers")
    placement: str = Field(..., min_length=2, max_length=40, description="sticker, counter, banner, poster ...")
    label: str | None = Field(None, max_length=120)


def _qr_rows(db: Session, cfg: PartnerConfig, days: int) -> list[dict[str, Any]]:
    from greenbay_ai_evaluator.partners import qr

    stats = reports.funnel_by_code(db, cfg.slug, days=days)
    out = []
    for code in reports.qr_codes(db, cfg):
        s = stats.get(code["code"], {"funnel": {}, "redeemed_amount": 0.0})
        out.append({**code, "partner": cfg.slug, "url": qr.short_url(code["code"]),
                    "funnel": {k: int(s["funnel"].get(k, 0)) for k in reports.FUNNEL},
                    "redeemed_amount": round(float(s["redeemed_amount"]), 2)})
    return out


@partner_admin_router.get("/partners/admin/qr")
def admin_qr_list(partner: str = Query("", max_length=40), days: int = Query(90, ge=1, le=3650),
                  db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    cfgs = [_partner(partner)] if partner else all_partners()
    return {"items": [row for cfg in cfgs for row in _qr_rows(db, cfg, days)]}


@partner_admin_router.post("/partners/admin/qr")
def admin_qr_create(body: NewQr, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    from greenbay_ai_evaluator.models.partner_models import PartnerQrCode
    from greenbay_ai_evaluator.partners import qr

    cfg = _partner(body.partner)
    channel = qr.slug(body.channel, 20)
    if channel not in qr.CHANNELS:
        raise HTTPException(422, f"Channel must be one of {', '.join(qr.CHANNELS)}.")
    shop = qr.slug(body.shop, 60) or None
    if channel == "shop" and not shop:
        raise HTTPException(422, "Name the shop for a shop sticker.")
    placement = qr.slug(body.placement, 40)
    taken = [r.code for r in db.query(PartnerQrCode.code).all()] + [p.code for c in all_partners() for p in c.placements]
    code = qr.new_code(cfg, channel=channel, shop=shop, taken=taken)
    label = (body.label or " ".join(p for p in ((body.shop or "").strip(), body.placement.strip()) if p)
             or channel).strip()[:120]
    row = PartnerQrCode(code=code, partner_id=cfg.slug, channel=channel, shop=shop, placement=placement,
                        label=label, active=True, created_by=body.actor.strip()[:100])
    db.add(row)
    db.commit()
    return next(r for r in _qr_rows(db, cfg, 90) if r["code"] == code)


@partner_admin_router.post("/partners/admin/qr/{code}/deactivate")
def admin_qr_deactivate(code: str, body: _Action, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    from greenbay_ai_evaluator.models.partner_models import PartnerQrCode

    row = db.query(PartnerQrCode).filter(PartnerQrCode.code == code.upper()).first()
    if row is None:
        raise HTTPException(409, {"code": "config_code", "message": "Codes set in a partner's config file are changed there, not here."})
    row.active = False
    db.commit()
    return {"code": row.code, "active": False, "by": body.actor}


def _codes_for(db: Session, cfg: PartnerConfig, codes: str) -> list[dict[str, Any]]:
    wanted = [c.strip().upper() for c in codes.split(",") if c.strip()]
    known = {r["code"]: r for r in reports.qr_codes(db, cfg) if r["active"]}
    if not wanted:
        return list(known.values())
    missing = [c for c in wanted if c not in known]
    if missing:
        raise HTTPException(404, f"Not active codes for {cfg.name}: {', '.join(missing)}")
    return [known[c] for c in wanted][:40]


@partner_admin_router.get("/partners/admin/qr/{code}/svg")
def admin_qr_svg(code: str, db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    from greenbay_ai_evaluator.partners import qr
    from greenbay_ai_evaluator.partners.service import lookup_code

    hit = lookup_code(db, code)
    if hit is None:
        raise HTTPException(404, "Unknown or inactive code")
    cfg = _partner(hit["partner_id"])
    ink = ((cfg.theme or {}).get("colors") or {}).get("ink", "#111111")
    return Response(qr.qr_svg(hit["code"], ink=ink), media_type="image/svg+xml",
                    headers={"Content-Disposition": f'attachment; filename="qr-{hit["code"]}.svg"'})


@partner_admin_router.get("/partners/admin/qr-sheet")
def admin_qr_sheet(partner: str = Query(..., max_length=40), codes: str = Query("", max_length=1000),
                   format: str = Query("pdf", pattern="^(pdf|svg)$"),
                   db: Session = Depends(get_db), _: bool = Depends(verify_admin_key)):
    from greenbay_ai_evaluator.partners import qr

    cfg = _partner(partner)
    rows = _codes_for(db, cfg, codes)
    name = f"stickers-{cfg.slug}-{len(rows)}"
    if format == "svg":
        return Response(qr.sheet_svg(cfg, rows), media_type="image/svg+xml",
                        headers={"Content-Disposition": f'attachment; filename="{name}.svg"'})
    return Response(qr.sheet_pdf(cfg, rows), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
