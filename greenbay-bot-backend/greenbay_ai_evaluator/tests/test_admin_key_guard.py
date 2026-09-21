"""
Tests for the admin-key guard (Sep 2026, Pulse Sentinel D03 "Dashboard key is
the committed default and also gates destructive admin routes"):

  - a DASHBOARD_KEY that is unset, blank, the committed default or the
    env.example placeholder opens NOTHING: admin routes answer 503 with the fix
  - a sound READONLY_DASHBOARD_KEY keeps the read routes working meanwhile
  - a read-only key that is public, or equal to the admin key, is ignored
  - ALLOW_DEFAULT_DASHBOARD_KEY=true restores the old behaviour for local work
  - the destructive routes (delete, purge, backfills, repairs) are admin only
  - app/main.py no longer keeps its own copy of the key or its own comparison
  - no key value is ever logged

Deterministic: FastAPI's TestClient on a two-route app, plus inspection of the
real router's dependency graph. No DB, no network.
"""

import re
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from greenbay_ai_evaluator.api import security
from greenbay_ai_evaluator.api.security import (
    ADMIN_KEY_NOT_SET_DETAIL,
    PUBLIC_KEY_VALUES,
    verify_admin_key,
    verify_readonly_or_admin_key,
)

ADMIN = "admin-key-for-tests-0123456789"
READONLY = "readonly-key-for-tests-9876543210"
COMMITTED_DEFAULT = security._DEFAULT_KEY

# POST routes that delete or rewrite records. Each must be admin only.
DESTRUCTIVE_PATHS = {
    "/admin/delete-eval-record",
    "/admin/purge-test-records",
    "/admin/airtable-backfill",
    "/admin/airtable-patch-missing",
    "/admin/fix-justification-field",
    "/admin/airtable-fill-newprice",
    "/admin/backfill-justification",
    "/admin/reprice-historical",
    "/admin/sync-internal-prices",
    "/admin/backfill-sheet-newprice",
    "/admin/backfill-sheet-missing",
    "/admin/sheet-repair",
    "/admin/recalibrate-ratios",
    "/admin/tracker-selftest",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("DASHBOARD_KEY", "READONLY_DASHBOARD_KEY", "ALLOW_DEFAULT_DASHBOARD_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/read")
    def read(_: bool = Depends(verify_readonly_or_admin_key)):
        return {"ok": "read"}

    @app.post("/purge")
    def purge(_: bool = Depends(verify_admin_key)):
        return {"ok": "purge"}

    return TestClient(app)


def _h(key):
    return {"X-Admin-Key": key}


class TestPublicAdminKeyOpensNothing:
    @pytest.mark.parametrize("configured", [None, "", "   ", *sorted(PUBLIC_KEY_VALUES)])
    def test_admin_route_refuses_with_503_and_the_fix(self, monkeypatch, client, configured):
        if configured is not None:
            monkeypatch.setenv("DASHBOARD_KEY", configured)
        for presented in (COMMITTED_DEFAULT, configured or "", "anything"):
            r = client.post("/purge", headers=_h(presented))
            assert r.status_code == 503, (configured, presented)
            assert r.json()["detail"] == ADMIN_KEY_NOT_SET_DETAIL

    def test_the_committed_default_is_public(self):
        assert COMMITTED_DEFAULT in PUBLIC_KEY_VALUES
        # The placeholder the operator is told to replace is public too.
        example = (Path(__file__).resolve().parents[2] / "env.example").read_text()
        placeholder = re.search(r"^DASHBOARD_KEY=(.*)$", example, re.MULTILINE).group(1).strip()
        assert placeholder in PUBLIC_KEY_VALUES

    def test_read_route_refuses_the_default_too(self, client):
        assert client.get("/read", headers=_h(COMMITTED_DEFAULT)).status_code == 503
        assert client.get("/read", params={"key": COMMITTED_DEFAULT}).status_code == 503

    def test_no_key_presented_is_still_403(self, client):
        assert client.get("/read").status_code == 403

    def test_private_admin_key_works_everywhere(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        assert client.post("/purge", headers=_h(ADMIN)).status_code == 200
        assert client.get("/read", headers=_h(ADMIN)).status_code == 200
        assert client.post("/purge", headers=_h(COMMITTED_DEFAULT)).status_code == 403
        assert client.post("/purge", headers=_h("wrong")).status_code == 403

    def test_surrounding_whitespace_in_the_env_value_is_ignored(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", f"  {ADMIN}\n")
        assert client.post("/purge", headers=_h(ADMIN)).status_code == 200

    def test_non_ascii_key_is_a_403_not_a_500(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        assert client.post("/purge", params={"key": "clé-secrète"}).status_code == 403


class TestReadonlyKeyWhileAdminKeyIsPublic:
    def test_sound_readonly_key_keeps_read_routes_open(self, monkeypatch, client):
        # Pulse must not be cut off because the admin key was never rotated.
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", READONLY)
        assert client.get("/read", headers=_h(READONLY)).status_code == 200
        assert client.get("/read", headers=_h(COMMITTED_DEFAULT)).status_code == 503
        assert client.post("/purge", headers=_h(READONLY)).status_code == 503

    def test_readonly_key_never_opens_a_destructive_route(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", READONLY)
        assert client.post("/purge", headers=_h(READONLY)).status_code == 403
        assert client.post("/purge", params={"key": READONLY}).status_code == 403

    @pytest.mark.parametrize("public", sorted(PUBLIC_KEY_VALUES))
    def test_public_readonly_key_is_ignored(self, monkeypatch, client, public):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", public)
        assert client.get("/read", headers=_h(public)).status_code == 403
        assert security.readonly_key_problem()

    def test_readonly_key_equal_to_admin_key_is_flagged(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", ADMIN)
        assert "would not be read-only" in security.readonly_key_problem()


class TestLocalDevelopmentEscapeHatch:
    def test_default_key_works_only_when_explicitly_allowed(self, monkeypatch, client):
        assert client.post("/purge", headers=_h(COMMITTED_DEFAULT)).status_code == 503
        monkeypatch.setenv("ALLOW_DEFAULT_DASHBOARD_KEY", "true")
        assert client.post("/purge", headers=_h(COMMITTED_DEFAULT)).status_code == 200
        assert client.post("/purge", headers=_h("wrong")).status_code == 403

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
    def test_anything_but_a_clear_yes_keeps_the_guard_on(self, monkeypatch, value):
        monkeypatch.setenv("ALLOW_DEFAULT_DASHBOARD_KEY", value)
        assert security.admin_key_problem()


class TestStartupLog:
    def _capture(self):
        from loguru import logger
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="INFO")
        return logger, sink, lines

    def test_public_key_is_logged_as_a_failure(self):
        logger, sink, lines = self._capture()
        try:
            assert security.log_key_hygiene() is False
        finally:
            logger.remove(sink)
        assert any("[FAIL] Admin key" in line for line in lines)

    def test_no_key_value_is_ever_logged(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", READONLY)
        logger, sink, lines = self._capture()
        try:
            assert security.log_key_hygiene() is True
        finally:
            logger.remove(sink)
        text = "\n".join(lines)
        assert "[OK]   Admin key" in text and "[OK]   Read-only key" in text
        assert ADMIN not in text and READONLY not in text


class TestRealRouterWiring:
    @pytest.fixture(scope="class")
    def routes(self):
        from greenbay_ai_evaluator.api.router import evaluator_router
        return {
            (route.path, method): {dep.call for dep in route.dependant.dependencies}
            for route in evaluator_router.routes
            for method in route.methods
        }

    def test_every_destructive_route_is_admin_only(self, routes):
        for path in DESTRUCTIVE_PATHS:
            deps = routes[(path, "POST")]
            assert verify_admin_key in deps, path
            assert verify_readonly_or_admin_key not in deps, path

    def test_every_admin_post_is_on_the_list(self, routes):
        # A new /admin POST must be added to DESTRUCTIVE_PATHS (and so be
        # checked above), not slip in ungated.
        admin_posts = {p for (p, m) in routes if m == "POST" and p.startswith("/admin/")}
        assert admin_posts == DESTRUCTIVE_PATHS

    def test_no_admin_route_is_ungated(self, routes):
        for (path, method), deps in routes.items():
            if path.startswith("/admin/") or path in ("/health/services", "/dashboard/metrics",
                                                      "/dashboard/calibration"):
                assert deps & {verify_admin_key, verify_readonly_or_admin_key}, (path, method)


class TestMainAppUsesTheSharedGate:
    _MAIN = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(encoding="utf-8")

    def test_no_second_copy_of_the_default_key(self):
        assert COMMITTED_DEFAULT not in self._MAIN
        assert 'os.environ.get("DASHBOARD_KEY"' not in self._MAIN

    def test_dashboard_routes_depend_on_the_shared_gate(self):
        assert "verify_admin_key as _require_dashboard_key" in self._MAIN
        gated = re.findall(r'@app\.get\("(/dashboard/[a-z\-]+)"\)', self._MAIN)
        assert set(gated) == {"/dashboard/status", "/dashboard/resources", "/dashboard/recent",
                              "/dashboard/analytics", "/dashboard/learning-diagnostic"}
        assert self._MAIN.count("Depends(_require_dashboard_key)") == len(gated)

    def test_startup_reports_key_hygiene(self):
        assert "log_key_hygiene()" in self._MAIN
