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
    """Nov 2026: the wizard was cut from 10 steps to 4 compact screens for
    mobile (appliance, photos, price, contact). Keep the two files in step."""

    def test_total_steps_is_four(self, app_js: str):
        assert re.search(r"TOTAL_STEPS\s*=\s*4\b", app_js)

    def test_no_ownership_remnants(self, app_js: str):
        assert "ownership" not in app_js.lower()
        assert "ownership" not in INDEX_HTML.read_text(
            encoding="utf-8", errors="replace"
        ).lower()

    def test_html_declares_exactly_four_steps(self):
        html = INDEX_HTML.read_text(encoding="utf-8", errors="replace")
        steps = sorted(int(n) for n in re.findall(r'data-step="(\d+)"', html))
        assert steps == list(range(1, 5)), f"expected steps 1..4, got {steps}"

    def test_every_step_has_validation_and_a_label(self, app_js: str):
        for n in range(1, 5):
            assert re.search(rf"^\s*case {n}:", app_js, re.M), f"isStepValid missing case {n}"
            assert re.search(rf"^\s*{n}: '", app_js, re.M), f"STEP_LABELS missing {n}"

    def test_contact_step_is_the_last_step(self, app_js: str):
        # The phone re-check and the 422 path must land on the contact screen,
        # not on whatever step happens to carry that number.
        body = app_js[app_js.index("function returnToContactStep"):][:400]
        assert "goToStep(TOTAL_STEPS)" in body
        assert "isStepValid(TOTAL_STEPS)" in app_js
        html = INDEX_HTML.read_text(encoding="utf-8", errors="replace")
        step4 = html[html.index('data-step="4"'):html.index('data-step="results"')]
        assert 'id="sellerPhoneInput"' in step4 and 'id="sellerNameInput"' in step4

    def test_photo_changes_revalidate_the_current_step(self, app_js: str):
        assert "isStepValid(10)" not in app_js
        assert "isStepValid(9)" not in app_js


class TestLabelPhotoReadsBrandAndModel:
    """Brand/model selection was replaced by a label photo the AI reads."""

    def test_calls_the_route_that_exists(self, app_js: str):
        # /api/evaluator/model-lookup never existed; every read 404'd silently.
        assert "/api/evaluator/model-lookup" not in app_js
        assert "${API_BASE}/tradein/model-lookup" in app_js

    def test_a_failed_read_offers_the_brand_dropdown(self, app_js: str):
        body = app_js[app_js.index("async function lookupModelFromPhoto"):]
        body = body[:body.index("\n}\n")]
        assert "plateRead = 'failed'" in body
        assert "showBrandFallback(" in body

    def test_cannot_continue_without_a_brand_or_while_reading(self, app_js: str):
        case2 = re.search(r"case 2:([^\n]+)", app_js).group(1)
        assert "a.brand" in case2 and "'reading'" in case2 and "photos.length >= 3" in case2

    def test_html_has_label_zone_and_fallback(self):
        html = INDEX_HTML.read_text(encoding="utf-8", errors="replace")
        for marker in ('id="modelPhotoInput"', 'id="brandFallback"', 'id="brandSelect"', 'id="plateStatus"'):
            assert marker in html, marker


class TestNegotiationNeverAgreesInTheBrowser:
    """The /counter route is paused; an accepted counter must be recorded by
    the server (acceptOffer) before any deal is confirmed."""

    def test_accept_branch_goes_through_accept_offer(self, app_js: str):
        body = app_js[app_js.index("function handleNegotiationResponse"):]
        accept = body[body.index("if (data.decision === 'accept')"):body.index("} else if (data.decision === 'decline')")]
        assert "acceptOffer(" in accept
        assert "showPickupDropoffChoice" not in accept
        assert "status = 'accepted'" not in accept

    def test_no_call_to_the_paused_counter_route(self, app_js: str):
        assert "/counter`" not in app_js


class TestMarkets:
    """Kenya, Uganda and Nigeria portals share one page."""

    def test_portal_path_decides_the_market(self, app_js: str):
        assert r"/^\/app\/(ke|ug|ng)\/?$/i" in app_js

    def test_uganda_and_nigeria_never_show_confidence(self, app_js: str):
        for code in ("UG", "NG"):
            block = app_js[app_js.index(f"    {code}: {{"):]
            block = block[:block.index("copy: {")]
            assert "showConfidence: false" in block, code
        assert "showConfidence: true" in app_js[app_js.index("    KE: {"):app_js.index("    UG: {")]

    def test_confidence_rendering_is_gated(self, app_js: str):
        # Every place that prints "Confidence" to the customer checks the market.
        results = app_js[app_js.index("function showResults"):app_js.index("function submitExpertFeedback")]
        for m in re.finditer(r"(?<!show)Confidence", results):
            window = results[max(0, m.start() - 400): m.start()]
            assert "MARKET.showConfidence" in window, results[m.start() - 80: m.start() + 40]

    def test_payload_sends_the_market(self, app_js: str):
        assert "country: MARKET.code" in app_js

    def test_no_hardcoded_whatsapp_line_in_the_wizard(self, app_js: str):
        assert not re.search(r"wa\.me/\d", app_js), "use MARKET.whatsapp"
