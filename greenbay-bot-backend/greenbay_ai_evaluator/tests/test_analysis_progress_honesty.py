"""The analysis animation must not run in front of the request, or invent results.

Two defects this locks down:

1. `callEvaluationAPI()` used to be awaited only AFTER ~7.2s of scripted
   animation, so the customer watched every step tick to complete and then
   waited out the ~30s call on a finished progress bar.
2. The chat claimed "Brand verified" and "Condition: Grade B" while the
   animation ran -- before the backend had answered. Same family as
   generateDemoResults and the vision stub: a result asserted from nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "frontend" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8", errors="replace")


class TestRequestOverlapsTheAnimation:
    def test_request_is_started_before_the_animation_loop(self, app_js: str):
        start = app_js.index("const evaluationInFlight = callEvaluationAPI()")
        loop = app_js.index("for (let i = 0; i < steps.length; i++)")
        assert start < loop

    def test_request_is_awaited_after_the_animation(self, app_js: str):
        start = app_js.index("const evaluationInFlight = callEvaluationAPI()")
        assert app_js.index("await evaluationInFlight", start) > start

    def test_the_old_trailing_await_is_gone(self, app_js: str):
        assert "await callEvaluationAPI();" not in app_js


class TestNoResultIsClaimedBeforeTheBackendAnswers:
    @staticmethod
    def _animation_block(app_js: str) -> str:
        """Only the scripted pre-answer messages, not the real results render."""
        start = app_js.index("const evaluationInFlight = callEvaluationAPI()")
        return app_js[start: app_js.index("await evaluationInFlight", start)]

    @pytest.mark.parametrize("claim", ["Brand verified", "Condition: Grade"])
    def test_scripted_claims_removed(self, app_js: str, claim: str):
        # Rendering data.condition_grade from the real response is fine; the
        # defect was asserting it while the request was still in flight.
        assert claim not in self._animation_block(app_js)

    def test_progress_wording_is_used_instead(self, app_js: str):
        block = self._animation_block(app_js)
        assert "Reading the details for" in block
        assert "Reviewing your photos" in block

    def test_real_results_still_render_from_backend_data(self, app_js: str):
        assert "data.condition_grade" in app_js


class TestLongWaitIsAcknowledged:
    def test_last_step_keeps_spinning_until_the_answer_lands(self, app_js: str):
        tail = app_js[app_js.index("const evaluationInFlight"):]
        assert "lastStep.classList.add('active')" in tail

    def test_a_still_working_message_exists(self, app_js: str):
        assert "Still working" in app_js

    def test_the_still_working_timer_is_always_cleared(self, app_js: str):
        tail = app_js[app_js.index("const stillWorking"):]
        assert "finally" in tail and "clearTimeout(stillWorking)" in tail
