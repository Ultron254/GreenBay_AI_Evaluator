"""Partner trade-in (Mika, Vision Plus), stage 1: config, attribution,
partner isolation and the credit ledger.

Offline and deterministic: in-memory SQLite, the real /evaluate and
/accept-offer handlers with every network service replaced (the fixtures from
test_markets), and FastAPI's TestClient for the public partner routes.
"""

from __future__ import annotations

import re as _re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import greenbay_ai_evaluator.api.router as router
from greenbay_ai_evaluator.api.schemas import EvaluateRequest, PartnerContext
from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
from greenbay_ai_evaluator.models.partner_models import (
    PartnerCredit,
    PartnerCreditEvent,
    PartnerFunnelEvent,
    PartnerQrCode,
)
from greenbay_ai_evaluator.partners import ledger, rules
from greenbay_ai_evaluator.partners.config import all_partners, get_partner, public_config
from greenbay_ai_evaluator.tests.test_markets import calls, db  # noqa: F401 (fixtures)

MIKA = get_partner("mika")
VPLUS = get_partner("vision-plus")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def sent_emails(monkeypatch):
    """No test ever sends an email: partner notifications are recorded here."""
    import greenbay_ai_evaluator.services.email_service as email

    box: list[tuple[str, str, str, list[str]]] = []
    monkeypatch.setattr(email, "_send_ses", lambda subject, text, html, recipients=None:
                        box.append((subject, text, html, list(recipients or []))) or True)
    return box


@pytest.fixture
def kenya(calls, monkeypatch):  # noqa: F811
    """test_markets' recorder, plus an empty marketplace for the Kenyan path."""

    class _Mkt:
        count = 0
        avg_price = None
        listings: list = []

    async def marketplace(**k):
        return _Mkt()

    monkeypatch.setattr(router, "get_marketplace_prices", marketplace)
    return calls


@pytest.fixture
def engine_offers(monkeypatch):
    """Make the real engine's result an offer the customer sees: offline the
    Kenyan path has no evidence and routes to review. Set ``.offer`` to pick
    the figure."""
    original = router.compute_valuation
    box = {"offer": 8200.0}

    def _wrapped(*a, **k):
        result = original(*a, **k)
        result.decision = "negotiate"
        result.confidence_score = 86.0
        result.opening_offer = box["offer"]
        result.acquisition_ceiling = max(result.acquisition_ceiling or 0, box["offer"] * 1.1)
        return result

    monkeypatch.setattr(router, "compute_valuation", _wrapped)
    return box


def _req(brand="Mika", category="refrigerator", partner=None, **kw) -> EvaluateRequest:
    body = dict(
        category=category, brand=brand, model="MRNF202XDMV",
        age_years=3, condition_grade="B", condition_score=70,
        defects=[], seller_asking_price=None, seller_name="Wanjiku Test",
        seller_phone="0712345678", image_urls=[], image_data=[],
        retail_price=65000, retail_price_source="category_default", country="KE",
    )
    if partner is not None:
        body["partner"] = partner
    body.update(kw)
    return EvaluateRequest(**body)


class _VS:
    """A stand-in valuation session for ledger tests."""

    _n = 0

    def __init__(self, partner="mika", phone="254712345678", **kw):
        _VS._n += 1
        self.id = f"00000000-0000-0000-0000-{_VS._n:012d}"
        self.partner_id = partner
        self.seller_name = "Wanjiku"
        self.seller_phone = phone
        self.currency_code = "KES"
        self.category = "refrigerator"
        self.brand = "Mika"
        self.model = "MRNF202XDMV"
        self.age_years = 3
        self.size_value = None
        self.partner_channel = "website"
        self.partner_shop = None
        self.partner_qr_code = "MK-WEB1"
        self.partner_source = "mika-website-banner"
        self.buy_intent_category = "cooker_oven"
        self.buy_intent_model = None
        self.__dict__.update(kw)


def _issued(db, cfg=MIKA, amount=8000, **kw):  # noqa: F811
    credit = ledger.create_offered(db, cfg, _VS(partner=cfg.slug, **kw), amount=amount)
    ledger.accept(db, credit, amount=amount)
    ledger.start_validation(db, credit, method="technician", actor="Tech Otieno")
    ledger.validate(db, credit, actor="Tech Otieno")
    ledger.issue(db, credit, actor="CX Achieng")
    db.commit()
    return credit


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
class TestConfig:
    def test_both_launch_partners_load_for_kenya(self):
        slugs = {p.slug for p in all_partners()}
        assert {"mika", "vision-plus"} <= slugs
        assert MIKA.countries == ["KE"] and VPLUS.countries == ["KE"]
        assert MIKA.path == "/mika" and VPLUS.path == "/vision-plus"

    def test_qr_codes_are_unique_across_partners(self):
        codes = [p.code for cfg in all_partners() for p in cfg.placements]
        assert len(codes) == len(set(codes))

    def test_public_config_carries_no_emails_or_internal_rules(self):
        email = _re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
        for cfg in all_partners():
            text = repr(public_config(cfg))
            assert not email.search(text)
            assert "max_auto_risk_score" not in text and "notify" not in text

    def test_every_notify_event_has_recipients(self):
        for cfg in all_partners():
            for event in ("accepted", "needs_validation", "credit_issued", "credit_used"):
                assert cfg.notify.get(event), (cfg.slug, event)


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------
class TestRules:
    @pytest.mark.parametrize("brand", ["Mika", "MIKA", " mika ", "Mika Appliances"])
    def test_mika_spellings(self, brand):
        assert rules.brand_allowed(MIKA, brand)

    @pytest.mark.parametrize("brand", ["Vision Plus", "Vision+", "vision-plus", "VISIONPLUS"])
    def test_vision_plus_spellings(self, brand):
        assert rules.brand_allowed(VPLUS, brand)

    @pytest.mark.parametrize("cfg,brand", [(MIKA, "Samsung"), (MIKA, "Vision Plus"), (VPLUS, "Mika"), (MIKA, "")])
    def test_other_brands_refused(self, cfg, brand):
        assert not rules.brand_allowed(cfg, brand)

    def test_categories(self):
        assert rules.category_allowed(VPLUS, "tv_monitor")
        assert not rules.category_allowed(MIKA, "tv_monitor")

    def test_credit_equals_the_offer_above_the_floor(self):
        assert rules.credit_offer(MIKA, decision="negotiate", opening_offer=8200, age_years=3, risk_score=10) == 8200

    def test_floor_lifts_a_tiny_offer(self):
        assert rules.credit_offer(MIKA, decision="accept", opening_offer=300, age_years=6, risk_score=10) == 500

    def test_twelve_years_and_over_is_nominal_whatever_the_engine_says(self):
        for decision, offer in (("negotiate", 9000), ("review", 0)):
            assert rules.credit_offer(MIKA, decision=decision, opening_offer=offer, age_years=12, risk_score=10) == 500

    def test_review_means_a_person_decides(self):
        assert rules.credit_offer(MIKA, decision="review", opening_offer=9000, age_years=3, risk_score=10) is None

    def test_high_risk_never_gets_an_automatic_figure(self):
        assert rules.credit_offer(MIKA, decision="accept", opening_offer=9000, age_years=14, risk_score=85) is None

    def test_accept_keeps_floor_and_nominal(self):
        assert rules.credit_for_accept(MIKA, accepted_amount=200, offered_credit=500, age_years=4) == 500
        assert rules.credit_for_accept(MIKA, accepted_amount=7000, offered_credit=7500, age_years=4) == 7000
        assert rules.credit_for_accept(MIKA, accepted_amount=7000, offered_credit=500, age_years=15) == 500

    def test_bulky(self):
        assert rules.is_bulky(MIKA, "refrigerator", None)
        assert not rules.is_bulky(MIKA, "microwave", None)
        assert rules.is_bulky(VPLUS, "tv_monitor", 55) and not rules.is_bulky(VPLUS, "tv_monitor", 32)

    def test_source_tag(self):
        assert rules.source_tag("mika", "shop", "roysambu", "sticker") == "mika-roysambu-sticker"
        assert rules.source_tag("mika", "website", None, "banner") == "mika-website-banner"
        assert rules.source_tag("mika", None, None, None) == "mika-direct"


# ---------------------------------------------------------------------------
# attribution on the request
# ---------------------------------------------------------------------------
class TestPartnerContext:
    def test_labels_are_cleaned_never_rejected(self):
        ctx = PartnerContext(slug=" Mika ", channel="Web Site!", qr_code="mk-web1 <x>",
                             buy_category="Cooker Oven", buy_model="<b>MST50</b> " + "x" * 400)
        assert ctx.slug == "mika" and ctx.channel == "web-site"
        assert ctx.qr_code == "MK-WEB1X" or ctx.qr_code.startswith("MK-WEB1")
        assert ctx.buy_category == "cooker-oven"
        assert "<" not in ctx.buy_model and len(ctx.buy_model) <= 200

    def test_unknown_keys_ignored(self):
        assert PartnerContext(slug="mika", nonsense=1).slug == "mika"

    def test_evaluate_request_accepts_it_and_older_clients_still_work(self):
        assert _req(partner={"slug": "mika"}).partner.slug == "mika"
        assert _req().partner is None


# ---------------------------------------------------------------------------
# the real /evaluate and /accept-offer handlers
# ---------------------------------------------------------------------------
class TestEvaluateOnPartnerSite:
    def test_lead_is_labelled_and_credit_offered(self, db, kenya, engine_offers):  # noqa: F811
        out = router.evaluate_trade_in(
            _req(partner={"slug": "mika", "qr_code": "MK-WEB1", "buy_category": "cooker_oven"}), db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        assert vs.partner_id == "mika"
        assert vs.partner_qr_code == "MK-WEB1" and vs.partner_channel == "website"
        assert vs.partner_source == "mika-website-banner"
        assert vs.buy_intent_category == "cooker-oven"
        assert out.partner["slug"] == "mika"
        types = [e.event_type for e in db.query(PartnerFunnelEvent).all()]
        assert types == ["complete", "offer"]
        assert out.partner["credit_offer"] == 8200 and vs.partner_offer == 8200
        credit = db.query(PartnerCredit).one()
        assert credit.status == ledger.OFFERED and credit.partner_id == "mika"
        assert credit.offered_amount == 8200 and credit.reference.startswith("MK-")
        assert (credit.qr_code, credit.source_tag, credit.bulky) == ("MK-WEB1", "mika-website-banner", True)
        assert credit.customer_phone == "254712345678"

    def test_review_opens_no_credit_but_still_counts(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika"}), db)
        assert out.decision == "review" and out.partner["credit_offer"] is None
        assert db.query(PartnerCredit).count() == 0
        assert [e.event_type for e in db.query(PartnerFunnelEvent).all()] == ["complete"]

    def test_floor_lifts_a_tiny_engine_offer(self, db, kenya, engine_offers):  # noqa: F811
        engine_offers["offer"] = 300.0
        out = router.evaluate_trade_in(_req(partner={"slug": "mika"}), db)
        assert out.opening_offer == 300 and out.partner["credit_offer"] == 500
        res = router.accept_offer(out.session_id, {"accepted_amount": 500}, db)
        assert res.final_offer == 500

    def test_qr_code_from_the_server_wins_over_the_browser(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(
            _req(partner={"slug": "mika", "qr_code": "MK-WEB2", "channel": "shop", "shop": "fake"}), db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        assert (vs.partner_channel, vs.partner_shop, vs.partner_placement) == ("website", None, "product-page")

    def test_another_partners_qr_code_is_not_trusted(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika", "qr_code": "VP-WEB1"}), db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        assert vs.partner_qr_code is None and vs.partner_source == "mika-direct"

    def test_wrong_brand_is_refused_before_pricing(self, db, kenya):  # noqa: F811
        with pytest.raises(HTTPException) as e:
            router.evaluate_trade_in(_req(brand="Samsung", partner={"slug": "mika"}), db)
        assert e.value.status_code == 422 and e.value.detail["code"] == "partner_brand"
        assert "Mika appliances" in e.value.detail["message"] and "—" not in e.value.detail["message"]
        assert db.query(ValuationSession).count() == 0
        assert "search_internet_price" not in kenya

    def test_unknown_partner_and_wrong_country_refused(self, db, kenya):  # noqa: F811
        with pytest.raises(HTTPException) as e:
            router.evaluate_trade_in(_req(partner={"slug": "nobody"}), db)
        assert e.value.detail["code"] == "partner_unknown"
        with pytest.raises(HTTPException) as e:
            router.evaluate_trade_in(_req(partner={"slug": "mika"}, country="UG", seller_phone="0772123456"), db)
        assert e.value.detail["code"] == "partner_country"

    def test_greenbay_evaluations_are_untouched(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(brand="Samsung"), db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        assert vs.partner_id is None and out.partner is None
        assert db.query(PartnerCredit).count() == 0
        assert db.query(PartnerFunnelEvent).count() == 0

    def test_very_old_item_gets_the_nominal_credit(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika"}, age_years=13), db)
        assert out.partner["credit_offer"] == 500 and out.partner["nominal"] is True
        assert db.query(PartnerCredit).one().is_nominal is True

    def test_airtable_row_carries_the_partner(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika", "qr_code": "MK-WEB1"}), db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        fields = router._pulse_airtable_fields(vs)
        assert fields["Partner"] == "mika" and fields["Partner Source"] == "mika-website-banner"
        assert fields["QR Code"] == "MK-WEB1"
        plain = ValuationSession(id="x", decision="accept")
        assert "Partner" not in router._pulse_airtable_fields(plain)


class TestAcceptOnPartnerSite:
    def _evaluate(self, db, **kw):  # noqa: F811
        return router.evaluate_trade_in(_req(partner={"slug": "mika", "qr_code": "MK-WEB1"}, **kw), db)

    def test_accept_moves_the_credit_and_returns_a_reference(self, db, kenya):  # noqa: F811
        out = self._evaluate(db, age_years=13)
        res = router.accept_offer(out.session_id, {"accepted_amount": 500}, db)
        credit = db.query(PartnerCredit).one()
        assert credit.status == ledger.ACCEPTED
        assert res.reference == credit.reference and credit.reference in res.message
        assert res.final_offer == 500
        assert "—" not in res.message and "instant" not in res.message.lower()
        types = [e.event_type for e in db.query(PartnerFunnelEvent).all()]
        assert types.count("accept") == 1

    def test_accept_cannot_name_its_own_price(self, db, kenya):  # noqa: F811
        out = self._evaluate(db, age_years=13)
        res = router.accept_offer(out.session_id, {"accepted_amount": 900000}, db)
        assert res.final_offer == 500

    def test_negotiated_figure_is_the_credit(self, db, kenya, engine_offers):  # noqa: F811
        out = self._evaluate(db)
        res = router.accept_offer(out.session_id, {"accepted_amount": 8600}, db)
        credit = db.query(PartnerCredit).one()
        assert res.final_offer == 8600 == credit.offered_amount
        assert db.query(ValuationSession).filter_by(id=out.session_id).one().final_offer == 8600

    def test_double_tap_is_harmless(self, db, kenya):  # noqa: F811
        out = self._evaluate(db, age_years=13)
        router.accept_offer(out.session_id, {"accepted_amount": 500}, db)
        router.accept_offer(out.session_id, {"accepted_amount": 500}, db)
        assert db.query(PartnerCredit).one().status == ledger.ACCEPTED
        assert db.query(PartnerCreditEvent).filter_by(event_type="accepted").count() == 1

    def test_no_offer_means_no_accept(self, db, kenya):  # noqa: F811
        out = self._evaluate(db)
        vs = db.query(ValuationSession).filter_by(id=out.session_id).one()
        vs.partner_offer = None
        db.commit()
        with pytest.raises(HTTPException) as e:
            router.accept_offer(out.session_id, {}, db)
        assert e.value.status_code == 409

    def test_greenbay_accept_has_no_reference(self, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(brand="Samsung"), db)
        res = router.accept_offer(out.session_id, {}, db)
        assert res.reference is None


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------
class TestLedger:
    def test_full_life_with_audit_trail(self, db):  # noqa: F811
        credit = _issued(db, amount=8000)
        assert credit.status == ledger.ISSUED and credit.balance == 8000
        assert credit.voucher_code.startswith("MIKA-") and len(credit.voucher_code) == len("MIKA-XXXX-XX")
        exp = ledger._aware(credit.expires_at) - ledger._aware(credit.issued_at)
        assert exp == timedelta(days=90)
        ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code, amount=3000, actor="CX Achieng")
        assert credit.status == ledger.PARTIALLY_REDEEMED and credit.balance == 5000
        ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code.lower(), amount=5000, actor="CX Achieng")
        assert credit.status == ledger.REDEEMED and credit.balance == 0
        trail = [(e.event_type, e.from_status, e.to_status) for e in ledger.history(db, credit)]
        assert trail == [
            ("offered", None, "offered"),
            ("accepted", "offered", "accepted"),
            ("validation_started", "accepted", "pending_validation"),
            ("validated", "pending_validation", "validated"),
            ("issued", "validated", "issued"),
            ("redeemed", "issued", "partially_redeemed"),
            ("redeemed", "partially_redeemed", "redeemed"),
        ]
        assert all(e.actor for e in ledger.history(db, credit))

    def test_no_skipping_steps(self, db):  # noqa: F811
        credit = ledger.create_offered(db, MIKA, _VS(), amount=4000)
        with pytest.raises(ledger.LedgerError) as e:
            ledger.issue(db, credit, actor="CX")
        assert e.value.code == "bad_transition"
        with pytest.raises(ledger.LedgerError):
            ledger.validate(db, credit, actor="Tech")

    def test_every_action_needs_a_name(self, db):  # noqa: F811
        credit = ledger.create_offered(db, MIKA, _VS(), amount=4000)
        ledger.accept(db, credit, amount=4000)
        with pytest.raises(ledger.LedgerError) as e:
            ledger.start_validation(db, credit, method="technician", actor="  ")
        assert e.value.code == "actor_required"

    def test_inspection_can_lower_never_raise(self, db):  # noqa: F811
        credit = ledger.create_offered(db, MIKA, _VS(), amount=6000)
        ledger.accept(db, credit, amount=6000)
        ledger.start_validation(db, credit, method="drop_partner", actor="Tech")
        with pytest.raises(ledger.LedgerError) as e:
            ledger.validate(db, credit, actor="Tech", agreed_amount=6500)
        assert e.value.code == "above_offer"
        ledger.validate(db, credit, actor="Tech", agreed_amount=4500, note="Door seal torn, agreed with customer")
        ledger.issue(db, credit, actor="CX")
        assert credit.issued_amount == 4500

    def test_failed_inspection_cancels(self, db):  # noqa: F811
        credit = ledger.create_offered(db, MIKA, _VS(), amount=6000)
        ledger.accept(db, credit, amount=6000)
        ledger.start_validation(db, credit, method="technician", actor="Tech")
        ledger.reject_inspection(db, credit, actor="Tech", note="Compressor dead")
        assert credit.status == ledger.CANCELLED and credit.voucher_code is None

    def test_cannot_spend_more_than_the_balance(self, db):  # noqa: F811
        credit = _issued(db, amount=2000)
        with pytest.raises(ledger.LedgerError) as e:
            ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code, amount=2500, actor="CX")
        assert e.value.code == "over_balance"
        for bad in (0, -5, "abc"):
            with pytest.raises(ledger.LedgerError):
                ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code, amount=bad, actor="CX")
        assert credit.balance == 2000

    def test_expired_credit_cannot_be_spent_and_is_swept(self, db):  # noqa: F811
        credit = _issued(db, amount=2000)
        later = datetime.now(timezone.utc) + timedelta(days=91)
        with pytest.raises(ledger.LedgerError) as e:
            ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code, amount=100, actor="CX", now=later)
        assert e.value.code == "expired" and credit.status == ledger.ISSUED
        assert ledger.expire_due(db, now=later) == 1
        assert credit.status == ledger.EXPIRED
        assert ledger.expire_due(db, now=later) == 0

    def test_not_yet_due_is_left_alone(self, db):  # noqa: F811
        _issued(db, amount=2000)
        assert ledger.expire_due(db, now=datetime.now(timezone.utc) + timedelta(days=89)) == 0

    def test_spent_credit_cannot_be_cancelled(self, db):  # noqa: F811
        credit = _issued(db, amount=2000)
        ledger.redeem(db, partner_id="mika", voucher_code=credit.voucher_code, amount=100, actor="CX")
        with pytest.raises(ledger.LedgerError) as e:
            ledger.cancel(db, credit, actor="CX")
        assert e.value.code == "already_spent"

    def test_no_cash_out_path_exists(self):
        assert "cashed_out" not in ledger.STATUSES
        assert all("cash" not in s for targets in ledger.TRANSITIONS.values() for s in targets)


class TestPartnerIsolation:
    def test_one_partner_cannot_spend_anothers_credit(self, db):  # noqa: F811
        mika_credit = _issued(db, MIKA, amount=5000)
        with pytest.raises(ledger.LedgerError) as e:
            ledger.redeem(db, partner_id="vision-plus", voucher_code=mika_credit.voucher_code,
                          amount=100, actor="VP staff")
        assert e.value.code == "not_found"
        assert mika_credit.balance == 5000

    def test_lookups_never_cross_partners(self, db):  # noqa: F811
        same_phone = "254711000111"
        m = _issued(db, MIKA, amount=5000, phone=same_phone)
        v = _issued(db, VPLUS, amount=7000, phone=same_phone)
        assert [c.id for c in ledger.find_for_partner(db, "mika", query="0711 000 111")] == [m.id]
        assert [c.id for c in ledger.find_for_partner(db, "vision-plus", query="+254711000111")] == [v.id]
        assert ledger.find_for_partner(db, "mika", query=v.voucher_code) == []
        assert ledger.find_for_partner(db, "vision-plus", query=m.reference) == []
        assert ledger.get_for_partner(db, "vision-plus", credit_id=m.id) is None

    def test_codes_carry_the_partner_prefix(self, db):  # noqa: F811
        assert _issued(db, VPLUS, amount=100).voucher_code.startswith("VPLUS-")

    def test_every_audit_row_names_its_partner(self, db):  # noqa: F811
        _issued(db, MIKA, amount=5000)
        _issued(db, VPLUS, amount=5000)
        for ev in db.query(PartnerCreditEvent).all():
            credit = db.query(PartnerCredit).filter_by(id=ev.credit_id).one()
            assert ev.partner_id == credit.partner_id


# ---------------------------------------------------------------------------
# public routes
# ---------------------------------------------------------------------------
@pytest.fixture
def client(db):  # noqa: F811
    from app.database.db import get_db
    from greenbay_ai_evaluator.partners.api import (
        partner_api_router, partner_page_router, register_partner_pages,
    )

    app = FastAPI()
    app.include_router(partner_api_router, prefix="/tradein")
    app.include_router(partner_page_router)
    register_partner_pages(app)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class TestPublicRoutes:
    def test_config(self, client):
        r = client.get("/tradein/partners/mika/config")
        assert r.status_code == 200 and r.json()["name"] == "Mika" and "@greenbay.market" not in r.text
        assert client.get("/tradein/partners/nobody/config").status_code == 404

    def test_qr_scan_is_logged_then_redirected(self, client, db):  # noqa: F811
        r = client.get("/q/mk-web1", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/mika?qr=MK-WEB1"
        ev = db.query(PartnerFunnelEvent).one()
        assert (ev.partner_id, ev.event_type, ev.qr_code, ev.channel) == ("mika", "scan", "MK-WEB1", "website")

    def test_admin_made_code_and_deactivated_code(self, client, db):  # noqa: F811
        db.add(PartnerQrCode(code="MK-RSB1", partner_id="mika", channel="shop", shop="roysambu",
                             placement="sticker", active=True))
        db.add(PartnerQrCode(code="MK-OLD1", partner_id="mika", channel="shop", placement="sticker", active=False))
        db.commit()
        r = client.get("/q/MK-RSB1", follow_redirects=False)
        assert r.headers["location"] == "/mika?qr=MK-RSB1"
        assert client.get("/q/MK-OLD1", follow_redirects=False).headers["location"] == "/app/"

    def test_unknown_code_lands_on_the_evaluator(self, client, db):  # noqa: F811
        r = client.get("/q/NOPE", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/app/"
        assert db.query(PartnerFunnelEvent).count() == 0

    def test_start_ping(self, client, db):  # noqa: F811
        assert client.post("/tradein/partners/mika/events", json={"type": "start", "qr_code": "MK-WEB1"}).status_code == 204
        assert client.post("/tradein/partners/mika/events", json={"type": "redeemed"}).status_code == 422
        ev = db.query(PartnerFunnelEvent).one()
        assert ev.event_type == "start" and ev.qr_code == "MK-WEB1"

    def test_start_ping_ignores_another_partners_code(self, client, db):  # noqa: F811
        client.post("/tradein/partners/mika/events", json={"type": "start", "qr_code": "VP-WEB1"})
        ev = db.query(PartnerFunnelEvent).one()
        assert ev.partner_id == "mika" and ev.qr_code is None

    def test_partner_page(self, client):
        r = client.get("/mika")
        assert r.status_code == 200
        assert '<meta name="gb-partner" content="mika" data-country="KE">' in r.text
        assert 'data-partner="mika"' in r.text
        assert 'href="/app/styles.css' in r.text and 'src="/app/app.js' in r.text
        assert 'src="/app/partner.js' in r.text and 'href="/app/partner.css' in r.text
        assert 'id="evaluate"' in r.text
        assert client.get("/vision-plus").status_code == 200
        assert client.get("/mika/", follow_redirects=False).headers["location"] == "/mika"


# ---------------------------------------------------------------------------
# migration v640_partners
# ---------------------------------------------------------------------------
class TestMigration:
    def _run_upgrade(self, engine):
        import importlib.util
        from pathlib import Path

        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "v6_4_0_add_partner_tables.py"
        spec = importlib.util.spec_from_file_location("v640", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                mod.upgrade()
        return mod

    def test_adds_columns_and_tables_and_is_idempotent(self):
        pytest.importorskip("alembic")
        from sqlalchemy import create_engine, inspect, text

        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE valuation_sessions (id VARCHAR(36) PRIMARY KEY, brand VARCHAR(100))"))
        mod = self._run_upgrade(engine)
        assert mod.down_revision == "v630_image_keys"
        self._run_upgrade(engine)  # second run is a no-op, not an error
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("valuation_sessions")}
        assert {name for name, _ in mod._SESSION_COLUMNS} <= cols
        for table in ("partner_credits", "partner_credit_events", "partner_qr_codes", "partner_funnel_events"):
            assert table in insp.get_table_names()

    def test_migration_matches_the_models(self):
        pytest.importorskip("alembic")
        from sqlalchemy import create_engine, inspect, text

        from app.database.db import Base

        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE valuation_sessions (id VARCHAR(36) PRIMARY KEY)"))
        self._run_upgrade(engine)
        insp = inspect(engine)
        for table in ("partner_credits", "partner_credit_events", "partner_qr_codes", "partner_funnel_events"):
            model_cols = set(Base.metadata.tables[table].columns.keys())
            db_cols = {c["name"] for c in insp.get_columns(table)}
            assert model_cols == db_cols, table


# ---------------------------------------------------------------------------
# stage 2: the branded partner page
# ---------------------------------------------------------------------------
import json as _json  # noqa: E402
import re as _re  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _subprocess  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from greenbay_ai_evaluator.partners import pages  # noqa: E402

_FRONTEND = _Path(__file__).resolve().parents[2] / "frontend"
_BRANDED = [cfg for cfg in all_partners() if cfg.copy_text]


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    chans = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("cfg", _BRANDED, ids=lambda c: c.slug)
class TestBrandedPage:
    def test_every_placeholder_is_filled(self, cfg):
        html = pages.render(cfg.slug)
        assert "{{" not in html

    def test_the_wizard_is_the_shared_one_not_a_copy(self, cfg):
        """The page reuses index.html's evaluation section: same steps, same ids."""
        html = pages.render(cfg.slug)
        shared = pages.extract_wizard((_FRONTEND / "index.html").read_text(encoding="utf-8"))
        for marker in ('id="step1"', 'id="step2"', 'id="step3"', 'id="step4"', 'id="stepResults"',
                       'id="chatPanel"', 'id="sellerPhoneInput"', 'id="nextBtn"'):
            assert marker in shared and html.count(marker) == 1, marker
        assert (_FRONTEND / "partner.html").read_text(encoding="utf-8").count('id="step1"') == 0

    def test_embedded_config_is_public_only(self, cfg):
        html = pages.render(cfg.slug)
        raw = _re.search(r'<script type="application/json" id="gbPartnerConfig">(.*?)</script>', html, _re.S).group(1)
        data = _json.loads(raw)
        assert data["slug"] == cfg.slug and data["brands"] == cfg.brands
        assert "@greenbay.market" not in raw and "notify" not in data

    def test_logos_are_official_files_that_exist(self, cfg):
        for key in ("logo", "logo_on_dark"):
            path = cfg.theme[key]
            assert path.startswith("/app/partners/" + cfg.slug + "/"), path
            assert (_FRONTEND / path[len("/app/"):]).is_file(), path
        assert (_FRONTEND / "partners/greenbay/greenbay-wordmark-white.png").is_file()

    def test_copy_is_plain(self, cfg):
        """No em or en dashes, no instant promises, no reply-time promises."""
        text = _json.dumps(cfg.copy_text, ensure_ascii=False)
        assert "\u2014" not in text and "\u2013" not in text
        low = text.lower()
        for banned in ("instant", "guarantee", "within 24", "within 48", "same day", "right away", "immediately"):
            assert banned not in low, banned

    def test_wcag_aa_contrast(self, cfg):
        c = cfg.theme["colors"]
        pairs = [
            ("ink", "paper"), ("muted", "paper"), ("ink", "stone"), ("muted", "stone"),
            ("on_ink", "ink"), ("on_ink_muted", "ink"), ("cosign", "paper"), ("ink", "cosign_soft"),
        ]
        for fg, bg in pairs:
            assert _contrast(c[fg], c[bg]) >= 4.5, (fg, bg, round(_contrast(c[fg], c[bg]), 2))
        assert _contrast("#FFFFFF", c["accent"]) >= 4.5
        assert _contrast("#FFFFFF", c["muted"]) >= 4.5  # muted text also sits on white cards
        band = c.get("band", c["ink"])  # bar, hero, offer header and footer
        for fg in ("on_ink", "on_ink_muted"):
            assert _contrast(c[fg], band) >= 4.5, (fg, "band", round(_contrast(c[fg], band), 2))
        assert _contrast(c["ink"], c["paper"]) >= 7  # body text: AAA on the page


class TestPageSafety:
    def test_config_text_is_escaped(self, monkeypatch):
        cfg = MIKA.model_copy(deep=True)
        cfg.copy_text["headline_1"] = '<img src=x onerror=alert(1)>'
        monkeypatch.setattr(pages, "get_partner", lambda slug: cfg)
        pages._render.cache_clear()
        try:
            html = pages._render("mika", "test")
        finally:
            pages._render.cache_clear()
        assert "<img src=x onerror" not in html and "&lt;img src=x" in html

    def test_theme_values_cannot_break_out_of_css(self):
        cfg = MIKA.model_copy(deep=True)
        cfg.theme = dict(cfg.theme, colors=dict(cfg.theme["colors"], ink="red;}</style><script>x</script>"),
                         font_body="x;}</style>", radius_card="1px;}")
        css = pages.theme_css(cfg)
        assert "</style>" not in css and "<script>" not in css and "1px;}" not in css
        assert "--p-ink:#111111" in css

    def test_embedded_json_cannot_close_its_script_tag(self, monkeypatch):
        cfg = MIKA.model_copy(deep=True)
        cfg.copy_text["greeting"] = "</script><script>alert(1)</script>"
        assert "</script>" not in pages._config_json(cfg)


@pytest.mark.skipif(_shutil.which("node") is None, reason="node is not installed")
def test_browser_and_server_agree_on_brand_spellings():
    """app.js partnerBrandOk must fold brand names exactly like rules.brand_allowed."""
    src = (_FRONTEND / "app.js").read_text(encoding="utf-8")
    start = src.index("function partnerBrandOk(")
    end = src.index("/** On a partner site the brand is the partner's")
    cases = []
    for cfg in all_partners():
        for brand in ["Mika", "MIKA", "Mika Appliances", "Vision Plus", "Vision+", "vision-plus", "VPlus",
                      "Samsung", "", "Hisense", " mika "]:
            cases.append([cfg.brands, brand, rules.brand_allowed(cfg, brand) and normalise_in(cfg, brand)])
    script = (
        "let GB_PARTNER = null;\n" + src[start:end]
        + "\nconst cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));\n"
        + "console.log(JSON.stringify(cases.map(c => { GB_PARTNER = {config: {brands: c[0]}}; return partnerBrandOk(c[1]); })));\n"
    )
    out = _subprocess.run(["node", "-e", script], input=_json.dumps(cases), capture_output=True,
                          text=True, timeout=30, check=True)
    got = _json.loads(out.stdout)
    assert [bool(g) for g in got] == [bool(c[2]) for c in cases]


def normalise_in(cfg, brand) -> bool:
    """The browser only knows the brand names, not the aliases: a label that
    reads an alias is let through by the server, so the browser must never be
    stricter than the server on the names themselves."""
    return rules.normalise_brand(brand) in {rules.normalise_brand(b) for b in cfg.brands}


# ---------------------------------------------------------------------------
# stage 4: ops routes (admin key) and portal routes for Pulse (read-only key)
# ---------------------------------------------------------------------------
from greenbay_ai_evaluator.api.security import verify_admin_key, verify_readonly_or_admin_key  # noqa: E402
from greenbay_ai_evaluator.partners import data_api, reports  # noqa: E402

_ADMIN = "admin-key-for-tests-0123456789"
_READONLY = "readonly-key-for-tests-9876543210"


def _gates(route) -> set:
    return {d.call for d in route.dependant.dependencies if d.call in (verify_admin_key, verify_readonly_or_admin_key)}


class TestKeyWiring:
    def test_every_ops_route_is_admin_only(self):
        for r in data_api.partner_admin_router.routes:
            assert _gates(r) == {verify_admin_key}, r.path

    def test_portal_routes_are_get_and_readonly(self):
        for r in data_api.partner_portal_router.routes:
            assert set(r.methods) == {"GET"}, r.path
            assert _gates(r) == {verify_readonly_or_admin_key}, r.path
            assert r.path.startswith("/partners/{slug}/portal/"), r.path

    def test_public_partner_routes_carry_no_data(self):
        from greenbay_ai_evaluator.partners.api import partner_api_router
        paths = {(r.path, tuple(sorted(r.methods))) for r in partner_api_router.routes}
        assert paths == {("/partners/{slug}/config", ("GET",)), ("/partners/{slug}/events", ("POST",))}

    def test_admin_is_a_reserved_slug(self):
        from greenbay_ai_evaluator.partners.config import PartnerConfig
        with pytest.raises(Exception):
            PartnerConfig(slug="admin", name="x", path="/x", ref_prefix="X", voucher_prefix="X",
                          countries=["KE"], brands=["x"], categories=["refrigerator"])


@pytest.fixture
def ops(db, monkeypatch):  # noqa: F811
    from app.database.db import get_db

    monkeypatch.setenv("DASHBOARD_KEY", _ADMIN)
    monkeypatch.setenv("READONLY_DASHBOARD_KEY", _READONLY)
    app = FastAPI()
    app.include_router(data_api.partner_admin_router, prefix="/tradein")
    app.include_router(data_api.partner_portal_router, prefix="/tradein")
    app.dependency_overrides[get_db] = lambda: db
    c = TestClient(app)
    c.admin = {"X-Admin-Key": _ADMIN}
    c.ro = {"X-Admin-Key": _READONLY}
    return c


def _accepted(db, cfg=MIKA, amount=8000, phone="254712345678", name="Wanjiku Test", **kw):  # noqa: F811
    vs = _VS(partner=cfg.slug, phone=phone, seller_name=name, **kw)
    credit = ledger.create_offered(db, cfg, vs, amount=amount)
    ledger.accept(db, credit, amount=amount)
    db.commit()
    return credit


class TestOpsFlow:
    def test_keys(self, ops):
        assert ops.get("/tradein/partners/admin/overview").status_code == 403
        assert ops.get("/tradein/partners/admin/overview", headers=ops.ro).status_code == 403
        assert ops.get("/tradein/partners/admin/overview", headers=ops.admin).status_code == 200
        assert ops.get("/tradein/partners/mika/portal/summary").status_code == 403
        assert ops.get("/tradein/partners/mika/portal/summary", headers=ops.ro).status_code == 200

    def test_check_to_settlement(self, ops, db):  # noqa: F811
        c = _accepted(db, amount=8000)
        base = f"/tradein/partners/admin/credits/{c.id}"
        r = ops.post(base + "/start-validation", headers=ops.admin, json={"actor": "Tech Otieno", "method": "technician"})
        assert r.status_code == 200 and r.json()["status"] == "pending_validation"
        r = ops.post(base + "/validate", headers=ops.admin, json={"actor": "Tech Otieno", "agreed_amount": 7500,
                                                                  "note": "Door seal worn"})
        assert r.json()["agreed_amount"] == 7500
        r = ops.post(base + "/issue", headers=ops.admin, json={"actor": "CX Achieng"})
        code = r.json()["voucher_code"]
        assert code.startswith("MIKA-") and r.json()["balance"] == 7500
        r = ops.post("/tradein/partners/admin/redeem", headers=ops.admin,
                     json={"actor": "CX Achieng", "partner": "mika", "voucher_code": code, "amount": 3000,
                           "note": "Mika receipt 4411"})
        assert r.json()["balance"] == 4500 and r.json()["status"] == "partially_redeemed"
        actors = [h["actor"] for h in r.json()["history"]]
        assert actors == ["system", "customer", "Tech Otieno", "Tech Otieno", "CX Achieng", "CX Achieng"]
        month = reports.now().strftime("%Y-%m")
        s = ops.get(f"/tradein/partners/admin/settlement?partner=mika&month={month}", headers=ops.admin).json()
        assert (s["issued"], s["amount_due"], s["outstanding_at_end"]) == (7500, 3000, 4500)
        assert s["redemptions"][0]["note"] == "Mika receipt 4411"
        csv_r = ops.get(f"/tradein/partners/admin/settlement?partner=mika&month={month}&format=csv", headers=ops.admin)
        assert csv_r.headers["content-type"].startswith("text/csv") and "3000.00" in csv_r.text
        # Vision Plus owes nothing for Mika's credit.
        v = ops.get(f"/tradein/partners/admin/settlement?partner=vision-plus&month={month}", headers=ops.admin).json()
        assert v["amount_due"] == 0 and v["redemptions"] == []

    def test_refusals_are_409_with_a_reason(self, ops, db):  # noqa: F811
        c = _accepted(db)
        r = ops.post(f"/tradein/partners/admin/credits/{c.id}/issue", headers=ops.admin, json={"actor": "CX"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "bad_transition"
        assert db.query(PartnerCredit).filter_by(id=c.id).one().status == "accepted"

    def test_no_name_no_action(self, ops, db):  # noqa: F811
        c = _accepted(db)
        r = ops.post(f"/tradein/partners/admin/credits/{c.id}/start-validation", headers=ops.admin,
                     json={"method": "technician"})
        assert r.status_code == 422

    def test_redeem_cannot_cross_partners(self, ops, db):  # noqa: F811
        c = _issued(db, MIKA, amount=5000)
        r = ops.post("/tradein/partners/admin/redeem", headers=ops.admin,
                     json={"actor": "CX", "partner": "vision-plus", "voucher_code": c.voucher_code, "amount": 100})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_found"

    def test_search_goes_in_a_header(self, ops, db):  # noqa: F811
        _issued(db, MIKA, amount=5000, phone="254700111222")
        r = ops.get("/tradein/partners/admin/credits", headers={**ops.admin, "X-Lookup-Query": "0700 111 222"})
        assert len(r.json()["items"]) == 1
        assert "q" not in {p.name for p in next(
            rt for rt in data_api.partner_admin_router.routes if rt.path == "/partners/admin/credits"
        ).dependant.query_params}

    def test_review_lead_gets_a_price_from_a_person(self, ops, db, kenya):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika"}), db)
        assert out.partner["credit_offer"] is None
        leads = ops.get("/tradein/partners/admin/review-leads", headers=ops.admin).json()["items"]
        assert [x["session_id"] for x in leads] == [out.session_id]
        r = ops.post(f"/tradein/partners/admin/review-leads/{out.session_id}/offer", headers=ops.admin,
                     json={"actor": "Pricing Wairimu", "amount": 6200})
        assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["offered_amount"] == 6200
        assert ops.get("/tradein/partners/admin/review-leads", headers=ops.admin).json()["items"] == []
        again = ops.post(f"/tradein/partners/admin/review-leads/{out.session_id}/offer", headers=ops.admin,
                         json={"actor": "Pricing Wairimu", "amount": 6200})
        assert again.status_code == 409


class TestPortalForPulse:
    def _seed(self, db, kenya):  # noqa: F811
        router.evaluate_trade_in(_req(partner={"slug": "mika", "qr_code": "MK-WEB1", "buy_category": "cooker_oven",
                                               "buy_model": "MST50"}), db)
        router.evaluate_trade_in(_req(brand="Vision Plus", category="refrigerator",
                                      partner={"slug": "vision-plus", "qr_code": "VP-WEB1", "buy_category": "tv"}), db)
        m = _issued(db, MIKA, amount=5000, phone="254711000111")
        v = _issued(db, VPLUS, amount=7000, phone="254711000111")
        return m, v

    def test_each_partner_sees_only_its_own(self, ops, db, kenya):  # noqa: F811
        m, v = self._seed(db, kenya)
        for path in ("leads", "credits", "demand", "qr", "summary"):
            mika = ops.get(f"/tradein/partners/mika/portal/{path}", headers=ops.ro).text
            assert "vision" not in mika.lower() and "VP-" not in mika and v.voucher_code not in mika, path
            vp = ops.get(f"/tradein/partners/vision-plus/portal/{path}", headers=ops.ro).text
            assert "MK-" not in vp and m.voucher_code not in vp, path

    def test_lookup_is_header_only_and_scoped(self, ops, db, kenya):  # noqa: F811
        m, v = self._seed(db, kenya)
        url = "/tradein/partners/mika/portal/credit-lookup"
        assert ops.get(url, headers=ops.ro).status_code == 422
        own = ops.get(url, headers={**ops.ro, "X-Lookup-Query": m.voucher_code}).json()["items"]
        assert [x["voucher_code"] for x in own] == [m.voucher_code]
        assert ops.get(url, headers={**ops.ro, "X-Lookup-Query": v.voucher_code}).json()["items"] == []
        by_phone = ops.get(url, headers={**ops.ro, "X-Lookup-Query": "0711 000 111"}).json()["items"]
        assert [x["voucher_code"] for x in by_phone] == [m.voucher_code]
        assert {h["event"] for h in by_phone[0]["history"]} >= {"offered", "issued"}

    def test_payloads_hide_personal_and_staff_details(self, ops, db, kenya):  # noqa: F811
        self._seed(db, kenya)
        for path in ("leads", "credits"):
            body = ops.get(f"/tradein/partners/mika/portal/{path}", headers=ops.ro).text
            assert "254711000111" not in body and "254712345678" not in body, path
            assert "Tech Otieno" not in body and "CX Achieng" not in body and "actor" not in body, path
            assert "Test" not in body, path  # surnames never leave
        lead = ops.get("/tradein/partners/mika/portal/leads", headers=ops.ro).json()["items"][0]
        assert lead["customer"] == {"first_name": "Wanjiku", "phone_masked": "0712 *** 678"}
        assert set(lead) == {"ref", "created_at", "category", "brand", "model", "channel", "shop", "qr_code",
                             "source", "status", "credit_offer", "buy_intent", "customer"}

    def test_demand_and_qr(self, ops, db, kenya):  # noqa: F811
        self._seed(db, kenya)
        d = ops.get("/tradein/partners/mika/portal/demand", headers=ops.ro).json()
        assert d["total"] == 1 and d["items"][0]["label"] == "Cooker or oven" and d["items"][0]["models"] == ["MST50"]
        qr = {i["code"]: i for i in ops.get("/tradein/partners/mika/portal/qr", headers=ops.ro).json()["items"]}
        assert set(qr) == {"MK-WEB1", "MK-WEB2"} and qr["MK-WEB1"]["funnel"]["complete"] == 1
        assert qr["MK-WEB1"]["url"].endswith("/q/MK-WEB1")

    def test_unknown_partner_is_404(self, ops):
        assert ops.get("/tradein/partners/nobody/portal/summary", headers=ops.ro).status_code == 404


class TestSettlementMaths:
    def test_lapsed_and_outstanding_by_period(self, db):  # noqa: F811
        c = _issued(db, MIKA, amount=2000)
        ledger.redeem(db, partner_id="mika", voucher_code=c.voucher_code, amount=500, actor="CX")
        db.commit()
        later = datetime.now(timezone.utc) + timedelta(days=91)
        ledger.expire_due(db, now=later)
        db.commit()
        start = datetime.now(timezone.utc) - timedelta(days=1)
        rep = reports.settlement(db, "mika", start=start, end=datetime.now(timezone.utc) + timedelta(days=1))
        assert (rep["issued"], rep["amount_due"], rep["lapsed"], rep["outstanding_at_end"]) == (2000, 500, 1500, 0)
        before = reports.settlement(db, "mika", start=start - timedelta(days=30), end=start)
        assert before["issued"] == 0 and before["outstanding_at_end"] == 0

    def test_mask_phone(self):
        assert reports.mask_phone("254712345678") == "0712 *** 678"
        assert reports.mask_phone("") is None and reports.mask_phone(None) is None


class TestDemoSeed:
    def _mod(self):
        import importlib.util
        path = _Path(__file__).resolve().parents[2] / "scripts" / "seed_partner_demo.py"
        spec = importlib.util.spec_from_file_location("seed_partner_demo", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_seed_fills_every_state_for_every_partner_and_resets(self, db):  # noqa: F811
        mod = self._mod()
        made = mod.seed(db)
        assert set(made) >= {"mika", "vision-plus"}
        for cfg in (MIKA, VPLUS):
            statuses = {c.status for c in db.query(PartnerCredit).filter_by(partner_id=cfg.slug)}
            assert {"offered", "accepted", "pending_validation", "validated", "issued",
                    "partially_redeemed", "redeemed", "cancelled"} <= statuses, cfg.slug
            assert reports.leads(db, cfg.slug, days=90, only_review=True)
        from greenbay_ai_evaluator.services.reference_data_service import TEST_ROW_PATTERN
        for vs in db.query(ValuationSession).all():
            assert TEST_ROW_PATTERN.search(vs.model), vs.model  # purge and Pulse skip demo rows
        assert mod.reset(db) == db.query(ValuationSession).count() + sum(made.values())
        assert db.query(PartnerCredit).count() == 0

    def test_refuses_a_real_database(self, monkeypatch):
        mod = self._mod()
        monkeypatch.setenv("DATABASE_URL", "postgresql://prod/greenbay")
        monkeypatch.setattr("sys.argv", ["seed_partner_demo.py"])
        assert mod.main() == 2


def test_ledger_works_on_a_session_that_does_not_autoflush():
    """app/database/db.py makes sessions with autoflush=False. Issue then
    redeem in one transaction must still find the new voucher code."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database.db import Base
    import app.database.models  # noqa: F401

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False)()
    try:
        c = ledger.create_offered(s, MIKA, _VS(), amount=4000)
        ledger.accept(s, c, amount=4000)
        ledger.start_validation(s, c, method="technician", actor="Tech")
        ledger.validate(s, c, actor="Tech")
        ledger.issue(s, c, actor="CX")
        ledger.redeem(s, partner_id="mika", voucher_code=c.voucher_code, amount=1000, actor="CX")
        assert ledger.find_for_partner(s, "mika", query=c.voucher_code)[0].balance == 3000
        s.commit()
    finally:
        s.close()


class TestOpsPage:
    def test_key_and_searches_never_go_in_a_url(self):
        js = (_FRONTEND / "partners-ops.js").read_text(encoding="utf-8")
        assert "key=" not in js and "'X-Admin-Key'" in js
        assert "sessionStorage" in js  # the key lives for the tab only
        assert "q=" not in js and "X-Lookup-Query" in js

    def test_page_is_private_and_strict(self):
        html = (_FRONTEND / "partners-ops.html").read_text(encoding="utf-8")
        assert 'name="robots" content="noindex, nofollow"' in html
        csp = _re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
        assert "script-src 'self';" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert "<script>" not in html  # no inline script at all

    def test_every_action_sends_a_name(self):
        js = (_FRONTEND / "partners-ops.js").read_text(encoding="utf-8")
        assert js.count("const who = actor();") >= 3 and "json = { actor: who }" in js


# ---------------------------------------------------------------------------
# stage 5: QR codes, sticker sheets, notifications
# ---------------------------------------------------------------------------
from greenbay_ai_evaluator.partners import notify as pnotify, qr as pqr  # noqa: E402


@pytest.fixture
def inline_threads(monkeypatch):
    import threading

    class _Inline:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            if self._t:
                self._t()

    monkeypatch.setattr(threading, "Thread", _Inline)


class TestQrCodes:
    def test_codes_name_the_shop_or_channel_and_never_collide(self):
        assert pqr.new_code(MIKA, channel="shop", shop="roysambu", taken=[]) == "MK-ROY1"
        assert pqr.new_code(MIKA, channel="shop", shop="roysambu", taken=["MK-ROY1"]) == "MK-ROY2"
        assert pqr.new_code(VPLUS, channel="website", shop=None, taken=["VP-WEB1", "VP-WEB2"]) == "VP-WEB3"
        assert pqr.new_code(MIKA, channel="social", shop=None, taken=[]) == "MK-SOC1"

    def test_short_link(self, monkeypatch):
        monkeypatch.delenv("PARTNER_PUBLIC_BASE", raising=False)
        assert pqr.short_url("MK-ROY1") == "https://evaluate.greenbay.market/q/MK-ROY1"
        assert pqr.short_url_display("MK-ROY1") == "evaluate.greenbay.market/q/MK-ROY1"

    def test_single_qr_svg(self):
        svg = pqr.qr_svg("MK-WEB1")
        assert svg.startswith("<svg") and "https://evaluate.greenbay.market/q/MK-WEB1" in svg and 'd="M' in svg

    def test_matrix_is_error_correction_q_with_a_quiet_zone(self):
        import segno
        q = segno.make(pqr.short_url("MK-WEB1"), error="q", boost_error=False)
        assert q.error == "Q"
        m = pqr._matrix(pqr.short_url("MK-WEB1"))
        assert not any(m[0]) and not any(m[3]) and not any(r[0] for r in m)  # four blank modules round it

    @pytest.mark.parametrize("cfg", [MIKA, VPLUS], ids=lambda c: c.slug)
    def test_sheet_svg(self, cfg):
        codes = [{"code": f"{cfg.ref_prefix}-ROY{i}", "label": f"Roysambu <counter> {i}"} for i in range(1, 6)]
        svg = pqr.sheet_svg(cfg, codes)
        assert svg.count("data:image/png;base64,") == 2  # each official logo embedded once
        assert svg.count('href="#pLogo"') == 5 and svg.count('href="#gbMark"') == 5
        assert 'height="594.0mm"' in svg  # five stickers = two A4 pages
        assert "Roysambu &lt;counter&gt; 1" in svg and "<counter>" not in svg
        assert all(f"evaluate.greenbay.market/q/{c['code']}" in svg for c in codes)
        assert "Valued and bought by GreenBay" in svg and pqr.sticker_text(cfg)[0] in svg
        assert "—" not in svg

    @pytest.mark.parametrize("cfg", [MIKA, VPLUS], ids=lambda c: c.slug)
    def test_sheet_pdf(self, cfg):
        pdf = pqr.sheet_pdf(cfg, [{"code": f"{cfg.ref_prefix}-WEB{i}", "label": "x"} for i in range(1, 6)])
        assert pdf.startswith(b"%PDF") and pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= 2

    def test_print_logos_are_png_files_in_the_partner_folder(self):
        for cfg in (MIKA, VPLUS):
            path = pqr._print_logo(cfg)
            assert path.endswith(".png") and f"/partners/{cfg.slug}/" in path
            pqr._png_info(path)  # reads and checks the PNG signature

    def test_assets_cannot_escape_the_frontend_folder(self):
        with pytest.raises(ValueError):
            pqr._asset_path("/app/../../app/config.py")
        with pytest.raises(ValueError):
            pqr._asset_path("/etc/passwd")


class TestQrAdmin:
    def test_make_list_print_and_turn_off(self, ops, db):  # noqa: F811
        r = ops.post("/tradein/partners/admin/qr", headers=ops.admin,
                     json={"actor": "Allan", "partner": "mika", "channel": "shop", "shop": "Roysambu", "placement": "Counter"})
        assert r.status_code == 200, r.text
        q = r.json()
        assert q["code"] == "MK-ROY1" and q["shop"] == "roysambu" and q["placement"] == "counter"
        assert db.query(PartnerQrCode).filter_by(code="MK-ROY1").one().created_by == "Allan"
        listed = {x["code"] for x in ops.get("/tradein/partners/admin/qr?partner=mika", headers=ops.admin).json()["items"]}
        assert listed == {"MK-WEB1", "MK-WEB2", "MK-ROY1"}
        pdf = ops.get("/tradein/partners/admin/qr-sheet?partner=mika&codes=MK-ROY1,MK-WEB1", headers=ops.admin)
        assert pdf.headers["content-type"] == "application/pdf" and pdf.content.startswith(b"%PDF")
        svg = ops.get("/tradein/partners/admin/qr-sheet?partner=mika&format=svg", headers=ops.admin)
        assert svg.headers["content-type"].startswith("image/svg+xml") and "MK-ROY1" in svg.text
        one = ops.get("/tradein/partners/admin/qr/MK-ROY1/svg", headers=ops.admin)
        assert one.status_code == 200 and "/q/MK-ROY1" in one.text
        off = ops.post("/tradein/partners/admin/qr/MK-ROY1/deactivate", headers=ops.admin, json={"actor": "Allan"})
        assert off.json()["active"] is False
        assert ops.get("/tradein/partners/admin/qr-sheet?partner=mika&codes=MK-ROY1", headers=ops.admin).status_code == 404

    def test_refusals(self, ops):
        base = {"actor": "Allan", "partner": "mika", "placement": "counter"}
        assert ops.post("/tradein/partners/admin/qr", headers=ops.admin, json={**base, "channel": "shop"}).status_code == 422
        assert ops.post("/tradein/partners/admin/qr", headers=ops.admin, json={**base, "channel": "radio"}).status_code == 422
        assert ops.post("/tradein/partners/admin/qr/MK-WEB1/deactivate", headers=ops.admin,
                        json={"actor": "Allan"}).status_code == 409
        assert ops.get("/tradein/partners/admin/qr-sheet?partner=mika&codes=VP-WEB1", headers=ops.admin).status_code == 404
        assert ops.post("/tradein/partners/admin/qr", headers=ops.ro, json={**base, "channel": "website"}).status_code == 403


class TestNotifications:
    def _credit(self, db, **kw):  # noqa: F811
        c = _issued(db, MIKA, amount=6000, **kw)
        return c

    @pytest.mark.parametrize("event", ["accepted", "needs_validation", "credit_issued", "credit_used"])
    def test_every_event_has_a_plain_email(self, db, event):  # noqa: F811
        c = self._credit(db)
        subject, text, html = pnotify.build(event, pnotify.snapshot(c, amount=1000, note="R-1", actor="CX Achieng"))
        assert subject.startswith("[Mika swap]") and c.reference in subject
        for part in (subject, text, html):
            assert "—" not in part and "instant" not in part.lower()
        assert "partners-ops.html" in text and "<script" not in html

    def test_recipients_come_from_config(self):
        assert pnotify.recipients("credit_used", "mika") == MIKA.notify["credit_used"]
        assert pnotify.recipients("nonsense", "mika") == [] and pnotify.recipients("accepted", "nobody") == []

    def test_ops_actions_email_the_named_people(self, ops, db, sent_emails, inline_threads):  # noqa: F811
        c = _accepted(db, amount=8000)
        base = f"/tradein/partners/admin/credits/{c.id}"
        ops.post(base + "/start-validation", headers=ops.admin, json={"actor": "Tech", "method": "technician"})
        ops.post(base + "/validate", headers=ops.admin, json={"actor": "Tech"})
        code = ops.post(base + "/issue", headers=ops.admin, json={"actor": "CX"}).json()["voucher_code"]
        ops.post("/tradein/partners/admin/redeem", headers=ops.admin,
                 json={"actor": "CX", "partner": "mika", "voucher_code": code, "amount": 1000})
        subjects = [s[0] for s in sent_emails]
        assert [s.split("]")[1].split(":")[0].strip() for s in subjects] == [
            "Check booked", "Credit issued, call the customer", "Credit used"]
        assert all(s[3] == MIKA.notify["credit_used"] for s in sent_emails)
        assert code in sent_emails[1][1]

    def test_customer_accept_emails_once_and_labels_the_whatsapp_alert(self, db, kenya, sent_emails):  # noqa: F811
        out = router.evaluate_trade_in(_req(partner={"slug": "mika"}, age_years=13), db)
        before = len(kenya.get("notify", []))  # the evaluation's own alert
        router.accept_offer(out.session_id, {"accepted_amount": 500}, db)
        router.accept_offer(out.session_id, {"accepted_amount": 500}, db)  # double tap
        accepted = [s for s in sent_emails if "Swap accepted" in s[0]]
        assert len(accepted) == 1 and "KES 500" in accepted[0][0]
        assert len(kenya["notify"]) - before == 1  # one WhatsApp alert, not one per tap

    def test_whatsapp_payload_names_the_partner_and_reference(self, db):  # noqa: F811
        c = _accepted(db, amount=8000)
        p = pnotify.whatsapp_payload(c, {"outcome": "ACCEPTED", "ai_price": 1})
        assert p["outcome"] == "ACCEPTED, Mika swap" and p["ai_price"] == 8000 and c.reference in p["extra"]

    def test_test_records_are_never_emailed(self, db, sent_emails, inline_threads):  # noqa: F811
        c = _accepted(db, amount=8000, model="ZZTEST-DEMO-X")
        pnotify.notify("accepted", c)
        assert sent_emails == []
