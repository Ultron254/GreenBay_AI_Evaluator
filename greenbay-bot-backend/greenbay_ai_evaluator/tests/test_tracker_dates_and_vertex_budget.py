"""
Two fixes found from GreenBay Pulse (10 Nov 2026):

  - the tracker sheet's Date column. Every writer (append_tracker_row, the
    backfill of missing rows, the date-repair pass) wrote "DD/MM/YYYY" with
    USER_ENTERED into a month-first sheet on the host's UTC clock, so every
    day of 12 or under became a real date with day and month swapped (1 Nov
    read as 11 Jan). All of them now go through sheet_date_cell: text
    (leading apostrophe), day first, on the Nairobi day; still read day first.
  - Vertex (Gemini 2.5 Flash) spent its 512-token output budget thinking and
    returned no JSON, so its price was on about 4% of evaluations (Sentinel
    D06). Thinking is off for Flash, a small budget for Pro, more output room.

Deterministic: no network, no sheet, no Vertex call.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from greenbay_ai_evaluator.api import router as rt
from greenbay_ai_evaluator.services import google_sheets_service as gss
from greenbay_ai_evaluator.services import reference_data_service as rds
from greenbay_ai_evaluator.services import vertex_ai_service as vx
from greenbay_ai_evaluator.services.sheet_to_airtable_human_price_sync import (
    _parse_sheet_date,
)

NAIROBI = timezone(timedelta(hours=3))
HEADER = ["Date", "Item", "Model", "Condition", "Age", "New price", "AI Price",
          "AI Confidence", "Customer Price", "Internal Team Price",
          "Final Price Offered", "Accepted / Rejected", "Notes"]


# ------------------------------------------------------------ the helper

@pytest.mark.parametrize("when,cell", [
    # 22:30 UTC on 31 Oct is 01:30 on 1 Nov in Nairobi.
    (datetime(2026, 10, 31, 22, 30, tzinfo=timezone.utc), "'01/11/2026"),
    (datetime(2026, 10, 31, 22, 30), "'01/11/2026"),        # naive = UTC
    ("2026-10-31T22:30:00.000Z", "'01/11/2026"),             # Airtable
    ("2026-11-09T08:00:00+00:00", "'09/11/2026"),
    ("2026-11-09", "'09/11/2026"),
    (date(2026, 11, 9), "'09/11/2026"),
    ("09/11/2026", "'09/11/2026"),                           # day first
    ("'09/11/2026", "'09/11/2026"),                          # idempotent
    ("9/11/26", "'09/11/2026"),
])
def test_one_helper_gives_text_day_first_on_the_nairobi_day(when, cell):
    assert rds.sheet_date_cell(when) == cell
    assert _parse_sheet_date(cell.lstrip("'")).strftime("'%d/%m/%Y") == cell


def test_no_date_or_an_unreadable_one_is_today_in_nairobi():
    today = datetime.now(NAIROBI).strftime("'%d/%m/%Y")
    assert rds.sheet_date_cell() == today
    assert rds.sheet_date_cell("") == today
    assert rds.sheet_date_cell("not a date") == today
    # The old writer routes through the same helper.
    assert gss.sheet_date_cell() == today


# ------------------------------------------------------------ the writers

class _WS:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.appended = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def append_row(self, row, value_input_option=None):
        self.appended.append((list(row), value_input_option))
        self.rows.append(list(row))


class _GC:
    def __init__(self, ws):
        self.ws = ws

    def open_by_key(self, key):
        return self

    def worksheet(self, name):
        return self.ws


@pytest.fixture
def sheet(monkeypatch):
    ws = _WS([HEADER])
    monkeypatch.setattr(rds, "_get_gspread_client", lambda: _GC(ws))
    monkeypatch.setattr(rds, "_tracker_write_layout", None)
    return ws


def _date_of(ws, i=-1):
    row, how = ws.appended[i]
    return row[HEADER.index("Date")], how


def test_append_tracker_row_writes_the_date_as_nairobi_text(sheet):
    assert rds.append_tracker_row(item="Samsung Fridge", ai_price=17000)
    cell, how = _date_of(sheet)
    assert how == "USER_ENTERED"
    assert cell == datetime.now(NAIROBI).strftime("'%d/%m/%Y")
    # A date the caller passes is put in the same form.
    assert rds.append_tracker_row(item="LG TV", date_str="01/11/2026")
    assert _date_of(sheet)[0] == "'01/11/2026"
    assert rds.append_tracker_row(item="LG TV", date_str="2026-10-31T22:30:00Z")
    assert _date_of(sheet)[0] == "'01/11/2026"


def test_the_backfill_of_missing_rows_writes_the_nairobi_day(sheet, monkeypatch):
    from greenbay_ai_evaluator.services import airtable_service
    monkeypatch.setattr(airtable_service, "_get_config", lambda: object())
    monkeypatch.setattr(airtable_service, "list_records_paginated",
                        lambda cfg, fields=None: [{"id": "rec1", "fields": {
                            "Product Name": "Hisense refrigerator",
                            "AI Evaluated Price (KES)": 12000,
                            "Notes": "Ref: abcdef12",
                            "Date Submitted": "2026-10-31T22:30:00.000Z"}}])
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    prog = rt._backfill_sheet_missing(dry_run=False)
    assert prog["appended"] == 1
    assert _date_of(sheet)[0] == "'01/11/2026"


def test_the_date_repair_pass_writes_text_or_leaves_the_cell_alone():
    assert rt._tracker_date_cell("2026-10-31T22:30:00.000Z") == "'01/11/2026"
    assert rt._tracker_date_cell("2026-11-09") == "'09/11/2026"
    assert rt._tracker_date_cell("") == ""
    assert rt._tracker_date_cell("garbage") == ""


# ------------------------------------------------------------ Vertex

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


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        return self._payload


def _fake_vertex(monkeypatch, answers):
    import httpx
    from types import SimpleNamespace
    sent = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            sent.append(dict(json["generationConfig"]))
            return answers.pop(0)
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    import app.config as cfgmod
    monkeypatch.setattr(cfgmod, "get_settings", lambda: SimpleNamespace(
        google_vertex_credentials_file="/x.json",
        google_vertex_model="gemini-2.5-flash"))
    monkeypatch.setattr(vx, "_get_access_token", lambda: "t")
    monkeypatch.setattr(vx, "_build_endpoint", lambda: "https://vertex.invalid")
    return sent


def test_a_refused_thinking_config_is_dropped_and_the_call_made_once_more(monkeypatch):
    ok = {"candidates": [{"content": {"parts": [{"text": json.dumps({
        "condition_grade": "B", "condition_score": 7,
        "estimated_price_kes": 15000, "observations": "fine"})}]}}]}
    sent = _fake_vertex(monkeypatch, [
        _Resp(400, text='{"error": "Unknown name \\"thinkingConfig\\""}'),
        _Resp(200, ok)])
    out = asyncio.run(vx.vertex_evaluate(["aGVsbG8="], category="fridge"))
    assert out["estimated_price_kes"] == 15000
    assert "thinkingConfig" in sent[0] and "thinkingConfig" not in sent[1]


def test_an_answer_with_no_json_says_why(monkeypatch, caplog):
    blocked = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}],
               "promptFeedback": {"blockReason": "OTHER"}}
    _fake_vertex(monkeypatch, [_Resp(200, blocked)])
    assert asyncio.run(vx.vertex_evaluate(["aGVsbG8="])) is None
    assert "blocked OTHER" in (vx._status.get("last_error") or "")
    assert "finish SAFETY" in (vx._status.get("last_error") or "")
