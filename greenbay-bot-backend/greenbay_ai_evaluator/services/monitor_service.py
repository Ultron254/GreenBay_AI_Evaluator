"""
Background service monitor + alerting (issue #3).

Periodically runs the live health-check and emails the team when:
  - a service transitions from healthy -> failing (so you find out immediately,
    e.g. an API key expired or a paid service ran out of credit), or
  - it recovers (failing -> healthy), or
  - SES sending is close to its 24h quota (a "running low" signal for the one
    paid service that actually exposes usage).

Notes on "credits/funds": most paid APIs (Anthropic, Vertex) do NOT expose a
balance over their API, so we cannot read a remaining-credit figure. What we CAN
do reliably is alert the moment a service starts FAILING — which is exactly what
an exhausted balance or revoked key looks like in the health-check. SES is the
one service that reports usage, so we also warn before it hits its quota.

Controlled by env:
  MONITOR_ENABLED        default "true"
  MONITOR_INTERVAL_MIN   default 10  (minutes between checks)
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from loguru import logger

# Remembers the last known ok/fail state per service so we only alert on change.
_last_state: dict[str, bool] = {}
_ses_quota_warned = False


def _interval_seconds() -> int:
    try:
        return max(60, int(float(os.environ.get("MONITOR_INTERVAL_MIN", "10")) * 60))
    except (TypeError, ValueError):
        return 600


def _enabled() -> bool:
    return os.environ.get("MONITOR_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _check_once() -> None:
    global _ses_quota_warned
    from greenbay_ai_evaluator.services.live_healthcheck_service import run_live_healthcheck
    from greenbay_ai_evaluator.services.email_service import send_alert_email

    report = run_live_healthcheck()
    services = report.get("services", {})

    newly_failed: list[str] = []
    recovered: list[str] = []
    for name, res in services.items():
        ok = bool(res.get("ok"))
        prev = _last_state.get(name)
        if prev is None:
            _last_state[name] = ok
            # On first run, alert if something is already down.
            if not ok:
                newly_failed.append(f"{name}: {res.get('detail')}")
            continue
        if prev and not ok:
            newly_failed.append(f"{name}: {res.get('detail')}")
        elif not prev and ok:
            recovered.append(name)
        _last_state[name] = ok

    if newly_failed:
        body = (
            "One or more GreenBay Evaluator services just started FAILING:\n\n"
            + "\n".join(f"  ❌ {x}" for x in newly_failed)
            + "\n\nThis often means an API key expired or a paid service ran out "
            "of credit. Check the dashboard for details."
        )
        logger.error(f"MONITOR ALERT (failing): {newly_failed}")
        send_alert_email("Service(s) failing", body)

    if recovered:
        logger.info(f"MONITOR: services recovered: {recovered}")
        send_alert_email(
            "Service(s) recovered",
            "These services are healthy again:\n\n" + "\n".join(f"  ✅ {x}" for x in recovered),
        )

    # SES quota "running low" warning (the one paid service exposing usage)
    ses = services.get("ses_email", {})
    detail = str(ses.get("detail", ""))
    try:
        # detail looks like: "OK — region us-east-1, 24h quota 200.0, sent 0.0"
        if "quota" in detail and "sent" in detail:
            quota = float(detail.split("quota")[1].split(",")[0].strip())
            sent = float(detail.split("sent")[1].strip().rstrip("."))
            if quota > 0 and sent >= 0.8 * quota and not _ses_quota_warned:
                _ses_quota_warned = True
                send_alert_email(
                    "SES email quota almost exhausted",
                    f"SES has sent {sent:.0f} of its {quota:.0f} daily emails "
                    f"({sent / quota * 100:.0f}%). Request a quota increase or "
                    f"emails will start bouncing.",
                )
            elif sent < 0.5 * quota:
                _ses_quota_warned = False  # reset once usage drops
    except (ValueError, IndexError):
        pass


def _loop() -> None:
    # Small initial delay so the app finishes booting before the first probe.
    time.sleep(30)
    while True:
        try:
            _check_once()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"MONITOR: check failed: {e}")
        time.sleep(_interval_seconds())


def start_monitor_loop() -> None:
    """Start the background monitor (idempotent, daemon thread)."""
    if not _enabled():
        logger.info("MONITOR: disabled via MONITOR_ENABLED=false")
        return
    try:
        t = threading.Thread(target=_loop, name="evaluator-monitor", daemon=True)
        t.start()
        logger.info(f"MONITOR: started (every {_interval_seconds() // 60} min)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MONITOR: could not start: {e}")
