"""Uganda and Nigeria evaluations (Nov 2026).

Every GreenBay reference source except the internet new-price lookup holds
Kenyan shilling figures: sales stock, the pricing matrix, the tracker sheet,
expert feedback, DB comparables and Jiji/Jumia Kenya. A UG/NG evaluation must
not read them (a KES number labelled UGX is wrong by ~28x), must not write to
the KES tracker the ratios train on, prices in its own currency, and always
goes to a person until the market has history of its own.

These run POST /evaluate's real handler against an in-memory SQLite database
with every network service replaced by a recorder.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import greenbay_ai_evaluator.api.router as router
from greenbay_ai_evaluator.api.schemas import EvaluateRequest
from greenbay_ai_evaluator.config import (
    LOCAL_PRICING_CONFIDENCE_CAP,
    get_currency_config,
    local_units_per_kes,
    normalise_country,
    uses_kes_reference_data,
)
from greenbay_ai_evaluator.engine.offer_engine import PricingPolicyData, compute_valuation
from greenbay_ai_evaluator.services.comparables_service import ComparablesResult
from greenbay_ai_evaluator.services.market_price_service import InternetPriceResult


class TestConfig:
    def test_kenya_is_the_only_kes_reference_market(self):
        assert uses_kes_reference_data("KE")
        assert not uses_kes_reference_data("UG")
        assert not uses_kes_reference_data("NG")

    def test_unknown_or_lowercase_country_is_normalised(self):
        assert normalise_country("ug") == "UG"
        assert normalise_country(None) == "KE"
        assert normalise_country("ZZ") == "KE"

    def test_kenya_scale_is_one(self):
        assert local_units_per_kes("KE") == 1.0

    def test_local_scale_is_large_for_ugx_and_ngn(self):
        assert 20 < local_units_per_kes("UG") < 40
        assert 8 < local_units_per_kes("NG") < 16

    def test_round_steps_fit_each_currency(self):
        assert get_currency_config("KE")["round_step"] == 500
        assert get_currency_config("UG")["round_step"] >= 5000
        assert get_currency_config("NG")["round_step"] >= 1000


def _policy() -> PricingPolicyData:
    return PricingPolicyData(category="refrigerator", margin_pct=0.3, max_offer_pct=0.7, walkaway_pct=0.8)


def _valuation(**kw):
    base = dict(
        category="refrigerator", brand="Samsung", model="RT34", age_years=2,
        condition_grade="B", condition_score=70,
        defects=[{"type": "cosmetic_dent"}], seller_asking_price=None,
        image_quality_score=80, risk_score=10, comparables=[],
        pricing_policy=_policy(), retail_price=60000,
    )
    base.update(kw)
    return compute_valuation(**base)


class TestEngineInLocalCurrency:
    def test_kenya_unchanged_by_defaults(self):
        a = _valuation()
        b = _valuation(defect_scale=1.0, currency="KES", round_step=500)
        assert a.opening_offer == b.opening_offer
        assert a.defect_deduction_total == b.defect_deduction_total

    def test_defects_are_scaled_into_local_currency(self):
        ke = _valuation()
        ug = _valuation(retail_price=60000 * 28, defect_scale=28.0, currency="UGX", round_step=10000)
        assert ug.defect_deduction_total == pytest.approx(ke.defect_deduction_total * 28)

    def test_trace_never_labels_local_figures_as_kes(self):
        ug = _valuation(retail_price=1_700_000, defect_scale=28.0, currency="UGX", round_step=10000)
        assert "KES " not in ug.pricing_justification
        assert "UGX " in ug.pricing_justification
        assert "KES " not in ug.decision_reason

    def test_offer_rounds_to_the_market_step(self):
        ug = _valuation(retail_price=1_700_000, defect_scale=28.0, currency="UGX", round_step=10000)
        assert ug.opening_offer % 10000 == 0


# ---------------------------------------------------------------------------
# The real /evaluate handler, offline
# ---------------------------------------------------------------------------
@pytest.fixture
def db():
    from app.database.db import Base
    import app.database.models  # noqa: F401  (registers tables)
    import greenbay_ai_evaluator.models.evaluator_models as em

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(em.PricingPolicy(
        category="refrigerator", margin_pct=0.3, max_offer_pct=0.7, walkaway_pct=0.8,
        is_active=True, brand_premium_json={}, condition_multiplier_json={},
    ))
    session.commit()
    yield session
    session.close()


@pytest.fixture
def calls(monkeypatch):
    """Replace every network-facing service the handler touches; record calls."""
    seen: dict[str, list] = {}

    def rec(name, value=None):
        def _fn(*a, **k):
            seen.setdefault(name, []).append(k)
            return value() if callable(value) else value
        return _fn

    empty_comps = lambda: ComparablesResult(  # noqa: E731
        comparables=[], count=0, weighted_average=0.0, confidence=0.0,
        sources_breakdown={"historical": 0, "shopify": 0, "expert": 0},
    )

    def internet(**k):
        seen.setdefault("search_internet_price", []).append(k)
        currency = {"UG": "UGX", "NG": "NGN"}.get(k.get("country"), "KES")
        price = {"UGX": 1_800_000, "NGN": 700_000}.get(currency, 65_000)
        return InternetPriceResult(launch_price=price, confidence=70, currency=currency,
                                   status="ok", sources=[], providers={})

    class _Score:
        score = 80.0

    class _Rejections:
        any_rejected = False
        rejections: list = []
        rejected_count = 0

    class _Dup:
        is_duplicate = False

    async def marketplace(**k):
        seen.setdefault("get_marketplace_prices", []).append(k)
        raise RuntimeError("should not be reached for UG/NG")

    monkeypatch.setattr(router, "get_comparables", rec("get_comparables", empty_comps))
    monkeypatch.setattr(router, "search_internet_price", internet)
    monkeypatch.setattr(router, "internet_lookup_outage", lambda r: "")
    monkeypatch.setattr(router, "get_marketplace_prices", marketplace)
    monkeypatch.setattr(router, "get_historical_price", rec("get_historical_price", None))
    monkeypatch.setattr(router, "lookup_sales_stock", rec("lookup_sales_stock", None))
    monkeypatch.setattr(router, "lookup_matrix", rec("lookup_matrix", None))
    monkeypatch.setattr(router, "get_historical_accuracy_ratio", lambda *a, **k: None)
    monkeypatch.setattr(router, "score_images", lambda **k: _Score())
    monkeypatch.setattr(router, "score_images_with_rejections", lambda **k: _Rejections())
    monkeypatch.setattr(router, "check_duplicates", lambda **k: _Dup())
    monkeypatch.setattr(router, "store_session_hashes", lambda **k: None)
    monkeypatch.setattr(router, "_repressign_images_for_airtable", lambda **k: [])

    class _Risk:
        score = 10.0

    monkeypatch.setattr(router, "assess_risk", lambda **k: _Risk())
    monkeypatch.setattr(router, "airtable_write_async", rec("airtable", None))

    import greenbay_ai_evaluator.services.reference_data_service as ref
    import greenbay_ai_evaluator.services.internal_notification_service as notify
    import greenbay_ai_evaluator.services.email_service as email

    monkeypatch.setattr(ref, "append_tracker_row", rec("append_tracker_row", None))
    monkeypatch.setattr(ref, "append_market_tracker_row",
                        lambda country, **k: seen.setdefault("append_market_tracker_row", []).append({"country": country, **k}))
    monkeypatch.setattr(notify, "notify_internal_team", rec("notify", None))
    monkeypatch.setattr(email, "send_qualified_lead_email", lambda snap, outcome: seen.setdefault("lead_email", []).append(snap))

    # The tracker append runs on a thread; run it inline so the test sees it.
    import threading

    class _Inline:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            if self._t:
                self._t()

    monkeypatch.setattr(threading, "Thread", _Inline)
    return seen


def _req(country: str, **kw) -> EvaluateRequest:
    body = dict(
        category="refrigerator", brand="Samsung", model="RT34K5532S8",
        age_years=2, condition_grade="B", condition_score=70,
        defects=[{"type": "cosmetic_dent", "description": "Dents", "severity": "medium"}],
        seller_asking_price=None, seller_name="Test Seller",
        seller_phone={"KE": "0712345678", "UG": "0772123456", "NG": "08031234567"}[country],
        image_urls=[], image_data=[],
        retail_price=65000, retail_price_source="category_default",
        country=country,
    )
    body.update(kw)
    return EvaluateRequest(**body)


@pytest.mark.parametrize("country,currency", [("UG", "UGX"), ("NG", "NGN")])
class TestLocalMarketEvaluation:
    def test_kenyan_sources_are_not_read(self, db, calls, country, currency):
        router.evaluate_trade_in(_req(country), db)
        for kes_only in ("get_comparables", "get_historical_price", "lookup_sales_stock",
                         "lookup_matrix", "get_marketplace_prices"):
            assert kes_only not in calls, kes_only

    def test_new_price_is_looked_up_in_the_local_market(self, db, calls, country, currency):
        router.evaluate_trade_in(_req(country), db)
        assert calls["search_internet_price"][0]["country"] == country

    def test_priced_in_local_currency_and_sent_to_a_person(self, db, calls, country, currency):
        out = router.evaluate_trade_in(_req(country), db)
        assert out.currency_code == currency
        assert out.country == country
        assert out.decision == "review"
        assert out.confidence_score <= LOCAL_PRICING_CONFIDENCE_CAP
        assert out.opening_offer > 10_000  # local units, not a KES-sized number
        assert out.opening_offer % get_currency_config(country)["round_step"] == 0

    def test_kes_tracker_is_not_written(self, db, calls, country, currency):
        router.evaluate_trade_in(_req(country), db)
        assert "append_tracker_row" not in calls

    def test_market_tracker_tab_is_written(self, db, calls, country, currency):
        router.evaluate_trade_in(_req(country), db)
        row = calls["append_market_tracker_row"][0]
        assert row["country"] == country and row["currency"] == currency
        assert row["ai_price"] > 0 and "Ref: " in row["notes"]

    def test_airtable_and_team_still_hear_about_it(self, db, calls, country, currency):
        router.evaluate_trade_in(_req(country), db)
        assert calls.get("airtable"), "the evaluation must still reach Airtable"
        assert calls.get("notify"), "the internal WhatsApp alert must still fire"
        assert calls.get("lead_email"), "the lead email check must still run"
        assert calls["lead_email"][0]["currency_code"] == currency

    def test_session_is_stored_with_country_and_currency(self, db, calls, country, currency):
        from greenbay_ai_evaluator.models.evaluator_models import ValuationSession

        out = router.evaluate_trade_in(_req(country), db)
        vs = db.query(ValuationSession).filter(ValuationSession.id == out.session_id).one()
        assert vs.country == country and vs.currency_code == currency


class TestKenyaUnchanged:
    def test_kenyan_sources_are_still_read_and_tracker_written(self, db, calls):
        out = router.evaluate_trade_in(_req("KE"), db)
        for source in ("get_comparables", "get_historical_price", "lookup_sales_stock", "lookup_matrix"):
            assert source in calls, source
        assert "append_tracker_row" in calls
        assert "append_market_tracker_row" not in calls
        assert out.currency_code == "KES"


class TestLabelLookupReturnsBrand:
    def test_brand_read_from_label_is_returned(self, monkeypatch):
        import greenbay_ai_evaluator.services.vision_service as vision

        monkeypatch.setattr(vision, "analyze_model_label", lambda **k: {
            "model_verified": False, "model_number": "RT34K5532S8",
            "brand_from_label": "Samsung", "specs_summary": None,
        })
        out = router.model_lookup(router.ModelLookupRequest(image_data="data:image/jpeg;base64,AA=="))
        assert out.brand == "Samsung"
        assert out.model_number == "RT34K5532S8"

    @pytest.mark.parametrize("raw", [None, "null", "", "Unknown"])
    def test_nothing_read_is_none(self, monkeypatch, raw):
        import greenbay_ai_evaluator.services.vision_service as vision

        monkeypatch.setattr(vision, "analyze_model_label", lambda **k: {
            "model_verified": False, "model_number": None, "brand_from_label": raw,
        })
        out = router.model_lookup(router.ModelLookupRequest(image_data="x"))
        assert out.brand is None

    def test_markup_is_stripped(self, monkeypatch):
        import greenbay_ai_evaluator.services.vision_service as vision

        monkeypatch.setattr(vision, "analyze_model_label", lambda **k: {
            "brand_from_label": "<b>LG</b>",
        })
        out = router.model_lookup(router.ModelLookupRequest(image_data="x"))
        assert "<" not in out.brand and ">" not in out.brand


class TestLeadQualification:
    """Kenya keeps the KES lead floor; every Uganda/Nigeria evaluation is
    confirmed by hand, so the local team hears about each one."""

    def _vs(self, offer, currency, country):
        return {"seller_phone": "256772123456", "opening_offer": offer,
                "currency_code": currency, "country": country}

    @pytest.mark.parametrize("country,currency", [("UG", "UGX"), ("NG", "NGN")])
    def test_every_local_market_evaluation_reaches_the_team(self, country, currency):
        from greenbay_ai_evaluator.services.email_service import qualify_lead
        assert qualify_lead(self._vs(50_000, currency, country))[0]

    def test_no_phone_is_never_a_lead(self):
        from greenbay_ai_evaluator.services.email_service import qualify_lead
        vs = self._vs(900_000, "UGX", "UG")
        vs["seller_phone"] = ""
        assert not qualify_lead(vs)[0]

    def test_kenya_floor_unchanged(self):
        from greenbay_ai_evaluator.services.email_service import qualify_lead
        assert qualify_lead(self._vs(6_000, "KES", "KE"))[0]
        assert not qualify_lead(self._vs(4_000, "KES", "KE"))[0]


class TestEmailRouting:
    """Nigeria -> Sunday + Allan, Uganda -> Kalungi + Allan, Kenya unchanged."""

    @pytest.fixture(autouse=True)
    def settings(self, monkeypatch):
        from app.config import get_settings
        s = get_settings()
        monkeypatch.setattr(s, "team_notification_emails", "allanmatano@greenbay.market,alexander@greenbay.market")
        monkeypatch.setattr(s, "team_notification_emails_ug", "kalungi@greenbay.market,allanmatano@greenbay.market")
        monkeypatch.setattr(s, "team_notification_emails_ng", "sunday@greenbay.market,allanmatano@greenbay.market")
        return s

    def test_defaults_point_at_the_local_teams(self):
        from app.config import Settings
        f = Settings.model_fields
        assert "kalungi@greenbay.market" in f["team_notification_emails_ug"].default
        assert "sunday@greenbay.market" in f["team_notification_emails_ng"].default
        for key in ("team_notification_emails_ug", "team_notification_emails_ng"):
            assert "allanmatano@greenbay.market" in f[key].default
            assert "alexander" not in f[key].default

    @pytest.mark.parametrize("country,expected", [
        ("KE", {"allanmatano@greenbay.market", "alexander@greenbay.market"}),
        ("UG", {"kalungi@greenbay.market", "allanmatano@greenbay.market"}),
        ("NG", {"sunday@greenbay.market", "allanmatano@greenbay.market"}),
    ])
    def test_recipients_by_market(self, country, expected):
        from greenbay_ai_evaluator.services.email_service import _recipients
        assert set(_recipients(country)) == expected

    @pytest.mark.parametrize("country,currency,tag", [("UG", "UGX", "[Uganda]"), ("NG", "NGN", "[Nigeria]")])
    def test_subject_and_body_name_the_market(self, country, currency, tag):
        from greenbay_ai_evaluator.services.email_service import build_outcome_email
        subject, text, html = build_outcome_email(
            {"country": country, "currency_code": currency, "brand": "LG", "opening_offer": 500000}, "NEEDS REVIEW")
        assert subject.startswith(tag)
        assert tag.strip("[]") in text

    def test_kenya_subject_untagged(self):
        from greenbay_ai_evaluator.services.email_service import build_outcome_email
        subject, _, _ = build_outcome_email({"country": "KE", "currency_code": "KES"}, "ACCEPTED")
        assert subject.startswith("[GreenBay]")

    @pytest.mark.parametrize("country", ["UG", "NG"])
    def test_lead_email_goes_to_the_local_team_only(self, monkeypatch, country):
        import threading
        import greenbay_ai_evaluator.services.email_service as email

        sent = []
        monkeypatch.setattr(email, "_send_ses", lambda subj, t, h, recipients=None: sent.append((subj, recipients)))

        class _Inline:
            def __init__(self, target=None, daemon=None, **k):
                self._t = target

            def start(self):
                self._t()

        monkeypatch.setattr(threading, "Thread", _Inline)
        email.send_qualified_lead_email(
            {"country": country, "currency_code": {"UG": "UGX", "NG": "NGN"}[country],
             "seller_phone": "256772123456", "opening_offer": 700000, "image_urls": []},
            "NEEDS REVIEW")
        assert sent, "no email sent"
        _, recipients = sent[0]
        assert "alexander@greenbay.market" not in recipients
        assert "allanmatano@greenbay.market" in recipients


class TestKenyanWhatsAppAlertStaysKenyan:
    def test_local_market_alert_is_not_sent_to_the_kenyan_line(self, monkeypatch):
        import greenbay_ai_evaluator.services.internal_notification_service as n

        started = []
        monkeypatch.setattr(n.threading, "Thread", lambda **k: started.append(k) or type("T", (), {"start": lambda self: None})())
        n.notify_internal_team({"currency": "UGX", "outcome": "NEEDS REVIEW"})
        assert not started
        n.notify_internal_team({"currency": "KES", "outcome": "NEEDS REVIEW"})
        assert started


class TestMarketTrackerTabs:
    def test_tabs_are_separate_from_the_kes_tab(self):
        from greenbay_ai_evaluator.services.reference_data_service import (
            EVAL_TRACKER_TAB, MARKET_TRACKER_TABS,
        )
        assert set(MARKET_TRACKER_TABS) == {"UG", "NG"}
        assert EVAL_TRACKER_TAB not in MARKET_TRACKER_TABS.values()

    def test_row_matches_the_header(self):
        from greenbay_ai_evaluator.services.reference_data_service import (
            MARKET_TRACKER_HEADER, market_tracker_row,
        )
        row = market_tracker_row(currency="UGX", item="LG fridge", ai_price=760000,
                                 ai_confidence=55, new_price=1800000, status="Under Review",
                                 notes="Ref: abcd1234")
        assert len(row) == len(MARKET_TRACKER_HEADER)
        by = dict(zip(MARKET_TRACKER_HEADER, row))
        assert by["Currency"] == "UGX" and by["AI Price"] == 760000
        assert by["Final Price Offered"] == ""  # left for the local team
        assert by["Date"].startswith("'")

    def test_creates_the_tab_once_and_appends(self, monkeypatch):
        import greenbay_ai_evaluator.services.reference_data_service as ref

        class WS:
            def __init__(self):
                self.rows = []

            def append_row(self, row, value_input_option=None):
                self.rows.append(row)

        class SS:
            def __init__(self):
                self.tabs = {}

            def worksheet(self, title):
                if title not in self.tabs:
                    raise LookupError(title)
                return self.tabs[title]

            def add_worksheet(self, title, rows, cols):
                self.tabs[title] = WS()
                return self.tabs[title]

        ss = SS()

        class GC:
            def open_by_key(self, key):
                return ss

        monkeypatch.setattr(ref, "_get_gspread_client", lambda: GC())
        assert ref.append_market_tracker_row("NG", currency="NGN", item="Scanfrost", ai_price=320000)
        assert ref.append_market_tracker_row("NG", currency="NGN", item="Thermocool", ai_price=250000)
        rows = ss.tabs["Nigeria Evaluations"].rows
        assert rows[0] == ref.MARKET_TRACKER_HEADER
        assert len(rows) == 3


class TestProbeRowsNeverEmailMarketTeams:
    @pytest.fixture(autouse=True)
    def settings(self, monkeypatch):
        from app.config import get_settings
        s = get_settings()
        monkeypatch.setattr(s, "team_notification_emails", "allanmatano@greenbay.market,alexander@greenbay.market")
        monkeypatch.setattr(s, "team_notification_emails_ug", "kalungi@greenbay.market,allanmatano@greenbay.market")
        monkeypatch.setattr(s, "team_notification_emails_ng", "sunday@greenbay.market,allanmatano@greenbay.market")
        monkeypatch.setattr(s, "team_notification_emails_test", "allanmatano@greenbay.market")

    @pytest.mark.parametrize("country", ["KE", "UG", "NG"])
    def test_deploy_probe_goes_to_allan_only(self, country):
        from greenbay_ai_evaluator.services.email_service import _recipients_for
        rcpt = _recipients_for({"country": country, "seller_name": f"ZZSMOKE E2E {country}",
                                "model": f"ZZSMOKE-E2E-{country}"})
        assert rcpt == ["allanmatano@greenbay.market"]

    @pytest.mark.parametrize("country", ["KE", "UG", "NG"])
    def test_probe_subject_says_test(self, country):
        from greenbay_ai_evaluator.services.email_service import build_outcome_email
        subject, _, _ = build_outcome_email({"country": country, "seller_name": "ZZSMOKE E2E",
                                             "model": "ZZSMOKE-E2E"}, "NEEDS REVIEW")
        assert subject.startswith("[TEST] ")

    def test_real_ugandan_customer_goes_to_kalungi_and_allan(self):
        from greenbay_ai_evaluator.services.email_service import _recipients_for
        rcpt = _recipients_for({"country": "UG", "seller_name": "Nakato", "model": "LG GN-B202"})
        assert set(rcpt) == {"kalungi@greenbay.market", "allanmatano@greenbay.market"}

    def test_real_customer_goes_to_the_market_team(self):
        from greenbay_ai_evaluator.services.email_service import _recipients_for
        assert "sunday@greenbay.market" in _recipients_for({"country": "NG", "seller_name": "Chiamaka", "model": "Thermocool 200"})


def test_probe_rows_never_reach_the_market_tabs(monkeypatch):
    import greenbay_ai_evaluator.services.reference_data_service as ref
    monkeypatch.setattr(ref, "_get_gspread_client", lambda: (_ for _ in ()).throw(AssertionError("sheet opened")))
    assert ref.append_market_tracker_row("UG", currency="UGX", item="Samsung ZZSMOKE-E2E-UG", model="ZZSMOKE-E2E-UG")
