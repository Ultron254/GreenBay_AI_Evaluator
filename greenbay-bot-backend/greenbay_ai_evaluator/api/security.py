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


def verify_admin_key(
    key: str = Query("", alias="key", max_length=200),
    x_admin_key: str = Header("", alias="X-Admin-Key"),
) -> bool:
    """FastAPI dependency: allow only requests presenting the correct admin key.

    Raises 403 otherwise. Use as ``_: bool = Depends(verify_admin_key)``.
    """
    expected = admin_key()
    presented = (x_admin_key or key or "").strip()

    if expected == _DEFAULT_KEY:
        logger.warning(
            "SECURITY: DASHBOARD_KEY is still the default value. Set a strong, "
            "unique DASHBOARD_KEY in the environment to protect admin endpoints."
        )

    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="forbidden")
    return True
