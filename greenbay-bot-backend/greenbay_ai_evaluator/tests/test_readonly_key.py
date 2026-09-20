"""
Tests for the read-only dashboard key (Sep 2026, EVALUATOR_CHANGES_REQUIRED E1):

  - READONLY_DASHBOARD_KEY opens the seven read-only GET routes
  - it is rejected by verify_admin_key, so no mutating route accepts it
  - the admin key keeps working everywhere
  - an unset read-only key changes nothing
  - the evaluator router wires exactly the seven read routes to the relaxed
    gate and never a POST/PUT/PATCH/DELETE route

Deterministic: the route tests use FastAPI's TestClient on a small app made
of the two dependencies (no DB, no network); the wiring test inspects the
real router's dependency graph without running any handler.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from greenbay_ai_evaluator.api import security
from greenbay_ai_evaluator.api.security import (
    verify_admin_key,
    verify_readonly_or_admin_key,
)

ADMIN = "admin-key-for-tests-0123456789"
READONLY = "readonly-key-for-tests-9876543210"

# The routes E1 lists as readable with the read-only key (paths relative to
# the /tradein prefix that app.main applies).
READONLY_GET_PATHS = {
    "/health/services",
    "/dashboard/metrics",
    "/dashboard/calibration",
    "/admin/accuracy-report",
    "/admin/tracker-analysis",
    "/admin/airtable-audit",
    "/admin/contact-data-stats",
}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
    monkeypatch.setenv("READONLY_DASHBOARD_KEY", READONLY)


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/read")
    def read(_: bool = Depends(verify_readonly_or_admin_key)):
        return {"ok": "read"}

    @app.get("/write-get")
    def write_get(_: bool = Depends(verify_admin_key)):
        return {"ok": "write-get"}

    @app.post("/write")
    def write(_: bool = Depends(verify_admin_key)):
        return {"ok": "write"}

    return TestClient(app)


class TestReadOnlyKeyOnReadRoutes:
    def test_readonly_key_opens_read_route(self, keys, client):
        r = client.get("/read", headers={"X-Admin-Key": READONLY})
        assert r.status_code == 200
        assert r.json() == {"ok": "read"}

    def test_admin_key_still_opens_read_route(self, keys, client):
        assert client.get("/read", headers={"X-Admin-Key": ADMIN}).status_code == 200

    def test_readonly_key_via_query_param(self, keys, client):
        assert client.get("/read", params={"key": READONLY}).status_code == 200

    def test_wrong_key_rejected(self, keys, client):
        assert client.get("/read", headers={"X-Admin-Key": "nope"}).status_code == 403

    def test_missing_key_rejected(self, keys, client):
        assert client.get("/read").status_code == 403

    def test_unset_readonly_key_accepts_only_admin(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.delenv("READONLY_DASHBOARD_KEY", raising=False)
        assert client.get("/read", headers={"X-Admin-Key": ADMIN}).status_code == 200
        assert client.get("/read", headers={"X-Admin-Key": READONLY}).status_code == 403
        # An empty header must never match an empty (unset) read-only key.
        assert client.get("/read", headers={"X-Admin-Key": ""}).status_code == 403

    def test_blank_readonly_key_is_treated_as_unset(self, monkeypatch, client):
        monkeypatch.setenv("DASHBOARD_KEY", ADMIN)
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", "   ")
        assert client.get("/read", headers={"X-Admin-Key": "   "}).status_code == 403
        assert client.get("/read", headers={"X-Admin-Key": ""}).status_code == 403


class TestReadOnlyKeyOnMutatingRoutes:
    def test_readonly_key_rejected_on_post(self, keys, client):
        assert client.post("/write", headers={"X-Admin-Key": READONLY}).status_code == 403

    def test_readonly_key_rejected_on_admin_only_get(self, keys, client):
        assert client.get("/write-get", headers={"X-Admin-Key": READONLY}).status_code == 403

    def test_admin_key_accepted_on_post(self, keys, client):
        r = client.post("/write", headers={"X-Admin-Key": ADMIN})
        assert r.status_code == 200
        assert r.json() == {"ok": "write"}

    def test_readonly_key_rejected_on_post_via_query(self, keys, client):
        assert client.post("/write", params={"key": READONLY}).status_code == 403


class TestDependencyFunctions:
    def test_readonly_key_helper_strips_and_defaults(self, monkeypatch):
        monkeypatch.delenv("READONLY_DASHBOARD_KEY", raising=False)
        assert security.readonly_key() == ""
        monkeypatch.setenv("READONLY_DASHBOARD_KEY", "  abc  ")
        assert security.readonly_key() == "abc"

    def test_verify_admin_key_never_accepts_readonly(self, keys):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            verify_admin_key(key="", x_admin_key=READONLY)
        assert exc.value.status_code == 403
        assert verify_admin_key(key="", x_admin_key=ADMIN) is True

    def test_verify_readonly_or_admin_accepts_both(self, keys):
        assert verify_readonly_or_admin_key(key="", x_admin_key=READONLY) is True
        assert verify_readonly_or_admin_key(key="", x_admin_key=ADMIN) is True


class TestRouterWiring:
    """Inspect the real evaluator router: which gate does each keyed route use?"""

    @staticmethod
    def _gates(route) -> set:
        return {
            dep.call
            for dep in route.dependant.dependencies
            if dep.call in (verify_admin_key, verify_readonly_or_admin_key)
        }

    @pytest.fixture(scope="class")
    def keyed_routes(self):
        from greenbay_ai_evaluator.api.router import evaluator_router

        out = []
        for route in evaluator_router.routes:
            gates = self._gates(route)
            if gates:
                out.append((route.path, set(route.methods), gates))
        assert out, "expected keyed routes on the evaluator router"
        return out

    def test_exactly_the_seven_read_routes_accept_the_readonly_key(self, keyed_routes):
        relaxed = {
            path for path, methods, gates in keyed_routes
            if verify_readonly_or_admin_key in gates
        }
        assert relaxed == READONLY_GET_PATHS

    def test_relaxed_routes_are_get_only(self, keyed_routes):
        for path, methods, gates in keyed_routes:
            if verify_readonly_or_admin_key in gates:
                assert methods == {"GET"}, f"{path} must be GET only"

    def test_every_mutating_route_keeps_the_admin_gate(self, keyed_routes):
        for path, methods, gates in keyed_routes:
            if methods - {"GET", "HEAD"}:
                assert gates == {verify_admin_key}, f"{path} {methods} must stay admin only"

    def test_mutating_gets_keep_the_admin_gate(self, keyed_routes):
        # GET routes that trigger or report on writes (backfills, repairs,
        # repricing) are not in E1's read list and must stay admin only.
        for path, methods, gates in keyed_routes:
            if methods == {"GET"} and path not in READONLY_GET_PATHS:
                assert gates == {verify_admin_key}, f"{path} must stay admin only"
