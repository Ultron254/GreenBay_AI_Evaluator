"""The partner credit ledger.

One credit per partner trade-in that reached an offer. Its life:

    offered ──► accepted ──► pending_validation ──► validated ──► issued
                                                                    │
                                    partially_redeemed ◄────────────┤
                                            │                       │
                                            └──────► redeemed ◄─────┘

plus two exits: ``cancelled`` (customer declined, inspection failed, or the
team cancelled before any credit was spent) and ``expired`` (issued credit not
used within the partner's expiry window; whatever balance was left lapses).

Rules every caller gets for free, because every change goes through
``_move``:

* only the transitions in ``TRANSITIONS`` are possible;
* each change writes a PartnerCreditEvent row (actor, from, to, amount, note);
* a credit belongs to one partner for ever: lookups and redemptions always
  filter by partner_id, so one partner can never see or spend another's credit;
* redemption never exceeds the balance and never touches expired credit;
* credit is never paid out as cash: there is no such transition.

Reference (shown to the customer at acceptance) and voucher code (given out by
CX once issued) use an alphabet without 0/O, 1/I/L so they survive being read
out over the phone.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from greenbay_ai_evaluator.models.partner_models import (
    PartnerCredit,
    PartnerCreditEvent,
    PartnerFunnelEvent,
)
from greenbay_ai_evaluator.partners import rules
from greenbay_ai_evaluator.partners.config import PartnerConfig, get_partner

OFFERED = "offered"
ACCEPTED = "accepted"
PENDING_VALIDATION = "pending_validation"
VALIDATED = "validated"
ISSUED = "issued"
PARTIALLY_REDEEMED = "partially_redeemed"
REDEEMED = "redeemed"
EXPIRED = "expired"
CANCELLED = "cancelled"

STATUSES = (
    OFFERED, ACCEPTED, PENDING_VALIDATION, VALIDATED, ISSUED,
    PARTIALLY_REDEEMED, REDEEMED, EXPIRED, CANCELLED,
)
TERMINAL = frozenset({REDEEMED, EXPIRED, CANCELLED})
SPENDABLE = frozenset({ISSUED, PARTIALLY_REDEEMED})

TRANSITIONS: dict[str, frozenset[str]] = {
    OFFERED: frozenset({ACCEPTED, CANCELLED}),
    ACCEPTED: frozenset({PENDING_VALIDATION, CANCELLED}),
    PENDING_VALIDATION: frozenset({VALIDATED, CANCELLED}),
    VALIDATED: frozenset({ISSUED, CANCELLED}),
    ISSUED: frozenset({PARTIALLY_REDEEMED, REDEEMED, EXPIRED, CANCELLED}),
    PARTIALLY_REDEEMED: frozenset({PARTIALLY_REDEEMED, REDEEMED, EXPIRED}),
    REDEEMED: frozenset(),
    EXPIRED: frozenset(),
    CANCELLED: frozenset(),
}

VALIDATION_METHODS = ("technician", "drop_greenbay", "drop_partner")

_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


class LedgerError(Exception):
    """A refused ledger action. ``code`` is stable for API callers."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _random(n: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(n))


def _unique(db: Session, column, make) -> str:
    for _ in range(20):
        value = make()
        if not db.query(PartnerCredit.id).filter(column == value).first():
            return value
    raise LedgerError("code_space", "Could not make a unique code; try again.")


def _actor(actor: str | None) -> str:
    a = (actor or "").strip()
    if not a:
        raise LedgerError("actor_required", "Say who is doing this, so the audit trail has a name.")
    return a[:100]


def _money(value) -> float:
    try:
        v = round(float(value), 2)
    except (TypeError, ValueError):
        raise LedgerError("bad_amount", "Amount must be a number.")
    if v <= 0:
        raise LedgerError("bad_amount", "Amount must be more than zero.")
    return v


def _move(
    db: Session,
    credit: PartnerCredit,
    to_status: str,
    *,
    actor: str,
    event_type: str,
    amount: float | None = None,
    note: str | None = None,
    data: dict | None = None,
) -> PartnerCredit:
    frm = credit.status
    if to_status not in TRANSITIONS.get(frm, frozenset()):
        raise LedgerError(
            "bad_transition",
            f"Credit {credit.reference} is {frm.replace('_', ' ')}; it cannot become "
            f"{to_status.replace('_', ' ')}.",
        )
    credit.status = to_status
    db.add(PartnerCreditEvent(
        credit_id=credit.id,
        partner_id=credit.partner_id,
        event_type=event_type,
        from_status=frm,
        to_status=to_status,
        amount=amount,
        actor=actor,
        note=(note or None),
        data=data,
    ))
    return credit


def record_funnel(
    db: Session,
    partner_id: str,
    event_type: str,
    *,
    qr_code: str | None = None,
    channel: str | None = None,
    shop: str | None = None,
    valuation_session_id: str | None = None,
    credit_id: str | None = None,
    amount: float | None = None,
) -> None:
    db.add(PartnerFunnelEvent(
        partner_id=partner_id,
        event_type=event_type,
        qr_code=qr_code,
        channel=channel,
        shop=shop,
        valuation_session_id=valuation_session_id,
        credit_id=credit_id,
        amount=amount,
    ))


def _funnel_from_credit(db: Session, credit: PartnerCredit, event_type: str, amount: float | None) -> None:
    record_funnel(
        db, credit.partner_id, event_type,
        qr_code=credit.qr_code, channel=credit.channel, shop=credit.shop,
        valuation_session_id=credit.valuation_session_id, credit_id=credit.id, amount=amount,
    )


# ---------------------------------------------------------------------------
# lookups (always scoped to one partner)
# ---------------------------------------------------------------------------
def get_for_partner(db: Session, partner_id: str, *, credit_id: str | None = None,
                    reference: str | None = None, voucher_code: str | None = None) -> PartnerCredit | None:
    q = db.query(PartnerCredit).filter(PartnerCredit.partner_id == partner_id)
    if credit_id:
        q = q.filter(PartnerCredit.id == credit_id)
    elif reference:
        q = q.filter(PartnerCredit.reference == reference.strip().upper())
    elif voucher_code:
        q = q.filter(PartnerCredit.voucher_code == voucher_code.strip().upper())
    else:
        return None
    return q.first()


def find_for_partner(db: Session, partner_id: str, *, query: str) -> list[PartnerCredit]:
    """Look up by voucher code, reference or phone, inside one partner only."""
    from greenbay_ai_evaluator.api.schemas import normalise_phone

    q = (query or "").strip()
    if not q:
        return []
    base = db.query(PartnerCredit).filter(PartnerCredit.partner_id == partner_id)
    code = q.upper()
    hits = base.filter((PartnerCredit.voucher_code == code) | (PartnerCredit.reference == code)).all()
    if hits:
        return hits
    phone = normalise_phone(q, "KE")
    if not phone:
        return []
    return base.filter(PartnerCredit.customer_phone == phone).order_by(PartnerCredit.created_at.desc()).all()


def for_session(db: Session, valuation_session_id: str) -> PartnerCredit | None:
    return db.query(PartnerCredit).filter(PartnerCredit.valuation_session_id == valuation_session_id).first()


# ---------------------------------------------------------------------------
# life cycle
# ---------------------------------------------------------------------------
def create_offered(db: Session, cfg: PartnerConfig, vs, *, amount: float) -> PartnerCredit:
    """A partner evaluation reached an offer: open its credit record."""
    existing = for_session(db, vs.id)
    if existing:
        return existing
    credit = PartnerCredit(
        partner_id=cfg.slug,
        valuation_session_id=vs.id,
        reference=_unique(db, PartnerCredit.reference, lambda: f"{cfg.ref_prefix}-{_random(4)}"),
        status=OFFERED,
        customer_name=getattr(vs, "seller_name", None),
        customer_phone=getattr(vs, "seller_phone", None),
        currency=getattr(vs, "currency_code", None) or cfg.currency,
        offered_amount=_money(amount),
        category=getattr(vs, "category", None),
        brand=getattr(vs, "brand", None),
        model=getattr(vs, "model", None),
        is_nominal=rules.is_nominal(cfg, getattr(vs, "age_years", None)),
        bulky=rules.is_bulky(cfg, getattr(vs, "category", None), getattr(vs, "size_value", None)),
        channel=getattr(vs, "partner_channel", None),
        shop=getattr(vs, "partner_shop", None),
        qr_code=getattr(vs, "partner_qr_code", None),
        source_tag=getattr(vs, "partner_source", None),
        buy_intent_category=getattr(vs, "buy_intent_category", None),
        buy_intent_model=getattr(vs, "buy_intent_model", None),
        offered_at=_now(),
    )
    db.add(credit)
    db.flush()
    db.add(PartnerCreditEvent(
        credit_id=credit.id, partner_id=credit.partner_id, event_type="offered",
        from_status=None, to_status=OFFERED, amount=credit.offered_amount, actor="system",
    ))
    return credit


def accept(db: Session, credit: PartnerCredit, *, amount: float, actor: str = "customer") -> PartnerCredit:
    if credit.status == ACCEPTED:
        return credit  # a double tap on Accept is not an error
    credit.offered_amount = _money(amount)
    _move(db, credit, ACCEPTED, actor=_actor(actor), event_type="accepted", amount=credit.offered_amount)
    credit.accepted_at = _now()
    _funnel_from_credit(db, credit, "accept", credit.offered_amount)
    return credit


def start_validation(db: Session, credit: PartnerCredit, *, method: str, actor: str,
                     note: str | None = None) -> PartnerCredit:
    if method not in VALIDATION_METHODS:
        raise LedgerError("bad_method", f"Inspection method must be one of {', '.join(VALIDATION_METHODS)}.")
    _move(db, credit, PENDING_VALIDATION, actor=_actor(actor), event_type="validation_started",
          note=note, data={"method": method})
    credit.validation_method = method
    credit.validation_started_at = _now()
    return credit


def validate(db: Session, credit: PartnerCredit, *, actor: str, agreed_amount: float | None = None,
             note: str | None = None) -> PartnerCredit:
    """Inspection passed. The agreed value may be lower (settled with the
    customer offline), never higher than what was accepted."""
    agreed = credit.offered_amount if agreed_amount is None else _money(agreed_amount)
    if agreed > credit.offered_amount:
        raise LedgerError("above_offer", "The agreed value cannot be more than the accepted offer.")
    _move(db, credit, VALIDATED, actor=_actor(actor), event_type="validated", amount=agreed, note=note,
          data={"accepted": credit.offered_amount, "agreed": agreed})
    credit.agreed_amount = agreed
    credit.validated_at = _now()
    _funnel_from_credit(db, credit, "validated", agreed)
    return credit


def reject_inspection(db: Session, credit: PartnerCredit, *, actor: str, note: str | None = None) -> PartnerCredit:
    if credit.status != PENDING_VALIDATION:
        raise LedgerError("bad_transition", "Only an item awaiting inspection can fail inspection.")
    _move(db, credit, CANCELLED, actor=_actor(actor), event_type="inspection_failed", note=note)
    credit.closed_at = _now()
    return credit


def issue(db: Session, credit: PartnerCredit, *, actor: str, now: datetime | None = None) -> PartnerCredit:
    cfg = get_partner(credit.partner_id)
    if cfg is None:
        raise LedgerError("unknown_partner", f"No config for partner {credit.partner_id!r}.")
    now = now or _now()
    amount = credit.agreed_amount if credit.agreed_amount is not None else credit.offered_amount
    _move(db, credit, ISSUED, actor=_actor(actor), event_type="issued", amount=amount)
    credit.voucher_code = _unique(
        db, PartnerCredit.voucher_code, lambda: f"{cfg.voucher_prefix}-{_random(4)}-{_random(2)}",
    )
    credit.issued_amount = amount
    credit.redeemed_amount = 0.0
    credit.issued_at = now
    credit.expires_at = now + timedelta(days=cfg.credit.expiry_days)
    _funnel_from_credit(db, credit, "issued", amount)
    return credit


def redeem(db: Session, *, partner_id: str, voucher_code: str, amount: float, actor: str,
           note: str | None = None, now: datetime | None = None) -> PartnerCredit:
    """Record credit spent at the partner. Looked up inside that partner only."""
    credit = get_for_partner(db, partner_id, voucher_code=voucher_code)
    if credit is None:
        raise LedgerError("not_found", "No credit with that code for this partner.")
    now = now or _now()
    # Past its date but not yet swept by expire_due: refuse without changing
    # anything, so a refused redemption never leaves a half-written row.
    if credit.status in SPENDABLE and _aware(credit.expires_at) and _aware(credit.expires_at) <= now:
        raise LedgerError("expired", f"Credit {credit.voucher_code} expired on "
                                     f"{_aware(credit.expires_at):%d %b %Y}.")
    if credit.status not in SPENDABLE:
        raise LedgerError("not_spendable", f"Credit {credit.voucher_code or credit.reference} is "
                                           f"{credit.status.replace('_', ' ')}.")
    amt = _money(amount)
    if amt > credit.balance + 1e-9:
        raise LedgerError("over_balance", f"Only {credit.currency} {credit.balance:,.0f} is left on this credit.")
    credit.redeemed_amount = round(float(credit.redeemed_amount or 0) + amt, 2)
    to = REDEEMED if credit.balance <= 0.004 else PARTIALLY_REDEEMED
    _move(db, credit, to, actor=_actor(actor), event_type="redeemed", amount=amt, note=note,
          data={"balance_after": credit.balance})
    if to == REDEEMED:
        credit.closed_at = now
    _funnel_from_credit(db, credit, "redeemed", amt)
    return credit


def cancel(db: Session, credit: PartnerCredit, *, actor: str, note: str | None = None) -> PartnerCredit:
    if credit.status == PARTIALLY_REDEEMED or float(credit.redeemed_amount or 0) > 0:
        raise LedgerError("already_spent", "Part of this credit has been spent; it cannot be cancelled.")
    _move(db, credit, CANCELLED, actor=_actor(actor), event_type="cancelled", note=note)
    credit.closed_at = _now()
    return credit


def _expire(db: Session, credit: PartnerCredit) -> None:
    _move(db, credit, EXPIRED, actor="system", event_type="expired", amount=credit.balance,
          data={"lapsed_balance": credit.balance})
    credit.closed_at = _now()


def expire_due(db: Session, *, now: datetime | None = None, partner_id: str | None = None) -> int:
    """Close every spendable credit past its expiry date. Returns how many."""
    now = now or _now()
    q = db.query(PartnerCredit).filter(PartnerCredit.status.in_(tuple(SPENDABLE)))
    if partner_id:
        q = q.filter(PartnerCredit.partner_id == partner_id)
    n = 0
    for credit in q.all():
        exp = _aware(credit.expires_at)
        if exp and exp <= now:
            _expire(db, credit)
            n += 1
    return n


def history(db: Session, credit: PartnerCredit) -> Iterable[PartnerCreditEvent]:
    return (
        db.query(PartnerCreditEvent)
        .filter(PartnerCreditEvent.credit_id == credit.id,
                PartnerCreditEvent.partner_id == credit.partner_id)
        .order_by(PartnerCreditEvent.id.asc())
        .all()
    )
