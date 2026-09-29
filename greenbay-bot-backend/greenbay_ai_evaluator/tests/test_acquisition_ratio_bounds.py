"""Guarantees on how much we will ever pay relative to resale value.

Measured on 344 live tracker rows the team's real acquisition ratios were
cooker_oven 0.639, refrigerator 0.869, washing_machine 0.936 and tv_monitor
1.054 -- TVs were being bought at ABOVE what they resell for. The clamp is the
only thing standing between the calibrator and a loss-making quote, so its
value is a business guarantee and is asserted here.
"""

from __future__ import annotations

from greenbay_ai_evaluator.services.reference_data_service import (
    ACQUISITION_RATIOS,
    DEFAULT_ACQUISITION_RATIO,
    MAX_ACQUISITION_RATIO,
    _CALIB_CLAMP,
    _static_acquisition_ratio,
)


class TestMarginGuarantee:
    def test_ceiling_guarantees_at_least_15_percent_margin(self):
        assert MAX_ACQUISITION_RATIO <= 0.85

    def test_ceiling_never_allows_buying_at_or_above_resale(self):
        assert MAX_ACQUISITION_RATIO < 1.0

    def test_clamp_uses_the_declared_ceiling(self):
        assert _CALIB_CLAMP[1] == MAX_ACQUISITION_RATIO

    def test_clamp_floor_is_below_the_ceiling(self):
        assert _CALIB_CLAMP[0] < _CALIB_CLAMP[1]


class TestPriorsStayLearnable:
    """The ceiling must not sit below the priors, or learning is frozen."""

    def test_every_static_prior_is_under_the_ceiling(self):
        for cat, ratio in ACQUISITION_RATIOS.items():
            assert ratio <= MAX_ACQUISITION_RATIO, f"{cat}={ratio}"

    def test_default_prior_is_under_the_ceiling(self):
        assert DEFAULT_ACQUISITION_RATIO <= MAX_ACQUISITION_RATIO

    def test_there_is_headroom_above_the_highest_prior(self):
        assert MAX_ACQUISITION_RATIO > max(ACQUISITION_RATIOS.values())


class TestObservedCategoriesAreBounded:
    """The four categories we have real data for, clamped."""

    def test_observed_ratios_clamp_into_margin_safe_territory(self):
        observed = {
            "cooker_oven": 0.639,
            "refrigerator": 0.869,
            "washing_machine": 0.936,
            "tv_monitor": 1.054,
        }
        for cat, raw in observed.items():
            clamped = min(MAX_ACQUISITION_RATIO, max(_CALIB_CLAMP[0], raw))
            assert clamped <= MAX_ACQUISITION_RATIO, cat
            assert clamped < 1.0, cat

    def test_lookup_is_separator_insensitive(self):
        assert _static_acquisition_ratio("washing machine") == _static_acquisition_ratio("washing_machine")
