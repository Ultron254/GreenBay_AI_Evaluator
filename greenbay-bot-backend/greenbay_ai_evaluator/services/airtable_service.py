"""
Airtable integration service — backup data repository for evaluated products.

This module provides a write-only persistence path to an Airtable base.
Every completed evaluation fires a (non-blocking) POST here so the team
has a second source of truth in addition to the Postgres database and
Google Sheet.

IMPORTANT: Airtable is a backup data repository only. Read helpers
(`read_comparables`, `get_historical_accuracy_ratio`) are exposed for
future use but MUST NOT be wired into the live pricing path until
explicitly approved.

Fallback behavior: if Airtable is unreachable, the payload is written
as JSON to `/app/airtable_fallback/` and replayed by
`retry_failed_writes()` at next startup.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Module state (thread-safe counters for dashboard)
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_state: dict[str, Any] = {
    "last_successful_write": None,  # ISO timestamp
    "last_error": None,              # str
    "writes_today": 0,
    "writes_today_date": None,       # YYYY-MM-DD string
    "total_writes": 0,
    "total_failures": 0,
}


FALLBACK_DIR = Path("/app/airtable_fallback")
RETRY_BACKOFFS = (1, 3, 9)           # seconds between retries
REQUEST_TIMEOUT = 15                 # per-request timeout
RATE_LIMIT_DELAY = 0.25              # 5 req/sec max → 250ms spacing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_config() -> Optional[dict[str, str]]:
    """Return Airtable config dict or None if not configured."""
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception as e:
        logger.warning(f"Airtable: settings unavailable ({e})")
        return None

    token = settings.airtable_api_token
    base = settings.airtable_base_id
    table = settings.airtable_table_name

    if not token or not base:
        return None

    return {
        "token": token,
        "base_id": base,
        "table": table,
    }


def _base_url(cfg: dict[str, str]) -> str:
    """Build the Airtable REST endpoint URL (URL-encoded table name)."""
    from urllib.parse import quote
    return f"https://api.airtable.com/v0/{cfg['base_id']}/{quote(cfg['table'])}"


def _auth_headers(cfg: dict[str, str]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['token']}",
        "Content-Type": "application/json",
    }


def _bump_today_counter(success: bool) -> None:
    """Increment daily write counter, resetting at UTC midnight."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _state_lock:
        if _state["writes_today_date"] != today:
            _state["writes_today_date"] = today
            _state["writes_today"] = 0
        if success:
            _state["writes_today"] += 1
            _state["total_writes"] += 1
            _state["last_successful_write"] = datetime.now(timezone.utc).isoformat()
        else:
            _state["total_failures"] += 1


def _count_fallback_records() -> int:
    try:
        if not FALLBACK_DIR.exists():
            return 0
        return sum(1 for p in FALLBACK_DIR.iterdir() if p.is_file() and p.suffix == ".json")
    except Exception:
        return 0


def _dump_fallback(payload: dict) -> Optional[Path]:
    """Persist a failed Airtable write to disk for later retry."""
    try:
        FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        fp = FALLBACK_DIR / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
        fp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        logger.warning(f"Airtable: payload queued to fallback at {fp.name}")
        return fp
    except Exception as e:
        logger.error(f"Airtable: could not write fallback file: {e}")
        return None


def _post_record(cfg: dict[str, str], fields: dict) -> tuple[bool, Optional[str]]:
    """POST a single record to Airtable. Returns (success, error_message)."""
    import requests

    body = {"fields": fields, "typecast": True}
    try:
        resp = requests.post(
            _base_url(cfg),
            headers=_auth_headers(cfg),
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:240]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _patch_record(cfg: dict[str, str], record_id: str, fields: dict) -> tuple[bool, Optional[str]]:
    """PATCH fields on an existing Airtable record."""
    import requests

    from urllib.parse import quote

    table = quote(cfg["table"])
    url = f"https://api.airtable.com/v0/{cfg['base_id']}/{table}/{record_id}"
    body = {"fields": fields, "typecast": True}
    try:
        resp = requests.patch(
            url,
            headers=_auth_headers(cfg),
            json=body,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:240]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def list_records_paginated(
    cfg: dict[str, str],
    *,
    fields: Optional[list[str]] = None,
    page_size: int = 100,
) -> list[dict]:
    """Fetch all records from the evaluations table (paginated GET).

    Returns raw Airtable records ``{"id", "fields", ...}``.
    """
    import requests

    out: list[dict] = []
    offset: Optional[str] = None
    params_base: dict[str, Any] = {"pageSize": max(1, min(page_size, 100))}
    if fields:
        for i, fname in enumerate(fields):
            params_base[f"fields[{i}]"] = fname

    while True:
        params = dict(params_base)
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(
                _base_url(cfg),
                headers=_auth_headers(cfg),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Airtable list_records_paginated: HTTP {resp.status_code} {resp.text[:200]}"
                )
                break
            payload = resp.json()
        except Exception as e:
            logger.warning(f"Airtable list_records_paginated failed: {e}")
            break

        out.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break
        time.sleep(RATE_LIMIT_DELAY)

    return out


def patch_in_house_evaluator_price(record_id: str, price_kes: float) -> bool:
    """Set In-House Evaluator Price (KES) on one record."""
    cfg = _get_config()
    if cfg is None:
        return False
    field = "In-House Evaluator Price (KES)"
    ok, err = _patch_record(cfg, record_id, {field: float(price_kes)})
    if ok:
        return True
    logger.warning(f"Airtable PATCH {record_id}: {err}")
    return False


# ---------------------------------------------------------------------------
# Public API — WRITE
# ---------------------------------------------------------------------------
def write_evaluation(data: dict) -> bool:
    """Write a single evaluation record to Airtable.

    Retries 3 times with 1s / 3s / 9s backoff. On total failure, payload is
    saved to `/app/airtable_fallback/` for later replay via `retry_failed_writes`.

    The caller is responsible for assembling the `data` dict. Expected keys
    (all optional — missing keys simply omit the column):

        - Date Submitted (str, ISO-8601)
        - Product Name (str)
        - Brand (str)
        - Model Number (str)
        - Category (str)
        - Age (Years) (number)
        - Condition (str — e.g. "A", "B", "Good")
        - Product Images (list[{"url": "..."}])
        - Customer Asking Price (KES) (number)
        - AI Evaluated Price (KES) (number)
        - Vertex AI Price (KES) (number)
        - In-House Evaluator Price (KES) (number — usually left blank)

    Never raises. Never blocks the caller more than ~13 seconds total.
    """
    cfg = _get_config()
    if cfg is None:
        logger.debug("Airtable: not configured, skipping write")
        return False

    # Strip any keys whose value is None / empty string so we don't overwrite
    # Airtable fields with blanks (Airtable treats "" differently from missing).
    fields = {k: v for k, v in data.items() if v is not None and v != ""}
    if not fields:
        return False

    last_err: Optional[str] = None
    for attempt, backoff in enumerate((0,) + RETRY_BACKOFFS, start=1):
        if backoff:
            time.sleep(backoff)
        ok, err = _post_record(cfg, fields)
        if ok:
            _bump_today_counter(success=True)
            logger.info(
                f"Airtable: wrote record (attempt {attempt}) — "
                f"{fields.get('Product Name', '?')}"
            )
            with _state_lock:
                _state["last_error"] = None
            return True
        last_err = err
        logger.warning(f"Airtable write attempt {attempt} failed: {err}")

    # All retries exhausted → persist to fallback
    _bump_today_counter(success=False)
    with _state_lock:
        _state["last_error"] = last_err
    _dump_fallback(fields)
    return False


def retry_failed_writes() -> int:
    """Replay any pending fallback JSON files into Airtable.

    Called at startup. Returns the number of records successfully recovered.
    Files that succeed are deleted; files that fail again stay on disk for
    the next boot.
    """
    cfg = _get_config()
    if cfg is None:
        return 0
    if not FALLBACK_DIR.exists():
        return 0

    recovered = 0
    pending_files = sorted(
        p for p in FALLBACK_DIR.iterdir()
        if p.is_file() and p.suffix == ".json"
    )
    if not pending_files:
        return 0

    logger.info(f"Airtable: attempting to replay {len(pending_files)} pending records")

    for fp in pending_files:
        try:
            fields = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Airtable replay: skipping unreadable {fp.name}: {e}")
            continue

        ok, err = _post_record(cfg, fields)
        if ok:
            try:
                fp.unlink()
            except Exception:
                pass
            recovered += 1
            _bump_today_counter(success=True)
        else:
            logger.warning(f"Airtable replay: {fp.name} still failing — {err}")
            _bump_today_counter(success=False)
            # Stop replaying on first failure to avoid hammering the API
            # during a known outage.
            break

        time.sleep(RATE_LIMIT_DELAY)

    if recovered:
        logger.info(f"Airtable: recovered {recovered} pending records")
    return recovered


# ---------------------------------------------------------------------------
# Public API — READ (defined but NOT wired into the live pricing path)
# ---------------------------------------------------------------------------
def read_comparables(
    brand: str,
    category: str,
    limit: int = 10,
) -> list[dict]:
    """Query Airtable for past evaluations that have a human price filled in.

    Returns a list of dicts: `{ai_price, human_price, condition_grade, age_years}`.

    NOTE: This is NOT currently called from the live evaluator. It is exposed
    for future use once the learning loop is formally enabled.
    """
    cfg = _get_config()
    if cfg is None or not brand or not category:
        return []

    import requests

    safe_brand = brand.replace("'", "\\'")
    safe_cat = category.replace("'", "\\'")
    formula = (
        f"AND({{Brand}}='{safe_brand}',"
        f"{{Category}}='{safe_cat}',"
        f"{{In-House Evaluator Price (KES)}}>0)"
    )
    params = {
        "filterByFormula": formula,
        "maxRecords": max(1, min(int(limit or 10), 100)),
        "sort[0][field]": "Date Submitted",
        "sort[0][direction]": "desc",
    }

    try:
        resp = requests.get(
            _base_url(cfg),
            headers=_auth_headers(cfg),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"Airtable read_comparables: HTTP {resp.status_code}")
            return []
        records = resp.json().get("records", [])
    except Exception as e:
        logger.warning(f"Airtable read_comparables failed: {e}")
        return []

    out: list[dict] = []
    for r in records:
        f = r.get("fields", {})
        out.append({
            "ai_price": f.get("AI Evaluated Price (KES)") or 0,
            "human_price": f.get("In-House Evaluator Price (KES)") or 0,
            "condition_grade": f.get("Condition") or "",
            "age_years": f.get("Age (Years)") or 0,
        })
    return out


def get_historical_accuracy_ratio(brand: str, category: str) -> Optional[float]:
    """Return mean(human_price / ai_price) from recent Airtable comparables.

    Returns None if fewer than 3 valid data points are available.

    Used **after** ``reconcile_retail_price()`` in the evaluator router as a
    separate learning multiplier (Sheet/DB drive reconciliation; Airtable ratio
    corrects systematic AI-vs-human bias without double-counting Sheet rows).
    """
    comps = read_comparables(brand=brand, category=category, limit=25)
    ratios: list[float] = []
    for c in comps:
        ai = float(c.get("ai_price") or 0)
        human = float(c.get("human_price") or 0)
        if ai > 0 and human > 0:
            ratios.append(human / ai)
    if len(ratios) < 3:
        return None
    return round(sum(ratios) / len(ratios), 4)


# ---------------------------------------------------------------------------
# Public API — STATUS (for dashboard)
# ---------------------------------------------------------------------------
def get_service_status() -> dict[str, Any]:
    """Return a small status dict suitable for the ops dashboard."""
    cfg = _get_config()
    configured = cfg is not None

    pending = _count_fallback_records()
    with _state_lock:
        snap = dict(_state)

    if not configured:
        status = "disabled"
    elif snap["last_error"] or pending > 0:
        status = "degraded"
    elif snap["last_successful_write"]:
        status = "online"
    else:
        status = "idle"

    total_records: Optional[int] = None
    # Optionally query Airtable for total record count (cheap: 1 page, pageSize=1)
    if configured:
        try:
            import requests
            resp = requests.get(
                _base_url(cfg),
                headers=_auth_headers(cfg),
                params={"pageSize": 1, "fields[]": []},
                timeout=5,
            )
            if resp.status_code == 200:
                # Airtable doesn't expose a total count; approximate via offset
                # presence. For a simple "is reachable" signal we treat 200 as OK.
                total_records = None
        except Exception:
            pass

    return {
        "service": "airtable",
        "status": status,
        "configured": configured,
        "base_id": (cfg or {}).get("base_id"),
        "table": (cfg or {}).get("table"),
        "last_successful_write": snap.get("last_successful_write"),
        "last_error": snap.get("last_error"),
        "writes_today": snap.get("writes_today", 0),
        "total_writes": snap.get("total_writes", 0),
        "total_failures": snap.get("total_failures", 0),
        "pending_fallback_records": pending,
        "total_records": total_records,
    }


# ---------------------------------------------------------------------------
# Convenience: fire-and-forget from sync code
# ---------------------------------------------------------------------------
def write_evaluation_async(data: dict) -> None:
    """Spawn a daemon thread to write_evaluation. Never blocks, never raises."""
    try:
        t = threading.Thread(target=write_evaluation, args=(data,), daemon=True)
        t.start()
    except Exception as e:
        logger.warning(f"Airtable async write dispatch failed: {e}")
