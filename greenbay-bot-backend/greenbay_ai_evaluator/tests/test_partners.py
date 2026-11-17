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
