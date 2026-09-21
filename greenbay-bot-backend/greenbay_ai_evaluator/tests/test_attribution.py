"""
Tests for the campaign attribution added for Pulse (Sep 2026):

  - EvaluateRequest accepts an optional `attribution` block and still accepts
    the old body without it
  - attribution values are tag-stripped and truncated, never rejected
  - the Airtable payload builders add Session ID, Confidence, Decision and the
    UTM / Referrer / Landing URL columns from the stored session

All deterministic: no network, DB, or LLM calls.
"""

from types import SimpleNamespace

import pytest

from greenbay_ai_evaluator.api.schemas import (
    ATTRIBUTION_MAX_LEN,
    EvaluateRequest,
    EvaluationAttribution,
)


def _minimal_body(**extra) -> dict:
    body = {
        "category": "tv_monitor",
        "brand": "Samsung",
        "condition_grade": "B",
        "condition_score": 75,
        "retail_price": 45000,
        "seller_phone": "0712345678",  # required since Sep 2026 (test_seller_phone.py)
    }
    body.update(extra)
    return body


class TestEvaluateRequestAttribution:
    def test_body_without_attribution_still_validates(self):
        req = EvaluateRequest(**_minimal_body())
        assert req.attribution is None

    def test_attribution_round_trips(self):
        req = EvaluateRequest(**_minimal_body(attribution={
            "utm_source": "facebook",
            "utm_medium": "paid",
            "utm_campaign": "sept-tv",
            "utm_content": "carousel-2",
            "referrer": "https://www.facebook.com/",
            "landing_url": "https://evaluate.greenbay.market/app/?utm_source=facebook#evaluate",
        }))
        assert req.attribution is not None
        assert req.attribution.utm_source == "facebook"
        assert req.attribution.utm_medium == "paid"
        assert req.attribution.utm_campaign == "sept-tv"
        assert req.attribution.utm_content == "carousel-2"
        assert req.attribution.referrer == "https://www.facebook.com/"
        assert req.attribution.landing_url.endswith("#evaluate")

    def test_nulls_and_blanks_become_none(self):
        req = EvaluateRequest(**_minimal_body(attribution={
            "utm_source": None, "utm_medium": "", "referrer": "   ",
        }))
        assert req.attribution.utm_source is None
        assert req.attribution.utm_medium is None
        assert req.attribution.referrer is None

    def test_tags_stripped_and_long_values_truncated_not_rejected(self):
        long_url = "https://example.com/?q=" + ("x" * 5000)
        attr = EvaluationAttribution(
            utm_source="<script>alert(1)</script>google",
            landing_url=long_url,
        )
        assert attr.utm_source == "alert(1)google"
        assert len(attr.landing_url) == ATTRIBUTION_MAX_LEN["landing_url"]

    def test_unknown_attribution_keys_are_ignored_not_fatal(self):
        req = EvaluateRequest(**_minimal_body(attribution={
            "utm_source": "x", "utm_term": "not-captured",
        }))
        assert req.attribution.utm_source == "x"

    def test_top_level_unknown_keys_still_forbidden(self):
        with pytest.raises(Exception):
            EvaluateRequest(**_minimal_body(utm_source="top-level-not-allowed"))


class TestAirtablePulseFields:
    @pytest.fixture
    def session(self):
        return SimpleNamespace(
            id="0123abcd-4567-89ef-0123-456789abcdef",
            decision="negotiate",
            confidence_score=71.5,
            utm_source="facebook",
            utm_medium="paid",
            utm_campaign="sept-tv",
            utm_content=None,
            referrer="https://www.facebook.com/",
            landing_url="https://evaluate.greenbay.market/app/#evaluate",
        )

    def test_pulse_fields_from_session(self, session):
        from greenbay_ai_evaluator.api.router import _pulse_airtable_fields

        fields = _pulse_airtable_fields(session)
        assert fields["Session ID"] == "0123abcd"
        assert fields["Decision"] == "negotiate"
        assert fields["Confidence"] == 71.5
        assert fields["UTM Source"] == "facebook"
        assert fields["UTM Medium"] == "paid"
        assert fields["UTM Campaign"] == "sept-tv"
        assert "UTM Content" not in fields
        assert fields["Referrer"] == "https://www.facebook.com/"
        assert fields["Landing URL"] == "https://evaluate.greenbay.market/app/#evaluate"

    def test_missing_confidence_is_omitted(self, session):
        from greenbay_ai_evaluator.api.router import _pulse_airtable_fields

        session.confidence_score = None
        fields = _pulse_airtable_fields(session)
        assert "Confidence" not in fields
        assert fields["Session ID"] == "0123abcd"

    def test_attribution_columns_from_request(self):
        from greenbay_ai_evaluator.api.router import _attribution_columns

        req = EvaluateRequest(**_minimal_body(attribution={"utm_source": "google", "utm_medium": ""}))
        cols = _attribution_columns(req)
        assert cols["utm_source"] == "google"
        assert cols["utm_medium"] is None
        assert set(cols) == {
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "referrer", "landing_url",
        }

    def test_attribution_columns_empty_without_block(self):
        from greenbay_ai_evaluator.api.router import _attribution_columns

        assert _attribution_columns(EvaluateRequest(**_minimal_body())) == {}
