"""
Live service health-check (v6.1).

Unlike the config-presence dashboard, this module makes a REAL lightweight
call to each external dependency and reports whether it actually works right
now. Use it to diagnose silent failures in production (expired keys, missing
google-auth, unreachable Vertex, Airtable 422s, etc.).

Every probe is wrapped so it can never raise — it returns a dict:
    {"ok": bool, "detail": str, "latency_ms": int | None}

Exposed via GET /tradein/health/services (key-gated in the router).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from loguru import logger


def _timed(fn: Callable[[], tuple[bool, str]]) -> dict[str, Any]:
    """Run a probe, capture timing, and never raise."""
    start = time.time()
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001 — probes must never crash the endpoint
        ok, detail = False, f"{type(e).__name__}: {e}"
    return {
        "ok": ok,
        "detail": detail,
        "latency_ms": int((time.time() - start) * 1000),
    }


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------
def _probe_anthropic() -> tuple[bool, str]:
    from app.config import get_settings
    s = get_settings()
    if not s.anthropic_api_key:
        return False, "ANTHROPIC_API_KEY not configured"
    import requests
    # Minimal models list call — cheap, validates the key without spending tokens.
    resp = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": s.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=10,
    )
    if resp.status_code == 200:
        return True, f"OK (model configured: {s.anthropic_primary_model})"
    return False, f"HTTP {resp.status_code}: {resp.text[:160]}"


def _probe_vertex_gemini() -> tuple[bool, str]:
    from app.config import get_settings
    s = get_settings()
    if not s.google_vertex_credentials_file:
        return False, "GOOGLE_VERTEX_CREDENTIALS_FILE not configured"
    # First confirm google-auth is importable — this is the exact dependency
    # that was missing in the May audit ("No module named 'google.oauth2'").
    try:
        import google.oauth2.service_account  # noqa: F401
        import google.auth.transport.requests  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"google-auth not installed: {e}"

    from greenbay_ai_evaluator.services.vertex_ai_service import (
        _get_access_token,
        _build_endpoint,
    )
    token = _get_access_token()  # raises if creds bad
    import requests
    endpoint = _build_endpoint()
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with the single word OK."}]}],
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 8},
    }
    resp = requests.post(
        endpoint,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=20,
    )
    if resp.status_code == 200:
        return True, f"OK (model: {s.google_vertex_model}, region: {s.google_vertex_region})"
    return False, f"HTTP {resp.status_code}: {resp.text[:160]}"


# The probes price one well-known product and pass its size, exactly as a real
# evaluation does: both prompts insist on a size match, so a probe with no size
# nudges the model towards answering "no price".
_PROBE_PRODUCT = dict(
    brand="Samsung", model="UA43T5300", category="tv_monitor", country="KE",
    size_value=43, size_unit="inch",
)


def _probe_gemini_search_grounding() -> tuple[bool, str]:
    """Probe the Gemini half of the NEW-PRICE lookup (googleSearch tool).

    Calls Gemini alone. It used to call search_internet_price, which merges
    Gemini with Sonar, so a working Sonar could mask a dead Gemini (and the
    Sonar key was billed twice per health-check)."""
    from greenbay_ai_evaluator.services.market_price_service import gemini_price_research
    res = gemini_price_research(**_PROBE_PRODUCT)
    if res.launch_price and res.launch_price > 0:
        return True, f"OK — grounded price KES {res.launch_price:,.0f} from {len(res.sources)} sources"
    return False, (
        f"Gemini search grounding returned NO price [{res.status or 'unknown'}] "
        f"{res.status_detail} (would silently fall back to a guess)"
    )


def _probe_perplexity_sonar() -> tuple[bool, str]:
    """Probe the second grounded new-price source (Perplexity Sonar).

    Optional dependency: when PERPLEXITY_API_KEY is not set the system runs
    Gemini-only by design, so 'not configured' reports ok=True and simply says
    so. When the key IS set, run a real product lookup end to end (auth, JSON
    parsing, sanity band) — a wrong/expired key must show up here loudly.

    The detail names the cause (Sep 2026): the HTTP status and Perplexity's own
    error message, a timeout, an empty answer, a parse failure, or a price
    outside the sanity band. It never contains the key."""
    from app.config import get_settings
    api_key = getattr(get_settings(), "perplexity_api_key", None) or ""
    if not api_key.strip():
        return True, "PERPLEXITY_API_KEY not set — running Gemini-only (optional)"
    from greenbay_ai_evaluator.services.market_price_service import sonar_price_research
    res = sonar_price_research(**_PROBE_PRODUCT)
    if res.launch_price and res.launch_price > 0:
        return True, (
            f"OK — Sonar grounded price KES {res.launch_price:,.0f} "
            f"({len(res.sources)} citations); dual-source cross-check ACTIVE"
        )
    # Cause first: consumers (Pulse's Sentinel) keep the first 300 characters.
    return False, (
        f"Sonar returned NO price [{res.status or 'unknown'}] {res.status_detail} "
        f"(evaluations still work Gemini-only)"
    )


def _probe_google_sheets() -> tuple[bool, str]:
    try:
        import gspread  # noqa: F401
        from google.oauth2.service_account import Credentials  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"gspread/google-auth not installed: {e}"
    from greenbay_ai_evaluator.services import reference_data_service as rds
    gc = rds._get_gspread_client()
    if gc is None:
        return False, "could not authorize gspread client (check credentials file)"
    ss = gc.open_by_key(rds.PRICING_MATRIX_SHEET_ID)
    titles = [ws.title for ws in ss.worksheets()]
    return True, f"OK — pricing matrix opened, {len(titles)} tabs"


def _probe_reference_cache() -> tuple[bool, str]:
    from greenbay_ai_evaluator.services.reference_data_service import get_refresh_status
    st = get_refresh_status()
    matrix = st.get("matrix_rows_cached", 0)
    sales = st.get("sales_rows_cached", 0)
    if matrix > 0 or sales > 0:
        return True, f"OK — {matrix} matrix rows, {sales} sales rows cached (last refresh {st.get('last_refresh')})"
    return False, "reference cache is EMPTY — pricing has no matrix/sales-stock data to use"


def _probe_airtable() -> tuple[bool, str]:
    from app.config import get_settings
    s = get_settings()
    if not s.airtable_api_token or not s.airtable_base_id:
        return False, "AIRTABLE_API_TOKEN / AIRTABLE_BASE_ID not configured"
    import requests
    import urllib.parse
    table = urllib.parse.quote(s.airtable_table_name)
    url = f"https://api.airtable.com/v0/{s.airtable_base_id}/{table}?maxRecords=1"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {s.airtable_api_token}"}, timeout=10,
    )
    if resp.status_code == 200:
        return True, f"OK — table '{s.airtable_table_name}' reachable"
    if resp.status_code == 422:
        return False, f"HTTP 422 — table/field mismatch: {resp.text[:160]}"
    if resp.status_code in (401, 403):
        return False, f"HTTP {resp.status_code} — token invalid/expired: {resp.text[:120]}"
    return False, f"HTTP {resp.status_code}: {resp.text[:160]}"


def _probe_airtable_schema() -> tuple[bool, str]:
    """Verify the columns the writer sends actually exist on the base (root cause of May-22 stop)."""
    from app.config import get_settings
    s = get_settings()
    if not s.airtable_api_token or not s.airtable_base_id:
        return False, "Airtable not configured"
    import requests
    url = f"https://api.airtable.com/v0/meta/bases/{s.airtable_base_id}/tables"
    resp = requests.get(
        url, headers={"Authorization": f"Bearer {s.airtable_api_token}"}, timeout=10,
    )
    if resp.status_code != 200:
        return False, f"cannot read base schema (HTTP {resp.status_code}); needs schema.bases:read scope"
    tables = resp.json().get("tables", [])
    target = next((t for t in tables if t.get("name") == s.airtable_table_name), None)
    if not target:
        return False, f"table '{s.airtable_table_name}' not found in base"
    existing = {f.get("name") for f in target.get("fields", [])}
    required = {
        "AI Pricing Justification", "Country", "Currency",
        "Customer Asking Price (KES)", "Vertex AI Price (KES)",
        "In-House Evaluator Price (KES)", "Customer Name", "Customer Phone",
    }
    missing = sorted(required - existing)
    if missing:
        return False, f"MISSING columns (causes 422 / silent write failure): {missing}"
    return True, "OK — all expected columns present"


def _probe_s3() -> tuple[bool, str]:
    from app.config import get_settings, runtime_s3_bucket_name
    s = get_settings()
    if not (s.aws_access_key_id and s.aws_secret_access_key):
        return False, "AWS credentials not configured"
    import boto3
    bucket = runtime_s3_bucket_name()
    client = boto3.client(
        "s3",
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        region_name=s.aws_region,
    )
    client.head_bucket(Bucket=bucket)
    return True, f"OK — bucket '{bucket}' reachable in {s.aws_region}"


def _probe_ses() -> tuple[bool, str]:
    """Probe AWS SES (used for the team email notifications, issue #13)."""
    from app.config import get_settings
    s = get_settings()
    if not (s.aws_access_key_id and s.aws_secret_access_key):
        return False, "AWS credentials not configured"
    import boto3
    region = getattr(s, "ses_region", None) or s.aws_region
    client = boto3.client(
        "ses",
        aws_access_key_id=s.aws_access_key_id,
        aws_secret_access_key=s.aws_secret_access_key,
        region_name=region,
    )
    quota = client.get_send_quota()
    sender = getattr(s, "ses_sender_email", "") or ""
    verified = ""
    if sender:
        ids = client.list_verified_email_addresses().get("VerifiedEmailAddresses", [])
        verified = " (sender VERIFIED)" if sender in ids else " (sender NOT verified — SES will reject)"
    return True, (
        f"OK — region {region}, 24h quota {quota.get('Max24HourSend')}, "
        f"sent {quota.get('SentLast24Hours')}{verified}"
    )


def _probe_database() -> tuple[bool, str]:
    from app.database.db import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return True, "OK"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------
_PROBES: dict[str, Callable[[], tuple[bool, str]]] = {
    "anthropic_vision": _probe_anthropic,
    "vertex_gemini": _probe_vertex_gemini,
    "gemini_search_newprice": _probe_gemini_search_grounding,
    "perplexity_sonar": _probe_perplexity_sonar,
    "google_sheets": _probe_google_sheets,
    "reference_cache": _probe_reference_cache,
    "airtable_reachable": _probe_airtable,
    "airtable_schema": _probe_airtable_schema,
    "s3": _probe_s3,
    "ses_email": _probe_ses,
    "database": _probe_database,
}


# ---------------------------------------------------------------------------
# Paid probes (Sep 2026)
# ---------------------------------------------------------------------------
# These two probes run a real, BILLED web-search lookup. The service monitor
# calls run_live_healthcheck every MONITOR_INTERVAL_MIN (default 10 minutes),
# so from 13 Jul 2026 monitoring alone made 288 Perplexity requests a day (the
# Gemini probe called Sonar too) against a prepaid credit balance, before one
# customer was priced. Their result is now reused between runs.
_PAID_PROBES = frozenset({"gemini_search_newprice", "perplexity_sonar"})
_PAID_PROBE_FAILURE_RECHECK_S = 30 * 60   # a failure is re-probed sooner
_paid_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_paid_probe_lock = threading.Lock()


def _paid_probe_interval_seconds() -> int:
    """HEALTH_PAID_PROBE_INTERVAL_MIN, default 360 (four lookups a day each).
    0 restores the old behaviour: a billed lookup on every health-check."""
    try:
        return max(0, int(float(os.environ.get("HEALTH_PAID_PROBE_INTERVAL_MIN", "360")) * 60))
    except (TypeError, ValueError):
        return 360 * 60


def _run_paid_probe(name: str, fn: Callable[[], tuple[bool, str]], fresh: bool) -> dict[str, Any]:
    with _paid_probe_lock:
        interval = _paid_probe_interval_seconds()
        cached = _paid_probe_cache.get(name)
        if cached and not fresh and interval > 0:
            checked_at, result = cached
            age = time.time() - checked_at
            limit = interval if result["ok"] else min(interval, _PAID_PROBE_FAILURE_RECHECK_S)
            if age < limit:
                return {
                    **result,
                    "cached": True,
                    "age_s": int(age),
                    "checked_at": datetime.fromtimestamp(checked_at, timezone.utc).isoformat(),
                }
        result = _timed(fn)
        now = time.time()
        _paid_probe_cache[name] = (now, result)
        return {
            **result,
            "cached": False,
            "age_s": 0,
            "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        }


def run_live_healthcheck(fresh: bool = False) -> dict[str, Any]:
    """Run every probe and return a structured report.

    The two billed price-lookup probes reuse their last result for
    HEALTH_PAID_PROBE_INTERVAL_MIN (a failure for at most 30 minutes); their
    entries say so with ``cached`` / ``age_s`` / ``checked_at``. Pass
    ``fresh=True`` (``?fresh=true`` on the endpoint) to force a real lookup,
    e.g. right after topping up credits."""
    results: dict[str, Any] = {}
    for name, fn in _PROBES.items():
        if name in _PAID_PROBES:
            results[name] = _run_paid_probe(name, fn, fresh)
        else:
            results[name] = _timed(fn)
        status = "OK" if results[name]["ok"] else "FAIL"
        logger.info(f"Live health-check [{name}]: {status} — {results[name]['detail']}")

    healthy = sum(1 for r in results.values() if r["ok"])
    total = len(results)
    return {
        "summary": {
            "healthy": healthy,
            "total": total,
            "all_ok": healthy == total,
            "degraded": [k for k, v in results.items() if not v["ok"]],
        },
        "services": results,
    }
