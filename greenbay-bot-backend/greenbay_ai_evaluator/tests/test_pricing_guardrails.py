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
        # Jul 2026: caps recalibrated to sit near the internal team's ~P80
        # pay-ratio (team medians: TV 0.59, fridge 0.50, microwave 0.26) so
        # the ceiling catches absurdity without clipping normal deals.
        assert _max_tradein_to_new("tv") == 0.70
        assert _max_tradein_to_new("refrigerator") == 0.70
        assert _max_tradein_to_new("microwave") == 0.45
        assert _max_tradein_to_new("kettle") == 0.30

    def test_separator_insensitive(self):
        assert _max_tradein_to_new("tv monitor") == 0.70
        assert _max_tradein_to_new("tv-monitor") == 0.70

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
            # Inflated resale base (45k for a 40k-new TV) so the raw offer
            # clearly breaches the 0.70 ceiling.
            reference_resale_value=45_000.0,
            reference_source="sales_stock_exact",
            has_matrix_match=True,
            has_sales_stock_match=True,
        )
        base.update(over)
        return base

    def test_offer_capped_below_new_price(self, policy):
        result = compute_valuation(**self._inputs(policy))
        # TV ceiling is 70% of the 40k new price => 28,000 max.
        assert result.opening_offer <= 28_000
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
        # Cap is now 0.70 * 160k = 112k, so the offer is not clipped to 28k.
        assert result.opening_offer > 28_000


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
# reconcile_retail_price — trade-in contamination fix (Jul 2026)
# Historical closed-deal / expert / marketplace prices are TRADE-IN level and
# must never drag the NEW-price blend down (the "Roch fridge offered 5,000
# because its past trade-in price masqueraded as the new price" bug).
# ---------------------------------------------------------------------------
class TestReconcileTradeinExclusion:
    def test_historical_tradein_does_not_move_new_price(self):
        pv = reconcile_retail_price(
            frontend_price=45_000,
            frontend_source="category_default",
            internet_price=38_000,     # genuine Gemini new price
            historical_avg=10_000,     # team's past trade-in price
        )
        # The new price must stay at the grounded 38k, NOT collapse toward 10k.
        assert pv["reconciled_price"] == pytest.approx(38_000, abs=500)
        assert pv["new_price_verified"] is True
        hist = next(s for s in pv["sources"] if s["source"] == "historical_sheet")
        assert hist.get("excluded_from_blend") is True

    def test_genuine_new_price_never_discarded_as_outlier(self):
        # Old logic discarded the AI tier when it deviated >2x from the
        # trade-in tier — which is ALWAYS true since new ≈ 2x trade-in.
        pv = reconcile_retail_price(
            frontend_price=0,
            internet_price=38_000,
            historical_avg=10_000,
            expert_avg=11_000,
        )
        internet = next(s for s in pv["sources"] if s["source"] == "internet_lookup")
        assert not internet.get("discarded")
        assert pv["reconciled_price"] == pytest.approx(38_000, abs=500)

    def test_tradein_only_falls_back_unverified(self):
        # With no genuine new-price signal the blend cannot be verified.
        pv = reconcile_retail_price(
            frontend_price=45_000,
            frontend_source="category_default",
            historical_avg=10_000,
        )
        assert pv["new_price_verified"] is False
        # Trade-in intel still corroborates (counts as a real source).
        assert pv["num_real_sources"] == 1

    def test_marketplace_listing_excluded_from_new_blend(self):
        pv = reconcile_retail_price(
            frontend_price=0,
            internet_price=40_000,
            marketplace_avg=22_000,  # used Jiji listing, not a new price
        )
        assert pv["reconciled_price"] == pytest.approx(40_000, abs=500)


# ---------------------------------------------------------------------------
# Dual-source new-price merging (Gemini + Perplexity Sonar) — Jul 2026
# ---------------------------------------------------------------------------
class TestCombineNewPriceSignals:
    def _combine(self, g, s):
        from greenbay_ai_evaluator.services.market_price_service import (
            combine_new_price_signals,
        )
        return combine_new_price_signals(g, s)

    def test_agreement_averages(self):
        price, how = self._combine(40_000, 44_000)
        assert price == pytest.approx(42_000)
        assert "agree" in how

    def test_conflict_takes_lower(self):
        # Wrong-variant errors inflate; conservative pick is the lower price.
        price, how = self._combine(136_999, 50_000)
        assert price == 50_000
        assert "lower" in how

    def test_single_source_passthrough(self):
        assert self._combine(38_000, None)[0] == 38_000
        assert self._combine(None, 41_000)[0] == 41_000

    def test_no_sources(self):
        assert self._combine(None, None)[0] is None
        assert self._combine(0, 0)[0] is None


# ---------------------------------------------------------------------------
# Calibrated acquisition ratios — learned from the team's own evaluations
# ---------------------------------------------------------------------------
class TestCalibratedRatios:
    def test_calibrated_ratio_preferred_when_learned(self):
        from greenbay_ai_evaluator.services import reference_data_service as rds
        rds._calibrated_ratios["tv_monitor"] = {
            "ratio": 0.62, "raw_median": 0.60, "static_prior": 0.81, "n": 30,
        }
        try:
            assert get_category_acquisition_ratio("tv") == 0.62
            assert get_category_acquisition_ratio("tv_monitor") == 0.62
        finally:
            rds._calibrated_ratios.clear()

    def test_fallback_to_static_without_calibration(self):
        from greenbay_ai_evaluator.services import reference_data_service as rds
        rds._calibrated_ratios.clear()
        assert get_category_acquisition_ratio("tv") == 0.81

    def test_shrinkage_math_is_sane(self):
        # With n samples and prior k=5, blended = (n*raw + k*prior)/(n+k):
        # few samples stay near the prior, many samples converge to the data.
        from greenbay_ai_evaluator.services.reference_data_service import (
            _CALIB_SHRINK_K,
        )
        prior, raw = 0.81, 0.50
        few = (4 * raw + _CALIB_SHRINK_K * prior) / (4 + _CALIB_SHRINK_K)
        many = (60 * raw + _CALIB_SHRINK_K * prior) / (60 + _CALIB_SHRINK_K)
        assert abs(few - prior) < abs(few - raw)    # 4 samples: closer to prior
        assert abs(many - raw) < 0.03               # 60 samples: data wins


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
        # Microwave MAX_TRADEIN_TO_NEW is 0.45 of the 17k anchor = 7,650.
        assert result.opening_offer <= 7_650

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
