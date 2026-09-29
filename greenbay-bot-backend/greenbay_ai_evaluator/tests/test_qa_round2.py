"""Regression guards for defects found in the second QA round."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from greenbay_ai_evaluator.api.security import (
    presented_is_admin_key,
    verify_readonly_or_admin_key,
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ROUTER = Path(__file__).resolve().parents[1] / "api" / "router.py"
FREE_TEXT = ("brand", "model", "sellerName", "sellerPhone", "issues", "otherDescription")


@pytest.fixture(scope="module")
def app_js() -> str:
    return (FRONTEND / "app.js").read_text(encoding="utf-8", errors="replace")


class TestNoSanitiserBypass:
    """Every write into state.answers for a free-text field must be sanitised.

    The model number was assigned straight from the /model-lookup OCR result,
    which is model-generated text read off a customer photo, and was then
    rendered into innerHTML on the next chat mirror.
    """

    def test_ocr_model_is_sanitised_before_storing(self, app_js: str):
        assert "const ocrModel = sanitiseText(" in app_js
        assert "state.answers.model = ocrModel" in app_js

    def test_no_raw_free_text_assignment(self, app_js: str):
        # Locals proved safe because the sanitiser produced them, e.g.
        # `const ocrModel = sanitiseText(...)`.
        safe_locals = set(re.findall(r"(?:const|let|var)\s+(\w+)\s*=\s*sanitiseText\(", app_js))
        offenders = []
        for line_no, line in enumerate(app_js.splitlines(), 1):
            m = re.search(r"state\.answers\.([A-Za-z]+)\s*=\s*([^;]+);", line)
            if not m or m.group(1) not in FREE_TEXT:
                continue
            value = m.group(2).strip()
            if "sanitiseText(" in value or value in safe_locals:
                continue
            offenders.append(f"app.js:{line_no}: {line.strip()}")
        assert not offenders, "unsanitised free-text assignment:\n" + "\n".join(offenders)

    @pytest.mark.parametrize("snippet", [
        "Brand: <strong>${escapeHtml(a.brand)}",
        "Model: <strong>${escapeHtml(a.model)}",
    ])
    def test_chat_mirror_escapes(self, app_js: str, snippet: str):
        # localStorage is attacker-writable, so escape at render too.
        assert snippet in app_js


class TestBilledProbeNeedsAdminKey:
    """?fresh=true spends Perplexity and Gemini credits on every call.

    Pulse polls this route roughly 170 times a day with the read-only key; a
    loop or a copied URL must not be able to bill the account.
    """

    @pytest.fixture
    def keys(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_KEY", "admin-key-for-test-0123456789")
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", "readonly-key-for-test-0123456789")
        return {
            "admin": "admin-key-for-test-0123456789",
            "ro": "readonly-key-for-test-0123456789",
        }

    @pytest.fixture
    def client(self):
        """Mirror the real route's gating without booting the whole app."""
        app = FastAPI()

        @app.get("/probe")
        def probe(
            fresh: bool = Query(False),
            _: bool = Depends(verify_readonly_or_admin_key),
            is_admin: bool = Depends(presented_is_admin_key),
        ):
            if fresh and not is_admin:
                raise HTTPException(status_code=403, detail="admin key required")
            return {"fresh": fresh}

        return TestClient(app)

    def test_readonly_key_cannot_force_fresh(self, keys, client):
        r = client.get("/probe", params={"fresh": "true"}, headers={"X-Admin-Key": keys["ro"]})
        assert r.status_code == 403

    def test_readonly_key_still_reads_cached(self, keys, client):
        r = client.get("/probe", headers={"X-Admin-Key": keys["ro"]})
        assert r.status_code == 200

    def test_admin_key_may_force_fresh(self, keys, client):
        r = client.get("/probe", params={"fresh": "true"}, headers={"X-Admin-Key": keys["admin"]})
        assert r.status_code == 200 and r.json()["fresh"] is True

    def test_the_real_route_has_this_gate(self):
        src = ROUTER.read_text(encoding="utf-8", errors="replace")
        block = src.split('@evaluator_router.get("/health/services")')[1][:1800]
        assert "presented_is_admin_key" in block
        assert "if fresh and not is_admin" in block
