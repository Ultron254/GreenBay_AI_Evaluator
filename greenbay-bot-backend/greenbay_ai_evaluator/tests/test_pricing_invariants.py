"""Invariants that must hold for EVERY offer, whatever path produced it.

Five pricing defects reached production this year, each found by inspecting one
code path at a time: offers above the seller's ask, offers at 100% of resale, a
fabricated anchor arming a guardrail, an unreachable confidence tier, and a
floor with no margin check. Per-path tests kept missing them because the bug
was always in the interaction between guardrails.

These tests assert properties over a wide grid of inputs instead, so a new
guardrail that violates one is caught without anyone thinking to test it.
"""

from __future__ import annotations

import itertools

import pytest

from greenbay_ai_evaluator.engine.offer_engine import (
    MAX_OFFER_TO_RESALE,
    PricingPolicyData,
    compute_valuation,
)

CATEGORIES = ["refrigerator", "tv_monitor", "washing_machine", "cooker_oven"]
GRADES = ["A", "B", "C", "D"]
AGES = [0.5, 2.0, 5.0, 12.0]
RETAILS = [8_000.0, 50_000.0, 250_000.0]
ASKS = [None, 5_000.0, 30_000.0, 500_000.0]


@pytest.fixture(scope="module")
def policy():
    return PricingPolicyData(
        category="tv",
        margin_pct=0.15,
        max_offer_pct=0.70,
        walkaway_pct=0.45,
        brand_premium_json={},
        condition_multiplier_json={"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.45},
    )


def _grid():
    return itertools.product(CATEGORIES, GRADES, AGES, RETAILS, ASKS)


def _run(policy, category, grade, age, retail, ask):
    return compute_valuation(
        pricing_policy=policy,
        category=category,
        brand="Samsung",
        model="TEST-1",
        age_years=age,
        condition_grade=grade,
        condition_score={"A": 95, "B": 78, "C": 55, "D": 30}[grade],
        retail_price=retail,
        seller_asking_price=ask,
        defects=[],
        comparables=[],
        image_quality_score=80.0,
        risk_score=10.0,
    )


@pytest.fixture(scope="module")
def results(policy):
    return [
        ((category, grade, age, retail, ask),
         _run(policy, category, grade, age, retail, ask))
        for category, grade, age, retail, ask in _grid()
    ]


class TestOfferOrdering:
    def test_walkaway_never_exceeds_offer(self, results):
        for case, r in results:
            assert r.walkaway_limit <= r.opening_offer + 1e-6, case

    def test_offer_never_exceeds_ceiling(self, results):
        for case, r in results:
            assert r.opening_offer <= r.acquisition_ceiling + 1e-6, case

    def test_nothing_is_negative(self, results):
        for case, r in results:
            assert r.opening_offer >= 0, case
            assert r.walkaway_limit >= 0, case
            assert r.estimated_resale_value >= 0, case


class TestNeverBidAboveTheAsk:
    def test_offer_respects_seller_asking_price(self, results):
        for case, r in results:
            ask = case[4]
            if ask:
                assert r.opening_offer <= ask + 1e-6, case


class TestMarginIsNeverFullyGivenAway:
    def test_offer_stays_under_resale_unless_resale_was_disowned(self, results):
        """The one legitimate exception is the under-sourced-resale floor."""
        for case, r in results:
            if r.estimated_resale_value <= 0 or getattr(r, "new_price_floor_applied", False):
                continue
            assert r.opening_offer <= r.estimated_resale_value * MAX_OFFER_TO_RESALE + 1e-6, case


class TestConfidenceIsBounded:
    def test_confidence_within_range(self, results):
        for case, r in results:
            assert 0.0 <= r.confidence_score <= 97.0, case

    def test_low_confidence_is_never_auto_accepted(self, results):
        for case, r in results:
            if r.confidence_score < 80.0:
                assert r.decision != "accept", case


class TestMonotonicity:
    """Same item, worse condition, must never be worth more."""

    def test_worse_grade_never_pays_more(self, policy):
        for category, age, retail in itertools.product(CATEGORIES, AGES, RETAILS):
            prev = None
            for grade in GRADES:
                r = _run(policy, category, grade, age, retail, None)
                if prev is not None:
                    assert r.opening_offer <= prev + 1e-6, (category, grade, age, retail)
                prev = r.opening_offer

    def test_older_item_never_pays_more(self, policy):
        for category, grade, retail in itertools.product(CATEGORIES, GRADES, RETAILS):
            prev = None
            for age in AGES:
                r = _run(policy, category, grade, age, retail, None)
                if prev is not None:
                    assert r.opening_offer <= prev + 1e-6, (category, grade, age, retail)
                prev = r.opening_offer
