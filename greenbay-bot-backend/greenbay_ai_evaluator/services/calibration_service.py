"""
Offline calibration / back-test (issue #12).

Goal: measure how close the engine's acquisition target would have been to what
GreenBay ACTUALLY paid, using the real sales-stock data we already have
(sales_stock_reference: purchase_cost = what we paid, selling_price = resale).

Method (deterministic, read-only, no network, no cost):
  predicted_offer = selling_price * policy.max_offer_pct[category]
  actual          = purchase_cost
  error%          = |predicted - actual| / actual * 100

Aggregated per category (mean/median error, hit-rate within ±20%). This tells us
which categories the policy is mis-calibrated for, so depreciation/offer factors
can be tuned with evidence rather than guesswork.

This NEVER touches the live evaluation path and never raises — a missing table
just yields an empty report.
"""

from __future__ import annotations

from statistics import median
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from greenbay_ai_evaluator.models.evaluator_models import (
    PricingPolicy,
    SalesStockReference,
)

# Loose mapping from free-text sales categories to policy category slugs.
_CATEGORY_ALIASES = {
    "tv": "tv_monitor", "television": "tv_monitor", "tvs": "tv_monitor",
    "monitor": "tv_monitor",
    "fridge": "refrigerator", "fridges": "refrigerator", "refrigerator": "refrigerator",
    "freezer": "freezer", "freezers": "freezer",
    "washing machine": "washing_machine", "washer": "washing_machine",
    "cooker": "cooker_oven", "cookers": "cooker_oven", "oven": "cooker_oven",
    "microwave": "microwave", "microwaves": "microwave",
}
_DEFAULT_OFFER_RATIO = 0.45  # fallback when no policy matches


def _norm_category(raw: str) -> str:
    c = (raw or "").strip().lower()
    if c in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[c]
    for key, slug in _CATEGORY_ALIASES.items():
        if key in c:
            return slug
    return c or "unknown"


def compute_calibration(db: Session, limit: int = 5000) -> dict[str, Any]:
    """Back-test the policy's offer ratio against real purchase/sell pairs."""
    out: dict[str, Any] = {"compared": 0, "by_category": [], "overall": {}}

    # Policy offer ratios per category
    ratios: dict[str, float] = {}
    try:
        for p in db.query(PricingPolicy).all():
            if p.max_offer_pct:
                ratios[(p.category or "").lower()] = float(p.max_offer_pct)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"calibration: policy load failed: {e}")

    try:
        rows = (
            db.query(SalesStockReference)
            .filter(
                SalesStockReference.purchase_cost > 0,
                SalesStockReference.selling_price > 0,
            )
            .limit(max(1, min(limit, 50000)))
            .all()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"calibration: sales load failed: {e}")
        return out

    buckets: dict[str, list[float]] = {}
    all_errors: list[float] = []
    for r in rows:
        try:
            purchase = float(r.purchase_cost)
            selling = float(r.selling_price)
            if purchase <= 0 or selling <= 0:
                continue
            cat = _norm_category(r.product_category or "")
            ratio = ratios.get(cat, _DEFAULT_OFFER_RATIO)
            predicted = selling * ratio
            err = abs(predicted - purchase) / purchase * 100.0
            # Guard against absurd outliers polluting the mean (bad source rows).
            if err > 500:
                continue
            buckets.setdefault(cat, []).append(err)
            all_errors.append(err)
        except (TypeError, ValueError, ZeroDivisionError):
            continue

    def _agg(errs: list[float]) -> dict[str, Any]:
        n = len(errs)
        return {
            "count": n,
            "mean_abs_error_pct": round(sum(errs) / n, 1) if n else 0.0,
            "median_abs_error_pct": round(median(errs), 1) if n else 0.0,
            "within_20pct_rate": round(sum(1 for e in errs if e <= 20) / n * 100, 1) if n else 0.0,
        }

    by_cat = []
    for cat, errs in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        entry = {"category": cat}
        entry.update(_agg(errs))
        by_cat.append(entry)

    out["compared"] = len(all_errors)
    out["by_category"] = by_cat
    out["overall"] = _agg(all_errors)
    out["note"] = (
        "predicted_offer = resale_price x policy_max_offer_pct; "
        "actual = recorded purchase_cost. Lower error = better-calibrated policy."
    )
    return out
