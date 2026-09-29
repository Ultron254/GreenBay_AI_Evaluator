"""The lead email must be actionable and must not overstate what we hold."""

from __future__ import annotations

from greenbay_ai_evaluator.services.email_service import _phone_actions, _photo_status, _strip_html


class TestPhoneIsOneTapActionable:
    def test_kenyan_local_format_is_normalised_for_whatsapp(self):
        out = _phone_actions("0712345678")
        assert "wa.me/254712345678" in out

    def test_international_format_preserved(self):
        assert "wa.me/254700123456" in _phone_actions("+254700123456")

    def test_bare_country_code_format(self):
        assert "wa.me/254700000000" in _phone_actions("254700000000")

    def test_includes_a_call_link(self):
        assert "tel:+254712345678" in _phone_actions("0712345678")

    def test_missing_phone_does_not_render_a_dead_link(self):
        out = _phone_actions("")
        assert "wa.me" not in out and out == "—"

    def test_non_numeric_phone_is_escaped_not_linked(self):
        out = _phone_actions("<script>alert(1)</script>")
        assert "<script>" not in out


class TestPhotoStatusIsHonest:
    def test_reports_the_count_when_links_exist(self):
        assert "2 attached" in _photo_status(["u1", "u2"], {})

    def test_customer_sent_none_is_stated_plainly(self):
        assert "none submitted" in _photo_status([], {})

    def test_stored_but_unlinkable_is_not_reported_as_none(self):
        """Photos we hold but cannot presign is an S3 fault, not an empty lead."""
        out = _photo_status([], {"image_s3_keys": ["k1"]})
        assert "none" not in out.lower()
        assert "S3" in out


class TestPlainTextFallback:
    def test_anchors_are_stripped_for_the_text_part(self):
        assert _strip_html(_phone_actions("0712345678")) .startswith("0712345678")

    def test_no_markup_leaks_into_plain_text(self):
        out = _strip_html(_phone_actions("0712345678"))
        assert "<" not in out and ">" not in out
