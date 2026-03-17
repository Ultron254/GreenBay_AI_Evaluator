"""
Deterministic offer engine for GreenBay trade-in valuations.

This module contains PURE COMPUTATION — no LLM calls, no network calls.
All data gathering (comparables, image scores, risk scores) must happen
before calling ``compute_valuation()``.

Every intermediate value is returned in the ``ValuationResult`` so it can
be stored for full auditability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from greenbay_ai_evaluator.config import (
    DEFECT_DEDUCTIONS,
    DEFAULT_DEFECT_DEDUCTION,
)


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
    depreciation_year1: float = 0.20
    depreciation_year2_3: float = 0.12
    depreciation_year4_5: float = 0.10
    depreciation_year6_plus: float = 0.08
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


# ---------------------------------------------------------------------------
# Depreciation helper
# ---------------------------------------------------------------------------
def _compute_depreciation_factor(
    age_years: float,
    policy: PricingPolicyData,
) -> float:
    """Return the remaining-value factor after *age_years* of depreciation.

    Year 1 depreciates at ``policy.depreciation_year1``, years 2-3 at
    ``policy.depreciation_year2_3``, etc.  The factor is clamped to
    [0.05, 1.0] so a product never reaches zero.
    """
    remaining = 1.0

    if age_years <= 0:
        return 1.0

    # Year 1
    y1 = min(age_years, 1.0)
    remaining *= 1.0 - policy.depreciation_year1 * y1

    if age_years <= 1.0:
        return max(0.05, remaining)

    # Years 2-3
    y2_3 = min(age_years - 1.0, 2.0)
    remaining *= (1.0 - policy.depreciation_year2_3) ** y2_3

    if age_years <= 3.0:
        return max(0.05, remaining)

    # Years 4-5
    y4_5 = min(age_years - 3.0, 2.0)
    remaining *= (1.0 - policy.depreciation_year4_5) ** y4_5

    if age_years <= 5.0:
        return max(0.05, remaining)

    # Year 6+
    y6p = age_years - 5.0
    remaining *= (1.0 - policy.depreciation_year6_plus) ** y6p

    return max(0.05, remaining)


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
) -> float:
    """Deterministic confidence score in [0, 100].

    Scoring breakdown (max 100):
    - Comparables: up to 25 pts
    - Image quality: up to 15 pts
    - Data completeness: up to 30 pts
    - Multi-source price verification: up to 30 pts
    """
    score = 0.0

    # Comparables contribution (max 25 pts)
    if comparables_count >= 5:
        score += 25.0
    elif comparables_count >= 3:
        score += 20.0
    elif comparables_count >= 1:
        score += 15.0
    # else 0

    # Image quality contribution (max 15 pts)
    score += (image_quality_score / 100.0) * 15.0

    # Data completeness (max 30 pts)
    if has_seller_asking:
        score += 10.0
    if has_age:
        score += 10.0
    if has_brand:
        score += 10.0

    # Multi-source price verification (max 30 pts)
    # Each verified source adds confidence
    if price_verification_sources >= 4:
        score += 30.0
    elif price_verification_sources >= 3:
        score += 25.0
    elif price_verification_sources >= 2:
        score += 20.0
    elif price_verification_sources >= 1:
        score += 12.0

    return min(100.0, round(score, 1))


# ---------------------------------------------------------------------------
# Multi-source price reconciliation
# ---------------------------------------------------------------------------
def reconcile_retail_price(
    *,
    frontend_price: float,
    internet_price: float | None = None,
    marketplace_avg: float | None = None,
    shopify_avg: float | None = None,
    expert_avg: float | None = None,
) -> dict[str, Any]:
    """Reconcile retail price from multiple sources.

    Returns a dict with the reconciled price and breakdown of sources.
    Each source gets a weight based on reliability.
    """
    sources = []
    total_weight = 0.0
    weighted_sum = 0.0

    # Frontend / AI-provided price (weight 1.0 — baseline)
    if frontend_price and frontend_price > 0:
        sources.append({"source": "frontend", "price": frontend_price, "weight": 1.0})
        weighted_sum += frontend_price * 1.0
        total_weight += 1.0

    # Internet lookup (weight 2.0 — real retail data)
    if internet_price and internet_price > 0:
        sources.append({"source": "internet_lookup", "price": internet_price, "weight": 2.0})
        weighted_sum += internet_price * 2.0
        total_weight += 2.0

    # Marketplace average from Jiji/Jumia (weight 2.5 — actual secondhand market)
    if marketplace_avg and marketplace_avg > 0:
        sources.append({"source": "marketplace_jiji_jumia", "price": marketplace_avg, "weight": 2.5})
        weighted_sum += marketplace_avg * 2.5
        total_weight += 2.5

    # Our Shopify inventory average (weight 3.0 — our own verified prices)
    if shopify_avg and shopify_avg > 0:
        sources.append({"source": "shopify_inventory", "price": shopify_avg, "weight": 3.0})
        weighted_sum += shopify_avg * 3.0
        total_weight += 3.0

    # Expert feedback (weight 4.0 — highest reliability)
    if expert_avg and expert_avg > 0:
        sources.append({"source": "expert_feedback", "price": expert_avg, "weight": 4.0})
        weighted_sum += expert_avg * 4.0
        total_weight += 4.0

    reconciled = weighted_sum / total_weight if total_weight > 0 else frontend_price
    num_sources = len(sources)

    return {
        "reconciled_price": round(reconciled, 2),
        "sources": sources,
        "num_sources": num_sources,
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

    Returns
    -------
    ValuationResult
        Fully auditable result with every intermediate value.
    """

    # -- STEP 1: Base value --------------------------------------------------
    # Use reconciled price if available from multi-source verification
    # Use reconciled price if multi-source verification found anything
    effective_retail = retail_price
    if price_verification and price_verification.get("reconciled_price"):
        reconciled = price_verification["reconciled_price"]
        # Only use reconciled if it's reasonable (within 5x of frontend price)
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

    if comparables and len(comparables) >= 3:
        # Strong comparables — use directly
        base_value = comparables_avg
        base_value_source = "comparables"
    elif comparables and len(comparables) >= 1 and num_pv_sources >= 1:
        # Blend comparables with reconciled price
        base_value = comparables_avg * 0.6 + effective_retail * 0.4
        base_value_source = "comparables+reconciled"
    elif num_pv_sources >= 1:
        # Multi-source reconciled price available
        base_value = effective_retail
        base_value_source = "reconciled"
    else:
        depreciation_factor = _compute_depreciation_factor(age_years, pricing_policy)
        base_value = effective_retail * depreciation_factor
        base_value_source = "depreciation"

    # -- STEP 2: Brand adjustment --------------------------------------------
    brand_key = brand.strip() if brand else ""
    brand_premium = pricing_policy.brand_premium_json.get(brand_key, 1.0)
    brand_adjusted_value = base_value * brand_premium

    # -- STEP 3: Condition adjustment ----------------------------------------
    grade_upper = condition_grade.upper().strip() if condition_grade else "C"
    condition_mult = pricing_policy.condition_multiplier_json.get(grade_upper, 0.65)
    condition_adjusted_value = brand_adjusted_value * condition_mult

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

    # Cap deductions at 60% of condition-adjusted value
    max_deduction = condition_adjusted_value * 0.60
    defect_total = min(defect_total, max_deduction)

    after_defects = condition_adjusted_value - defect_total

    # -- STEP 5: Image quality penalty ---------------------------------------
    image_quality_penalty_applied = False
    if image_quality_score < 50:
        after_defects *= 0.95  # -5%
        image_quality_penalty_applied = True
    needs_review_image = image_quality_score < 30

    # -- STEP 6: Estimated resale value --------------------------------------
    estimated_resale_value = round(max(0.0, after_defects), 2)

    # -- STEP 7-9: Ceiling, opening, walkaway --------------------------------
    acquisition_ceiling = round(estimated_resale_value * pricing_policy.max_offer_pct, 2)
    opening_offer = round(
        estimated_resale_value * (pricing_policy.max_offer_pct - pricing_policy.margin_pct), 2
    )
    walkaway_limit = round(estimated_resale_value * pricing_policy.walkaway_pct, 2)

    # -- STEP 10: Confidence score -------------------------------------------
    num_pv_sources = price_verification.get("num_sources", 0) if price_verification else 0
    confidence_score = _compute_confidence(
        comparables_count=len(comparables),
        image_quality_score=image_quality_score,
        has_seller_asking=seller_asking_price is not None,
        has_age=age_years > 0,
        has_brand=bool(brand_key),
        price_verification_sources=num_pv_sources,
    )

    # -- STEP 11: Risk adjustment --------------------------------------------
    risk_adjustment_applied = False
    needs_review_risk = risk_score > 70
    if risk_score > 50:
        acquisition_ceiling = round(acquisition_ceiling * 0.90, 2)
        risk_adjustment_applied = True

    # -- STEP 12: Decision logic ---------------------------------------------
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
