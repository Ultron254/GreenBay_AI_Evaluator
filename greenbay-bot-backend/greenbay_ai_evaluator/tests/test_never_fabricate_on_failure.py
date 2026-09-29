"""One guard for the whole defect class: never fabricate on failure.

Eight instances of a single pattern reached production this year. Each was
locked individually afterwards, which stops that instance and nothing else.
This file asserts the *shape* so the ninth is caught before it ships.

The shape: on a failure, timeout, missing key, empty result or parse error the
code returns a plausible DEFAULT rather than surfacing the failure, and
something downstream presents that default to a human as measured, verified,
confirmed or saved.

Fixed so far, all of this shape:
  1. generateDemoResults          invented a price when /evaluate errored
  2. _stub_analysis               Grade B / 65 when vision was unconfigured
  3. analyze_model_label          "verified" from the user's own typed text
  4. _probe_anthropic             judged the service on one of two models
  5. _probe_ses                   ok=True on a transport delivering nothing
  6. reconcile_retail_price       a vision guess counted as market-verified
  7. acceptOffer / reject         "Deal confirmed" after a failed POST
  8. analysis animation           "Brand verified" before the request returned

Assertions are deliberately narrow and file-scoped: rendering a real backend
value is legitimate and must not trip them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
APP_JS = BACKEND / "frontend" / "app.js"
VISION = BACKEND / "greenbay_ai_evaluator" / "services" / "vision_service.py"
HEALTH = BACKEND / "greenbay_ai_evaluator" / "services" / "live_healthcheck_service.py"
ENGINE = BACKEND / "greenbay_ai_evaluator" / "engine" / "offer_engine.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _read(APP_JS)


class TestNoInventedPricing:
    """A price shown to a customer must have come from the backend."""

    def test_the_demo_generator_stays_deleted(self, app_js: str):
        assert "generateDemoResults" not in app_js

    def test_any_hardcoded_retail_default_is_declared_as_an_estimate(self, app_js: str):
        """A category price table is fine only if it is labelled a guess.

        The frontend may seed retail_price when the customer does not know it,
        but it must ship retail_price_source='category_default' so the engine
        excludes it from the new-price blend. Unlabelled, it is exactly the
        fabrication that armed a pricing guardrail against real data.
        """
        table = re.search(r"(?:refrigerator|fridge)\s*:\s*\d{4,}", app_js)
        if not table:
            return
        window = app_js[table.start(): table.start() + 4000]
        assert "retail_price_source" in window, (
            "hardcoded retail table is not accompanied by a source label"
        )
        assert "category_default" in window

    def test_no_default_substitutes_a_plausible_price(self, app_js: str):
        """`|| 0` renders nothing; `|| 45000` renders a lie."""
        bad = re.findall(r"(?:opening_offer|estimated_resale_value)\s*\|\|\s*([1-9]\d{2,})", app_js)
        assert not bad, f"offer fields defaulted to plausible amounts: {bad[:5]}"

    def test_confidence_is_not_defaulted_to_a_plausible_score(self, app_js: str):
        bad = re.findall(r"confidence_score\s*\|\|\s*([1-9]\d+)", app_js)
        assert not bad, f"confidence defaulted to {bad[:5]}"


class TestNoVerifiedFlagWithoutAVerification:
    """*_verified must mean a check ran, not that input existed."""

    def test_model_verified_is_never_derived_from_typed_input(self):
        src = _read(VISION)
        assert "bool(typed_model)" not in src

    def test_vision_has_no_stub_analysis(self):
        assert "_stub_analysis" not in _read(VISION)

    def test_vision_failure_raises_rather_than_returning_a_grade(self):
        src = _read(VISION)
        assert "raise VisionUnavailableError" in src

    def test_a_vision_guess_is_not_a_verified_market_price(self):
        src = _read(ENGINE)
        block = src[src.index("_frontend_is_estimate"): src.index("_frontend_is_estimate") + 500]
        for token in ("google_lens", "vision_estimate"):
            assert token in block, token


class TestHealthProbesCannotReportFalseGreen:
    """A probe must not return OK while naming the reason it is broken."""

    FORBIDDEN = ("NOT verified", "will reject", "unavailable", "not configured")

    def test_no_true_return_carries_a_failure_phrase(self):
        src = _read(HEALTH)
        offenders = [
            line.strip()
            for line in src.splitlines()
            if re.match(r"\s*return True,", line)
            and any(f.lower() in line.lower() for f in self.FORBIDDEN)
        ]
        assert not offenders, offenders

    def test_vision_probe_covers_the_fallback_model(self):
        src = _read(HEALTH)
        block = src[src.index("def _probe_anthropic"):]
        assert "fallback" in block[:2000]

    def test_email_probe_covers_the_transport_actually_used(self):
        src = _read(HEALTH)
        block = src[src.index("def _probe_ses"):]
        assert "smtp_host" in block[:1200]


class TestNoSuccessMessageAfterAFailedCall:
    """State-changing calls must gate their confirmation on the response."""

    STATE_CHANGING = ("accept-offer", "rejection-choice")

    def test_each_state_changing_call_has_a_failure_branch(self, app_js: str):
        for endpoint in self.STATE_CHANGING:
            idx = app_js.index(endpoint)
            window = app_js[idx: idx + 1800]
            assert "resp.ok" in window, endpoint
            # A failure route the customer can actually use.
            assert "wa.me" in window, endpoint

    def test_no_unconditional_confirmation_strings(self, app_js: str):
        # These previously ran regardless of the response.
        assert "We've noted your preference" not in app_js
        assert "We\\'ve noted your preference" not in app_js

    def test_deal_confirmed_is_gated(self, app_js: str):
        idx = app_js.index("Deal confirmed at")
        preceding = app_js[max(0, idx - 1500): idx]
        assert "if (!accepted)" in preceding


class TestNoResultAssertedBeforeItExists:
    """Progress text must not state findings the backend has not returned."""

    def test_animation_does_not_claim_verification(self, app_js: str):
        start = app_js.index("const evaluationInFlight")
        block = app_js[start: app_js.index("await evaluationInFlight", start)]
        for claim in ("Brand verified", "Condition: Grade"):
            assert claim not in block, claim

    def test_the_request_is_not_awaited_only_after_the_animation(self, app_js: str):
        assert "await callEvaluationAPI();" not in app_js
