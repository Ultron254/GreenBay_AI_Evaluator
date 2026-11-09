"""
Two fixes found from GreenBay Pulse (10 Nov 2026):

  - the tracker sheet's Date column: written "01/11/26" with USER_ENTERED
    into a month-first sheet, every day of 12 or under became a real date
    with day and month swapped (1 Nov read as 11 Jan). It is now written as
    text, day first, on the Nairobi calendar, and still parses day first.
  - Vertex (Gemini 2.5 Flash) spent its 512-token output budget thinking and
    returned no JSON, so its price was on about 4% of evaluations (Sentinel
    D06). Thinking is off for Flash, a small budget for Pro, more output room.

Deterministic: no network, no sheet, no Vertex call.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from greenbay_ai_evaluator.services import google_sheets_service as gss
from greenbay_ai_evaluator.services import vertex_ai_service as vx
from greenbay_ai_evaluator.services.sheet_to_airtable_human_price_sync import (
    _parse_sheet_date,
)


def test_the_date_cell_is_text_day_first_on_the_nairobi_calendar():
    # 22:30 UTC on 31 Oct is 01:30 on 1 Nov in Nairobi.
    late = datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc).astimezone(
        ZoneInfo("Africa/Nairobi"))
    cell = gss.sheet_date_cell(late)
    assert cell == "'01/11/2026"
    # What the sheet shows (the apostrophe is not part of the value) is read
    # back day first by the evaluator's own reader.
    assert _parse_sheet_date(cell.lstrip("'")).isoformat() == "2026-11-01"
    assert gss.sheet_date_cell().startswith("'")


def test_flash_models_answer_without_thinking_and_with_room_to_answer():
    cfg = vx._generation_config("gemini-2.5-flash")
    assert cfg["thinkingConfig"] == {"thinkingBudget": 0}
    assert cfg["maxOutputTokens"] >= 1024
    assert cfg["responseMimeType"] == "application/json"


def test_pro_models_keep_a_small_thinking_budget_and_older_models_none():
    assert vx._generation_config("gemini-2.5-pro")["thinkingConfig"] == {
        "thinkingBudget": vx.VERTEX_PRO_THINKING_BUDGET}
    assert "thinkingConfig" not in vx._generation_config("gemini-1.5-flash-002")
    assert "thinkingConfig" not in vx._generation_config(None)
