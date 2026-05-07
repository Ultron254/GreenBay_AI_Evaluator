"""
Deterministic offer engine for GreenBay trade-in valuations.

This module contains PURE COMPUTATION — no LLM calls, no network calls.
All data gathering (comparables, image scores, risk scores) must happen
before calling ``compute_valuation()``.

Every intermediate value is returned in the ``ValuationResult`` so it can
be stored for full auditability.
"""
# Version marker for deployment verification
ENGINE_VERSION = "2.0.0-calibrated-2026-05-07"

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.config import (
    DEFECT_DEDUCTIONS,
    DEFAULT_DEFECT_DEDUCTION,
)


# ---------------------------------------------------------------------------
# Category-specific depreciation speed multipliers.
# Values < 1.0 mean the category depreciates SLOWER than average.
# TVs hold value well in Kenya's secondhand market; cookers/microwaves lose
# value faster.
# ---------------------------------------------------------------------------
CATEGORY_DEPRECIATION_MULTIPLIERS: dict[str, float] = {
    "tv_monitor": 0.55,
    "refrigerator": 0.80,
    "washing_machine": 1.0,
    "cooker_oven": 1.10,
    "microwave": 1.2,
    "small_kitchen": 1.0,
    "smartphone": 1.0,
    "other": 1.0,
}

# Trade-in acquisition factor: applied to estimated resale value to get
# what GreenBay actually pays. Calibrated against 90 real evaluations.
TRADE_IN_ACQUISITION_FACTOR: float = 0.50

# Condition multipliers: these reflect COMBINED condition impact on the
# acquisition price GreenBay pays. Grade A items are near-new and command
# close to full depreciated value.
CONDITION_MULTIPLIERS: dict[str, float] = {
    "A": 0.95,  # Excellent — near-new, minimal discount
    "B": 0.75,  # Good — used but well-maintained
    "C": 0.65,  # Fair — visible wear, functional
    "D": 0.52,  # Poor — significant wear, needs work
    "E": 0.30,  # Bad — barely functional, parts value
}

# Floor/ceiling guardrails: prevent wild outliers relative to team history.
PRICE_FLOOR_RATIO: float = 0.60   # AI price must be >= 60% of team avg
PRICE_CEILING_RATIO: float = 1.50  # AI price must be <= 150% of team avg


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
    weight: float = 1.0  # higher = more relevant


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
    base_value_source: str  # "comparables" | "depreciation" | "reconciled"
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


# ---------------------------------------------------------------------------
# Depreciation helper
# ---------------------------------------------------------------------------
def _compute_depreciation_factor(
    age_years: float,
    policy: PricingPolicyData,
    category: str = "",
) -> float:
    """Return the remaining-value factor after *age_years* of depreciation.

    Year 1 depreciates at ``policy.depreciation_year1``, years 2-3 at
    ``policy.depreciation_year2_3``, etc.  Category-specific speed
    multipliers slow or accelerate the rate (e.g. TVs hold value better).

    The factor is clamped to [0.15, 1.0] — minimum 15% of base value.
    """
    remaining = 1.0

    if age_years <= 0:
        return 1.0

    cat_key = category.lower().strip() if category else ""
    cat_mult = CATEGORY_DEPRECIATION_MULTIPLIERS.get(cat_key, 1.0)

    # Year 1
    y1 = min(age_years, 1.0)
    rate_y1 = min(policy.depreciation_year1 * cat_mult, 0.90)
    remaining *= 1.0 - rate_y1 * y1

    if age_years <= 1.0:
        return max(0.15, remaining)

    # Years 2-3
    y2_3 = min(age_years - 1.0, 2.0)
    rate_y23 = min(policy.depreciation_year2_3 * cat_mult, 0.90)
    remaining *= (1.0 - rate_y23) ** y2_3

    if age_years <= 3.0:
        return max(0.15, remaining)

    # Years 4-5
    y4_5 = min(age_years - 3.0, 2.0)
    rate_y45 = min(policy.depreciation_year4_5 * cat_mult, 0.90)
    remaining *= (1.0 - rate_y45) ** y4_5

    if age_years <= 5.0:
        return max(0.15, remaining)

    # Year 6+
    y6p = age_years - 5.0
    rate_y6 = min(policy.depreciation_year6_plus * cat_mult, 0.90)
    remaining *= (1.0 - rate_y6) ** y6p

    return max(0.15, remaining)


# ---------------------------------------------------------------------------
# Confidence score helper
# ---------------------------------------------------------------------------
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
) -> float:
    """Deterministic confidence score in [0, 100].

    Confidence reflects ACTUAL DATA QUALITY, not model certainty.
    Hard caps based on available evidence:
      - No historical data + no market lookup       = MAX 35%
      - Only internet retail price (no comparables) = MAX 50%
      - 1-2 historical comparables                  = MAX 65%
      - 3-5 comparables + market data               = MAX 80%
      - 5+ comparables + multiple sources + feedback = up to 95%
    """
    score = 0.0

    # ---- Data completeness (max 20 pts) ----
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

    # ---- Image quality (max 10 pts) ----
    score += (image_quality_score / 100.0) * 10.0

    # ---- Historical / comparable evidence (max 35 pts) ----
    if comparables_count >= 5:
        score += 35.0
    elif comparables_count >= 3:
        score += 25.0
    elif comparables_count >= 1:
        score += 15.0
    elif has_historical_data:
        score += 10.0

    # ---- Price verification from market sources (max 30 pts) ----
    if price_verification_sources >= 4:
        score += 30.0
    elif price_verification_sources >= 3:
        score += 25.0
    elif price_verification_sources >= 2:
        score += 20.0
    elif price_verification_sources >= 1:
        score += 12.0
    else:
        score += 0.0

    # ---- Apply hard caps based on evidence quality ----
    if comparables_count == 0 and price_verification_sources == 0 and not has_historical_data:
        score = min(score, 35.0)
    elif comparables_count == 0 and not has_historical_data:
        score = min(score, 50.0)
    elif comparables_count <= 2 and not has_historical_data:
        score = min(score, 65.0)
    elif comparables_count <= 5:
        score = min(score, 80.0)

    return min(95.0, round(score, 1))


# ---------------------------------------------------------------------------
# CR-2: Round to nearest KES 500
# ---------------------------------------------------------------------------
def _round_kes_500(value: float) -> float:
    """Round a KES price to the nearest 500."""
    if value <= 0:
        return 0.0
    return round(value / 500) * 500


# ---------------------------------------------------------------------------
# Multi-source price reconciliation (60% human intelligence / 40% AI market)
# ---------------------------------------------------------------------------
def _tier_weighted_average(entries: list[tuple[float, float]]) -> float | None:
    """Weighted mean of (price, intra-tier weight). None if no entries."""
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
) -> dict[str, Any]:
    """Reconcile retail price from multiple independent signals.

    Philosophy (60 / 40):
      * **Human intelligence (~60%)** — Ground truth from operations and history:
        pricing learner (Google Sheets), expert feedback (Postgres), and DB
        comparables (weighted inventory / historical resale anchors).
      * **AI market research (~40%)** — Automated probes of the wider market:
        Tavily internet lookup, Jiji/Jumia scrape, Shopify snapshot, and the
        frontend/AI retail hint.

    Within each tier, relative weights below determine how multiple simultaneous
    inputs blend **before** the 60/40 cross-tier blend.

    **Airtable is intentionally not a reconcile source.** It mirrors Sheet and
    DB human prices; ingesting it here would double-count. The Airtable-derived
    accuracy ratio is applied **after** this function returns (see evaluator router).

    Returns a dict with the reconciled price and breakdown of sources.
    """
    sources: list[dict[str, Any]] = []

    # --- Tier 1: Historical / human intelligence (internal weights 8 / 7 / 5) ---
    human_specs: list[tuple[str, float | None, float]] = [
        ("historical_sheet", historical_avg, 8.0),
        ("expert_feedback", expert_avg, 7.0),
        ("database_comparables", comparables_avg, 5.0),
    ]
    human_entries: list[tuple[float, float]] = []
    for key, price, w in human_specs:
        if price is not None and price > 0:
            human_entries.append((price, w))
            sources.append({
                "source": key,
                "price": price,
                "weight": w,
                "tier": "human_intelligence",
            })

    # --- Tier 2: AI market research (internal weights 3 / 2.5 / 2 / 1) ---
    ai_specs: list[tuple[str, float | None, float]] = [
        ("internet_lookup", internet_price, 3.0),
        ("marketplace_jiji_jumia", marketplace_avg, 2.5),
        ("shopify_inventory", shopify_avg, 2.0),
        ("frontend", frontend_price if frontend_price > 0 else None, 1.0),
    ]
    ai_entries: list[tuple[float, float]] = []
    for key, price, w in ai_specs:
        if price is not None and price > 0:
            ai_entries.append((price, w))
            sources.append({
                "source": key,
                "price": price,
                "weight": w,
                "tier": "ai_market_research",
            })

    human_avg = _tier_weighted_average(human_entries)
    ai_avg = _tier_weighted_average(ai_entries)

    # Outlier guard: live marketplace / Tavily vs human tier — discard AI tier if absurd
    web_probe: float | None = None
    if marketplace_avg and marketplace_avg > 0:
        web_probe = marketplace_avg
    elif internet_price and internet_price > 0:
        web_probe = internet_price

    discard_ai_tier = False
    if human_avg is not None and web_probe is not None and human_avg > 0:
        probe_ratio = web_probe / human_avg
        if probe_ratio > 2.0 or probe_ratio < 0.3:
            discard_ai_tier = True
            for s in sources:
                if s.get("tier") == "ai_market_research":
                    s["discarded"] = True
                    s["discard_reason"] = (
                        f"Outlier vs human-intelligence tier: web probe {probe_ratio:.2f}x"
                    )

    if discard_ai_tier and human_avg is not None:
        reconciled = human_avg
    elif human_avg is not None and ai_avg is not None:
        reconciled = 0.6 * human_avg + 0.4 * ai_avg
    elif human_avg is not None:
        reconciled = human_avg
    elif ai_avg is not None:
        reconciled = ai_avg
    else:
        reconciled = frontend_price if frontend_price > 0 else 0.0

    num_sources = len(sources)

    return {
        "reconciled_price": _round_kes_500(reconciled),
        "sources": sources,
        "num_sources": num_sources,
        "human_intelligence_avg": round(human_avg, 2) if human_avg is not None else None,
        "ai_market_research_avg": round(ai_avg, 2) if ai_avg is not None else None,
        "confidence": min(100.0, num_sources * 20.0 + 10.0),
        "frontend_price": frontend_price,
    }


# ---------------------------------------------------------------------------
# Main entry point
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
) -> ValuationResult:
    """Deterministic valuation computation.

    Parameters
    ----------
    category : str
        Product category (e.g. "refrigerator").
    brand : str
        Brand name.
    model : str
        Model identifier.
    age_years : float
        Approximate age of the product in years.
    condition_grade : str
        Letter grade A / B / C / D.
    condition_score : float
        Numeric condition score 0-100.
    defects : list[dict]
        Each dict must have ``type`` (str).  Optional keys: ``severity``,
        ``description``.
    seller_asking_price : float | None
        What the seller wants.  ``None`` if not provided.
    image_quality_score : float
        0-100 score from the image quality service.
    risk_score : float
        0-100 score from the risk service.
    comparables : list[Comparable]
        Market comparables gathered before calling this function.
    pricing_policy : PricingPolicyData
        Business rules for this category.
    retail_price : float
        Original retail price in KES.
    historical_team_avg : float | None
        Average team price for this brand+category from pricing learner.
        Used for floor/ceiling guardrails.

    Returns
    -------
    ValuationResult
        Fully auditable result with every intermediate value.
    """
    trace_lines: list[str] = []  # Build pricing justification as we go
    trace_lines.append("PRICE CALCULATION BREAKDOWN")
    trace_lines.append("===========================")
    trace_lines.append(
        f"Product: {brand} {model} {category} "
        f"({age_years:.1f} years, Condition {condition_grade})"
    )
    trace_lines.append("")

    # -- STEP 1: Base value --------------------------------------------------
    effective_retail = retail_price
    if price_verification and price_verification.get("reconciled_price"):
        reconciled = price_verification["reconciled_price"]
        if reconciled > 0 and (retail_price <= 0 or 0.2 <= reconciled / max(retail_price, 1) <= 5.0):
            effective_retail = reconciled

    # Compute weighted average of comparables
    total_weight = sum(c.weight for c in comparables) if comparables else 0.0
    comparables_avg = (
        sum(c.resale_price * c.weight for c in comparables) / total_weight
        if total_weight > 0
        else 0.0
    )

    num_pv_sources = price_verification.get("num_sources", 0) if price_verification else 0

    # Log data sources
    trace_lines.append("Data Sources Used:")
    if price_verification and price_verification.get("sources"):
        for src in price_verification["sources"]:
            discarded = " [DISCARDED]" if src.get("discarded") else ""
            trace_lines.append(
                f"- {src['source']}: KES {src['price']:,.0f} "
                f"[weight {src['weight']:.1f}]{discarded}"
            )
    if comparables:
        trace_lines.append(
            f"- DB comparables ({len(comparables)} items): "
            f"KES {comparables_avg:,.0f} avg"
        )
    trace_lines.append("")

    if comparables and len(comparables) >= 3:
        base_value = comparables_avg
        base_value_source = "comparables"
        trace_lines.append(f"Base value (strong comparables): KES {base_value:,.0f}")
    elif comparables and len(comparables) >= 1 and num_pv_sources >= 1:
        base_value = comparables_avg * 0.6 + effective_retail * 0.4
        base_value_source = "comparables+reconciled"
        trace_lines.append(
            f"Base value (comparables 60% + reconciled 40%): KES {base_value:,.0f}"
        )
    elif num_pv_sources >= 1:
        base_value = effective_retail
        base_value_source = "reconciled"
        trace_lines.append(f"Base value (reconciled multi-source): KES {base_value:,.0f}")
    else:
        depreciation_factor = _compute_depreciation_factor(age_years, pricing_policy, category)
        base_value = effective_retail * depreciation_factor
        base_value_source = "depreciation"
        trace_lines.append(
            f"Base value (retail KES {effective_retail:,.0f} * "
            f"depreciation {depreciation_factor:.3f}): KES {base_value:,.0f}"
        )

    # -- STEP 2: Apply depreciation to reconciled base values ONLY when ------
    # the reconciled price has no human-intelligence component (i.e., it's
    # essentially retail-level pricing from Tavily/marketplace only).
    if base_value_source == "reconciled" and price_verification:
        human_avg = price_verification.get("human_intelligence_avg")
        if human_avg is None or human_avg <= 0:
            depreciation_factor = _compute_depreciation_factor(age_years, pricing_policy, category)
            base_value = base_value * depreciation_factor
            trace_lines.append(
                f"Depreciation applied (no human data, factor {depreciation_factor:.3f}): "
                f"KES {base_value:,.0f}"
            )

    # -- STEP 3: Brand adjustment --------------------------------------------
    brand_key = brand.strip() if brand else ""
    brand_premium = pricing_policy.brand_premium_json.get(brand_key, 1.0)
    brand_adjusted_value = base_value * brand_premium
    if brand_premium != 1.0:
        trace_lines.append(f"Brand premium ({brand_key}: {brand_premium}): KES {brand_adjusted_value:,.0f}")

    # -- STEP 4: Condition adjustment ----------------------------------------
    grade_upper = condition_grade.upper().strip() if condition_grade else "C"
    # Use our hardcoded condition multipliers (not from DB which may be stale)
    condition_mult = CONDITION_MULTIPLIERS.get(grade_upper, 0.45)
    # Allow DB override only if it's MORE aggressive (lower)
    db_cond_mult = pricing_policy.condition_multiplier_json.get(grade_upper)
    if db_cond_mult is not None and db_cond_mult < condition_mult:
        condition_mult = db_cond_mult
    condition_adjusted_value = brand_adjusted_value * condition_mult
    trace_lines.append(
        f"Condition multiplier ({grade_upper}: {condition_mult}): "
        f"KES {condition_adjusted_value:,.0f}"
    )

    # -- STEP 5: Defect deductions -------------------------------------------
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

    # -- STEP 6: Image quality penalty ---------------------------------------
    image_quality_penalty_applied = False
    if image_quality_score < 50:
        after_defects *= 0.95
        image_quality_penalty_applied = True
        trace_lines.append(f"Image quality penalty (-5%): KES {after_defects:,.0f}")
    needs_review_image = image_quality_score < 30

    # -- STEP 7: Estimated resale value --------------------------------------
    estimated_resale_value = _round_kes_500(max(0.0, after_defects))
    trace_lines.append(f"Estimated resale value: KES {estimated_resale_value:,.0f}")

    # -- STEP 8: Trade-in acquisition factor ---------------------------------
    # GreenBay buys at ~35% of resale to cover refurbishment + margin.
    acquisition_price = _round_kes_500(estimated_resale_value * TRADE_IN_ACQUISITION_FACTOR)
    trace_lines.append(
        f"Trade-in acquisition factor ({TRADE_IN_ACQUISITION_FACTOR}): "
        f"KES {acquisition_price:,.0f}"
    )

    # -- STEP 9: Ceiling, opening, walkaway ----------------------------------
    acquisition_ceiling = _round_kes_500(acquisition_price * 1.3)  # slight room above offer
    opening_offer = acquisition_price
    walkaway_limit = _round_kes_500(acquisition_price * 0.7)

    # -- STEP 10: Confidence score -------------------------------------------
    num_pv_sources = price_verification.get("num_sources", 0) if price_verification else 0
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
    )

    # -- STEP 11: Risk adjustment --------------------------------------------
    risk_adjustment_applied = False
    needs_review_risk = risk_score > 70
    if risk_score > 50:
        acquisition_ceiling = round(acquisition_ceiling * 0.90, 2)
        risk_adjustment_applied = True
        trace_lines.append(f"Risk adjustment (-10% ceiling): KES {acquisition_ceiling:,.0f}")

    # -- STEP 12: Floor/Ceiling guardrails -----------------------------------
    floor_applied = False
    ceiling_applied = False
    guardrail_note = ""

    if historical_team_avg and historical_team_avg > 0:
        floor_price = _round_kes_500(historical_team_avg * PRICE_FLOOR_RATIO)
        ceiling_price = _round_kes_500(historical_team_avg * PRICE_CEILING_RATIO)

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
            acquisition_ceiling = _round_kes_500(opening_offer * 1.3)
            walkaway_limit = _round_kes_500(opening_offer * 0.7)
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
            acquisition_ceiling = _round_kes_500(opening_offer * 1.3)
            walkaway_limit = _round_kes_500(opening_offer * 0.7)
            ceiling_applied = True

    trace_lines.append(f"Final offer: KES {opening_offer:,.0f}")
    trace_lines.append(f"Confidence: {confidence_score:.0f}%")
    if confidence_score < 50:
        trace_lines.append(
            "Low confidence: limited pricing data for this brand/model. "
            "Price may need manual review."
        )

    pricing_justification = "\n".join(trace_lines)

    # -- STEP 13: Decision logic ---------------------------------------------
    if confidence_score < 40 or needs_review_risk:
        decision = "review"
        decision_reason = _build_review_reason(confidence_score, risk_score, needs_review_image)
    elif seller_asking_price is None:
        decision = "negotiate"
        decision_reason = (
            f"No seller asking price provided. Opening negotiation at "
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
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_review_reason(
    confidence: float,
    risk: float,
    image_review: bool,
) -> str:
    parts: list[str] = []
    if confidence < 40:
        parts.append(f"Low confidence ({confidence:.0f}/100)")
    if risk > 70:
        parts.append(f"High risk score ({risk:.0f}/100)")
    if image_review:
        parts.append("Image quality below threshold — manual review required")
    return "Flagged for manual review: " + "; ".join(parts) + "."
