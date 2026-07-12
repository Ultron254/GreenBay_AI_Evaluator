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
    NEW_PRICE_FLOOR_SAFETY,
    PricingPolicyData,
    _max_tradein_to_new,
    compute_valuation,
    reconcile_retail_price,
    select_new_price_estimate,
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
# New-price consistency floor (STEP 11c) — Jul 2026
# The offer must not collapse to a token amount when a VERIFIED new price
# exists (the "microwave priced at 1,000 vs new 17,000" class of bug).
# ---------------------------------------------------------------------------
class TestNewPriceFloor:
    def _inputs(self, policy, **over):
        base = dict(
            category="microwave",
            brand="Samsung",
            model="ME731K-B",
            age_years=2.0,
            condition_grade="B",
            condition_score=70.0,
            defects=[],
            seller_asking_price=None,
            image_quality_score=85.0,
            risk_score=20.0,
            comparables=[],
            pricing_policy=policy,
            # Base value catastrophically under-sourced (a used listing was
            # mistaken for new) -> raw offer collapses to a token amount.
            retail_price=2_000.0,
            price_verification={
                # Verified new price says the unit retails at 17,000 new.
                "reconciled_price": 2_000,
                "num_real_sources": 2,
                "num_sources": 2,
                "sources": [{"source": "internet_lookup", "price": 17_000}],
            },
        )
        base.update(over)
        return base

    def test_collapsed_offer_raised_to_floor(self, policy):
        result = compute_valuation(**self._inputs(policy))
        # Expected-from-new for a 2y grade-B microwave is thousands, not
        # hundreds; the floor must lift the offer well above the collapse.
        assert result.new_price_floor_applied is True
        assert result.opening_offer >= 2_000
        assert "New-price floor" in result.guardrail_note

    def test_floor_forces_human_review(self, policy):
        result = compute_valuation(**self._inputs(policy))
        assert result.confidence_score <= 75.0
        assert result.decision == "review"

    def test_floor_never_exceeds_new_price_ceiling(self, policy):
        result = compute_valuation(**self._inputs(policy))
        # Microwave MAX_TRADEIN_TO_NEW is 0.40 of the 17k anchor = 6,800.
        assert result.opening_offer <= 6_800

    def test_healthy_offer_untouched(self, policy):
        # A well-sourced valuation must NOT trigger the floor.
        inputs = self._inputs(
            policy,
            retail_price=17_000.0,
            price_verification={
                "reconciled_price": 17_000,
                "num_real_sources": 3,
                "num_sources": 3,
                "sources": [{"source": "internet_lookup", "price": 17_000}],
            },
        )
        result = compute_valuation(**inputs)
        assert result.new_price_floor_applied is False

    def test_unverified_anchor_never_inflates(self, policy):
        # A frontend category-default guess must NOT raise the offer: with no
        # price_verification the anchor is unverified, so no floor.
        inputs = self._inputs(policy, price_verification=None, retail_price=50_000.0)
        result = compute_valuation(**inputs)
        assert result.new_price_floor_applied is False

    def test_safety_margin_is_conservative(self):
        assert 0.4 <= NEW_PRICE_FLOOR_SAFETY <= 0.8


# ---------------------------------------------------------------------------
# New-price estimate selection + Gemini/matrix cross-check — Jul 2026
# (the "136,999 for an 8kg washer" class of bug)
# ---------------------------------------------------------------------------
class TestSelectNewPriceEstimate:
    def test_gemini_preferred_when_sane(self):
        price, src = select_new_price_estimate(50_000, 48_000, 30_000, 35_000)
        assert price == 50_000
        assert src == "gemini"

    def test_gemini_outlier_high_rejected_for_matrix(self):
        # Gemini returns a premium washer-dryer (136,999) for an 8kg washer the
        # matrix knows retails at ~50k -> matrix must win.
        price, src = select_new_price_estimate(136_999, 50_000, 30_000, 35_000)
        assert price == 50_000
        assert "outlier" in src

    def test_gemini_outlier_low_rejected_for_matrix(self):
        price, src = select_new_price_estimate(9_000, 50_000, 30_000, 35_000)
        assert price == 50_000
        assert "outlier" in src

    def test_matrix_when_no_gemini(self):
        price, src = select_new_price_estimate(0, 48_000, 30_000, 35_000)
        assert price == 48_000
        assert src == "matrix"

    def test_reconciled_then_frontend_fallbacks(self):
        assert select_new_price_estimate(0, 0, 30_000, 35_000)[0] == 30_000
        assert select_new_price_estimate(0, 0, 0, 35_000)[0] == 35_000

    def test_no_matrix_keeps_gemini(self):
        # Without a matrix reference there is nothing to cross-check against.
        price, src = select_new_price_estimate(136_999, 0, 30_000, 35_000)
        assert price == 136_999
        assert src == "gemini"

    def test_garbage_inputs_are_safe(self):
        price, src = select_new_price_estimate("abc", None, 0, "", 0)
        assert price == 0.0
        assert src == "last_resort"


# ---------------------------------------------------------------------------
# Tracker header detection — write path must not depend on the human-managed
# "Internal Team Price" column (renaming it silently killed appends before).
# ---------------------------------------------------------------------------
class TestTrackerHeaderDetection:
    HEADER_FULL = ["Date", "Item", "Model", "Condition", "Age",
                   "New Price", "AI Price", "Confidence",
                   "Customer Selling Price", "Internal Team Price",
                   "Final Price Offered", "Accepted", "Notes"]
    HEADER_NO_INTERNAL = ["Date", "Item", "Model", "Condition", "Age",
                          "New Price", "AI Price", "Confidence",
                          "Customer Selling Price", "Accepted", "Notes"]

    def test_read_requires_internal_column(self):
        from greenbay_ai_evaluator.services.reference_data_service import (
            _find_header_index,
        )
        hidx, cmap = _find_header_index([self.HEADER_FULL])
        assert hidx == 0 and "internal_price" in cmap
        hidx, _ = _find_header_index([self.HEADER_NO_INTERNAL])
        assert hidx == -1  # strict read contract unchanged

    def test_write_tolerates_missing_internal_column(self):
        from greenbay_ai_evaluator.services.reference_data_service import (
            _find_header_index,
        )
        hidx, cmap = _find_header_index(
            [self.HEADER_NO_INTERNAL], require_internal=False
        )
        assert hidx == 0
        assert cmap["item"] == 1
        assert cmap["ai_price"] == 6


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
