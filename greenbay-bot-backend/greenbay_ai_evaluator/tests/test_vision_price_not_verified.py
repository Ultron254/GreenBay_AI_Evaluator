"""A vision guess at retail price must not count as market verification.

Chain this broke: vision estimates a retail price, router tags it
"google_lens", reconcile_retail_price treated that as a genuine new-price
signal, new_price_verified went True, the STEP 11c floor armed on that
anchor and did `confidence_score = min(confidence_score, 75.0)` -- below the
80 auto-accept threshold, so the evaluation was routed to a human. Every live
probe returned exactly 75.0.
"""

from __future__ import annotations

from greenbay_ai_evaluator.engine.offer_engine import reconcile_retail_price

VISION_SOURCES = ["google_lens", "vision_estimate", "vision", "ai_estimate"]
REAL_SOURCES = ["user_entered", "shopify", "receipt", "invoice"]


def _reconcile(**kw):
    base = dict(
        frontend_price=68_000.0,
        frontend_source="",
        internet_price=None,
        historical_avg=None,
        expert_avg=None,
        comparables_avg=None,
        marketplace_avg=None,
        shopify_avg=None,
    )
    base.update(kw)
    return reconcile_retail_price(**base)


def _frontend(result):
    return next(s for s in result["sources"] if s["source"] == "frontend")


class TestVisionGuessIsNotVerification:
    def test_vision_sources_are_marked_estimate(self):
        for src in VISION_SOURCES:
            assert _frontend(_reconcile(frontend_source=src)).get("is_estimate") is True, src

    def test_vision_only_is_not_new_price_verified(self):
        for src in VISION_SOURCES:
            assert _reconcile(frontend_source=src)["new_price_verified"] is False, src

    def test_vision_guess_excluded_from_the_blend(self):
        for src in VISION_SOURCES:
            assert _frontend(_reconcile(frontend_source=src)).get("excluded_from_blend") is True, src

    def test_vision_guess_does_not_count_as_a_real_source(self):
        r = _reconcile(frontend_source="google_lens")
        assert r["num_real_sources"] == 0

    def test_case_and_whitespace_insensitive(self):
        assert _frontend(_reconcile(frontend_source="  GOOGLE_LENS  ")).get("is_estimate") is True


class TestGenuineSourcesStillVerify:
    def test_a_real_frontend_price_still_verifies(self):
        for src in REAL_SOURCES:
            r = _reconcile(frontend_source=src)
            assert r["new_price_verified"] is True, src
            assert _frontend(r).get("is_estimate") is not True, src

    def test_grounded_internet_price_verifies_even_with_a_vision_frontend(self):
        # The vision guess is still ignored, but a real grounded lookup arms
        # the anchor legitimately.
        r = _reconcile(frontend_source="google_lens", internet_price=150_000.0)
        assert r["new_price_verified"] is True
        assert r["reconciled_price"] == 150_000

    def test_vision_guess_never_drags_the_blend(self):
        r = _reconcile(frontend_source="google_lens", internet_price=150_000.0)
        # 68k vision guess must not average against the 150k grounded price.
        assert r["reconciled_price"] == 150_000


class TestPreviouslyKnownEstimatesUnchanged:
    def test_category_default_still_an_estimate(self):
        assert _frontend(_reconcile(frontend_source="category_default")).get("is_estimate") is True

    def test_whatsapp_estimate_still_an_estimate(self):
        assert _frontend(_reconcile(frontend_source="whatsapp_category_estimate")).get("is_estimate") is True
