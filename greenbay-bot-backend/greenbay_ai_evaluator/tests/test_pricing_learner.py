"""
Tests for the closed-deal detection in pricing_learner.

Guards the substring bug where ``"accept" in status`` wrongly treated
"Not Accepted" / "Unaccepted" / "Rejected" as accepted deals — which would
corrupt the historical learning signal.
"""

import pytest

from greenbay_ai_evaluator.services.pricing_learner import _is_accepted_status


@pytest.mark.parametrize("status", [
    "Accepted",
    "accepted",
    "ACCEPT",
    "Yes",
    "Closed",
    "Done",
    "Completed",
])
def test_accepted_variants(status):
    assert _is_accepted_status(status) is True


@pytest.mark.parametrize("status", [
    "Not Accepted",
    "not accepted",
    "Unaccepted",
    "Rejected",
    "Declined",
    "No",
    "Pending",
    "",
    None,
    "   ",
])
def test_not_accepted_variants(status):
    assert _is_accepted_status(status) is False
