"""
Evaluator performance metrics (powers the ops dashboard).

Computes, from the live database:
  - volume + decision breakdown
  - acceptance rate
  - confidence distribution (and the share below the 80% floor)
  - AI vs in-house/expert accuracy (mean/median % error, hit-rate within bands)
  - per-category summary
  - the most recent evaluations

All queries are read-only and defensive: a missing table or column degrades
gracefully to zeros rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from greenbay_ai_evaluator.models.evaluator_models import ValuationSession

try:
    from app.database.models import ExpertPriceFeedback
except Exception:  # pragma: no cover
    ExpertPriceFeedback = None  # type: ignore


def _pct(n: float, d: float) -> float:
    return round((n / d) * 100, 1) if d else 0.0


def compute_evaluator_metrics(db: Session, days: int = 30) -> dict[str, Any]:
    """Return a structured performance report for the last *days* days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    out: dict[str, Any] = {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        sessions = (
            db.query(ValuationSession)
            .filter(ValuationSession.created_at >= since)
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"metrics: session query failed: {e}")
        sessions = []

    total = len(sessions)
    out["total_evaluations"] = total

    # Decision breakdown (use final_decision if set, else decision)
    decisions: dict[str, int] = {}
    accepted = 0
    conf_values: list[float] = []
    below_floor = 0
    for s in sessions:
        d = (s.final_decision or s.decision or "unknown").lower()
        decisions[d] = decisions.get(d, 0) + 1
        if d == "accepted" or d.startswith("accept"):
            accepted += 1
        if s.confidence_score is not None:
            conf_values.append(float(s.confidence_score))
            if float(s.confidence_score) < 80:
                below_floor += 1

    out["decision_breakdown"] = decisions
    out["acceptance_rate_pct"] = _pct(accepted, total)
    out["avg_confidence"] = round(sum(conf_values) / len(conf_values), 1) if conf_values else 0.0
    out["confidence_below_80_pct"] = _pct(below_floor, len(conf_values))

    # AI vs in-house/expert accuracy
    accuracy: dict[str, Any] = {"compared": 0}
    if ExpertPriceFeedback is not None:
        try:
            feedback = (
                db.query(ExpertPriceFeedback)
                .filter(
                    ExpertPriceFeedback.created_at >= since,
                    ExpertPriceFeedback.expert_price > 0,
                    ExpertPriceFeedback.system_price > 0,
                )
                .all()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"metrics: expert feedback query failed: {e}")
            feedback = []

        errors_pct: list[float] = []
        within_10 = within_20 = 0
        for f in feedback:
            try:
                exp = float(f.expert_price)
                sysp = float(f.system_price)
                if exp <= 0:
                    continue
                err = abs(sysp - exp) / exp * 100.0
                errors_pct.append(err)
                if err <= 10:
                    within_10 += 1
                if err <= 20:
                    within_20 += 1
            except (TypeError, ValueError, ZeroDivisionError):
                continue

        n = len(errors_pct)
        accuracy = {
            "compared": n,
            "mean_abs_error_pct": round(sum(errors_pct) / n, 1) if n else 0.0,
            "median_abs_error_pct": round(median(errors_pct), 1) if n else 0.0,
            "within_10pct_rate": _pct(within_10, n),
            "within_20pct_rate": _pct(within_20, n),
        }
    out["ai_vs_internal"] = accuracy

    # Per-category summary
    cats: dict[str, dict[str, Any]] = {}
    for s in sessions:
        c = (s.category or "unknown").lower()
        entry = cats.setdefault(c, {"count": 0, "offers": [], "accepted": 0})
        entry["count"] += 1
        if s.opening_offer:
            entry["offers"].append(float(s.opening_offer))
        d = (s.final_decision or s.decision or "").lower()
        if d.startswith("accept"):
            entry["accepted"] += 1
    category_summary = []
    for c, e in sorted(cats.items(), key=lambda kv: -kv[1]["count"]):
        offers = e["offers"]
        category_summary.append({
            "category": c,
            "count": e["count"],
            "avg_offer": round(sum(offers) / len(offers), 0) if offers else 0,
            "acceptance_rate_pct": _pct(e["accepted"], e["count"]),
        })
    out["by_category"] = category_summary

    # Daily time series (volume + avg confidence) for trend charts
    daily: dict[str, dict[str, Any]] = {}
    for s in sessions:
        if not s.created_at:
            continue
        day = s.created_at.date().isoformat()
        b = daily.setdefault(day, {"count": 0, "conf_sum": 0.0, "conf_n": 0, "accepted": 0})
        b["count"] += 1
        if s.confidence_score is not None:
            b["conf_sum"] += float(s.confidence_score)
            b["conf_n"] += 1
        d = (s.final_decision or s.decision or "").lower()
        if d.startswith("accept"):
            b["accepted"] += 1
    timeseries = [
        {
            "date": day,
            "count": b["count"],
            "avg_confidence": round(b["conf_sum"] / b["conf_n"], 1) if b["conf_n"] else 0.0,
            "accepted": b["accepted"],
        }
        for day, b in sorted(daily.items())
    ]
    out["timeseries"] = timeseries

    # Recent evaluations (latest 20)
    recent = []
    for s in sorted(sessions, key=lambda x: x.created_at or since, reverse=True)[:20]:
        size = ""
        if s.size_value and s.size_unit:
            size = f"{s.size_value:g} {s.size_unit}"
        recent.append({
            "when": s.created_at.isoformat() if s.created_at else "",
            "item": " ".join(p for p in [s.brand, s.model, s.category] if p),
            "size": size,
            "ai_offer": round(float(s.opening_offer), 0) if s.opening_offer else None,
            "asking": round(float(s.seller_asking_price), 0) if s.seller_asking_price else None,
            "confidence": round(float(s.confidence_score), 0) if s.confidence_score is not None else None,
            "decision": (s.final_decision or s.decision or ""),
            "currency": s.currency_code or "KES",
        })
    out["recent"] = recent

    return out
