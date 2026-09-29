"""Guards for the Airtable durability defects found in QA round two."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenbay_ai_evaluator.services import airtable_service as svc


class _Resp:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class TestTransientFailuresAreRetried:
    """A rate limit is usually a one-second problem.

    Before this, any 429 or 5xx dropped the record straight onto the disk
    fallback queue, so a burst of evaluations quietly stopped reaching
    Airtable and Pulse under-reported the day.
    """

    @pytest.fixture(autouse=True)
    def _no_sleeping(self, monkeypatch):
        monkeypatch.setattr(svc.time, "sleep", lambda *_: None)

    def _run(self, monkeypatch, responses):
        calls = {"n": 0}

        def fake_post(*_a, **_kw):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return r

        import requests
        monkeypatch.setattr(requests, "post", fake_post)
        cfg = {"base_id": "appTEST", "table": "Appliance Evaluations", "token": "tok"}
        ok, err = svc._post_record(cfg, {"Brand": "Samsung"})
        return ok, err, calls["n"]

    def test_429_then_success(self, monkeypatch):
        ok, err, n = self._run(monkeypatch, [_Resp(429, "slow down"), _Resp(200)])
        assert ok is True and err is None
        assert n == 2, "should have retried the rate limit"

    def test_500_then_success(self, monkeypatch):
        ok, _err, n = self._run(monkeypatch, [_Resp(503, "upstream"), _Resp(201)])
        assert ok is True and n == 2

    def test_retry_after_header_is_honoured(self, monkeypatch):
        slept = []
        monkeypatch.setattr(svc.time, "sleep", lambda d: slept.append(d))
        self._run(monkeypatch, [_Resp(429, "x", {"Retry-After": "7"}), _Resp(200)])
        assert slept == [7.0]

    def test_retries_are_bounded(self, monkeypatch):
        ok, _err, n = self._run(monkeypatch, [_Resp(429, "always")])
        assert ok is False
        assert n <= 8, "retry loop must terminate"

    def test_permanent_error_is_not_retried(self, monkeypatch):
        ok, err, n = self._run(monkeypatch, [_Resp(401, "bad token")])
        assert ok is False and "401" in err
        assert n == 1, "auth failures must fail fast, not spin"


class TestFallbackQueueIsBounded:
    """Nothing else deletes from the fallback directory, so without a prune a
    long outage fills the disk and takes the whole app down."""

    @pytest.fixture
    def tmp_queue(self, tmp_path, monkeypatch):
        monkeypatch.setattr(svc, "FALLBACK_DIR", tmp_path)
        return tmp_path

    def test_old_entries_are_pruned(self, tmp_queue: Path):
        fresh = tmp_queue / "fresh.json"
        fresh.write_text("{}")
        old = tmp_queue / "old.json"
        old.write_text("{}")
        ancient = time.time() - 60 * 86400
        import os
        os.utime(old, (ancient, ancient))

        removed = svc._prune_fallback(max_age_days=30)
        assert removed == 1
        assert fresh.exists() and not old.exists()

    def test_file_count_is_capped(self, tmp_queue: Path):
        for i in range(12):
            (tmp_queue / f"{i:03d}.json").write_text("{}")
        svc._prune_fallback(max_age_days=365, max_files=5)
        assert len(list(tmp_queue.iterdir())) == 5

    def test_prune_never_raises(self, monkeypatch):
        monkeypatch.setattr(svc, "FALLBACK_DIR", Path("/definitely/not/here"))
        assert svc._prune_fallback() == 0
