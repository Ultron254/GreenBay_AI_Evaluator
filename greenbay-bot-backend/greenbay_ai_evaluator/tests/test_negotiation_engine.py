"""
Tests for greenbay_ai_evaluator.engine.negotiation_engine

Every test is deterministic — no mocking of external services required.
"""

import pytest

from greenbay_ai_evaluator.engine.negotiation_engine import (
    NegotiationResult,
    NegotiationState,
    process_counter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def base_state() -> NegotiationState:
    """Standard starting negotiation state."""
    return NegotiationState(
        current_round=1,
        previous_system_offer=30_000.0,
        acquisition_ceiling=42_000.0,
        walkaway_limit=21_000.0,
    )


MAX_ROUNDS = 3
STEP_PCT = 0.05


def _call(seller_counter: float, state: NegotiationState) -> NegotiationResult:
    return process_counter(
        seller_counter=seller_counter,
        state=state,
        max_negotiation_rounds=MAX_ROUNDS,
        round_step_pct=STEP_PCT,
    )


# ---------------------------------------------------------------------------
# Rule 1: Max rounds exceeded → decline
# ---------------------------------------------------------------------------
class TestMaxRoundsExceeded:
    def test_decline_when_rounds_exceeded(self, base_state):
        base_state.current_round = MAX_ROUNDS  # next round will be 4 > 3
        result = _call(50_000, base_state)
        assert result.decision == "decline"
        assert result.rounds_remaining == 0

    def test_system_offer_stays_at_last(self, base_state):
        base_state.current_round = MAX_ROUNDS
        result = _call(50_000, base_state)
        assert result.system_offer == base_state.previous_system_offer


# ---------------------------------------------------------------------------
# Rule 2: Seller counter ≤ previous system offer → accept
# ---------------------------------------------------------------------------
class TestSellerAcceptsOurOffer:
    def test_exact_match_accepted(self, base_state):
        result = _call(30_000, base_state)
        assert result.decision == "accept"
        assert result.system_offer == 30_000

    def test_below_our_offer_accepted(self, base_state):
        result = _call(25_000, base_state)
        assert result.decision == "accept"
        assert result.system_offer == 25_000


# ---------------------------------------------------------------------------
# Rule 3: Seller counter ≤ ceiling → accept
# ---------------------------------------------------------------------------
class TestSellerWithinCeiling:
    def test_within_ceiling_accepted(self, base_state):
        result = _call(40_000, base_state)
        assert result.decision == "accept"
        assert result.system_offer == 40_000

    def test_at_ceiling_accepted(self, base_state):
        result = _call(42_000, base_state)
        assert result.decision == "accept"


# ---------------------------------------------------------------------------
# Rule 6: Seller counter > 150% of ceiling → decline
# ---------------------------------------------------------------------------
class TestSellerWayTooHigh:
    def test_decline_above_150pct_ceiling(self, base_state):
        # 42000 * 1.5 = 63000, so anything above 63k should decline
        result = _call(63_001, base_state)
        assert result.decision == "decline"

    def test_exactly_at_150pct_still_negotiable(self, base_state):
        result = _call(63_000, base_state)
        # 63000 is NOT > 63000 (it's equal), so should NOT decline
        assert result.decision != "decline"


# ---------------------------------------------------------------------------
# Rules 4-5, 7: Regular counter-offer
# ---------------------------------------------------------------------------
class TestCounterOffer:
    def test_counter_when_above_ceiling(self, base_state):
        # 50k is above ceiling (42k) but below 150% of ceiling (63k)
        result = _call(50_000, base_state)
        assert result.decision == "counter"

    def test_next_offer_increased(self, base_state):
        result = _call(50_000, base_state)
        expected = 30_000 + (42_000 - 30_000) * STEP_PCT
        assert result.system_offer == pytest.approx(expected, rel=1e-6)

    def test_offer_capped_at_ceiling(self, base_state):
        # Force previous offer very close to ceiling
        base_state.previous_system_offer = 41_500
        result = _call(50_000, base_state)
        assert result.system_offer <= base_state.acquisition_ceiling

    def test_rounds_remaining_decremented(self, base_state):
        result = _call(50_000, base_state)
        # current_round=1, next_round=2, remaining = max(0, 3-2) = 1
        assert result.rounds_remaining == 1


# ---------------------------------------------------------------------------
# Multi-round scenario
# ---------------------------------------------------------------------------
class TestMultiRoundNegotiation:
    def test_three_round_negotiation(self):
        """Simulate seller countering three times, each time above ceiling."""
        state = NegotiationState(
            current_round=0,
            previous_system_offer=20_000,
            acquisition_ceiling=30_000,
            walkaway_limit=15_000,
        )

        # Round 1
        r1 = process_counter(
            seller_counter=35_000,
            state=state,
            max_negotiation_rounds=3,
            round_step_pct=0.10,
        )
        assert r1.decision == "counter"
        state.current_round = r1.round_number
        state.previous_system_offer = r1.system_offer

        # Round 2
        r2 = process_counter(
            seller_counter=33_000,
            state=state,
            max_negotiation_rounds=3,
            round_step_pct=0.10,
        )
        assert r2.decision == "counter"
        state.current_round = r2.round_number
        state.previous_system_offer = r2.system_offer

        # Round 3
        r3 = process_counter(
            seller_counter=32_000,
            state=state,
            max_negotiation_rounds=3,
            round_step_pct=0.10,
        )
        assert r3.decision == "counter"
        state.current_round = r3.round_number
        state.previous_system_offer = r3.system_offer

        # Round 4 — should decline (exceeded max rounds)
        r4 = process_counter(
            seller_counter=31_000,
            state=state,
            max_negotiation_rounds=3,
            round_step_pct=0.10,
        )
        assert r4.decision == "decline"

    def test_seller_eventually_accepts(self):
        """Seller starts high but eventually comes within ceiling."""
        state = NegotiationState(
            current_round=0,
            previous_system_offer=20_000,
            acquisition_ceiling=30_000,
            walkaway_limit=15_000,
        )

        # Round 1: counter (above ceiling)
        r1 = _call(50_000, state)
        assert r1.decision != "accept"

        state.current_round = r1.round_number
        state.previous_system_offer = r1.system_offer

        # Round 2: seller drops to within ceiling
        r2 = _call(28_000, state)
        assert r2.decision == "accept"
        assert r2.system_offer == 28_000


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
class TestDeterminism:
    def test_identical_inputs_identical_outputs(self, base_state):
        r1 = _call(50_000, base_state)
        r2 = _call(50_000, base_state)
        assert r1.decision == r2.decision
        assert r1.system_offer == r2.system_offer
        assert r1.reason == r2.reason

    def test_result_contains_reason(self, base_state):
        result = _call(50_000, base_state)
        assert isinstance(result.reason, str)
        assert len(result.reason) > 0
