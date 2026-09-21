"""
Deterministic offer engine for GreenBay trade-in valuations (v6).

This module contains PURE COMPUTATION — no LLM calls, no network calls.
All data gathering (comparables, image scores, risk scores, reference data)
must happen before calling ``compute_valuation()``.

Every intermediate value is returned in the ``ValuationResult`` so it can
be stored for full auditability.

v6 changes:
  - Data-driven acquisition ratios from 1,697 real sales
  - Linear-interpolation depreciation curve with category modifiers
  - Condition factors: A=1.00, B=0.85, C=0.70, D=route-to-human
  - Confidence scoring aware of matrix + sales-stock matches
  - 80% confidence floor for customer-facing prices
"""

from __future__ import annotations

ENGINE_VERSION = "6.0.0-data-driven-2026-05-24"

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.config import (
    DEFECT_DEDUCTIONS,
    DEFAULT_DEFECT_DEDUCTION,
)
from greenbay_ai_evaluator.services.reference_data_service import (
    get_category_acquisition_ratio,
)


# ---------------------------------------------------------------------------
# Depreciation curve — derived from pricing matrix age bands.
# Tuples of (age_years_midpoint, remaining_value_fraction).
# ---------------------------------------------------------------------------
_DEPRECIATION_CURVE: list[tuple[float, float]] = [
    (0.0, 1.00),
    (0.5, 0.85),   # 0–12 months midpoint
    (1.5, 0.72),   # 1–2 years midpoint
    (2.5, 0.60),   # 2–3 years
    (3.5, 0.50),   # 3–4 years
    (4.5, 0.42),   # 4–5 years
    (7.5, 0.35),   # 5–10 years midpoint
    (12.0, 0.22),  # 10+ years
]

# Category-specific depreciation modifiers (multiplied onto the curve value)
_CATEGORY_DEPRECIATION_MODIFIERS: dict[str, float] = {
    "tv": 1.10,
    "tv_monitor": 1.10,
    "refrigerator": 1.00,
    "fridge": 1.00,
    "freezer": 1.00,
    "chiller": 1.00,
    "washing_machine": 0.97,
    "washer": 0.97,
    "cooker": 0.95,
    "cooker_oven": 0.95,
    "microwave": 0.90,
}

# Condition multipliers (v6 — aligned with matrix grade logic)
CONDITION_FACTORS: dict[str, float] = {
    "A": 1.00,
    "B": 0.85,
    "C": 0.70,
}

# Floor/ceiling guardrails: prevent wild outliers relative to team history.
PRICE_FLOOR_RATIO: float = 0.60
PRICE_CEILING_RATIO: float = 1.50

# Hard consistency ceiling: a used trade-in offer may never approach the price
# of a brand-new unit. This is the strongest sanity net — it catches both
# over-valuation (offer too high) AND a wrongly-low sourced new price (which
# would otherwise let the offer exceed "new").
#
# Calibrated Jul 2026 against the internal team's own closed evaluations
# (n=114 pairs). The team's median pay-ratio vs new: TV 0.59 (IQR 0.47-0.77),
# fridge 0.50 (0.40-0.93), washer 0.52 (0.42-1.04), microwave 0.26, cooker
# 0.33. The previous caps (TV 0.45, fridge/washer 0.50) sat AT or BELOW the
# team's median — mathematically forbidding the engine from matching the team
# on half of all good units. Caps now sit near the team's ~P80 so they catch
# absurdity without clipping normal deals; the *targeting* is done by the
# calibrated acquisition ratios, not by this ceiling.
DEFAULT_MAX_TRADEIN_TO_NEW: float = 0.60
MAX_TRADEIN_TO_NEW: dict[str, float] = {
    "tv": 0.70, "tv_monitor": 0.70,
    "refrigerator": 0.70, "fridge": 0.70, "freezer": 0.70, "chiller": 0.70,
    "washing_machine": 0.70, "dishwasher": 0.70,
    "cooker": 0.60, "cooker_oven": 0.60, "oven": 0.60,
    "microwave": 0.45, "water_dispenser": 0.45, "air_fryer": 0.45,
    "soundbar": 0.40, "woofer": 0.40, "speaker": 0.40, "home_theatre": 0.40,
    "fan": 0.35, "iron_box": 0.30, "kettle": 0.30, "blender": 0.30,
}


def _max_tradein_to_new(category: str) -> float:
    """Resolve the max offer-to-new ratio for a category (separator-insensitive)."""
    if not category:
        return DEFAULT_MAX_TRADEIN_TO_NEW
    key = re.sub(r"[\s\-/]+", "_", category.lower().strip())
    if key in MAX_TRADEIN_TO_NEW:
        return MAX_TRADEIN_TO_NEW[key]
    for k, v in MAX_TRADEIN_TO_NEW.items():
        if k in key or key in k:
            return v
    return DEFAULT_MAX_TRADEIN_TO_NEW


# Lower consistency floor (STEP 11c): when a VERIFIED new-price signal exists,
# the offer must not collapse to a token amount (e.g. KES 1,000 for a microwave
# that retails at 17,000 new). The floor is the engine's own math re-derived
# straight from the new price — new_price x depreciation x condition − defects,
# then x acquisition ratio — scaled by a safety margin so legitimately cheap
# outcomes (very old / battered units) still pass. A breach means the base
# value was under-sourced (e.g. a used listing mistaken for new), so the offer
# is raised to the floor AND routed to human review.
NEW_PRICE_FLOOR_SAFETY: float = 0.60


def select_new_price_estimate(
    gemini_new: float,
    matrix_new: float,
    reconciled: float,
    frontend_new: float,
    last_resort: float = 0.0,
) -> tuple[float, str]:
    """Choose the "New Price (Estimate)" from the available signals.

    Priority: Gemini grounded launch price -> pricing-matrix new price ->
    reconciled blend -> frontend guess. BUT Gemini grounding can occasionally
    return the wrong variant/bundle (e.g. a premium washer-dryer priced at
    136,999 for a plain 8kg washer). When the curated matrix also has a new
    price and Gemini deviates by more than 2x in either direction, the matrix
    wins — its rows are size/spec-specific and human-curated.

    Returns (price, source_label). Pure function so it is unit-testable.
    """
    def _pos(v) -> float:
        try:
            return float(v) if v and float(v) > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    g, m, r, f = _pos(gemini_new), _pos(matrix_new), _pos(reconciled), _pos(frontend_new)
    if g and m and (g > m * 2.0 or g < m * 0.5):
        return m, "matrix (gemini outlier rejected)"
    if g:
        return g, "gemini"
    if m:
        return m, "matrix"
    if r:
        return r, "reconciled"
    if f:
        return f, "frontend"
    return _pos(last_resort), "last_resort"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class PricingPolicyData:
    """Flat representation of a PricingPolicy row (no ORM dependency)."""

    category: str
    margin_pct: float
    max_offer_pct: float
    walkaway_pct: float
    depreciation_year1: float = 0.25
    depreciation_year2_3: float = 0.15
    depreciation_year4_5: float = 0.10
    depreciation_year6_plus: float = 0.07
    round_step_pct: float = 0.05
    max_negotiation_rounds: int = 3
    brand_premium_json: dict[str, float] = field(default_factory=dict)
    condition_multiplier_json: dict[str, float] = field(default_factory=dict)


@dataclass
class Comparable:
    """A single market comparable."""

    resale_price: float
    weight: float = 1.0


@dataclass
class ValuationResult:
    """Full output of ``compute_valuation()`` — every field is auditable."""

    # Core outputs
    estimated_resale_value: float
    acquisition_ceiling: float
    opening_offer: float
    walkaway_limit: float
    confidence_score: float
    decision: str  # accept | negotiate | decline | review
    decision_reason: str

    # Intermediates
    base_value: float
    base_value_source: str
    brand_adjusted_value: float
    condition_adjusted_value: float
    defect_deduction_total: float
    defect_details: list[dict[str, Any]]
    image_quality_penalty_applied: bool
    risk_adjustment_applied: bool

    # Pass-through inputs for snapshot
    condition_grade: str
    risk_score: float

    # Multi-source price verification
    price_verification: dict[str, Any] = field(default_factory=dict)

    # Pricing justification trace (human-readable breakdown)
    pricing_justification: str = ""

    # Guardrail info
    floor_applied: bool = False
    ceiling_applied: bool = False
    guardrail_note: str = ""
    new_price_ceiling_applied: bool = False
    new_price_floor_applied: bool = False


# ---------------------------------------------------------------------------
# Depreciation helper (v6 — linear interpolation)
# ---------------------------------------------------------------------------
def depreciation_factor(age_years: float, category: str = "") -> float:
    """Return remaining-value fraction after *age_years* of use.

    Uses linear interpolation between band midpoints from the pricing matrix,
    then applies a category-specific modifier. Result clamped to [0.15, 0.90].
    """
    if age_years <= 0:
        return min(0.90, 1.00)

    curve = _DEPRECIATION_CURVE

    # Interpolate
    if age_years <= curve[0][0]:
        base = curve[0][1]
    elif age_years >= curve[-1][0]:
        base = curve[-1][1]
    else:
        for i in range(len(curve) - 1):
            x0, y0 = curve[i]
            x1, y1 = curve[i + 1]
            if x0 <= age_years <= x1:
                t = (age_years - x0) / (x1 - x0) if (x1 - x0) > 0 else 0
                base = y0 + t * (y1 - y0)
                break
        else:
            base = curve[-1][1]

    cat_key = category.lower().strip() if category else ""
    modifier = _CATEGORY_DEPRECIATION_MODIFIERS.get(cat_key, 1.00)
    result = base * modifier

    return max(0.15, min(0.90, result))


# ---------------------------------------------------------------------------
# Condition factor helper
# ---------------------------------------------------------------------------
def condition_factor(grade: str) -> float | None:
    """Return the condition multiplier for a grade, or None for Grade D (route to human)."""
    g = grade.upper().strip() if grade else "C"
    if g == "D":
        return None
    return CONDITION_FACTORS.get(g, 0.70)


# ---------------------------------------------------------------------------
# Confidence score helper (v6 — matrix + sales-stock aware)
# ---------------------------------------------------------------------------
# The price sources reconcile_retail_price can count: internet lookup, frontend,
# historical sheet, expert feedback, database comparables, marketplace, Shopify.
PRICE_VERIFICATION_SOURCE_COUNT = 7
# Outages beyond this many are ignored by the scaling. With more than three of
# seven sources down the evaluation is simply short of evidence, and the
# scaling must not grow with the size of the outage (uncapped, six down would
# add 18 points). The evaluate route can report two today.
MAX_RENORMALISED_OUTAGES = 3
_VERIFICATION_TIERS = (4, 3, 2, 1)  # sources needed for 30 / 25 / 20 / 12 points


def _verification_tier_thresholds(unavailable: int = 0) -> tuple[int, int, int, int]:
    """Source counts needed for the 30/25/20/12-point verification tiers.

    With no outage these are the fixed 4/3/2/1. With *unavailable* sources
    down they shrink in proportion to the sources that could answer, rounded
    UP (so a partial source never earns a tier) and never below 1. One source
    down of seven changes nothing (4*6/7 = 3.43 -> 4); it takes two down for
    three answers to earn the top tier. The count is clamped to
    MAX_RENORMALISED_OUTAGES, which keeps the gain at 5 points or less.
    Deliberately mild: fewer answers is less evidence, and an outage must not
    manufacture confidence.
    """
    import math

    down = max(0, min(int(unavailable or 0), MAX_RENORMALISED_OUTAGES))
    if down == 0:
        return _VERIFICATION_TIERS
    share = (PRICE_VERIFICATION_SOURCE_COUNT - down) / PRICE_VERIFICATION_SOURCE_COUNT
    return tuple(max(1, math.ceil(t * share - 1e-9)) for t in _VERIFICATION_TIERS)  # type: ignore[return-value]


def _compute_confidence(
    comparables_count: int,
    image_quality_score: float,
    has_seller_asking: bool,
    has_age: bool,
    has_brand: bool,
    price_verification_sources: int = 0,
    has_model: bool = False,
    has_vision_analysis: bool = False,
    has_historical_data: bool = False,
    has_matrix_match: bool = False,
    has_sales_stock_match: bool = False,
    unavailable_verification_sources: int = 0,
    renormalise_unavailable_sources: bool = False,
) -> float:
    """Deterministic confidence score in [0, 100].

    Hard caps based on available evidence (v6):
      - No historical/matrix data + no market lookup       = MAX 35%
      - Only internet retail price (no comparables)        = MAX 50%
      - Matrix match OR 1-2 comparables                    = MAX 70%
      - Matrix match + 3+ comparables                      = MAX 85%
      - Matrix + comparables + sales-stock match           = up to 97%

    Source outages (Sep 2026). *unavailable_verification_sources* is how many
    of the PRICE_VERIFICATION_SOURCE_COUNT price sources were DOWN for this
    evaluation (infrastructure failure, not "no data found"). It is ignored
    unless *renormalise_unavailable_sources* is True (setting
    CONFIDENCE_RENORMALISE_UNAVAILABLE_SOURCES, default False = score exactly
    as before). When on, the price-verification tiers below are scaled to the
    sources that could answer; a dead source is never counted as evidence, the
    other blocks are untouched and every hard cap still applies, so the most
    this can add is 5 points.
    """
    score = 0.0

    # Data completeness (max 20 pts)
    if has_brand:
        score += 5.0
    if has_age:
        score += 5.0
    if has_seller_asking:
        score += 3.0
    if has_model:
        score += 4.0
    if has_vision_analysis:
        score += 3.0

    # Image quality (max 10 pts)
    score += (image_quality_score / 100.0) * 10.0

    # Historical / comparable evidence (max 35 pts)
    if has_sales_stock_match:
        score += 35.0
    elif comparables_count >= 5:
        score += 35.0
    elif comparables_count >= 3:
        score += 25.0
    elif has_matrix_match:
        score += 20.0
    elif comparables_count >= 1:
        score += 15.0
    elif has_historical_data:
        score += 10.0

    # Price verification from market sources (max 30 pts)
    t4, t3, t2, t1 = _verification_tier_thresholds(
        unavailable_verification_sources if renormalise_unavailable_sources else 0
    )
    if price_verification_sources >= t4:
        score += 30.0
    elif price_verification_sources >= t3:
        score += 25.0
    elif price_verification_sources >= t2:
        score += 20.0
    elif price_verification_sources >= t1:
        score += 12.0

    # Hard caps based on evidence quality
    if has_sales_stock_match and has_matrix_match and comparables_count >= 1:
        score = min(score, 97.0)
    elif has_matrix_match and comparables_count >= 3:
        score = min(score, 85.0)
    elif has_matrix_match or (1 <= comparables_count <= 2):
        score = min(score, 70.0)
    elif comparables_count == 0 and not has_matrix_match and price_verification_sources >= 1 and not has_historical_data:
        score = min(score, 50.0)
    elif comparables_count == 0 and not has_matrix_match and price_verification_sources == 0 and not has_historical_data:
        score = min(score, 35.0)

    return min(97.0, round(score, 1))


# ---------------------------------------------------------------------------
# Round price to nearest step (multi-currency)
# ---------------------------------------------------------------------------
def _round_price(value: float, round_step: int = 500) -> float:
    """Round a price to the nearest *round_step*."""
    if value <= 0:
        return 0.0
    return round(value / round_step) * round_step


def _round_kes_500(value: float) -> float:
    """Backward-compatible alias."""
    return _round_price(value, 500)


# ---------------------------------------------------------------------------
# Multi-source price reconciliation (60% human intelligence / 40% AI market)
# ---------------------------------------------------------------------------
def _tier_weighted_average(entries: list[tuple[float, float]]) -> float | None:
    if not entries:
        return None
    tw = sum(w for _, w in entries)
    if tw <= 0:
        return None
    return sum(p * w for p, w in entries) / tw


def reconcile_retail_price(
    *,
    frontend_price: float,
    internet_price: float | None = None,
    marketplace_avg: float | None = None,
    shopify_avg: float | None = None,
    expert_avg: float | None = None,
    historical_avg: float | None = None,
    comparables_avg: float | None = None,
    frontend_source: str = "",
) -> dict[str, Any]:
    """Reconcile retail price from multiple independent signals.

    *frontend_source* lets the caller flag that the frontend price is merely a
    hardcoded category-default estimate (e.g. "category_default"). When it is,
    that price is NOT counted as a real verification source — so a fabricated
    guess can never masquerade as a verified market price, and confidence does
    not get inflated by it.

    CRITICAL CONTRACT (Jul 2026 rewrite): ``reconciled_price`` is the NEW-retail
    price. Only genuine new-price signals may enter the blend:
      - internet_lookup (Gemini/Sonar grounded launch price)
      - frontend, when it is a real user/db-sourced retail price

    Trade-in-level signals — historical closed-deal prices, expert trade-in
    feedback, past resale comparables — are recorded for transparency and still
    count toward corroboration (num_real_sources), but are EXCLUDED from the
    blend. Mixing them in was the root cause of systematic under-pricing: a
    team trade-in price (~50% of new) dragged the "new price" down to trade-in
    level, the genuine Gemini new price was then discarded as a >2x "outlier",
    and the trade-in-to-new ceiling squashed the offer to ~25% of fair value
    (e.g. Roch RCF-300-G offered at 5,000 vs a 38,000 new price). Trade-in
    history keeps its proper channel: the historical floor/ceiling guardrail
    (STEP 11) anchored on ``historical_team_avg``.
    """
    sources: list[dict[str, Any]] = []
    _frontend_is_estimate = frontend_source.lower().strip() in (
        "category_default", "whatsapp_category_estimate", "default", "",
    )

    # Trade-in / resale-level intel: listed + counted for corroboration, never
    # blended into the NEW price.
    tradein_specs: list[tuple[str, float | None, float, str]] = [
        ("historical_sheet", historical_avg, 8.0,
         "closed-deal trade-in price, not a new-price signal"),
        ("expert_feedback", expert_avg, 7.0,
         "expert trade-in feedback, not a new-price signal"),
        ("database_comparables", comparables_avg, 5.0,
         "past resale comparables, not a new-price signal"),
        ("marketplace_jiji_jumia", marketplace_avg, 2.5,
         "used-listing price, not a new-price signal"),
        ("shopify_inventory", shopify_avg, 2.0,
         "refurbished inventory price, not a new-price signal"),
    ]
    tradein_entries: list[tuple[float, float]] = []
    for key, price, w, reason in tradein_specs:
        if price is not None and price > 0:
            tradein_entries.append((price, w))
            sources.append({
                "source": key, "price": price, "weight": w,
                "tier": "tradein_intelligence",
                "excluded_from_blend": True,
                "exclusion_reason": reason,
            })

    # Genuine NEW-price signals — the only inputs to the blend.
    new_specs: list[tuple[str, float | None, float]] = [
        ("internet_lookup", internet_price, 3.0),
        ("frontend", frontend_price if frontend_price > 0 else None, 1.0),
    ]
    new_entries: list[tuple[float, float]] = []
    for key, price, w in new_specs:
        if price is not None and price > 0:
            is_estimate = key == "frontend" and _frontend_is_estimate
            if not is_estimate:
                new_entries.append((price, w))
            src_entry = {
                "source": key, "price": price, "weight": w,
                "tier": "new_price_signal",
            }
            if is_estimate:
                src_entry["is_estimate"] = True
                src_entry["excluded_from_blend"] = True
            sources.append(src_entry)

    tradein_avg = _tier_weighted_average(tradein_entries)
    new_avg = _tier_weighted_average(new_entries)

    if new_avg is not None:
        reconciled = new_avg
    else:
        # No genuine new-price signal: fall back to the frontend estimate so
        # downstream always has *something*, but new_price_verified stays False.
        reconciled = frontend_price if frontend_price > 0 else 0.0

    num_sources = len(sources)
    # Real sources exclude the frontend category-default estimate. Trade-in
    # intel still corroborates that the item/valuation is grounded in data.
    num_real_sources = sum(1 for s in sources if not s.get("is_estimate"))
    # Verified only when a genuine NEW-price signal drove the blend.
    new_price_verified = new_avg is not None

    return {
        "reconciled_price": _round_kes_500(reconciled),
        "sources": sources,
        "num_sources": num_sources,
        "num_real_sources": num_real_sources,
        "new_price_verified": new_price_verified,
        # Canonical names post-rewrite; the legacy keys are kept as aliases so
        # anything reading the raw dict keeps working, but note their meaning:
        # "human_intelligence_avg" is the TRADE-IN-level average (excluded from
        # the blend) and "ai_market_research_avg" is the NEW-price signal avg.
        "tradein_intelligence_avg": round(tradein_avg, 2) if tradein_avg is not None else None,
        "new_price_signal_avg": round(new_avg, 2) if new_avg is not None else None,
        "human_intelligence_avg": round(tradein_avg, 2) if tradein_avg is not None else None,
        "ai_market_research_avg": round(new_avg, 2) if new_avg is not None else None,
        "confidence": min(100.0, num_real_sources * 20.0 + 10.0),
        "frontend_price": frontend_price,
    }


# ---------------------------------------------------------------------------
# Main entry point (v6)
# ---------------------------------------------------------------------------
def compute_valuation(
    *,
    category: str,
    brand: str,
    model: str,
    age_years: float,
    condition_grade: str,
    condition_score: float,
    defects: list[dict[str, Any]],
    seller_asking_price: float | None,
    image_quality_score: float,
    risk_score: float,
    comparables: list[Comparable],
    pricing_policy: PricingPolicyData,
    retail_price: float,
    price_verification: dict[str, Any] | None = None,
    historical_team_avg: float | None = None,
    # v6 new parameters
    reference_resale_value: float | None = None,
    reference_source: str = "",
    has_matrix_match: bool = False,
    has_sales_stock_match: bool = False,
    round_step: int = 500,
    # Sep 2026: price sources that were DOWN (infrastructure failure) while this
    # evaluation ran, e.g. ["internet_lookup"]. Always named in the trace and
    # the review reason so an outage is never silent; they change the score
    # only when renormalise_unavailable_sources is True (default False).
    unavailable_sources: list[str] | None = None,
    renormalise_unavailable_sources: bool = False,
) -> ValuationResult:
    """Deterministic valuation computation (v6 data-driven formula).

    The caller resolves the best data source (sales stock > matrix > external)
    and passes it via *reference_resale_value* / *reference_source*.

    Formula:
      resale_value = new_price * depreciation_factor(age) * condition_factor(grade)
      trade_in_offer = resale_value * acquisition_ratio[category]
    """
    trace_lines: list[str] = []
    trace_lines.append("PRICE CALCULATION BREAKDOWN (v6 data-driven)")
    trace_lines.append("=" * 50)
    trace_lines.append(
        f"Product: {brand} {model} {category} "
        f"({age_years:.1f} years, Condition {condition_grade})"
    )
    trace_lines.append("")

    # -- STEP 1: Determine base resale value ---------------------------------
    grade_upper = condition_grade.upper().strip() if condition_grade else "C"
    cond_mult = condition_factor(grade_upper)
    if cond_mult is None:
        cond_mult = 0.70

    if reference_resale_value and reference_resale_value > 0 and reference_source in (
        "sales_stock_exact", "sales_stock_category"
    ):
        # Sales stock gives us a real resale price — use directly
        base_value = reference_resale_value
        base_value_source = reference_source
        trace_lines.append(
            f"Base value ({reference_source}): "
            f"median resale from GreenBay stock = KES {base_value:,.0f}"
        )
    elif reference_resale_value and reference_resale_value > 0 and reference_source == "matrix":
        # Matrix gives recommended price range — apply depreciation + condition
        depr = depreciation_factor(age_years, category)
        base_value = reference_resale_value * depr * cond_mult
        base_value_source = "matrix"
        trace_lines.append(
            f"Base value (matrix): KES {reference_resale_value:,.0f} "
            f"* depr {depr:.3f} * cond {cond_mult:.2f} = KES {base_value:,.0f}"
        )
    else:
        # Fallback: use reconciled/frontend retail price + depreciation
        effective_retail = retail_price
        if price_verification and price_verification.get("reconciled_price"):
            reconciled = price_verification["reconciled_price"]
            if reconciled > 0 and (retail_price <= 0 or 0.2 <= reconciled / max(retail_price, 1) <= 5.0):
                effective_retail = reconciled

        depr = depreciation_factor(age_years, category)
        base_value = effective_retail * depr * cond_mult
        base_value_source = "external_research"
        trace_lines.append(
            f"Base value (external): KES {effective_retail:,.0f} "
            f"* depr {depr:.3f} * cond {cond_mult:.2f} = KES {base_value:,.0f}"
        )

    # -- STEP 2: Brand adjustment --------------------------------------------
    brand_key = brand.strip() if brand else ""
    brand_premium = pricing_policy.brand_premium_json.get(brand_key, 1.0)
    brand_adjusted_value = base_value * brand_premium
    if brand_premium != 1.0:
        trace_lines.append(f"Brand premium ({brand_key}: {brand_premium}): KES {brand_adjusted_value:,.0f}")

    # -- STEP 3: Condition already applied in base_value for matrix/external --
    # For sales stock sources, condition was already factored in at sale time,
    # but we still apply it as a quality multiplier
    if base_value_source in ("sales_stock_exact", "sales_stock_category"):
        condition_adjusted_value = brand_adjusted_value * cond_mult
        trace_lines.append(
            f"Condition multiplier ({grade_upper}: {cond_mult}): "
            f"KES {condition_adjusted_value:,.0f}"
        )
    else:
        condition_adjusted_value = brand_adjusted_value
        trace_lines.append(
            f"Condition ({grade_upper}: {cond_mult}): already applied in base value"
        )

    # -- STEP 4: Defect deductions -------------------------------------------
    cat_lower = category.lower().strip() if category else ""
    cat_deductions = DEFECT_DEDUCTIONS.get(cat_lower, {})

    defect_details: list[dict[str, Any]] = []
    defect_total = 0.0

    for defect in defects:
        defect_type = defect.get("type", "unknown").lower().replace(" ", "_")
        kes = cat_deductions.get(defect_type, DEFAULT_DEFECT_DEDUCTION)
        defect_details.append({"type": defect_type, "deduction_kes": kes})
        defect_total += kes

    max_deduction = condition_adjusted_value * 0.60
    defect_total = min(defect_total, max_deduction)

    after_defects = condition_adjusted_value - defect_total
    if defect_total > 0:
        trace_lines.append(f"Defect deductions: -KES {defect_total:,.0f}")
        trace_lines.append(f"After defects: KES {after_defects:,.0f}")

    # -- STEP 5: Image quality penalty ---------------------------------------
    image_quality_penalty_applied = False
    if image_quality_score < 50:
        after_defects *= 0.95
        image_quality_penalty_applied = True
        trace_lines.append(f"Image quality penalty (-5%): KES {after_defects:,.0f}")

    # -- STEP 6: Estimated resale value --------------------------------------
    estimated_resale_value = _round_price(max(0.0, after_defects), round_step)
    trace_lines.append(f"Estimated resale value: KES {estimated_resale_value:,.0f}")

    # -- STEP 7: Trade-in acquisition factor (v6 — category-specific) --------
    acq_ratio = get_category_acquisition_ratio(category)
    acquisition_price = _round_price(estimated_resale_value * acq_ratio, round_step)
    trace_lines.append(
        f"Acquisition ratio ({category}: {acq_ratio}): "
        f"KES {acquisition_price:,.0f}"
    )

    # -- STEP 8: Ceiling, opening, walkaway ----------------------------------
    acquisition_ceiling = _round_price(acquisition_price * 1.3, round_step)
    opening_offer = acquisition_price
    walkaway_limit = _round_price(acquisition_price * 0.7, round_step)

    # -- STEP 9: Confidence score --------------------------------------------
    # Use REAL sources only (a frontend category-default guess must not inflate
    # confidence or make a fabricated price look verified).
    if price_verification:
        num_pv_sources = price_verification.get(
            "num_real_sources", price_verification.get("num_sources", 0)
        )
    else:
        num_pv_sources = 0
    has_vision = bool(
        price_verification
        and (price_verification.get("google_lens_data") or price_verification.get("vision_analysis"))
    )
    has_historical = bool(historical_team_avg and historical_team_avg > 0)
    confidence_score = _compute_confidence(
        comparables_count=len(comparables),
        image_quality_score=image_quality_score,
        has_seller_asking=seller_asking_price is not None,
        has_age=age_years > 0,
        has_brand=bool(brand_key),
        price_verification_sources=num_pv_sources,
        has_model=bool(model),
        has_vision_analysis=has_vision,
        has_historical_data=has_historical,
        has_matrix_match=has_matrix_match,
        has_sales_stock_match=has_sales_stock_match,
        unavailable_verification_sources=len(unavailable_sources or []),
        renormalise_unavailable_sources=renormalise_unavailable_sources,
    )

    # -- STEP 10: Risk adjustment --------------------------------------------
    risk_adjustment_applied = False
    needs_review_risk = risk_score > 70
    if risk_score > 50:
        acquisition_ceiling = round(acquisition_ceiling * 0.90, 2)
        risk_adjustment_applied = True
        trace_lines.append(f"Risk adjustment (-10% ceiling): KES {acquisition_ceiling:,.0f}")

    # -- STEP 11: Floor/Ceiling guardrails -----------------------------------
    floor_applied = False
    ceiling_applied = False
    guardrail_note = ""

    if historical_team_avg and historical_team_avg > 0:
        floor_price = _round_price(historical_team_avg * PRICE_FLOOR_RATIO, round_step)
        ceiling_price = _round_price(historical_team_avg * PRICE_CEILING_RATIO, round_step)

        if opening_offer < floor_price:
            guardrail_note = (
                f"Price floor applied: AI calculated KES {opening_offer:,.0f} but "
                f"historical team average for {brand} {category} is "
                f"KES {historical_team_avg:,.0f}. Raised to KES {floor_price:,.0f} "
                f"({PRICE_FLOOR_RATIO:.0%} floor)."
            )
            logger.warning(guardrail_note)
            trace_lines.append(f"GUARDRAIL: {guardrail_note}")
            opening_offer = floor_price
            acquisition_ceiling = _round_price(opening_offer * 1.3, round_step)
            walkaway_limit = _round_price(opening_offer * 0.7, round_step)
            floor_applied = True
        elif opening_offer > ceiling_price:
            guardrail_note = (
                f"Price ceiling applied: AI calculated KES {opening_offer:,.0f} but "
                f"historical team average for {brand} {category} is "
                f"KES {historical_team_avg:,.0f}. Capped to KES {ceiling_price:,.0f} "
                f"({PRICE_CEILING_RATIO:.0%} ceiling)."
            )
            logger.warning(guardrail_note)
            trace_lines.append(f"GUARDRAIL: {guardrail_note}")
            opening_offer = ceiling_price
            acquisition_ceiling = _round_price(opening_offer * 1.3, round_step)
            walkaway_limit = _round_price(opening_offer * 0.7, round_step)
            ceiling_applied = True

    # -- STEP 11b: New-price consistency ceiling -----------------------------
    # The offer must never approach the price of a brand-new unit. Anchor on the
    # reconciled (market-verified) new price, falling back to the supplied retail
    # price. If the offer breaches the per-category ceiling it means either the
    # offer is too high OR the sourced new price is too low — both are wrong, so
    # we cap the offer AND route to human review (confidence forced below 80).
    new_price_ceiling_applied = False
    new_price_anchor = 0.0

    def _f(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    # Anchor on a GENUINE new-price signal. The blended ``reconciled_price`` mixes
    # resale/acquisition figures and can sit well below the true new price, which
    # would make this ceiling fire spuriously. Prefer the grounded internet
    # (Gemini) new-price source when present, then the blend, ignoring the
    # frontend category-default estimate (used only as a last resort).
    anchor_verified = False  # True only when the anchor came from real sources
    if price_verification:
        internet_new = 0.0
        for s in (price_verification.get("sources") or []):
            if s.get("source") == "internet_lookup" and not s.get("discarded"):
                internet_new = max(internet_new, _f(s.get("price")))
        new_price_anchor = max(internet_new, _f(price_verification.get("reconciled_price")))
        # Verified only when a genuine new-price signal exists — the reconciled
        # value can be a frontend-estimate fallback, which must never arm the
        # floor (11c) as if it were market-verified.
        anchor_verified = internet_new > 0 or bool(
            price_verification.get("new_price_verified")
        )
    if new_price_anchor <= 0:
        new_price_anchor = _f(retail_price)

    if new_price_anchor > 0:
        max_ratio = _max_tradein_to_new(category)
        new_price_cap = _round_price(new_price_anchor * max_ratio, round_step)
        if new_price_cap > 0 and opening_offer > new_price_cap:
            # Graduated response (Jul 2026): a near-new grade-A item can
            # structurally overshoot the cap by a few percent (depr 0.90 x
            # cond 1.0 x calibrated acq 0.81+ > 0.70) — that is the math
            # working, not an unreliable input, so clip silently. Only a
            # MEANINGFUL overshoot (>15%) signals a genuinely suspect offer
            # or new price and forces human review.
            overshoot = opening_offer / new_price_cap
            big_breach = overshoot > 1.15
            guardrail_note = (
                f"New-price ceiling: offer KES {opening_offer:,.0f} exceeded "
                f"{max_ratio:.0%} of the new price (KES {new_price_anchor:,.0f}). "
                f"Capped to KES {new_price_cap:,.0f}"
                + (
                    " and flagged for review (either the offer was too high "
                    "or the sourced new price too low)."
                    if big_breach else
                    " (small structural overshoot; inputs consistent)."
                )
            )
            logger.warning(guardrail_note)
            trace_lines.append(f"GUARDRAIL: {guardrail_note}")
            opening_offer = new_price_cap
            acquisition_ceiling = _round_price(opening_offer * 1.3, round_step)
            walkaway_limit = _round_price(opening_offer * 0.7, round_step)
            new_price_ceiling_applied = True
            if big_breach:
                # A large breach signals an unreliable input — never ship it
                # as "verified".
                confidence_score = min(confidence_score, 75.0)

    # -- STEP 11c: New-price consistency floor --------------------------------
    # Symmetric counterpart to 11b. Every other guardrail is an UPPER bound, so
    # an under-sourced base value (e.g. a used/resale listing mistaken for new)
    # could collapse the offer to a token amount with nothing to catch it.
    # Re-derive the expected offer straight from the verified new price using
    # the engine's own factors; if the computed offer is far below that, raise
    # it to the floor and force human review. Only fires on a VERIFIED anchor —
    # a frontend category-default guess must never inflate an offer.
    new_price_floor_applied = False
    if anchor_verified and new_price_anchor > 0 and opening_offer > 0:
        depr = depreciation_factor(age_years, category)
        cond_f = CONDITION_FACTORS.get(grade_upper, 0.70)
        expected_from_new = max(
            0.0, new_price_anchor * depr * cond_f - defect_total
        ) * acq_ratio
        min_offer = _round_price(
            expected_from_new * NEW_PRICE_FLOOR_SAFETY, round_step
        )
        # Never let the floor push the offer above the 11b ceiling.
        max_cap = _round_price(
            new_price_anchor * _max_tradein_to_new(category), round_step
        )
        if max_cap > 0:
            min_offer = min(min_offer, max_cap)
        if min_offer > 0 and opening_offer < min_offer:
            guardrail_note = (
                f"New-price floor: offer KES {opening_offer:,.0f} fell far below "
                f"the expected value derived from the verified new price "
                f"(KES {new_price_anchor:,.0f} x depreciation {depr:.2f} x "
                f"condition {cond_f:.2f} - defects, x acquisition {acq_ratio:.2f} "
                f"= KES {expected_from_new:,.0f}). Raised to KES {min_offer:,.0f} "
                f"({NEW_PRICE_FLOOR_SAFETY:.0%} safety floor) and flagged for "
                f"review (the base value was likely under-sourced)."
            )
            logger.warning(guardrail_note)
            trace_lines.append(f"GUARDRAIL: {guardrail_note}")
            opening_offer = min_offer
            acquisition_ceiling = _round_price(opening_offer * 1.3, round_step)
            walkaway_limit = _round_price(opening_offer * 0.7, round_step)
            new_price_floor_applied = True
            # An under-sourced base is unreliable — route to human review.
            confidence_score = min(confidence_score, 75.0)

    trace_lines.append(f"Final offer: KES {opening_offer:,.0f}")
    trace_lines.append(f"Confidence: {confidence_score:.0f}%")
    if unavailable_sources:
        trace_lines.append(
            "SOURCE OUTAGE: " + ", ".join(unavailable_sources) + " unavailable during "
            "this evaluation (infrastructure failure, not missing data); confidence "
            + ("was scaled to the sources that could answer."
               if renormalise_unavailable_sources
               else "reflects fewer sources than usual.")
        )

    # -- STEP 12: Confidence-based routing -----------------------------------
    needs_review_image = image_quality_score < 30

    if confidence_score < 80:
        trace_lines.append(
            f"AUTO-ROUTED TO HUMAN: confidence {confidence_score:.0f}% below 80% threshold."
        )

    pricing_justification = "\n".join(trace_lines)

    # -- STEP 13: Decision logic ---------------------------------------------
    if confidence_score < 80 or needs_review_risk:
        decision = "review"
        decision_reason = _build_review_reason(
            confidence_score, risk_score, needs_review_image,
            unavailable_sources=unavailable_sources,
        )
    elif seller_asking_price is None:
        decision = "negotiate"
        decision_reason = (
            f"No seller asking price provided. Opening offer at "
            f"KES {opening_offer:,.0f}."
        )
    elif seller_asking_price <= opening_offer:
        decision = "accept"
        decision_reason = (
            f"Seller asking KES {seller_asking_price:,.0f} is at or below "
            f"opening offer of KES {opening_offer:,.0f}."
        )
    elif seller_asking_price <= acquisition_ceiling:
        decision = "negotiate"
        decision_reason = (
            f"Seller asking KES {seller_asking_price:,.0f} is between opening "
            f"offer (KES {opening_offer:,.0f}) and ceiling (KES {acquisition_ceiling:,.0f}). "
            f"Opening negotiation at KES {opening_offer:,.0f}."
        )
    elif seller_asking_price > acquisition_ceiling * 1.5:
        decision = "decline"
        decision_reason = (
            f"Seller asking KES {seller_asking_price:,.0f} exceeds 150% of "
            f"ceiling (KES {acquisition_ceiling:,.0f}). Deal not viable."
        )
    else:
        decision = "negotiate"
        decision_reason = (
            f"Seller asking KES {seller_asking_price:,.0f} exceeds ceiling of "
            f"KES {acquisition_ceiling:,.0f}. Opening negotiation at "
            f"KES {opening_offer:,.0f}."
        )

    return ValuationResult(
        estimated_resale_value=estimated_resale_value,
        acquisition_ceiling=acquisition_ceiling,
        opening_offer=opening_offer,
        walkaway_limit=walkaway_limit,
        confidence_score=confidence_score,
        decision=decision,
        decision_reason=decision_reason,
        base_value=round(base_value, 2),
        base_value_source=base_value_source,
        brand_adjusted_value=round(brand_adjusted_value, 2),
        condition_adjusted_value=round(condition_adjusted_value, 2),
        defect_deduction_total=round(defect_total, 2),
        defect_details=defect_details,
        image_quality_penalty_applied=image_quality_penalty_applied,
        risk_adjustment_applied=risk_adjustment_applied,
        condition_grade=grade_upper,
        risk_score=risk_score,
        price_verification=price_verification or {},
        pricing_justification=pricing_justification,
        floor_applied=floor_applied,
        ceiling_applied=ceiling_applied,
        guardrail_note=guardrail_note,
        new_price_ceiling_applied=new_price_ceiling_applied,
        new_price_floor_applied=new_price_floor_applied,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_review_reason(
    confidence: float,
    risk: float,
    image_review: bool,
    unavailable_sources: list[str] | None = None,
) -> str:
    parts: list[str] = []
    if confidence < 80:
        parts.append(f"AUTO-ROUTED TO HUMAN: confidence {confidence:.0f}% below 80% threshold")
        if unavailable_sources:
            parts.append(
                "price source(s) down during this evaluation: " + ", ".join(unavailable_sources)
            )
    if risk > 70:
        parts.append(f"High risk score ({risk:.0f}/100)")
    if image_review:
        parts.append("Image quality below threshold — manual review required")
    return "Flagged for manual review: " + "; ".join(parts) + "."
