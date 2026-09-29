"""A deal is only confirmed once the server has stored it.

Two defects this locks down, both found on the live accept/reject paths:

1. acceptOffer() checked `if (resp.ok)` with no else, so a 404, 500 or network
   error fell straight through to "Wonderful! Deal confirmed at KES X". Same
   defect as the deleted generateDemoResults, surviving in a second place.
2. The POST carried no body, so the backend always stored opening_offer. After
   a negotiation the customer was shown one figure and the team recorded
   another -- a money bug.

selectRejectionOption() had the same silent fallthrough ("We've noted your
preference"), which is the most likely reason ~380 evaluations produced a
single recorded rejection.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import greenbay_ai_evaluator.api.router as router

APP_JS = Path(__file__).resolve().parents[2] / "frontend" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8", errors="replace")


def _fn(app_js: str, name: str) -> str:
    start = app_js.index(f"async function {name}(")
    return app_js[start: app_js.index("\nasync function ", start + 10)
                  if "\nasync function " in app_js[start + 10:] else start + 4000]


class TestAcceptNeverConfirmsAnUnsavedDeal:
    def test_success_is_gated_on_a_recorded_response(self, app_js: str):
        body = _fn(app_js, "acceptOffer")
        assert "if (!accepted)" in body

    def test_failure_does_not_say_deal_confirmed(self, app_js: str):
        body = _fn(app_js, "acceptOffer")
        fail = body[body.index("if (!accepted)"): body.index("state.negotiation.status = 'accepted'")]
        assert "Deal confirmed" not in fail

    def test_failure_offers_a_human_route(self, app_js: str):
        body = _fn(app_js, "acceptOffer")
        assert "wa.me" in body

    def test_status_is_not_marked_accepted_before_the_call(self, app_js: str):
        body = _fn(app_js, "acceptOffer")
        assert body.index("await fetch") < body.index("state.negotiation.status = 'accepted'")

    def test_non_2xx_is_treated_as_failure(self, app_js: str):
        assert "failureReason = `HTTP ${resp.status}`" in _fn(app_js, "acceptOffer")


class TestNegotiatedAmountReachesTheServer:
    def test_amount_is_sent_in_the_request_body(self, app_js: str):
        assert "accepted_amount: amount" in _fn(app_js, "acceptOffer")

    def test_ui_displays_the_stored_amount(self, app_js: str):
        assert "data.final_offer" in _fn(app_js, "acceptOffer")

    def test_backend_reads_the_accepted_amount(self):
        assert "accepted_amount" in inspect.getsource(router.accept_offer)

    def test_backend_clamps_to_the_engine_ceiling(self):
        src = inspect.getsource(router.accept_offer)
        assert "acquisition_ceiling" in src and "min(" in src

    def test_backend_returns_what_it_stored(self):
        assert "final_offer=float(vs.final_offer or 0)" in inspect.getsource(router.accept_offer)


class TestDecisionsAreNotSilentlyOverwritten:
    def test_a_settled_decision_is_rejected_with_409(self):
        src = inspect.getsource(router.accept_offer)
        assert "409" in src and "final_decision" in src


class TestRejectNeverClaimsAnUnsavedChoice:
    def test_no_unconditional_noted_your_preference(self, app_js: str):
        assert "We've noted your preference" not in app_js
        assert "We\\'ve noted your preference" not in app_js

    def test_failure_path_offers_a_human_route(self, app_js: str):
        start = app_js.index("Rejection choice API failed")
        assert "wa.me" in app_js[start: start + 900]
