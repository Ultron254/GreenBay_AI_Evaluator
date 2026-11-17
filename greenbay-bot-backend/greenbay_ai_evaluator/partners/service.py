"""Glue between the evaluate / accept routes and the partner layer.

The routes call four functions and nothing else:

* ``resolve(db, req)``           before pricing: which partner, is this item
                                 accepted there, where did the lead come from
* ``session_columns(resolved)``  the partner columns for valuation_sessions
* ``after_evaluation(...)``      after pricing: the credit offer, the ledger
                                 row and the funnel events
* ``on_accept(...)``             the customer said yes: credit amount and
                                 reference

Bookkeeping after pricing runs in a savepoint and never fails an evaluation:
if it breaks, the customer still sees their result, the error is logged, and
``on_accept`` creates the missing ledger row later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy.orm import Session

from greenbay_ai_evaluator.models.partner_models import PartnerQrCode
from greenbay_ai_evaluator.partners import ledger, rules
from greenbay_ai_evaluator.partners.config import PartnerConfig, get_partner


@dataclass
class ResolvedPartner:
    cfg: PartnerConfig
    channel: str
    shop: str | None
    placement: str | None
    qr_code: str | None
    source_tag: str
    buy_category: str | None
    buy_model: str | None


def _refuse(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def lookup_code(db: Session | None, code: str | None) -> dict[str, Any] | None:
    """A QR code's partner, channel, shop and placement, from the admin-made
    codes first, then from partner config files."""
    if not code:
        return None
    code = code.strip().upper()
    if db is not None:
        try:
            row = db.query(PartnerQrCode).filter(PartnerQrCode.code == code).first()
        except Exception as e:  # noqa: BLE001, table missing on an old database
            logger.warning(f"partner QR lookup failed for {code}: {e}")
            row = None
        if row is not None:
            if not row.active:
                return None
            return {"partner_id": row.partner_id, "channel": row.channel, "shop": row.shop,
                    "placement": row.placement, "code": row.code}
    from greenbay_ai_evaluator.partners.config import partner_for_code

    hit = partner_for_code(code)
    if hit is None:
        return None
    cfg, p = hit
    return {"partner_id": cfg.slug, "channel": p.channel, "shop": p.shop,
            "placement": p.placement, "code": p.code}


def resolve(db: Session, req) -> ResolvedPartner | None:
    ctx = getattr(req, "partner", None)
    if ctx is None:
        return None
    cfg = get_partner(ctx.slug)
    if cfg is None:
        raise _refuse("partner_unknown", "This trade-in link is not active. Please use the link from the shop or website.")
    if not rules.country_allowed(cfg, getattr(req, "country", "KE")):
        raise _refuse("partner_country", f"The {cfg.name} swap is not open in this country yet.")
    if not rules.category_allowed(cfg, getattr(req, "category", None)):
        raise _refuse("partner_category", f"The {cfg.name} swap does not take this kind of appliance.")
    if not rules.brand_allowed(cfg, getattr(req, "brand", None)):
        raise _refuse(
            "partner_brand",
            f"The {cfg.name} swap is for {cfg.name} appliances. For another brand, "
            "GreenBay can still make you a cash offer at evaluate.greenbay.market.",
        )

    channel, shop, placement, qr = ctx.channel, ctx.shop, ctx.placement, None
    code = lookup_code(db, ctx.qr_code)
    if code and code["partner_id"] == cfg.slug:
        # The server's record of the code wins over what the browser says.
        channel, shop, placement, qr = code["channel"], code["shop"], code["placement"], code["code"]
    elif ctx.qr_code:
        logger.info(f"partner {cfg.slug}: ignoring unknown or foreign QR code {ctx.qr_code}")
    channel = channel or "direct"
    return ResolvedPartner(
        cfg=cfg,
        channel=channel,
        shop=shop,
        placement=placement,
        qr_code=qr,
        source_tag=rules.source_tag(cfg.slug, channel, shop, placement),
        buy_category=ctx.buy_category,
        buy_model=ctx.buy_model,
    )


def session_columns(rp: ResolvedPartner | None) -> dict[str, Any]:
    if rp is None:
        return {}
    return {
        "partner_id": rp.cfg.slug,
        "partner_channel": rp.channel,
        "partner_shop": rp.shop,
        "partner_placement": rp.placement,
        "partner_qr_code": rp.qr_code,
        "partner_source": rp.source_tag,
        "buy_intent_category": rp.buy_category,
        "buy_intent_model": rp.buy_model,
    }


def after_evaluation(db: Session, rp: ResolvedPartner | None, vs) -> dict | None:
    """Credit offer for the response, plus ledger row and funnel events."""
    if rp is None:
        return None
    cfg = rp.cfg
    offer = rules.credit_offer(
        cfg,
        decision=getattr(vs, "decision", None),
        opening_offer=getattr(vs, "opening_offer", None),
        age_years=getattr(vs, "age_years", None),
        risk_score=getattr(vs, "risk_score", None),
    )
    vs.partner_offer = offer
    try:
        with db.begin_nested():
            ledger.record_funnel(db, cfg.slug, "complete", qr_code=rp.qr_code, channel=rp.channel,
                                 shop=rp.shop, valuation_session_id=vs.id)
            if offer is not None:
                credit = ledger.create_offered(db, cfg, vs, amount=offer)
                ledger.record_funnel(db, cfg.slug, "offer", qr_code=rp.qr_code, channel=rp.channel,
                                     shop=rp.shop, valuation_session_id=vs.id, credit_id=credit.id,
                                     amount=offer)
    except Exception as e:  # noqa: BLE001, never fail the customer's evaluation
        logger.error(f"partner {cfg.slug}: ledger bookkeeping failed for {str(vs.id)[:8]}: {e}")
    return {
        "slug": cfg.slug,
        "credit_offer": offer,
        "currency": getattr(vs, "currency_code", None) or cfg.currency,
        "nominal": rules.is_nominal(cfg, getattr(vs, "age_years", None)),
    }


def on_accept(db: Session, vs, accepted_amount: float | None):
    """The customer accepted on a partner site. Returns (amount, reference,
    credit, newly_accepted), or None for a GreenBay evaluation. Raises 409
    when no credit can be offered (a person has to decide first)."""
    cfg = get_partner(getattr(vs, "partner_id", None))
    if cfg is None:
        return None
    offered = getattr(vs, "partner_offer", None)
    if offered is None:
        raise HTTPException(409, "Our team needs to look at this one before an offer is made. We will call you.")
    amount = rules.credit_for_accept(
        cfg, accepted_amount=accepted_amount, offered_credit=offered, age_years=getattr(vs, "age_years", None),
    )
    credit = ledger.for_session(db, vs.id) or ledger.create_offered(db, cfg, vs, amount=offered)
    if credit.status not in (ledger.OFFERED, ledger.ACCEPTED):
        raise HTTPException(409, f"This trade-in is already {credit.status.replace('_', ' ')}.")
    newly = credit.status == ledger.OFFERED
    ledger.accept(db, credit, amount=amount)
    return amount, credit.reference, credit, newly


def airtable_fields(vs) -> dict[str, Any]:
    """Partner columns for the Airtable row (dropped if the base lacks them)."""
    if not getattr(vs, "partner_id", None):
        return {}
    out: dict[str, Any] = {
        "Partner": vs.partner_id,
        "Partner Source": vs.partner_source or "",
    }
    if vs.partner_shop:
        out["Partner Shop"] = vs.partner_shop
    if vs.partner_qr_code:
        out["QR Code"] = vs.partner_qr_code
    if vs.buy_intent_category:
        out["Buy Intent"] = " ".join(p for p in (vs.buy_intent_category, vs.buy_intent_model or "") if p)
    if vs.partner_offer is not None:
        out["Partner Credit Offer"] = float(vs.partner_offer)
    return out
