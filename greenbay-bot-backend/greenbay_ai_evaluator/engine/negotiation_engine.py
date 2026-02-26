"""
Bounded, deterministic negotiation engine.

This module is PURE ARITHMETIC with policy lookups — it NEVER calls an LLM.
All persistence (negotiation_round, decision_ledger) must be done by the
caller after receiving the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class NegotiationState:
    """Snapshot of negotiation history required by the engine."""

    current_round: int  # how many rounds have already occurred
    previous_system_offer: float  # last offer the system made
    acquisition_ceiling: float
    walkaway_limit: float


@dataclass
class NegotiationResult:
    """Output of ``process_counter()``."""

    round_number: int
    decision: str  # accept | counter | decline
    system_offer: float
    ceiling: float
    reason: str
    rounds_remaining: int


def process_counter(
    *,
    seller_counter: float,
    state: NegotiationState,
    max_negotiation_rounds: int,
    round_step_pct: float,
) -> NegotiationResult:
    """Process a seller counter-offer and return the system's response.

    Rules (non-negotiable, deterministic):

    1. If we have exceeded ``max_negotiation_rounds`` → decline.
    2. If ``seller_counter ≤ previous_system_offer`` → accept at seller_counter.
    3. If ``seller_counter ≤ acquisition_ceiling`` → accept at seller_counter.
    4. Compute next system offer:
       ``previous + (ceiling − previous) × round_step_pct``
    5. Cap at ceiling.
    6. If ``seller_counter > ceiling × 1.5`` → decline.
    7. Otherwise → counter with new system offer.

    Parameters
    ----------
    seller_counter : float
        The amount the seller is asking for.
    state : NegotiationState
        Current negotiation state (round count, last offer, ceiling).
    max_negotiation_rounds : int
        Maximum number of rounds allowed (from pricing policy).
    round_step_pct : float
        Fractional step increase per round (from pricing policy).

    Returns
    -------
    NegotiationResult
    """
    next_round = state.current_round + 1
    ceiling = state.acquisition_ceiling
    remaining = max(0, max_negotiation_rounds - next_round)

    # Rule 1: Exceeded max rounds
    if next_round > max_negotiation_rounds:
        return NegotiationResult(
            round_number=next_round,
            decision="decline",
            system_offer=state.previous_system_offer,
            ceiling=ceiling,
            reason=(
                f"Maximum negotiation rounds ({max_negotiation_rounds}) "
                f"exceeded. Unable to continue negotiation."
            ),
            rounds_remaining=0,
        )

    # Rule 2: Seller accepts at or below our last offer
    if seller_counter <= state.previous_system_offer:
        return NegotiationResult(
            round_number=next_round,
            decision="accept",
            system_offer=seller_counter,
            ceiling=ceiling,
            reason=(
                f"Seller counter KES {seller_counter:,.0f} is at or below "
                f"previous system offer of KES {state.previous_system_offer:,.0f}. "
                f"Deal accepted."
            ),
            rounds_remaining=remaining,
        )

    # Rule 3: Seller asks within ceiling
    if seller_counter <= ceiling:
        return NegotiationResult(
            round_number=next_round,
            decision="accept",
            system_offer=seller_counter,
            ceiling=ceiling,
            reason=(
                f"Seller counter KES {seller_counter:,.0f} is within "
                f"acquisition ceiling of KES {ceiling:,.0f}. Deal accepted."
            ),
            rounds_remaining=remaining,
        )

    # Rule 6 (checked before computing next offer for efficiency):
    # Seller asking way too high
    if seller_counter > ceiling * 1.5:
        return NegotiationResult(
            round_number=next_round,
            decision="decline",
            system_offer=state.previous_system_offer,
            ceiling=ceiling,
            reason=(
                f"Seller counter KES {seller_counter:,.0f} exceeds 150% of "
                f"ceiling (KES {ceiling:,.0f}). Deal not viable."
            ),
            rounds_remaining=remaining,
        )

    # Rules 4-5: Compute next system offer, cap at ceiling
    next_offer = state.previous_system_offer + (
        (ceiling - state.previous_system_offer) * round_step_pct
    )
    next_offer = min(next_offer, ceiling)
    next_offer = round(next_offer, 2)

    # Rule 7: Counter
    return NegotiationResult(
        round_number=next_round,
        decision="counter",
        system_offer=next_offer,
        ceiling=ceiling,
        reason=(
            f"Seller counter KES {seller_counter:,.0f} exceeds ceiling. "
            f"System counter at KES {next_offer:,.0f} "
            f"(round {next_round}/{max_negotiation_rounds})."
        ),
        rounds_remaining=remaining,
    )
