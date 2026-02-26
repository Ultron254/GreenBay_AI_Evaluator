"""
Tests for greenbay_ai_evaluator.engine.offer_engine

Every test is deterministic — no mocking of external services required.
"""

import pytest

from greenbay_ai_evaluator.engine.offer_engine import (
    Comparable,
    PricingPolicyData,
    ValuationResult,
    _compute_confidence,
    _compute_depreciation_factor,
    compute_valuation,
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
    """Minimal valid inputs for ``compute_valuation``."""
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


# ---------------------------------------------------------------------------
# _compute_depreciation_factor
# ---------------------------------------------------------------------------
class TestDepreciation:
    def test_zero_age_returns_1(self, default_policy):
        assert _compute_depreciation_factor(0, default_policy) == 1.0

    def test_negative_age_returns_1(self, default_policy):
        assert _compute_depreciation_factor(-3, default_policy) == 1.0

    def test_year1_depreciation(self, default_policy):
        factor = _compute_depreciation_factor(1.0, default_policy)
        expected = 1.0 - 0.20  # default depreciation_year1
        assert factor == pytest.approx(expected, rel=1e-6)

    def test_year3_depreciation(self, default_policy):
        factor = _compute_depreciation_factor(3.0, default_policy)
        # Year 1: 0.80, then 2 years of 0.12 each
        expected = 0.80 * (1 - 0.12) ** 2
        assert factor == pytest.approx(expected, rel=1e-6)

    def test_year6_depreciation(self, default_policy):
        factor = _compute_depreciation_factor(6.0, default_policy)
        expected = 0.80 * (1 - 0.12) ** 2 * (1 - 0.10) ** 2 * (1 - 0.08) ** 1
        assert factor == pytest.approx(expected, rel=1e-6)

    def test_very_old_product_floors_at_5pct(self, default_policy):
        factor = _compute_depreciation_factor(50, default_policy)
        assert factor >= 0.05


# ---------------------------------------------------------------------------
# _compute_confidence
# ---------------------------------------------------------------------------
class TestConfidence:
    def test_no_data(self):
        score = _compute_confidence(0, 0.0, False, False, False)
        assert score == 0.0

    def test_full_data(self):
        score = _compute_confidence(5, 100.0, True, True, True)
        assert score == 100.0

    def test_comparables_scale(self):
        s1 = _compute_confidence(1, 50.0, False, False, False)
        s3 = _compute_confidence(3, 50.0, False, False, False)
        s5 = _compute_confidence(5, 50.0, False, False, False)
        assert s1 < s3 < s5

    def test_image_quality_contribution(self):
        low = _compute_confidence(0, 10.0, False, False, False)
        high = _compute_confidence(0, 90.0, False, False, False)
        assert high > low


# ---------------------------------------------------------------------------
# compute_valuation — full pipeline
# ---------------------------------------------------------------------------
class TestComputeValuation:
    def test_returns_valuation_result(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert isinstance(result, ValuationResult)

    def test_no_comparables_uses_depreciation(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.base_value_source == "depreciation"

    def test_sufficient_comparables_uses_comparables(self, basic_inputs, default_policy):
        comps = [Comparable(resale_price=60_000, weight=1.0) for _ in range(5)]
        basic_inputs["comparables"] = comps
        result = compute_valuation(**basic_inputs)
        assert result.base_value_source == "comparables"
        assert result.base_value == pytest.approx(60_000.0, rel=1e-6)

    def test_brand_premium_applied(self, basic_inputs):
        # Samsung has 1.10 premium
        result_samsung = compute_valuation(**basic_inputs)
        basic_inputs["brand"] = "NoBrand"
        result_nobrand = compute_valuation(**basic_inputs)
        # Samsung should have 10% higher brand-adjusted value
        assert result_samsung.brand_adjusted_value > result_nobrand.brand_adjusted_value

    def test_condition_multiplier_applied(self, basic_inputs):
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
        # Many defects to exceed the 60% cap
        basic_inputs["defects"] = [{"type": "compressor_noise"}] * 10  # 3000 each = 30000
        result = compute_valuation(**basic_inputs)
        assert result.defect_deduction_total <= result.condition_adjusted_value * 0.60

    def test_image_quality_penalty(self, basic_inputs):
        basic_inputs["image_quality_score"] = 80.0
        result_good = compute_valuation(**basic_inputs)

        basic_inputs["image_quality_score"] = 30.0
        result_bad = compute_valuation(**basic_inputs)

        assert result_bad.image_quality_penalty_applied is True
        assert result_good.image_quality_penalty_applied is False
        assert result_bad.estimated_resale_value < result_good.estimated_resale_value

    def test_risk_adjustment(self, basic_inputs):
        basic_inputs["risk_score"] = 20.0
        result_low = compute_valuation(**basic_inputs)
        basic_inputs["risk_score"] = 60.0
        result_high = compute_valuation(**basic_inputs)

        assert result_low.risk_adjustment_applied is False
        assert result_high.risk_adjustment_applied is True
        assert result_high.acquisition_ceiling < result_low.acquisition_ceiling

    def test_ceiling_always_gte_opening(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.acquisition_ceiling >= result.opening_offer

    def test_opening_always_gte_walkaway(self, basic_inputs):
        result = compute_valuation(**basic_inputs)
        assert result.opening_offer >= result.walkaway_limit


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------
class TestDecisionLogic:
    def test_accept_when_seller_below_opening(self, basic_inputs):
        result_no_ask = compute_valuation(**basic_inputs)
        basic_inputs["seller_asking_price"] = result_no_ask.opening_offer * 0.8
        result = compute_valuation(**basic_inputs)
        assert result.decision == "accept"

    def test_negotiate_when_no_asking_price(self, basic_inputs):
        basic_inputs["seller_asking_price"] = None
        result = compute_valuation(**basic_inputs)
        assert result.decision == "negotiate"

    def test_negotiate_when_between_opening_and_ceiling(self, basic_inputs):
        result_ref = compute_valuation(**basic_inputs)
        mid = (result_ref.opening_offer + result_ref.acquisition_ceiling) / 2
        basic_inputs["seller_asking_price"] = mid
        result = compute_valuation(**basic_inputs)
        assert result.decision == "negotiate"

    def test_decline_when_way_above_ceiling(self, basic_inputs):
        result_ref = compute_valuation(**basic_inputs)
        basic_inputs["seller_asking_price"] = result_ref.acquisition_ceiling * 2
        result = compute_valuation(**basic_inputs)
        assert result.decision == "decline"

    def test_review_when_low_confidence(self, basic_inputs):
        basic_inputs["comparables"] = []
        basic_inputs["image_quality_score"] = 10.0
        basic_inputs["brand"] = ""
        basic_inputs["age_years"] = 0
        # This should give very low confidence
        result = compute_valuation(**basic_inputs)
        if result.confidence_score < 40:
            assert result.decision == "review"

    def test_review_when_high_risk(self, basic_inputs):
        basic_inputs["risk_score"] = 80.0
        result = compute_valuation(**basic_inputs)
        assert result.decision == "review"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_identical_inputs_produce_identical_outputs(self, basic_inputs):
        r1 = compute_valuation(**basic_inputs)
        r2 = compute_valuation(**basic_inputs)
        assert r1.estimated_resale_value == r2.estimated_resale_value
        assert r1.acquisition_ceiling == r2.acquisition_ceiling
        assert r1.opening_offer == r2.opening_offer
        assert r1.walkaway_limit == r2.walkaway_limit
        assert r1.confidence_score == r2.confidence_score
        assert r1.decision == r2.decision

    def test_output_independent_of_call_order(self, basic_inputs):
        """Run valuation, change inputs, run again, then re-run original — should match."""
        r1 = compute_valuation(**basic_inputs)
        basic_inputs["retail_price"] = 200_000
        _ = compute_valuation(**basic_inputs)
        basic_inputs["retail_price"] = 100_000
        r3 = compute_valuation(**basic_inputs)
        assert r1.estimated_resale_value == r3.estimated_resale_value
