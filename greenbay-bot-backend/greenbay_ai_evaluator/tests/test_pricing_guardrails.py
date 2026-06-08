"""
Regression tests for the recent pricing-safety changes (Jun 2026):

  - New-price consistency ceiling (offer may never approach a new unit's price)
  - Per-category MAX_TRADEIN_TO_NEW resolution
  - reconcile_retail_price excludes frontend category-default estimates
  - Tuned acquisition ratios for small/low-value appliances

All deterministic — no network, DB, or LLM calls.
"""

import pytest

from greenbay_ai_evaluator.engine.offer_engine import (
    DEFAULT_MAX_TRADEIN_TO_NEW,
    PricingPolicyData,
    _max_tradein_to_new,
    compute_valuation,
    reconcile_retail_price,
)
from greenbay_ai_evaluator.services.reference_data_service import (
    DEFAULT_ACQUISITION_RATIO,
    get_category_acquisition_ratio,
)


@pytest.fixture
def policy() -> PricingPolicyData:
    return PricingPolicyData(
        category="tv",
        margin_pct=0.15,
        max_offer_pct=0.70,
        walkaway_pct=0.45,
        brand_premium_json={},
        condition_multiplier_json={"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.45},
    )


# ---------------------------------------------------------------------------
# _max_tradein_to_new
# ---------------------------------------------------------------------------
class TestMaxTradeInToNew:
    def test_known_categories(self):
        assert _max_tradein_to_new("tv") == 0.45
        assert _max_tradein_to_new("refrigerator") == 0.50
        assert _max_tradein_to_new("microwave") == 0.40
        assert _max_tradein_to_new("kettle") == 0.30

    def test_separator_insensitive(self):
        assert _max_tradein_to_new("tv monitor") == 0.45
        assert _max_tradein_to_new("tv-monitor") == 0.45

    def test_unknown_uses_default(self):
        assert _max_tradein_to_new("spaceship") == DEFAULT_MAX_TRADEIN_TO_NEW

    def test_empty_uses_default(self):
        assert _max_tradein_to_new("") == DEFAULT_MAX_TRADEIN_TO_NEW


# ---------------------------------------------------------------------------
# New-price consistency ceiling (STEP 11b)
# ---------------------------------------------------------------------------
class TestNewPriceCeiling:
    def _inputs(self, policy, **over):
        base = dict(
            category="tv",
            brand="Sony",
            model="X90",
            age_years=1.0,
            condition_grade="A",
            condition_score=90.0,
            defects=[],
            seller_asking_price=None,
            image_quality_score=85.0,
            risk_score=20.0,
            comparables=[],
            pricing_policy=policy,
            retail_price=40_000.0,
            # Anchor the "new price" at 40k via the blend, no internet source.
            price_verification={
                "reconciled_price": 40_000,
                "num_real_sources": 4,
                "num_sources": 4,
                "sources": [],
            },
            has_matrix_match=True,
            has_sales_stock_match=True,
        )
        base.update(over)
        return base

    def test_offer_capped_below_new_price(self, policy):
        result = compute_valuation(**self._inputs(policy))
        # TV ceiling is 45% of the 40k new price => 18,000 max.
        assert result.opening_offer <= 18_000
        assert "New-price ceiling" in result.guardrail_note

    def test_breach_forces_human_review(self, policy):
        result = compute_valuation(**self._inputs(policy))
        # A breach must drop confidence below the 80% auto threshold.
        assert result.confidence_score <= 75.0
        assert result.decision == "review"

    def test_prefers_internet_new_price_over_blend(self, policy):
        # When a grounded internet new price exists, the anchor should rise to it
        # (160k), so a legit offer is NOT spuriously capped.
        inputs = self._inputs(policy)
        inputs["price_verification"] = {
            "reconciled_price": 40_000,
            "num_real_sources": 4,
            "num_sources": 4,
            "sources": [{"source": "internet_lookup", "price": 160_000}],
        }
        result = compute_valuation(**inputs)
        # Cap is now 0.45 * 160k = 72k, so the offer is not clipped to 18k.
        assert result.opening_offer > 18_000


# ---------------------------------------------------------------------------
# reconcile_retail_price — estimate exclusion
# ---------------------------------------------------------------------------
class TestReconcileEstimateExclusion:
    def test_category_default_not_a_real_source(self):
        pv = reconcile_retail_price(
            frontend_price=45_000, frontend_source="category_default",
        )
        assert pv["num_real_sources"] == 0
        assert pv["new_price_verified"] is False
        frontend_src = next(s for s in pv["sources"] if s["source"] == "frontend")
        assert frontend_src.get("excluded_from_blend") is True

    def test_estimate_excluded_from_blend(self):
        # Estimate (45k) must NOT drag the reconciled price; only the real
        # internet price (30k) should drive it.
        pv = reconcile_retail_price(
            frontend_price=45_000,
            frontend_source="category_default",
            internet_price=30_000,
        )
        assert pv["num_real_sources"] == 1
        assert pv["reconciled_price"] == pytest.approx(30_000, abs=500)

    def test_real_frontend_source_counts(self):
        pv = reconcile_retail_price(
            frontend_price=50_000, frontend_source="user_entered",
        )
        assert pv["num_real_sources"] == 1
        assert pv["reconciled_price"] == pytest.approx(50_000, abs=500)


# ---------------------------------------------------------------------------
# Acquisition ratios — tuned small/low-value appliances
# ---------------------------------------------------------------------------
class TestAcquisitionRatios:
    def test_small_appliances_trimmed(self):
        assert get_category_acquisition_ratio("microwave") == 0.55
        assert get_category_acquisition_ratio("iron_box") == 0.55
        assert get_category_acquisition_ratio("kettle") == 0.60
        assert get_category_acquisition_ratio("blender") == 0.50

    def test_large_appliances_unchanged(self):
        assert get_category_acquisition_ratio("refrigerator") == 0.73
        assert get_category_acquisition_ratio("tv") == 0.81

    def test_separator_insensitive(self):
        assert get_category_acquisition_ratio("iron box") == 0.55

    def test_unknown_uses_default(self):
        assert get_category_acquisition_ratio("spaceship") == DEFAULT_ACQUISITION_RATIO
