"""
Security helpers for admin/ops endpoints (issue #14).

Provides a single, hardened gate for privileged endpoints (live health-check,
Airtable backfill, dashboard). Improvements over the previous inline check:

- Constant-time key comparison (hmac.compare_digest) — defeats timing attacks.
- Key accepted via the ``X-Admin-Key`` header OR the ``key`` query param
  (header preferred; query kept for backward-compatible browser access).
- Loud warning when the default key is still in use, so weak config is visible.

This intentionally does NOT touch the public seller-facing endpoints, so it
adds zero friction for normal users.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Query
from loguru import logger

_DEFAULT_KEY = "greenbay-admin-2026"


def admin_key() -> str:
    """The configured admin key (env DASHBOARD_KEY), defaulting to the legacy value."""
    return os.environ.get("DASHBOARD_KEY", _DEFAULT_KEY)


def readonly_key() -> str:
    """The optional read-only key (env READONLY_DASHBOARD_KEY), or "" if unset.

    Issued to Pulse (EVALUATOR_CHANGES_REQUIRED.md, E1). It opens only the
    read-only GET routes that use ``verify_readonly_or_admin_key``; every
    mutating route keeps ``verify_admin_key`` and rejects it.
    """
    return (os.environ.get("READONLY_DASHBOARD_KEY") or "").strip()


def _presented_key(x_admin_key: str, key: str) -> str:
    return (x_admin_key or key or "").strip()


def _warn_if_default_admin_key(expected: str) -> None:
    if expected == _DEFAULT_KEY:
        logger.warning(
            "SECURITY: DASHBOARD_KEY is still the default value. Set a strong, "
            "unique DASHBOARD_KEY in the environment to protect admin endpoints."
        )


def verify_admin_key(
    key: str = Query("", alias="key", max_length=200),
    x_admin_key: str = Header("", alias="X-Admin-Key"),
) -> bool:
    """FastAPI dependency: allow only requests presenting the correct admin key.

    Raises 403 otherwise. Use as ``_: bool = Depends(verify_admin_key)``.
    The read-only key is NOT accepted here: this is the gate for every route
    that mutates anything (POST/PUT/PATCH/DELETE, backfills, repairs, purges).
    """
    expected = admin_key()
    presented = _presented_key(x_admin_key, key)
    _warn_if_default_admin_key(expected)

    if not presented or not hmac.compare_digest(presented, expected):
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
    expected_admin = admin_key()
    expected_readonly = readonly_key()
    presented = _presented_key(x_admin_key, key)
    _warn_if_default_admin_key(expected_admin)

    if not presented:
        raise HTTPException(status_code=403, detail="forbidden")
    if hmac.compare_digest(presented, expected_admin):
        return True
    if expected_readonly and hmac.compare_digest(presented, expected_readonly):
        return True
    raise HTTPException(status_code=403, detail="forbidden")
