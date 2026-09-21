"""
Security helpers for admin/ops endpoints (issue #14).

Provides a single, hardened gate for privileged endpoints (live health-check,
Airtable backfill, dashboard). Improvements over the previous inline check:

- Constant-time key comparison (hmac.compare_digest) — defeats timing attacks.
- Key accepted via the ``X-Admin-Key`` header OR the ``key`` query param
  (header preferred; query kept for backward-compatible browser access).
- A key that is public is refused (Sep 2026). The legacy default and the
  env.example placeholder are committed to this repository, so anyone who can
  read it could call the destructive admin routes (delete-eval-record,
  purge-test-records). When DASHBOARD_KEY is unset, blank or one of those
  values, every admin route answers 503 with the fix, and start-up logs an
  error. The customer-facing wizard is unaffected. Local development can opt
  back in with ALLOW_DEFAULT_DASHBOARD_KEY=true.

This intentionally does NOT touch the public seller-facing endpoints, so it
adds zero friction for normal users.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Query
from loguru import logger

_DEFAULT_KEY = "greenbay-admin-2026"

# Key values that are public because they are committed to this repository:
# the legacy default above and the placeholder in env.example. Neither can
# protect anything. Add to this set if another value is ever committed.
PUBLIC_KEY_VALUES = frozenset({_DEFAULT_KEY, "change-me-to-a-long-random-secret"})

ADMIN_KEY_NOT_SET_DETAIL = (
    "admin routes are disabled: DASHBOARD_KEY is unset or is a value committed "
    "to the repository. Set a private DASHBOARD_KEY in the server environment "
    "and restart."
)


def admin_key() -> str:
    """The configured admin key (env DASHBOARD_KEY), defaulting to the legacy value."""
    return os.environ.get("DASHBOARD_KEY", _DEFAULT_KEY)


def default_key_allowed() -> bool:
    """ALLOW_DEFAULT_DASHBOARD_KEY=true: accept a public key value anyway.
    For local development only; never set it on a server."""
    return (os.environ.get("ALLOW_DEFAULT_DASHBOARD_KEY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def admin_key_problem() -> str:
    """"" when the admin key can be trusted, else why it cannot.

    Read on every request, like the key itself, so fixing the environment and
    restarting is all it takes; nothing is cached."""
    if default_key_allowed():
        return ""
    key = admin_key().strip()
    if not key:
        return "DASHBOARD_KEY is blank"
    if key in PUBLIC_KEY_VALUES:
        return "DASHBOARD_KEY is a value committed to the repository"
    return ""


def readonly_key() -> str:
    """The optional read-only key (env READONLY_DASHBOARD_KEY), or "" if unset.

    Issued to Pulse (EVALUATOR_CHANGES_REQUIRED.md, E1). It opens only the
    read-only GET routes that use ``verify_readonly_or_admin_key``; every
    mutating route keeps ``verify_admin_key`` and rejects it.
    """
    return (os.environ.get("READONLY_DASHBOARD_KEY") or "").strip()


def readonly_key_problem() -> str:
    """"" when the read-only key can be trusted (or is unset), else why not.
    A read-only key with a problem is ignored, never accepted."""
    key = readonly_key()
    if not key:
        return ""
    if key in PUBLIC_KEY_VALUES and not default_key_allowed():
        return "READONLY_DASHBOARD_KEY is a value committed to the repository"
    if _same(key, admin_key().strip()):
        return "READONLY_DASHBOARD_KEY equals DASHBOARD_KEY, so it would not be read-only"
    return ""


def log_key_hygiene() -> bool:
    """Log the state of both keys once at start-up. Returns True when admin
    routes are usable. Never logs a key value."""
    ok = True
    problem = admin_key_problem()
    if problem:
        ok = False
        logger.error(f"[FAIL] Admin key: {problem}. {ADMIN_KEY_NOT_SET_DETAIL}")
    elif default_key_allowed() and admin_key().strip() in PUBLIC_KEY_VALUES:
        logger.warning(
            "[WARN] Admin key: a public value is accepted because "
            "ALLOW_DEFAULT_DASHBOARD_KEY is set. Never set it on a server."
        )
    else:
        logger.info("[OK]   Admin key: private DASHBOARD_KEY configured")
    ro_problem = readonly_key_problem()
    if ro_problem:
        logger.error(f"[FAIL] Read-only key: {ro_problem}; it is being ignored")
    elif readonly_key():
        logger.info("[OK]   Read-only key: READONLY_DASHBOARD_KEY configured")
    else:
        logger.info("[OK]   Read-only key: not set (admin key only)")
    return ok


def _same(a: str, b: str) -> bool:
    """Constant-time equality. Compared as bytes: hmac.compare_digest raises
    TypeError on a non-ASCII str, which would turn a junk key into a 500."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _presented_key(x_admin_key: str, key: str) -> str:
    return (x_admin_key or key or "").strip()


_refusal_logged = False


def _refuse_if_admin_key_is_public() -> None:
    """503 (not 403) so the operator sees a configuration fault, with the fix,
    rather than a wrong-password answer. Says nothing an attacker can use: the
    public values no longer open anything."""
    global _refusal_logged
    problem = admin_key_problem()
    if problem:
        # Once at ERROR (start-up already logged it too), then DEBUG: these
        # routes are on the internet, and a loop of requests must not be able
        # to fill the log.
        if not _refusal_logged:
            _refusal_logged = True
            logger.error(f"SECURITY: admin route refused: {problem}.")
        else:
            logger.debug(f"SECURITY: admin route refused: {problem}.")
        raise HTTPException(status_code=503, detail=ADMIN_KEY_NOT_SET_DETAIL)


def verify_admin_key(
    key: str = Query("", alias="key", max_length=200),
    x_admin_key: str = Header("", alias="X-Admin-Key"),
) -> bool:
    """FastAPI dependency: allow only requests presenting the correct admin key.

    Raises 403 otherwise. Use as ``_: bool = Depends(verify_admin_key)``.
    The read-only key is NOT accepted here: this is the gate for every route
    that mutates anything (POST/PUT/PATCH/DELETE, backfills, repairs, purges).
    """
    _refuse_if_admin_key_is_public()
    expected = admin_key().strip()
    presented = _presented_key(x_admin_key, key)

    if not presented or not _same(presented, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    return True


def verify_readonly_or_admin_key(
    key: str = Query("", alias="key", max_length=200),
    x_admin_key: str = Header("", alias="X-Admin-Key"),
) -> bool:
    """FastAPI dependency for read-only GET routes: admin key OR read-only key.

    Same header (``X-Admin-Key``) and query param as ``verify_admin_key``.
    When READONLY_DASHBOARD_KEY is unset only the admin key is accepted, so
    behaviour is unchanged until the second key is configured.
    """
    expected_readonly = "" if readonly_key_problem() else readonly_key()
    presented = _presented_key(x_admin_key, key)

    if not presented:
        raise HTTPException(status_code=403, detail="forbidden")
    # A sound read-only key keeps working while the admin key is unset or
    # public, so Pulse is not cut off by the admin-key guard.
    if expected_readonly and _same(presented, expected_readonly):
        return True
    _refuse_if_admin_key_is_public()
    if _same(presented, admin_key().strip()):
        return True
    raise HTTPException(status_code=403, detail="forbidden")
