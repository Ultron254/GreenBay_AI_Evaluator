"""Pure partner rules: brand gating, the credit figure, channel tags.

No database, no network. The pricing engine is never changed for a partner:
these rules run before it (is this item accepted on this partner's site?) and
after it (what credit does the engine's figure become?).
"""

from __future__ import annotations

import re

from greenbay_ai_evaluator.partners.config import PartnerConfig

_ALNUM = re.compile(r"[^a-z0-9+]")
_TAG_SAFE = re.compile(r"[^a-z0-9-]+")

# Engine decisions that put a figure in front of the customer.
OFFER_DECISIONS = frozenset({"accept", "negotiate", "decline"})


def normalise_brand(brand: str | None) -> str:
    """'Vision Plus', 'vision-plus', 'VISION+' all become comparable keys."""
    s = _ALNUM.sub("", (brand or "").strip().lower())
    return s.replace("+", "plus")


def brand_allowed(cfg: PartnerConfig, brand: str | None) -> bool:
    key = normalise_brand(brand)
    if not key:
        return False
    allowed = {normalise_brand(b) for b in cfg.brands}
    allowed |= {normalise_brand(a) for a in cfg.brand_aliases}
    return key in allowed


def category_allowed(cfg: PartnerConfig, category: str | None) -> bool:
    return (category or "").strip().lower() in cfg.categories


def country_allowed(cfg: PartnerConfig, country: str | None) -> bool:
    return (country or "KE").strip().upper() in cfg.countries


def is_nominal(cfg: PartnerConfig, age_years: float | None) -> bool:
    """Very old items get the flat nominal value, whatever the engine says."""
    try:
        return float(age_years or 0) >= cfg.credit.nominal_age_years
    except (TypeError, ValueError):
        return False


def credit_offer(
    cfg: PartnerConfig,
    *,
    decision: str | None,
    opening_offer: float | None,
    age_years: float | None,
    risk_score: float | None,
) -> float | None:
    """The credit the customer is offered on a partner site, or None.

    None means a person decides: the engine routed the item to review, or the
    risk score is too high to put any figure on screen. Otherwise the credit
    equals the engine's offer, never below the floor, and items at or over the
    nominal age get the nominal value.
    """
    try:
        if risk_score is not None and float(risk_score) > cfg.credit.max_auto_risk_score:
            return None
    except (TypeError, ValueError):
        pass
    if is_nominal(cfg, age_years):
        return float(cfg.credit.nominal_value)
    if (decision or "").strip().lower() not in OFFER_DECISIONS:
        return None
    try:
        offer = float(opening_offer or 0)
    except (TypeError, ValueError):
        offer = 0.0
    return float(max(offer, cfg.credit.floor))


def credit_for_accept(
    cfg: PartnerConfig,
    *,
    accepted_amount: float | None,
    offered_credit: float | None,
    age_years: float | None,
) -> float:
    """Credit stored when the customer accepts.

    The accepted amount has already been clamped to the engine's ceiling by
    the accept route. The floor and the nominal rule still apply on top.
    """
    if is_nominal(cfg, age_years):
        return float(cfg.credit.nominal_value)
    base = accepted_amount if accepted_amount and accepted_amount > 0 else (offered_credit or 0)
    return float(max(float(base or 0), cfg.credit.floor))


def is_bulky(cfg: PartnerConfig, category: str | None, size_value: float | None) -> bool:
    """Bulky items get a technician visit; small ones are dropped at a shop."""
    cat = (category or "").strip().lower()
    if cat not in cfg.bulky_categories:
        return False
    min_size = cfg.bulky_min_size.get(cat)
    if min_size is None:
        return True
    try:
        return float(size_value or 0) >= min_size
    except (TypeError, ValueError):
        return False


def _tag_part(value: str | None) -> str:
    return _TAG_SAFE.sub("-", (value or "").strip().lower()).strip("-")[:40]


def source_tag(slug: str, channel: str | None, shop: str | None, placement: str | None) -> str:
    """One readable label per lead: mika-roysambu-sticker, mika-website-banner."""
    parts = [_tag_part(slug), _tag_part(shop) or _tag_part(channel) or "direct"]
    if placement:
        parts.append(_tag_part(placement))
    return "-".join(p for p in parts if p)[:160]
