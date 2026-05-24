"""
Internal team notification service (v6 — Workstream 7).

Sends WhatsApp notifications to the GreenBay operations team for every
evaluation outcome: ACCEPTED, REJECTED, NEEDS REVIEW.

Transport: Flowcart API (swappable to WhatsApp Business Cloud API).
Delivery: async / non-blocking via threading.
Fallback: writes to /app/notification_fallback/ on failure, retried every 15 min.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

INTERNAL_TEAM_NUMBER = "+254709155506"
FALLBACK_DIR = Path("/app/notification_fallback")


def notify_internal_team(payload: dict[str, Any]) -> None:
    """Fire-and-forget notification to the internal team.

    *payload* should contain:
      outcome: str — ACCEPTED / REJECTED / NEEDS REVIEW
      product: str — e.g. "Samsung Fridge 92L"
      ai_price: float
      currency: str — KES / UGX / NGN
      confidence: float
      customer_name: str
      customer_phone: str
      image_url: str | None
      session_id: str
      extra: str | None — e.g. rejection option chosen
    """
    t = threading.Thread(
        target=_send_notification, args=(payload,), daemon=True,
    )
    t.start()


def _send_notification(payload: dict[str, Any]) -> None:
    """Worker: format + send the notification, fall back to disk on failure."""
    try:
        message = _format_message(payload)
        success = _send_whatsapp_message(INTERNAL_TEAM_NUMBER, message)
        if success:
            logger.info(f"Internal notification sent: {payload.get('outcome')}")
        else:
            _queue_fallback(payload)
    except Exception as e:
        logger.error(f"Internal notification error: {e}")
        _queue_fallback(payload)


def _format_message(p: dict[str, Any]) -> str:
    """Build a concise WhatsApp message for the ops team."""
    outcome = p.get("outcome", "UNKNOWN")
    currency = p.get("currency", "KES")

    lines = [
        f"📋 *GreenBay Trade-In — {outcome}*",
        "",
        f"🔧 {p.get('product', 'Unknown appliance')}",
        f"💰 AI Price: {currency} {p.get('ai_price', 0):,.0f}",
        f"📊 Confidence: {p.get('confidence', 0):.0f}%",
        "",
        f"👤 {p.get('customer_name', 'Unknown')}",
        f"📱 {p.get('customer_phone', '')}",
    ]

    extra = p.get("extra")
    if extra:
        lines.append(f"ℹ️ {extra}")

    session_id = p.get("session_id", "")
    if session_id:
        lines.append(f"🔗 Session: {session_id[:8]}…")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transport layer — Flowcart API (swappable)
# ---------------------------------------------------------------------------
def _send_whatsapp_message(to_number: str, message: str) -> bool:
    """Send a text message via Flowcart API. Returns True on success."""
    try:
        from app.config import get_settings
        settings = get_settings()
    except Exception:
        logger.warning("Internal notification: cannot load settings")
        return False

    api_key = getattr(settings, "flowcart_api_key", None)
    api_url = getattr(settings, "flowcart_api_url", "https://api.flowcart.io/v1")

    if not api_key:
        logger.warning("Internal notification: Flowcart API key not configured")
        return False

    try:
        import requests
        resp = requests.post(
            f"{api_url}/messages/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "to": to_number,
                "type": "text",
                "text": message,
            },
            timeout=10,
        )
        if resp.status_code in (200, 201, 202):
            return True
        logger.warning(f"Internal notification: Flowcart returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Internal notification: Flowcart request failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Fallback queue (disk-based, retried periodically)
# ---------------------------------------------------------------------------
def _queue_fallback(payload: dict[str, Any]) -> None:
    """Write payload to disk for later retry."""
    try:
        FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = FALLBACK_DIR / f"notify_{ts}.json"
        path.write_text(json.dumps(payload, default=str))
        logger.info(f"Internal notification queued for retry: {path.name}")
    except Exception as e:
        logger.error(f"Internal notification: fallback queue write failed: {e}")


def retry_failed_notifications() -> int:
    """Retry any notifications in the fallback queue. Returns count recovered."""
    if not FALLBACK_DIR.exists():
        return 0

    recovered = 0
    for path in sorted(FALLBACK_DIR.glob("notify_*.json")):
        try:
            payload = json.loads(path.read_text())
            message = _format_message(payload)
            if _send_whatsapp_message(INTERNAL_TEAM_NUMBER, message):
                path.unlink()
                recovered += 1
            else:
                age_hours = (time.time() - path.stat().st_mtime) / 3600
                if age_hours > 72:
                    logger.warning(f"Removing stale notification (>72h): {path.name}")
                    path.unlink()
        except Exception as e:
            logger.warning(f"Retry notification {path.name} failed: {e}")

    if recovered:
        logger.info(f"Internal notification: {recovered} queued notifications recovered")
    return recovered
