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
def _anthropic_model_call(api_key: str, model: str) -> tuple[bool, str]:
    """Spend one token on *model* and classify the outcome.

    A models-list call only proves the key exists — it still returns 200 on an
    account with no credit. A real completion makes an exhausted balance (the
    failure that silently broke pricing) visible.
    """
    import requests
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=20,
    )
    if resp.status_code == 200:
        return True, "OK"
    body = resp.text[:200]
    if resp.status_code in (400, 402) and "credit" in body.lower():
        return False, f"OUT OF CREDIT — top up billing. HTTP {resp.status_code}: {body}"
    if resp.status_code == 401:
        return False, f"key rejected (revoked or wrong). HTTP 401: {body}"
    if resp.status_code == 404:
        return False, f"model not available to this account. HTTP 404: {body}"
    if resp.status_code == 429:
        return False, f"rate limited / quota exhausted. HTTP 429: {body}"
    return False, f"HTTP {resp.status_code}: {body}"


def _probe_anthropic() -> tuple[bool, str]:
    """Probe vision the way analyze_appliance_images() actually calls it.

    That function loops over [primary, fallback] and succeeds if EITHER model
    answers, so probing the primary alone gives the wrong verdict twice over:
    it reports the whole service down when only the primary is unavailable
    (a false alarm), and it would report healthy while the fallback is broken,
    hiding the fact that there is no safety net left.
    """
    from app.config import get_settings
    s = get_settings()
    if not s.anthropic_api_key:
        return False, "ANTHROPIC_API_KEY not configured"

    primary, fallback = s.anthropic_primary_model, s.anthropic_fallback_model
    primary_ok, primary_detail = _anthropic_model_call(s.anthropic_api_key, primary)
    if primary_ok:
        return True, f"OK (billable call succeeded on {primary})"

    # Primary is unusable. Vision still works if the fallback answers, but the
    # wasted round-trip is on every single request, so this must stay visible.
    fallback_ok, fallback_detail = _anthropic_model_call(s.anthropic_api_key, fallback)
    if fallback_ok:
        return True, (
            f"DEGRADED — primary {primary} is unusable ({primary_detail}); "
            f"serving from fallback {fallback}. Every vision call pays a failed "
            f"round-trip first and there is no safety net left. Fix "
            f"ANTHROPIC_PRIMARY_MODEL."
        )
    return False, (
        f"vision DOWN — primary {primary}: {primary_detail} | "
        f"fallback {fallback}: {fallback_detail}"
        f"{_anthropic_available_models(s.anthropic_api_key)}"
    )


def _anthropic_available_models(api_key: str) -> str:
    """Model IDs this key can actually use, for the DOWN message.

    Without this the operator has to guess replacement IDs, and Anthropic
    retires dated model names over time.
    """
    import requests
    try:
        resp = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            params={"limit": 50},
            timeout=15,
        )
        if resp.status_code != 200:
            return f" | could not list available models: HTTP {resp.status_code}"
        ids = [m.get("id", "") for m in resp.json().get("data", [])]
        if not ids:
            return " | this key has access to NO models at all"
        return f" | models this key CAN use: {', '.join(ids)}"
    except Exception as e:  # noqa: BLE001
        return f" | could not list available models: {e}"


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


def _probe_smtp() -> tuple[bool, str]:
    """Connect, STARTTLS and authenticate against the configured SMTP server."""
    from app.config import get_settings
    import smtplib
    s = get_settings()
    host = getattr(s, "smtp_host", "") or ""
    port = int(getattr(s, "smtp_port", 587) or 587)
    user = getattr(s, "smtp_user", "") or ""
    pwd = getattr(s, "smtp_password", "") or ""
    sender = getattr(s, "smtp_from", "") or user
    if not sender:
        return False, f"SMTP_HOST is {host} but neither SMTP_FROM nor SMTP_USER is set"
    if not pwd:
        return False, f"SMTP_HOST is {host} but SMTP_PASSWORD is not set — every send will fail"
    try:
        with smtplib.SMTP(host, port, timeout=20) as srv:
            srv.starttls()
            srv.login(user, pwd)
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP auth rejected by {host}:{port} as {user} — check the app password. {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"SMTP {host}:{port} unusable: {type(e).__name__}: {e}"
    return True, f"OK — authenticated to {host}:{port} as {user}, sending as {sender}"


def _probe_ses() -> tuple[bool, str]:
    """Probe whichever transport email_service will actually use.

    email_service._send_ses() prefers SMTP whenever SMTP_HOST is set, so
    probing SES regardless reported on a path that is never taken. The SES
    branch also used to return OK with an unverified sender appended to the
    detail string — SES rejects every one of those sends, so a green tick
    there meant no email was being delivered at all.
    """
    from app.config import get_settings
    s = get_settings()
    if getattr(s, "smtp_host", ""):
        return _probe_smtp()
    if not (s.aws_access_key_id and s.aws_secret_access_key):
        return False, "no SMTP_HOST and no AWS credentials — email is OFF"
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
    if not sender:
        return False, f"SES reachable in {region} but SES_SENDER_EMAIL is not set — email is OFF"
    ids = client.list_verified_email_addresses().get("VerifiedEmailAddresses", [])
    if sender not in ids:
        return False, (
            f"SES sender {sender} is NOT verified in {region} — SES rejects every send. "
            f"Verify the identity, or set SMTP_HOST to use SMTP instead."
        )
    return True, (
        f"OK — SES region {region}, sender {sender} verified, "
        f"24h quota {quota.get('Max24HourSend')}, sent {quota.get('SentLast24Hours')}"
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
_PAID_PROBE_FRESH_FLOOR_S = 60            # ?fresh=true never re-runs sooner
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
    # The lock guards the cache only, never the lookup: a grounded search can
    # take over a minute, and the monitor thread must not make an API request
    # to /health/services wait behind it. Two overlapping checks can therefore
    # both run a lookup; that is rare and costs one extra request.
    interval = _paid_probe_interval_seconds()
    if fresh or interval > 0:
        with _paid_probe_lock:
            cached = _paid_probe_cache.get(name)
        if cached:
            checked_at, result = cached
            age = time.time() - checked_at
            if fresh:
                # The read-only key can pass fresh=true; without a floor a loop
                # of requests would spend the prepaid balance.
                limit = _PAID_PROBE_FRESH_FLOOR_S
            else:
                limit = interval if result["ok"] else min(interval, _PAID_PROBE_FAILURE_RECHECK_S)
            if 0 <= age < limit:
                return {
                    **result,
                    "cached": True,
                    "age_s": int(age),
                    "checked_at": datetime.fromtimestamp(checked_at, timezone.utc).isoformat(),
                }
    result = _timed(fn)
    now = time.time()
    with _paid_probe_lock:
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
    e.g. right after topping up credits; a result less than a minute old is
    still reused, so the flag cannot be looped to spend credits."""
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
