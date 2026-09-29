"""No guardrail may quote an offer with no resale margin left.

The historical floor, the new-price floor and the min-offer rule can all RAISE
opening_offer, and none of them compared the result to estimated_resale_value.
A live evaluation came back with offer == resale exactly (KES 20,500 both),
i.e. buy at 100% of what it can be sold for. STEP 11e is the backstop.
"""

from __future__ import annotations

import greenbay_ai_evaluator.engine.offer_engine as oe
from greenbay_ai_evaluator.engine.offer_engine import MAX_OFFER_TO_RESALE


class TestBackstopConstant:
    def test_leaves_room_for_the_calibration_clamp(self):
        # Learned ratios are clamped at 0.90; the backstop must not fight them.
        assert MAX_OFFER_TO_RESALE > 0.90

    def test_never_allows_a_full_price_purchase(self):
        assert MAX_OFFER_TO_RESALE < 1.0


class TestBackstopArithmetic:
    """The rule the engine applies, verified independently of I/O."""

    @staticmethod
    def _apply(offer: float, resale: float, step: int = 500) -> float:
        if resale > 0 and offer > resale * MAX_OFFER_TO_RESALE:
            return oe._round_price(resale * MAX_OFFER_TO_RESALE, step)
        return offer

    def test_offer_equal_to_resale_is_capped(self):
        assert self._apply(20_500, 20_500) < 20_500

    def test_offer_above_resale_is_capped(self):
        assert self._apply(26_500, 20_500) < 20_500

    def test_healthy_margin_is_untouched(self):
        # 0.73 static ratio - well inside the backstop.
        assert self._apply(14_965, 20_500) == 14_965

    def test_clamp_ceiling_ratio_is_untouched(self):
        # 0.90 learned ratio must not be clipped by the backstop.
        assert self._apply(18_450, 20_500) == 18_450

    def test_zero_resale_does_not_divide_by_zero(self):
        assert self._apply(5_000, 0) == 5_000


class TestEngineWiring:
    def test_step_11e_exists_in_the_engine(self):
        import inspect
        src = inspect.getsource(oe.compute_valuation)
        assert "Margin backstop" in src

    def test_backstop_runs_after_every_raising_guardrail(self):
        """A backstop placed before a floor would be bypassed by it."""
        import inspect
        src = inspect.getsource(oe.compute_valuation)
        backstop = src.index("Margin backstop")
        for raiser in ("Price floor applied", "New-price floor"):
            assert src.index(raiser) < backstop, raiser

    def test_asking_price_cap_reasserted_after_the_backstop(self):
        import inspect
        src = inspect.getsource(oe.compute_valuation)
        tail = src[src.index("Margin backstop"):]
        assert "seller_asking_price" in tail


class TestBackstopDefersToTheNewPriceFloor:
    """The floor fires when resale itself is under-sourced.

    Measuring against a resale value the engine has just declared wrong would
    re-collapse the offer to that bad number, which is the exact bug the floor
    was added to fix. That path forces review, so the loss risk is human-checked.
    """

    def test_backstop_is_skipped_when_the_floor_applied(self):
        import inspect
        src = inspect.getsource(oe.compute_valuation)
        tail = src[src.index("Margin backstop"):]
        guard = tail[: tail.index("estimated_resale_value > 0")]
        assert "not new_price_floor_applied" in guard
