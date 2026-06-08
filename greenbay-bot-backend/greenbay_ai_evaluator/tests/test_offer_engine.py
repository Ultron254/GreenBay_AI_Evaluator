"""
Tests for greenbay_ai_evaluator.engine.offer_engine

Every test is deterministic — no mocking of external services required.
Kept in sync with the current v6 data-driven engine:
  resale = new_price * depreciation_factor(age, category) * condition_factor(grade)
  trade_in_offer = resale * acquisition_ratio[category]
"""

import pytest

from greenbay_ai_evaluator.engine.offer_engine import (
    Comparable,
    PricingPolicyData,
    ValuationResult,
    _compute_confidence,
    compute_valuation,
    depreciation_factor,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def default_policy() -> PricingPolicyData:
    """Standard refrigerator pricing policy."""
    return PricingPolicyData(
        category="refrigerator",
        margin_pct=0.15,
        max_offer_pct=0.70,
        walkaway_pct=0.45,
        brand_premium_json={"Samsung": 1.10, "LG": 1.05},
        condition_multiplier_json={"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.45},
    )


@pytest.fixture
def basic_inputs(default_policy):
    """Minimal valid inputs — low evidence, so confidence stays low."""
    return dict(
        category="refrigerator",
        brand="Samsung",
        model="RF28",
        age_years=2.0,
        condition_grade="B",
        condition_score=75.0,
        defects=[],
        seller_asking_price=None,
        image_quality_score=80.0,
        risk_score=20.0,
        comparables=[],
        pricing_policy=default_policy,
        retail_price=100_000.0,
    )


@pytest.fixture
def high_conf_inputs(default_policy):
    """Inputs engineered to clear the 80% confidence gate so the decision
    branches (accept/negotiate/decline) can be exercised. Strong evidence:
    sales-stock + matrix + 5 comparables + 4 market sources + vision."""
    return dict(
        category="refrigerator",
        brand="Samsung",
        model="RF28",
        age_years=2.0,
        condition_grade="B",
        condition_score=75.0,
        defects=[],
        seller_asking_price=None,
        image_quality_score=85.0,
        risk_score=20.0,
        comparables=[Comparable(resale_price=40_000, weight=1.0) for _ in range(5)],
        pricing_policy=default_policy,
        retail_price=120_000.0,
        price_verification={
            "num_real_sources": 4,
            "num_sources": 4,
            "reconciled_price": 150_000,
            "vision_analysis": {"ok": 1},
            "sources": [],
        },
        has_matrix_match=True,
        has_sales_stock_match=True,
    )


# ---------------------------------------------------------------------------
# depreciation_factor — curve-based (v6)
# ---------------------------------------------------------------------------
class TestDepreciation:
    def test_zero_age_caps_at_90pct(self):
        assert depreciation_factor(0) == 0.90

    def test_negative_age_caps_at_90pct(self):
        assert depreciation_factor(-3) == 0.90

    def test_known_curve_points(self):
        # Default category (no modifier) follows the curve midpoints exactly.
        assert depreciation_factor(1.5, "") == pytest.approx(0.72, rel=1e-6)
        assert depreciation_factor(2.5, "") == pytest.approx(0.60, rel=1e-6)

    def test_monotonic_decreasing(self):
        f1 = depreciation_factor(1.0, "")
        f2 = depreciation_factor(2.0, "")
        f4 = depreciation_factor(4.0, "")
        f10 = depreciation_factor(10.0, "")
        assert f1 >= f2 >= f4 >= f10

    def test_clamped_range(self):
        for age in (0.1, 1, 3, 7, 20, 50):
            v = depreciation_factor(age, "refrigerator")
            assert 0.15 <= v <= 0.90


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------
class TestConfidence:
    def test_no_data_is_zero(self):
        assert _compute_confidence(0, 0.0, False, False, False) == 0.0

    def test_comparables_scale(self):
        s1 = _compute_confidence(1, 50.0, False, False, False)
        s3 = _compute_confidence(3, 50.0, False, False, False)
        s5 = _compute_confidence(5, 50.0, False, False, False)
        assert s1 < s3 < s5

    def test_image_quality_contribution(self):
        low = _compute_confidence(0, 10.0, False, False, False)
        high = _compute_confidence(0, 90.0, False, False, False)
        assert high > low

    def test_strong_evidence_clears_80(self):
        score = _compute_confidence(
            5, 85.0, True, True, True,
            price_verification_sources=4,
            has_model=True,
            has_vision_analysis=True,
            has_matrix_match=True,
            has_sales_stock_match=True,
        )
        assert score >= 80.0

    def test_matrix_only_capped_at_70(self):
        score = _compute_confidence(
            0, 100.0, True, True, True,
            price_verification_sources=4,
            has_matrix_match=True,
        )
        assert score <= 70.0


# ---------------------------------------------------------------------------
# compute_valuation — full pipeline
# ---------------------------------------------------------------------------
class TestComputeValuation:
    def test_returns_valuation_result(self, basic_inputs):
        assert isinstance(compute_valuation(**basic_inputs), ValuationResult)

    def test_no_reference_uses_external_research(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.base_value_source == "external_research"

    def test_brand_premium_applied(self, basic_inputs):
        result_samsung = compute_valuation(**basic_inputs)
        basic_inputs["brand"] = "NoBrand"
        result_nobrand = compute_valuation(**basic_inputs)
        assert result_samsung.brand_adjusted_value > result_nobrand.brand_adjusted_value

    def test_condition_affects_value(self, basic_inputs):
        basic_inputs["condition_grade"] = "A"
        result_a = compute_valuation(**basic_inputs)
        basic_inputs["condition_grade"] = "D"
        result_d = compute_valuation(**basic_inputs)
        assert result_a.condition_adjusted_value > result_d.condition_adjusted_value

    def test_defect_deductions(self, basic_inputs):
        basic_inputs["defects"] = [{"type": "broken_seal"}, {"type": "missing_shelf"}]
        result = compute_valuation(**basic_inputs)
        assert result.defect_deduction_total == 2500.0  # 2000 + 500

    def test_defect_deduction_capped_at_60pct(self, basic_inputs):
        basic_inputs["defects"] = [{"type": "compressor_noise"}] * 10
        result = compute_valuation(**basic_inputs)
        assert result.defect_deduction_total <= result.condition_adjusted_value * 0.60

    def test_image_quality_penalty(self, basic_inputs):
        basic_inputs["image_quality_score"] = 80.0
        good = compute_valuation(**basic_inputs)
        basic_inputs["image_quality_score"] = 30.0
        bad = compute_valuation(**basic_inputs)
        assert bad.image_quality_penalty_applied is True
        assert good.image_quality_penalty_applied is False
        assert bad.estimated_resale_value < good.estimated_resale_value

    def test_risk_adjustment(self, basic_inputs):
        basic_inputs["risk_score"] = 20.0
        low = compute_valuation(**basic_inputs)
        basic_inputs["risk_score"] = 60.0
        high = compute_valuation(**basic_inputs)
        assert low.risk_adjustment_applied is False
        assert high.risk_adjustment_applied is True
        assert high.acquisition_ceiling < low.acquisition_ceiling

    def test_ceiling_always_gte_opening(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.acquisition_ceiling >= result.opening_offer

    def test_opening_always_gte_walkaway(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.opening_offer >= result.walkaway_limit


# ---------------------------------------------------------------------------
# Decision logic — exercised with high-confidence inputs
# ---------------------------------------------------------------------------
class TestDecisionLogic:
    def test_high_conf_inputs_clear_gate(self, high_conf_inputs):
        result = compute_valuation(**high_conf_inputs)
        assert result.confidence_score >= 80.0

    def test_accept_when_seller_below_opening(self, high_conf_inputs):
        ref = compute_valuation(**high_conf_inputs)
        high_conf_inputs["seller_asking_price"] = ref.opening_offer * 0.8
        assert compute_valuation(**high_conf_inputs).decision == "accept"

    def test_negotiate_when_no_asking_price(self, high_conf_inputs):
        high_conf_inputs["seller_asking_price"] = None
        assert compute_valuation(**high_conf_inputs).decision == "negotiate"

    def test_negotiate_between_opening_and_ceiling(self, high_conf_inputs):
        ref = compute_valuation(**high_conf_inputs)
        high_conf_inputs["seller_asking_price"] = (ref.opening_offer + ref.acquisition_ceiling) / 2
        assert compute_valuation(**high_conf_inputs).decision == "negotiate"

    def test_decline_when_way_above_ceiling(self, high_conf_inputs):
        ref = compute_valuation(**high_conf_inputs)
        high_conf_inputs["seller_asking_price"] = ref.acquisition_ceiling * 2
        assert compute_valuation(**high_conf_inputs).decision == "decline"

    def test_review_when_high_risk(self, high_conf_inputs):
        high_conf_inputs["risk_score"] = 80.0
        assert compute_valuation(**high_conf_inputs).decision == "review"

    def test_low_evidence_routes_to_review(self, basic_inputs):
        # No reference, no matrix, no sales stock -> confidence < 80 -> review.
        result = compute_valuation(**basic_inputs)
        assert result.confidence_score < 80
        assert result.decision == "review"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_identical_inputs_produce_identical_outputs(self, high_conf_inputs):
        r1 = compute_valuation(**high_conf_inputs)
        r2 = compute_valuation(**high_conf_inputs)
        assert r1.estimated_resale_value == r2.estimated_resale_value
        assert r1.opening_offer == r2.opening_offer
        assert r1.confidence_score == r2.confidence_score
        assert r1.decision == r2.decision

    def test_output_independent_of_call_order(self, basic_inputs):
        r1 = compute_valuation(**basic_inputs)
        basic_inputs["retail_price"] = 200_000
        _ = compute_valuation(**basic_inputs)
        basic_inputs["retail_price"] = 100_000
        r3 = compute_valuation(**basic_inputs)
        assert r1.estimated_resale_value == r3.estimated_resale_value
