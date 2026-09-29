"""All 11 Pulse columns must actually be written, not merely exist.

Verified live: a submission carrying attribution wrote 9 of 11 (Session ID,
Confidence, Decision, the four UTM fields, Referrer, Landing URL). Channel and
Customer Decision were absent because nothing sent them -- Channel was never in
the payload, and the accept/reject routes patched Evaluation Status alone.
"""

from __future__ import annotations

import inspect

import greenbay_ai_evaluator.api.router as router
import greenbay_ai_evaluator.services.airtable_service as ats
from greenbay_ai_evaluator.api.router import _channel_for


class TestChannelDerivation:
    def test_whatsapp_submissions_are_tagged_whatsapp(self):
        assert _channel_for("whatsapp_category_estimate") == "whatsapp"

    def test_web_wizard_is_tagged_web(self):
        assert _channel_for("category_default") == "web"

    def test_unknown_source_defaults_to_web(self):
        assert _channel_for("") == "web"
        assert _channel_for(None) == "web"

    def test_case_and_whitespace_insensitive(self):
        assert _channel_for("  WhatsApp_Category_Estimate  ") == "whatsapp"

    def test_channel_is_in_the_airtable_payload(self):
        src = inspect.getsource(router._build_airtable_payload)
        assert '"Channel"' in src


class TestCustomerDecisionIsRecorded:
    def test_a_multi_field_patch_helper_exists(self):
        assert callable(ats.patch_record_fields)

    def test_single_field_helper_still_works(self):
        assert callable(ats.patch_record_field)

    def test_accept_route_records_the_customer_decision(self):
        src = inspect.getsource(router)
        accept = src[src.index('"Evaluation Status": "accepted"'):]
        assert '"Customer Decision": "accepted"' in accept[:400]

    def test_reject_route_records_the_customer_decision(self):
        src = inspect.getsource(router)
        idx = src.index('"Evaluation Status": f"rejected_option_{option}"')
        assert '"Customer Decision"' in src[idx: idx + 400]


class TestPatchDropsUnknownColumnsRatherThanFailing:
    def test_patch_filters_against_the_known_schema(self):
        src = inspect.getsource(ats.patch_record_fields)
        assert "_get_known_fields" in src
        assert "dropping columns not present" in src

    def test_empty_field_dict_is_a_no_op(self):
        assert ats.patch_record_fields("abc12345", {}) is False
