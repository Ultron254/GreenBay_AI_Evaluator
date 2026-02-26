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
    base_value_source: str  # "comparables" | "depreciation"
    brand_adjusted_value: float
    condition_adjusted_value: float
    defect_deduction_total: float
    defect_details: list[dict[str, Any]]
    image_quality_penalty_applied: bool
    risk_adjustment_applied: bool

    # Pass-through inputs for snapshot
    condition_grade: str
    risk_score: float


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
) -> float:
    """Deterministic confidence score in [0, 100]."""
    score = 0.0

    # Comparables contribution (max 40 pts)
    if comparables_count >= 5:
        score += 40.0
    elif comparables_count >= 3:
        score += 30.0
    elif comparables_count >= 1:
        score += 20.0
    # else 0

    # Image quality contribution (max 25 pts)
    score += (image_quality_score / 100.0) * 25.0

    # Data completeness (max 35 pts)
    if has_seller_asking:
        score += 10.0
    if has_age:
        score += 15.0
    if has_brand:
        score += 10.0

    return min(100.0, round(score, 1))


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
    # Compute weighted average of comparables if confidence is high enough
    total_weight = sum(c.weight for c in comparables) if comparables else 0.0
    comparables_avg = (
        sum(c.resale_price * c.weight for c in comparables) / total_weight
        if total_weight > 0
        else 0.0
    )
    comparables_confidence = min(100.0, len(comparables) * 20.0)

    if comparables and comparables_confidence >= 70:
        base_value = comparables_avg
        base_value_source = "comparables"
    else:
        depreciation_factor = _compute_depreciation_factor(age_years, pricing_policy)
        base_value = retail_price * depreciation_factor
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
    confidence_score = _compute_confidence(
        comparables_count=len(comparables),
        image_quality_score=image_quality_score,
        has_seller_asking=seller_asking_price is not None,
        has_age=age_years > 0,
        has_brand=bool(brand_key),
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
