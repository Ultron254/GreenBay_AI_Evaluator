"""
Self-learning pricing service (CR-2 + CR-4).

Reads historical team-assessed prices from the Google Sheet and uses them
to improve AI pricing accuracy over time. Acts as the "expert/historical"
data source in the 60/40 pricing rebalance.

Runs on startup and refreshes hourly via a background task.
"""

from __future__ import annotations

import asyncio
import math
import re
from datetime import datetime, timedelta
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.services.sheet_price_parser import (
    parse_sheet_price as _parse_price,
)

# ---------------------------------------------------------------------------
# In-memory price lookup cache
# ---------------------------------------------------------------------------
_price_cache: dict[str, dict] = {}
_last_refresh: datetime | None = None
_accuracy_metrics: dict[str, Any] = {}

# Recency half-life: prices older than this lose weight exponentially
RECENCY_HALF_LIFE_DAYS = 90


def _parse_date(text: str) -> datetime | None:
    """Parse date from sheet (DD/MM/YY or YYYY-MM-DD).

    Two-digit years follow datetime/strptime ``%y`` rules (``00``–``68`` →
    ``2000``–``2068``, ``69``–``99`` → ``1969``–``1999``), so ``26`` means ``2026``,
    not ``1926``.
    """
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _recency_weight(date: datetime | None) -> float:
    """Calculate exponential decay weight based on date recency."""
    if date is None:
        return 0.5  # Unknown date gets half weight
    days_ago = (datetime.now() - date).days
    if days_ago < 0:
        days_ago = 0
    return math.exp(-0.693 * days_ago / RECENCY_HALF_LIFE_DAYS)


def _normalize_key(item: str, condition: int = 0, age: str = "") -> str:
    """Create a normalized lookup key from item description."""
    # Extract brand and category from item string
    item_lower = item.lower().strip()
    # Remove common noise words
    for word in ["used", "second hand", "secondhand", "refurbished"]:
        item_lower = item_lower.replace(word, "")
    return re.sub(r'\s+', ' ', item_lower).strip()


async def refresh_from_sheet() -> int:
    """Read Google Sheet data and rebuild the price lookup cache.

    Returns the number of data points loaded.
    """
    global _price_cache, _last_refresh, _accuracy_metrics

    try:
        from greenbay_ai_evaluator.services.google_sheets_service import (
            read_historical_data_async,
        )

        data = await read_historical_data_async()
        if not data:
            logger.info("Pricing learner: no historical data from Google Sheet")
            return 0

        new_cache: dict[str, dict] = {}
        ai_prices = []
        team_prices = []

        for row in data:
            item = row.get("Item", "").strip()
            if not item:
                continue

            # Parse team price (column J — the ground truth).
            # Uses ``sheet_price_parser.parse_sheet_price`` (imported as ``_parse_price``):
            # strips KES/ksh, commas, multiplies trailing k/K by 1000, rejects N/A sentinels.
            team_price_str = row.get("Internal Team Price", "").strip()
            team_price = _parse_price(team_price_str)

            # Parse AI price for accuracy tracking
            ai_price_str = row.get("AI Price", "").strip()
            ai_price = _parse_price(ai_price_str)

            # Parse date for recency weighting
            date_str = row.get("Date", "").strip()
            date = _parse_date(date_str)

            # Parse condition
            condition_str = row.get("Condition (1-5)", "").strip()
            try:
                condition = int(condition_str)
            except (ValueError, TypeError):
                condition = 3

            age_str = row.get("Age", "").strip()

            # Build lookup key
            key = _normalize_key(item, condition)

            if key not in new_cache:
                new_cache[key] = {
                    "prices": [],
                    "weights": [],
                    "conditions": [],
                    "ages": [],
                    "count": 0,
                }

            # Add team price if available
            if team_price and team_price > 0:
                weight = _recency_weight(date)
                new_cache[key]["prices"].append(team_price)
                new_cache[key]["weights"].append(weight)
                new_cache[key]["conditions"].append(condition)
                new_cache[key]["ages"].append(age_str)
                new_cache[key]["count"] += 1

            # Track AI accuracy
            if ai_price and team_price and ai_price > 0 and team_price > 0:
                ai_prices.append(ai_price)
                team_prices.append(team_price)

        # Calculate accuracy metrics
        if ai_prices and team_prices:
            errors = [
                abs(ai - team) / team * 100
                for ai, team in zip(ai_prices, team_prices)
            ]
            within_20 = sum(1 for e in errors if e <= 20)
            _accuracy_metrics = {
                "total_comparisons": len(errors),
                "mean_error_pct": sum(errors) / len(errors),
                "median_error_pct": sorted(errors)[len(errors) // 2],
                "within_20_pct": within_20 / len(errors) * 100,
                "within_20_count": within_20,
            }
            logger.info(
                f"Pricing learner accuracy: {_accuracy_metrics['within_20_pct']:.1f}% "
                f"of AI prices within 20% of team price "
                f"({within_20}/{len(errors)} comparisons)"
            )

        _price_cache = new_cache
        _last_refresh = datetime.now()
        total_points = sum(v["count"] for v in new_cache.values())

        logger.info(
            f"Pricing learner: loaded {total_points} price points "
            f"for {len(new_cache)} unique items"
        )
        return total_points

    except Exception as e:
        logger.error(f"Pricing learner refresh failed: {e}")
        return 0


def get_historical_price(
    *,
    brand: str,
    model: str = "",
    category: str = "",
    condition_grade: str = "B",
    age_years: float = 2,
) -> dict[str, Any] | None:
    """Look up the best historical price estimate for a product.

    Uses weighted average of team-assessed prices, with exponential
    decay favoring more recent data.

    Returns None if no historical data is available, or a dict with:
      - price: weighted average team price
      - confidence: 0-100 based on data quantity and recency
      - data_points: number of historical comparisons used
      - source: "google_sheet_historical"
    """
    if not _price_cache:
        return None

    # Build the lookup key
    item = f"{brand} {model}".strip() if model else f"{brand} {category}"
    key = _normalize_key(item)

    # Try exact match first
    entry = _price_cache.get(key)

    # If no exact match, try partial matching
    if entry is None:
        brand_lower = brand.lower()
        candidates = []
        for cache_key, cache_entry in _price_cache.items():
            if brand_lower in cache_key:
                candidates.append((cache_key, cache_entry))

        if candidates:
            # Pick the one with most data points
            candidates.sort(key=lambda x: x[1]["count"], reverse=True)
            entry = candidates[0][1]

    if entry is None or entry["count"] == 0:
        return None

    # Calculate weighted average
    total_weight = sum(entry["weights"])
    if total_weight == 0:
        return None

    weighted_price = sum(
        p * w for p, w in zip(entry["prices"], entry["weights"])
    ) / total_weight

    # Confidence based on data quantity and total weight
    confidence = min(95.0, entry["count"] * 15.0 + total_weight * 10.0)

    return {
        "price": round(weighted_price, 2),
        "confidence": round(confidence, 1),
        "data_points": entry["count"],
        "source": "google_sheet_historical",
    }


def get_accuracy_metrics() -> dict[str, Any]:
    """Return the latest accuracy metrics from the last refresh."""
    return {
        **_accuracy_metrics,
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
        "cache_size": len(_price_cache),
    }


def total_human_evaluator_datapoints() -> int:
    """Count of Sheet rows that contributed at least one human team price to the cache."""
    return sum(entry["count"] for entry in _price_cache.values())


def learning_loop_diagnostic_snapshot() -> dict[str, Any]:
    """Structured snapshot for ops dashboard / diagnostics."""
    m = get_accuracy_metrics()
    return {
        "human_evaluator_price_datapoints_loaded": total_human_evaluator_datapoints(),
        "distinct_normalized_item_keys": len(_price_cache),
        "sheet_ai_vs_team_accuracy": {
            k: m[k]
            for k in (
                "total_comparisons",
                "mean_error_pct",
                "median_error_pct",
                "within_20_pct",
                "within_20_count",
                "last_refresh",
            )
            if k in m
        },
        "cache_unique_keys": m.get("cache_size"),
    }


async def start_refresh_loop():
    """Background task: refresh pricing data on startup and every hour."""
    # Initial load
    count = await refresh_from_sheet()
    logger.info(f"Pricing learner: initial load complete ({count} data points)")

    # Hourly refresh
    while True:
        try:
            await asyncio.sleep(3600)  # 1 hour
            count = await refresh_from_sheet()
            logger.info(f"Pricing learner: hourly refresh ({count} data points)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Pricing learner refresh loop error: {e}")
            await asyncio.sleep(60)  # Retry in 1 minute on error
