"""Guards for frontend defects found in QA that must never come back.

These assert on the shipped ``frontend/app.js`` source rather than running a
browser, so they work in the deploy gate with no extra dependencies.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "frontend" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8", errors="replace")


class TestNeverInventAnOffer:
    """The wizard must not show a price the backend did not issue.

    A fabricated offer cannot be honoured, is written to no store, and nobody
    is notified — the customer leaves believing GreenBay committed to a number
    it has never seen. It also silently defeated the vision hard-fail, which
    returns 503 precisely so that no price is quoted.
    """

    def test_no_demo_offer_generator(self, app_js: str):
        assert "generateDemoResults" not in app_js
        assert "Demo mode" not in app_js

    def test_failure_shows_an_error_not_a_price(self, app_js: str):
        assert "function showEvaluationError" in app_js
        handler = re.search(
            r"catch \(err\) \{\s*console\.error\('Evaluation API error:'.*?\n\s*\}",
            app_js,
            re.S,
        )
        assert handler, "the /evaluate catch block moved; re-check it never shows a price"
        body = handler.group(0)
        assert "showEvaluationError" in body
        for banned in ("showResults", "opening_offer", "formatKES"):
            assert banned not in body, f"error path must not render an offer ({banned})"

    def test_error_path_escalates_to_a_human(self, app_js: str):
        assert "wa.me" in app_js.split("function showEvaluationError")[1][:1200]


class TestFreeTextCannotInjectMarkup:
    """brand/model/name/issues are free text and are interpolated into
    innerHTML in roughly twenty places, so they are stripped at the boundary."""

    def test_sanitiser_exists(self, app_js: str):
        assert "function sanitiseText" in app_js

    def test_update_answer_sanitises_strings(self, app_js: str):
        assert re.search(
            r"state\.answers\[field\]\s*=\s*typeof value === 'string'\s*\?\s*sanitiseText\(value\)",
            app_js,
        )

    def test_custom_brand_sanitised(self, app_js: str):
        assert "state.answers.brand = sanitiseText(" in app_js

    @pytest.mark.parametrize(
        "field",
        ["p.title", "p.product_url", "data.decision_reason"],
    )
    def test_backend_values_escaped(self, app_js: str, field: str):
        assert f"escapeHtml({field}" in app_js


class TestWizardShape:
    """The wizard was renumbered from 11 steps to 10; keep the two files in step."""

    def test_total_steps_is_ten(self, app_js: str):
        assert re.search(r"TOTAL_STEPS\s*=\s*10\b", app_js)

    def test_no_ownership_remnants(self, app_js: str):
        assert "ownership" not in app_js.lower()
        assert "ownership" not in INDEX_HTML.read_text(
            encoding="utf-8", errors="replace"
        ).lower()

    def test_html_declares_exactly_ten_steps(self):
        html = INDEX_HTML.read_text(encoding="utf-8", errors="replace")
        steps = sorted(int(n) for n in re.findall(r'data-step="(\d+)"', html))
        assert steps == list(range(1, 11)), f"expected steps 1..10, got {steps}"

    def test_every_step_has_validation_and_a_label(self, app_js: str):
        for n in range(1, 11):
            assert re.search(rf"^\s*case {n}:", app_js, re.M), f"isStepValid missing case {n}"
            assert re.search(rf"^\s*{n}: '", app_js, re.M), f"STEP_LABELS missing {n}"
