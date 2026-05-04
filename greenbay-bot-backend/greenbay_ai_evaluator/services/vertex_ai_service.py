"""
Google Vertex AI (Gemini) secondary-evaluation service.

Used to obtain a second opinion on an appliance photo+description alongside
the primary Claude vision analysis. This service is advisory only — it NEVER
blocks the main evaluation and any failure is swallowed with a log warning.

Auth: service-account JSON (GOOGLE_VERTEX_CREDENTIALS_FILE). Access tokens are
cached in memory and refreshed 5 minutes before expiry.

Endpoint:
  https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}/publishers/google/models/{model}:generateContent
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_cached_token_expiry: float = 0.0  # epoch seconds

_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "last_success_at": None,
    "last_failure_at": None,
    "last_error": None,
    "calls_today": 0,
    "calls_today_date": None,
    "total_calls": 0,
    "total_failures": 0,
}


VERTEX_SYSTEM_PROMPT = (
    "You are an expert secondhand-appliance valuer for the Nairobi, Kenya market "
    "(prices in KES). Given photos and basic product details, respond ONLY with a "
    "compact JSON object (no surrounding prose, no markdown fences) containing:\n"
    '  "condition_grade": one of "A" | "B" | "C" | "D",\n'
    '  "condition_score": integer 0-100,\n'
    '  "estimated_price_kes": integer,\n'
    '  "observations": short free-text note (<= 200 chars).\n'
    "If you cannot determine a field, use null."
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _get_access_token() -> str:
    """Return a valid Google Cloud OAuth access token.

    Tokens are cached and refreshed when within 5 minutes of expiry.
    Raises FileNotFoundError if the credentials file is not configured.
    """
    global _cached_token, _cached_token_expiry

    now = time.time()
    with _token_lock:
        if _cached_token and now < (_cached_token_expiry - 300):
            return _cached_token

        # Lazy-import to avoid hard dependency when Vertex is not configured
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        from app.config import get_settings
        settings = get_settings()
        creds_file = settings.google_vertex_credentials_file or ""
        if not creds_file or not Path(creds_file).exists():
            raise FileNotFoundError(
                f"Vertex AI credentials not found: {creds_file!r}"
            )

        credentials = service_account.Credentials.from_service_account_file(
            creds_file,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(Request())

        _cached_token = credentials.token
        # `expiry` is a naive UTC datetime per google-auth
        if credentials.expiry:
            _cached_token_expiry = credentials.expiry.replace(
                tzinfo=timezone.utc
            ).timestamp()
        else:
            _cached_token_expiry = now + 3000  # fallback: 50 min
        return _cached_token


def _build_endpoint() -> str:
    from app.config import get_settings
    s = get_settings()
    return (
        f"https://{s.google_vertex_region}-aiplatform.googleapis.com/v1/"
        f"projects/{s.google_vertex_project}/locations/{s.google_vertex_region}/"
        f"publishers/google/models/{s.google_vertex_model}:generateContent"
    )


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
def _bump_counter(success: bool, error: Optional[str] = None) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    with _status_lock:
        if _status["calls_today_date"] != today:
            _status["calls_today_date"] = today
            _status["calls_today"] = 0
        _status["calls_today"] += 1
        _status["total_calls"] += 1
        if success:
            _status["last_success_at"] = now_iso
            _status["last_error"] = None
        else:
            _status["last_failure_at"] = now_iso
            _status["last_error"] = error
            _status["total_failures"] += 1


def _extract_json(text: str) -> Optional[dict]:
    """Parse a JSON object out of a model response, tolerant of fences."""
    if not text:
        return None
    # Strip markdown fences if the model added any
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first {...} block
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def vertex_evaluate(
    images_base64: list[str],
    category: str = "",
    brand: str = "",
    age: float | None = None,
    working_status: str = "",
    timeout_seconds: float = 30.0,
) -> Optional[dict[str, Any]]:
    """Send images + context to Gemini for a second-opinion valuation.

    Returns a dict with keys:
        condition_grade (str), condition_score (int|None),
        estimated_price_kes (int|None), observations (str), model (str), latency_ms (int)

    Returns None if Vertex is not configured or any error occurs (always logs
    a warning — never raises).
    """
    from app.config import get_settings
    settings = get_settings()

    if not settings.google_vertex_credentials_file:
        logger.debug("Vertex AI: credentials not configured, skipping")
        return None

    if not images_base64:
        return None

    start_ms = time.time()
    try:
        token = _get_access_token()
    except Exception as e:
        _bump_counter(success=False, error=f"auth: {e}")
        logger.warning(f"Vertex AI: auth failed ({e})")
        return None

    # Trim payload: up to 3 images, ~1MB each to keep under Vertex limits
    image_parts: list[dict] = []
    for b64 in images_base64[:3]:
        if not b64:
            continue
        clean_b64 = b64.split(",", 1)[-1]  # strip data URI prefix if present
        image_parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": clean_b64,
            }
        })

    if not image_parts:
        return None

    user_text_lines = [
        f"Category: {category or 'unknown'}",
        f"Brand: {brand or 'unknown'}",
        f"Approx age (years): {age if age is not None else 'unknown'}",
        f"Seller-reported condition: {working_status or 'unknown'}",
        "Provide your JSON evaluation now.",
    ]
    user_text = "\n".join(user_text_lines)

    body = {
        "systemInstruction": {
            "role": "system",
            "parts": [{"text": VERTEX_SYSTEM_PROMPT}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_text}] + image_parts,
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",
        },
    }

    endpoint = _build_endpoint()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(endpoint, headers=headers, json=body)
    except Exception as e:
        _bump_counter(success=False, error=f"http: {e}")
        logger.warning(f"Vertex AI request failed: {e}")
        return None

    if resp.status_code != 200:
        msg = f"HTTP {resp.status_code}: {resp.text[:240]}"
        _bump_counter(success=False, error=msg)
        logger.warning(f"Vertex AI: {msg}")
        return None

    try:
        data = resp.json()
    except Exception as e:
        _bump_counter(success=False, error=f"decode: {e}")
        logger.warning(f"Vertex AI: could not decode response ({e})")
        return None

    # Extract model text: candidates[0].content.parts[*].text
    try:
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except Exception:
        text = ""

    parsed = _extract_json(text)
    if not parsed:
        _bump_counter(success=False, error="parse: empty or invalid JSON")
        logger.warning("Vertex AI: response had no parseable JSON")
        return None

    grade = parsed.get("condition_grade")
    if isinstance(grade, str):
        grade = grade.strip().upper()[:1]
        if grade not in {"A", "B", "C", "D"}:
            grade = None
    else:
        grade = None

    def _to_int(v: Any) -> Optional[int]:
        try:
            return int(round(float(v))) if v is not None else None
        except (TypeError, ValueError):
            return None

    result = {
        "condition_grade": grade,
        "condition_score": _to_int(parsed.get("condition_score")),
        "estimated_price_kes": _to_int(parsed.get("estimated_price_kes")),
        "observations": str(parsed.get("observations") or "")[:400],
        "model": settings.google_vertex_model,
        "latency_ms": int((time.time() - start_ms) * 1000),
    }
    _bump_counter(success=True)
    logger.info(
        f"Vertex AI: grade={result['condition_grade']}, "
        f"price={result['estimated_price_kes']}, "
        f"latency={result['latency_ms']}ms"
    )
    return result


def get_service_status() -> dict[str, Any]:
    """Return Vertex AI service status for the ops dashboard."""
    try:
        from app.config import get_settings
        s = get_settings()
        creds_file = s.google_vertex_credentials_file or ""
        creds_ok = bool(creds_file) and Path(creds_file).exists()
        project = s.google_vertex_project
        region = s.google_vertex_region
        model = s.google_vertex_model
    except Exception:
        creds_ok = False
        project = region = model = None

    with _status_lock:
        snap = dict(_status)

    if not creds_ok:
        status = "disabled"
    elif snap.get("last_error") and snap.get("last_failure_at") and not snap.get("last_success_at"):
        status = "degraded"
    elif snap.get("last_success_at"):
        status = "online"
    else:
        status = "idle"

    return {
        "service": "vertex_ai",
        "status": status,
        "configured": creds_ok,
        "project": project,
        "region": region,
        "model": model,
        "last_success_at": snap.get("last_success_at"),
        "last_failure_at": snap.get("last_failure_at"),
        "last_error": snap.get("last_error"),
        "calls_today": snap.get("calls_today", 0),
        "total_calls": snap.get("total_calls", 0),
        "total_failures": snap.get("total_failures", 0),
    }
