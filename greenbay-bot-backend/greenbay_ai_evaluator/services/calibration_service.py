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
    SalesStockReference,
)
from greenbay_ai_evaluator.services.reference_data_service import (
    get_category_acquisition_ratio,
)

# Guards for recommending a data-derived acquisition ratio.
_MIN_SAMPLES_FOR_RECO = 20
_RATIO_FLOOR, _RATIO_CEIL = 0.45, 0.85

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


def _norm_category(raw: str) -> str:
    c = (raw or "").strip().lower()
    if c in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[c]
    for key, slug in _CATEGORY_ALIASES.items():
        if key in c:
            return slug
    return c or "unknown"


def _clamp_ratio(value: float) -> float:
    return max(_RATIO_FLOOR, min(_RATIO_CEIL, value))


def compute_calibration(db: Session, limit: int = 5000) -> dict[str, Any]:
    """Back-test the engine's *actual* acquisition ratio against real purchase/
    sell pairs, and derive the data-optimal ratio per category.

    The live engine computes ``offer = resale_estimate * acquisition_ratio`` (see
    offer_engine STEP 7). For a comparable used unit, ``resale_estimate`` ≈ the
    sales-stock ``selling_price`` and the real acquisition is ``purchase_cost``.
    So:
      predicted_offer  = selling_price * get_category_acquisition_ratio(category)
      actual           = purchase_cost
      data_ratio       = purchase_cost / selling_price  (what we *actually* paid)

    ``recommended_ratio`` = clamped median(data_ratio) per category — the value
    that would have minimised error on real deals. Read-only, no network, no cost.
    """
    out: dict[str, Any] = {"compared": 0, "by_category": [], "overall": {}}

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

    err_buckets: dict[str, list[float]] = {}
    ratio_buckets: dict[str, list[float]] = {}
    current_ratio: dict[str, float] = {}
    all_errors: list[float] = []
    for r in rows:
        try:
            purchase = float(r.purchase_cost)
            selling = float(r.selling_price)
            if purchase <= 0 or selling <= 0:
                continue
            raw_cat = r.product_category or ""
            cat = _norm_category(raw_cat)
            ratio = get_category_acquisition_ratio(raw_cat)
            current_ratio.setdefault(cat, ratio)
            predicted = selling * ratio
            err = abs(predicted - purchase) / purchase * 100.0
            data_ratio = purchase / selling
            # Guard against absurd outliers polluting the mean (bad source rows).
            if err > 500 or data_ratio > 2.0:
                continue
            err_buckets.setdefault(cat, []).append(err)
            ratio_buckets.setdefault(cat, []).append(data_ratio)
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
    for cat, errs in sorted(err_buckets.items(), key=lambda kv: -len(kv[1])):
        entry = {"category": cat}
        entry.update(_agg(errs))
        data_ratios = ratio_buckets.get(cat, [])
        cur = round(current_ratio.get(cat, 0.0), 3)
        entry["current_ratio"] = cur
        if len(data_ratios) >= _MIN_SAMPLES_FOR_RECO:
            reco = _clamp_ratio(median(data_ratios))
            entry["recommended_ratio"] = round(reco, 3)
            entry["raw_median_ratio"] = round(median(data_ratios), 3)
            entry["ratio_delta"] = round(reco - cur, 3)
        else:
            entry["recommended_ratio"] = None
            entry["raw_median_ratio"] = round(median(data_ratios), 3) if data_ratios else None
            entry["ratio_delta"] = None
        by_cat.append(entry)

    out["compared"] = len(all_errors)
    out["by_category"] = by_cat
    out["overall"] = _agg(all_errors)
    out["note"] = (
        "predicted_offer = resale_price x acquisition_ratio (live engine STEP 7); "
        "actual = recorded purchase_cost. recommended_ratio = clamped median of "
        f"purchase/sell on >={_MIN_SAMPLES_FOR_RECO} real deals. Lower error = better."
    )
    return out
