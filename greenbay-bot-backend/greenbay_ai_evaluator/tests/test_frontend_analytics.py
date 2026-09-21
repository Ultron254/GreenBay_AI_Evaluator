"""
Static guarantees about the GA4 wiring in frontend/ (Sep 2026, Pulse Sentinel
D10 "No analytics, UTM or referrer capture on the evaluator frontend"):

  - the Google tag is loaded exactly once, on the wizard page only
  - GA4 is configured with a CLEANED page_location / page_referrer
  - every funnel event the brief lists has a call site
  - no call site passes anything but step / decision / option, and trackEvent
    drops every other parameter anyway (names, phones, prices never leave)
  - the URL allow-list in index.html is the backend's allow-list, and the
    browser cleaner agrees with the backend cleaner (run under node)

These read the shipped files; nothing is rendered and nothing is sent.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from greenbay_ai_evaluator.api.schemas import (
    ATTRIBUTION_QUERY_ALLOWLIST,
    clean_attribution_url,
)

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
_INDEX = (_FRONTEND / "index.html").read_text(encoding="utf-8")
_APP_JS = (_FRONTEND / "app.js").read_text(encoding="utf-8")
_DASHBOARD = (_FRONTEND / "dashboard.html").read_text(encoding="utf-8")

MEASUREMENT_ID = "G-2REFLT805D"

# (url, keep_query)
URL_CASES = [
    ("https://evaluate.greenbay.market/app/?utm_source=facebook&utm_medium=paid#evaluate", True),
    ("https://evaluate.greenbay.market/app/?utm_source=fb&phone=0712345678&name=John&key=SECRET#evaluate", True),
    ("https://evaluate.greenbay.market/app/?gclid=abc&fbclid=def&ttclid=ghi&x=1", True),
    ("https://evaluate.greenbay.market/app/#name=John", True),
    ("https://evaluate.greenbay.market/app/", True),
    ("https://evaluate.greenbay.market/app/dashboard.html?key=SECRET#x", False),
    ("https://www.google.com/search?q=sell+fridge+0712345678", False),
    ("https://www.facebook.com/", False),
]


def _inline_tag_script() -> str:
    m = re.search(r"<script>\s*window\.dataLayer.*?</script>", _INDEX, re.DOTALL)
    assert m, "inline gtag bootstrap not found in index.html"
    return m.group(0)


class TestTagLoadsOnce:
    def test_one_loader_and_one_config_on_the_wizard_page(self):
        assert _INDEX.count("googletagmanager.com/gtag/js") == 1
        assert _INDEX.count(f"gtag/js?id={MEASUREMENT_ID}") == 1
        assert len(re.findall(r"gtag\(\s*'config'", _INDEX)) == 1
        assert f"gtag('config', '{MEASUREMENT_ID}'" in _INDEX

    def test_app_js_never_loads_or_configures_the_tag(self):
        assert "googletagmanager.com" not in _APP_JS
        assert not re.search(r"gtag\(\s*'config'", _APP_JS)

    def test_ops_dashboard_is_not_tagged(self):
        assert "googletagmanager" not in _DASHBOARD
        assert "gtag(" not in _DASHBOARD

    def test_csp_allows_the_tag(self):
        csp = re.search(r'Content-Security-Policy" content="([^"]+)"', _INDEX).group(1)
        assert "https://www.googletagmanager.com" in csp.split("script-src")[1].split(";")[0]
        assert "google-analytics.com" in csp.split("connect-src")[1].split(";")[0]


class TestNothingPersonalReachesGa4:
    def test_config_sends_cleaned_urls(self):
        script = _inline_tag_script()
        assert "page_location: window.gbCleanUrl(window.location.href, true)" in script
        assert "page_referrer: window.gbCleanUrl(document.referrer, false)" in script
        # The cleaner must be defined before the config call that uses it.
        assert script.index("window.gbCleanUrl = function") < script.index("gtag('config'")

    def test_allow_list_matches_the_backend(self):
        m = re.search(r"var allow = \[(.*?)\];", _inline_tag_script(), re.DOTALL)
        assert m
        assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(ATTRIBUTION_QUERY_ALLOWLIST)

    def test_attribution_is_captured_through_the_cleaner(self):
        assert "referrer: cleanUrl(document.referrer, false)" in _APP_JS
        assert "landing_url: cleanUrl(window.location.href, true)" in _APP_JS

    def test_event_parameters_are_allow_listed(self):
        assert "const GB_EVENT_PARAMS = ['step', 'decision', 'option'];" in _APP_JS
        # The only place gtag('event') is called is trackEvent.
        assert len(re.findall(r"gtag\(\s*'event'", _APP_JS.split("function trackEvent")[1])) == 1
        assert len(re.findall(r"window\.gtag\(", _APP_JS)) == 1

    def test_call_sites_pass_only_step_decision_option(self):
        calls = re.findall(r"track(?:Event|Once)\('([a-z_]+)'(?:,\s*\{([^}]*)\})?\)", _APP_JS)
        assert calls
        for _name, params in calls:
            keys = set(re.findall(r"(\w+)\s*:", params or ""))
            assert keys <= {"step", "decision", "option"}, (_name, keys)

    def test_no_personal_field_is_named_in_any_tracking_call(self):
        for line in _APP_JS.splitlines():
            # Call sites only: every one names its event with a string literal
            # (trackOnce forwarding to trackEvent(name, params) is not one).
            if re.search(r"\btrack(?:Event|Once)\('", line):
                assert not re.search(r"seller|phone|name|price|photo|offer\s*:|amount", line, re.I), line


class TestFunnelEvents:
    @pytest.mark.parametrize("event", [
        "wizard_start", "wizard_step", "evaluate_submit", "offer_shown",
        "human_review_routed", "offer_accepted", "offer_declined", "rejection_option",
    ])
    def test_event_has_a_call_site(self, event):
        assert re.search(rf"track(?:Event|Once)\('{event}'", _APP_JS)

    def test_every_forward_step_is_tracked(self):
        next_step = _APP_JS.split("function nextStep()")[1].split("\nfunction ")[0]
        assert "trackEvent('wizard_step', { step: next })" in next_step

    def test_demo_results_are_not_tracked_as_offers(self):
        show = _APP_JS.split("function showResults(data)")[1].split("\nfunction ")[0]
        assert "if (!isDemo)" in show


class TestUrlCleanerParity:
    @pytest.mark.parametrize("url,keep_query", URL_CASES)
    def test_backend_never_keeps_a_secret_or_personal_value(self, url, keep_query):
        cleaned = clean_attribution_url(url, keep_query=keep_query) or ""
        for leaked in ("SECRET", "0712345678", "John"):
            assert leaked not in cleaned

    @pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
    def test_browser_cleaner_agrees_with_the_backend(self):
        body = re.search(
            r"window\.gbCleanUrl = function.*?\n      \};", _inline_tag_script(), re.DOTALL,
        ).group(0)
        script = (
            "const window = {location: {href: 'https://evaluate.greenbay.market/app/'}};\n"
            + body
            + "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
            + "console.log(JSON.stringify(cases.map(c => window.gbCleanUrl(c[0], c[1]))));\n"
        )
        out = subprocess.run(
            ["node", "-e", script], input=json.dumps(URL_CASES),
            capture_output=True, text=True, timeout=30, check=True,
        )
        got = json.loads(out.stdout)
        expected = [clean_attribution_url(u, keep_query=k) or "" for u, k in URL_CASES]
        assert got == expected
