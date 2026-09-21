"""
Tests for the Perplexity Sonar price source and its health probe (Sep 2026,
Pulse Sentinel D01 "Perplexity price source failing in production"):

  - every way of returning no price names its cause (status + detail)
  - the detail never contains the API key
  - answers are read the way Sonar actually writes them: JSON, fenced JSON,
    JSON broken by a thousands separator or a [1] citation mark, quoted
    "KES 42,999" strings, and single-price prose; ambiguous prose is refused
  - the model name comes from PERPLEXITY_MODEL
  - the health probe reports the cause first, and probes Gemini on its own
  - a source outage is recorded, named in the justification, and changes the
    confidence score only when the opt-in setting is on

All deterministic: `requests.post` and the settings are faked; no network.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from greenbay_ai_evaluator.services import market_price_service as mps
from greenbay_ai_evaluator.services.market_price_service import (
    InternetPriceResult,
    _coerce_price,
    _read_price_answer,
    internet_lookup_outage,
    sonar_price_research,
)

FAKE_KEY = "pplx-test-key-not-real-0123456789"


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _answer(content, **extra):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    body.update(extra)
    return _Resp(200, body)


@pytest.fixture
def sonar(monkeypatch):
    """Fake app.config.get_settings and requests.post; returns a controller."""
    settings = SimpleNamespace(perplexity_api_key=FAKE_KEY, perplexity_model="sonar")
    fake_config = types.ModuleType("app.config")
    fake_config.get_settings = lambda: settings
    fake_app = types.ModuleType("app")
    fake_app.config = fake_config
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setitem(sys.modules, "app.config", fake_config)

    ctl = SimpleNamespace(settings=settings, response=None, raises=None, calls=[])

    def _post(url, headers=None, json=None, timeout=None):
        ctl.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if ctl.raises is not None:
            raise ctl.raises
        return ctl.response

    fake_requests = types.ModuleType("requests")
    fake_requests.post = _post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    return ctl


def _lookup(**over):
    args = dict(brand="Samsung", model="UA43T5300", category="tv_monitor",
                country="KE", size_value=43, size_unit="inch")
    args.update(over)
    return sonar_price_research(**args)


# ---------------------------------------------------------------------------
# Reading the answer (pure)
# ---------------------------------------------------------------------------
class TestReadPriceAnswer:
    @pytest.mark.parametrize("text", [
        '{"new_price": 42999, "currency": "KES", "matched_product": "Samsung 43 T5300"}',
        '```json\n{"new_price": 42999, "currency": "KES"}\n```',
        'Here is the price:\n{"new_price": 42999.0}\nSources: jumia.co.ke',
        '{"new_price": "42,999", "currency": "KES"}',
        '{"new_price": "KES 42,999", "currency": "KES"}',
        '{"new_price": "KSh. 42,999.00"}',
        '{"new_price": 42,999, "currency": "KES"}',          # invalid JSON: separator
        '{"new_price": 42999[1], "currency": "KES"}',        # invalid JSON: citation mark
        '<think>compare Jumia and Kilimall</think>{"new_price": 42999}',
        'The Samsung UA43T5300 retails at KES 42,999 on Jumia Kenya [1][2].',
        'It costs 42,999 KES at most retailers.',
        'Jumia lists it at Ksh 42,999, and Kilimall also at KSh42,999.',
    ])
    def test_price_is_read(self, text):
        price, status, _detail, _parsed = _read_price_answer(text, "KES")
        assert status == mps.LOOKUP_OK
        assert price == pytest.approx(42_999)

    def test_null_price_is_an_answer_not_a_parse_failure(self):
        price, status, _d, _p = _read_price_answer('{"new_price": null}', "KES")
        assert price is None
        assert status == mps.LOOKUP_NULL_PRICE

    def test_empty_answer(self):
        assert _read_price_answer("", "KES")[1] == mps.LOOKUP_EMPTY_ANSWER
        assert _read_price_answer("   \n", "KES")[1] == mps.LOOKUP_EMPTY_ANSWER

    def test_prose_without_a_price_is_a_parse_failure(self):
        price, status, _d, _p = _read_price_answer(
            "I could not find this model at Kenyan retailers.", "KES")
        assert price is None
        assert status == mps.LOOKUP_PARSE_FAILURE

    def test_ambiguous_prose_is_refused(self):
        # Two different amounts: never guess which one is the new price.
        price, status, _d, _p = _read_price_answer(
            "It was KES 60,000 last year and is now KES 45,000.", "KES")
        assert price is None
        assert status == mps.LOOKUP_PARSE_FAILURE

    def test_untagged_numbers_in_prose_are_ignored(self):
        # A year or a screen size must never be read as a price.
        price, _s, _d, _p = _read_price_answer(
            "The 2024 model with a 43 inch panel is widely sold.", "KES")
        assert price is None

    def test_other_currencies(self):
        assert _read_price_answer("Sells for UGX 1,450,000 in Kampala.", "UGX")[0] == 1_450_000
        assert _read_price_answer("Priced at ₦450,000.", "NGN")[0] == 450_000

    @pytest.mark.parametrize("value,expected", [
        (42999, 42999.0), (42999.5, 42999.5), ("42999", 42999.0),
        ("42,999", 42999.0), ("42 999", 42999.0), ("KES 42,999/=", 42999.0),
        ("KSh. 42,999.00", 42999.0),
        (None, None), (True, None), (0, None), (-5, None), ("", None),
        ("unknown", None), ("40,000 to 45,000", None),
    ])
    def test_coerce_price(self, value, expected):
        assert _coerce_price(value) == expected


# ---------------------------------------------------------------------------
# The Sonar call: request shape and every failure mode
# ---------------------------------------------------------------------------
class TestSonarPriceResearch:
    def test_success_sets_price_status_and_citations(self, sonar):
        sonar.response = _answer(
            '{"new_price": 42999, "currency": "KES", "matched_product": "Samsung 43 T5300"}',
            citations=["https://jumia.co.ke/a"],
            search_results=[{"title": "b", "url": "https://kilimall.co.ke/b"}],
        )
        res = _lookup()
        assert res.launch_price == 42_999
        assert res.status == mps.LOOKUP_OK
        assert [s["url"] for s in res.sources] == ["https://jumia.co.ke/a", "https://kilimall.co.ke/b"]

    def test_request_shape(self, sonar):
        sonar.response = _answer('{"new_price": 42999}')
        _lookup()
        call = sonar.calls[0]
        assert call["url"] == "https://api.perplexity.ai/chat/completions"
        assert call["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert call["json"]["model"] == "sonar"
        assert call["json"]["messages"][0]["role"] == "user"
        assert call["timeout"] == 45

    def test_model_name_comes_from_settings(self, sonar):
        sonar.settings.perplexity_model = "sonar-pro"
        sonar.response = _answer('{"new_price": 42999}')
        _lookup()
        assert sonar.calls[0]["json"]["model"] == "sonar-pro"

    def test_blank_model_setting_falls_back_to_sonar(self, sonar):
        sonar.settings.perplexity_model = "  "
        sonar.response = _answer('{"new_price": 42999}')
        _lookup()
        assert sonar.calls[0]["json"]["model"] == "sonar"

    def test_size_match_demanded_only_when_size_known(self, sonar):
        sonar.response = _answer('{"new_price": 42999}')
        _lookup()
        assert "MUST match exactly" in sonar.calls[0]["json"]["messages"][0]["content"]
        _lookup(size_value=None, size_unit=None)
        assert "MUST match exactly" not in sonar.calls[1]["json"]["messages"][0]["content"]

    def test_not_configured_makes_no_call(self, sonar):
        sonar.settings.perplexity_api_key = None
        res = _lookup()
        assert res.launch_price is None
        assert res.status == mps.LOOKUP_NOT_CONFIGURED
        assert sonar.calls == []

    def test_http_401_reports_status_message_and_hint(self, sonar):
        sonar.response = _Resp(
            401, text='{"error": {"message": "Invalid API key provided", "type": "auth", "code": 401}}')
        res = _lookup()
        assert res.launch_price is None
        assert res.status == mps.LOOKUP_HTTP_ERROR
        assert "HTTP 401" in res.status_detail
        assert "Invalid API key provided" in res.status_detail
        assert "credit" in res.status_detail

    def test_http_error_never_leaks_the_key(self, sonar):
        sonar.response = _Resp(
            401, text='{"error": {"message": "key ' + FAKE_KEY + ' is not valid"}}')
        res = _lookup()
        assert FAKE_KEY not in res.status_detail
        assert "***" in res.status_detail

    def test_html_error_page_is_not_echoed(self, sonar):
        sonar.response = _Resp(502, text="<html><body>Bad gateway</body></html>")
        res = _lookup()
        assert res.status == mps.LOOKUP_HTTP_ERROR
        assert "HTTP 502" in res.status_detail
        assert "<html>" not in res.status_detail

    def test_http_400_points_at_the_model_name(self, sonar):
        sonar.response = _Resp(400, text='{"error": {"message": "Invalid model"}}')
        assert "PERPLEXITY_MODEL" in _lookup().status_detail

    def test_timeout(self, sonar):
        class ReadTimeout(Exception):
            pass
        sonar.raises = ReadTimeout(f"timed out with header Bearer {FAKE_KEY}")
        res = _lookup()
        assert res.status == mps.LOOKUP_TIMEOUT
        assert FAKE_KEY not in res.status_detail

    def test_network_error(self, sonar):
        sonar.raises = ConnectionError("dns failure")
        assert _lookup().status == mps.LOOKUP_REQUEST_FAILED

    def test_200_with_non_json_body(self, sonar):
        sonar.response = _Resp(200, payload=None, text="<html>")
        assert _lookup().status == mps.LOOKUP_BAD_RESPONSE

    def test_200_with_empty_answer(self, sonar):
        sonar.response = _answer("")
        assert _lookup().status == mps.LOOKUP_EMPTY_ANSWER

    def test_200_with_no_choices(self, sonar):
        sonar.response = _Resp(200, {"choices": []})
        assert _lookup().status == mps.LOOKUP_EMPTY_ANSWER

    def test_content_parts_list_is_read(self, sonar):
        sonar.response = _Resp(200, {"choices": [{"message": {"content": [
            {"type": "text", "text": '{"new_price": 42999}'}]}}]})
        assert _lookup().launch_price == 42_999

    def test_null_price(self, sonar):
        sonar.response = _answer('{"new_price": null}')
        res = _lookup()
        assert res.launch_price is None
        assert res.status == mps.LOOKUP_NULL_PRICE

    def test_out_of_band_price_is_discarded_with_a_reason(self, sonar):
        sonar.response = _answer('{"new_price": 4299900}')
        res = _lookup()
        assert res.launch_price is None
        assert res.status == mps.LOOKUP_OUT_OF_BAND
        assert "sanity band" in res.status_detail

    def test_kes_string_price_no_longer_fails(self, sonar):
        # Before Sep 2026 float("KES 42,999") raised and the lookup returned
        # nothing, with only "failed" in the log.
        sonar.response = _answer('{"new_price": "KES 42,999"}')
        assert _lookup().launch_price == 42_999


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------
class TestHealthProbe:
    def _probe(self):
        from greenbay_ai_evaluator.services.live_healthcheck_service import (
            _probe_perplexity_sonar,
        )
        return _probe_perplexity_sonar()

    def test_not_configured_is_ok_and_says_so(self, sonar):
        sonar.settings.perplexity_api_key = ""
        ok, detail = self._probe()
        assert ok is True
        assert "not set" in detail

    def test_failure_names_the_cause_first_and_hides_the_key(self, sonar):
        sonar.response = _Resp(401, text='{"error": {"message": "Invalid API key"}}')
        ok, detail = self._probe()
        assert ok is False
        assert detail.startswith("Sonar returned NO price [http_error] HTTP 401")
        assert FAKE_KEY not in detail
        assert len(detail) <= 300  # Pulse keeps the first 300 characters

    def test_parse_failure_is_distinguishable_from_a_dead_key(self, sonar):
        sonar.response = _answer("Sorry, I cannot help with that.")
        ok, detail = self._probe()
        assert ok is False
        assert "[parse_failure]" in detail

    def test_success(self, sonar):
        sonar.response = _answer('{"new_price": 42999}', citations=["https://jumia.co.ke/a"])
        ok, detail = self._probe()
        assert ok is True
        assert "42,999" in detail

    def test_probe_passes_the_size(self, sonar):
        sonar.response = _answer('{"new_price": 42999}')
        self._probe()
        assert "43 inch" in sonar.calls[0]["json"]["messages"][0]["content"]

    def test_gemini_probe_does_not_call_sonar(self, sonar, monkeypatch):
        from greenbay_ai_evaluator.services import live_healthcheck_service as lhs
        seen = {}

        def _gemini(**kw):
            seen.update(kw)
            return InternetPriceResult(status=mps.LOOKUP_HTTP_ERROR, status_detail="HTTP 429")

        monkeypatch.setattr(mps, "gemini_price_research", _gemini)
        ok, detail = lhs._probe_gemini_search_grounding()
        assert ok is False
        assert "[http_error] HTTP 429" in detail
        assert seen["size_value"] == 43
        assert sonar.calls == []


# ---------------------------------------------------------------------------
# Outage detection for the merged lookup
# ---------------------------------------------------------------------------
def _merged(gemini_status, sonar_status, price=None):
    return InternetPriceResult(
        launch_price=price,
        providers={
            "gemini": {"status": gemini_status, "detail": "g"},
            "sonar": {"status": sonar_status, "detail": "s"},
        },
    )


class TestInternetLookupOutage:
    def test_a_price_means_no_outage(self):
        assert internet_lookup_outage(_merged(mps.LOOKUP_OK, mps.LOOKUP_HTTP_ERROR, 40_000)) == ""

    def test_both_down_is_an_outage(self):
        out = internet_lookup_outage(_merged(mps.LOOKUP_TIMEOUT, mps.LOOKUP_HTTP_ERROR))
        assert "gemini: timeout" in out and "sonar: http_error" in out

    def test_one_provider_answering_no_price_is_not_an_outage(self):
        assert internet_lookup_outage(_merged(mps.LOOKUP_NULL_PRICE, mps.LOOKUP_HTTP_ERROR)) == ""

    def test_unconfigured_provider_is_ignored(self):
        assert internet_lookup_outage(_merged(mps.LOOKUP_HTTP_ERROR, mps.LOOKUP_NOT_CONFIGURED)) != ""
        assert internet_lookup_outage(_merged(mps.LOOKUP_NOT_CONFIGURED, mps.LOOKUP_NOT_CONFIGURED)) == ""

    def test_none_and_empty(self):
        assert internet_lookup_outage(None) == ""
        assert internet_lookup_outage(InternetPriceResult()) == ""

    def test_search_records_both_providers(self, monkeypatch):
        monkeypatch.setattr(mps, "gemini_price_research", lambda **kw: InternetPriceResult(
            launch_price=40_000, confidence=60, status=mps.LOOKUP_OK, status_detail="2 sources"))
        monkeypatch.setattr(mps, "sonar_price_research", lambda **kw: InternetPriceResult(
            status=mps.LOOKUP_HTTP_ERROR, status_detail="HTTP 401"))
        res = mps.search_internet_price(brand="Samsung", model="X", category="tv_monitor")
        assert res.launch_price == 40_000
        assert res.providers["gemini"]["status"] == mps.LOOKUP_OK
        assert res.providers["sonar"] == {"status": mps.LOOKUP_HTTP_ERROR, "detail": "HTTP 401"}


# ---------------------------------------------------------------------------
# Billed probes are not re-run on every health-check
# ---------------------------------------------------------------------------
class TestPaidProbeReuse:
    """The service monitor runs the health-check every 10 minutes. The two
    price-lookup probes are billed web searches, so their result is reused."""

    @pytest.fixture
    def lhs(self, monkeypatch):
        from greenbay_ai_evaluator.services import live_healthcheck_service as lhs
        calls = {"perplexity_sonar": 0, "gemini_search_newprice": 0, "database": 0}
        outcome = {"ok": True}

        def _make(name):
            def _probe():
                calls[name] += 1
                return outcome["ok"], f"{name} call {calls[name]}"
            return _probe

        monkeypatch.setattr(lhs, "_PROBES", {name: _make(name) for name in calls})
        monkeypatch.setattr(lhs, "_paid_probe_cache", {})
        monkeypatch.delenv("HEALTH_PAID_PROBE_INTERVAL_MIN", raising=False)
        lhs.calls, lhs.outcome = calls, outcome
        return lhs

    def test_paid_probes_run_once_free_probes_every_time(self, lhs):
        first = lhs.run_live_healthcheck()
        second = lhs.run_live_healthcheck()
        assert lhs.calls == {"perplexity_sonar": 1, "gemini_search_newprice": 1, "database": 2}
        assert first["services"]["perplexity_sonar"]["cached"] is False
        assert second["services"]["perplexity_sonar"]["cached"] is True
        assert second["services"]["perplexity_sonar"]["detail"] == "perplexity_sonar call 1"
        assert "checked_at" in second["services"]["perplexity_sonar"]
        assert "cached" not in second["services"]["database"]

    def test_fresh_forces_a_real_lookup(self, lhs):
        lhs.run_live_healthcheck()
        report = lhs.run_live_healthcheck(fresh=True)
        assert lhs.calls["perplexity_sonar"] == 2
        assert report["services"]["perplexity_sonar"]["cached"] is False

    def test_result_expires_after_the_interval(self, lhs, monkeypatch):
        monkeypatch.setenv("HEALTH_PAID_PROBE_INTERVAL_MIN", "60")
        lhs.run_live_healthcheck()
        checked_at, result = lhs._paid_probe_cache["perplexity_sonar"]
        lhs._paid_probe_cache["perplexity_sonar"] = (checked_at - 61 * 60, result)
        lhs.run_live_healthcheck()
        assert lhs.calls["perplexity_sonar"] == 2

    def test_a_failure_is_rechecked_within_thirty_minutes(self, lhs):
        lhs.outcome["ok"] = False
        lhs.run_live_healthcheck()
        checked_at, result = lhs._paid_probe_cache["perplexity_sonar"]
        lhs._paid_probe_cache["perplexity_sonar"] = (checked_at - 31 * 60, result)
        lhs.run_live_healthcheck()
        assert lhs.calls["perplexity_sonar"] == 2

    def test_interval_zero_restores_a_lookup_on_every_check(self, lhs, monkeypatch):
        monkeypatch.setenv("HEALTH_PAID_PROBE_INTERVAL_MIN", "0")
        lhs.run_live_healthcheck()
        lhs.run_live_healthcheck()
        assert lhs.calls["perplexity_sonar"] == 2

    def test_bad_interval_value_falls_back_to_the_default(self, lhs, monkeypatch):
        monkeypatch.setenv("HEALTH_PAID_PROBE_INTERVAL_MIN", "soon")
        assert lhs._paid_probe_interval_seconds() == 360 * 60

    def test_report_shape_pulse_reads_is_unchanged(self, lhs):
        entry = lhs.run_live_healthcheck()["services"]["perplexity_sonar"]
        assert {"ok", "detail", "latency_ms"} <= set(entry)
        # Pulse's parser reads a "status" key before "ok"; never add one.
        assert "status" not in entry
