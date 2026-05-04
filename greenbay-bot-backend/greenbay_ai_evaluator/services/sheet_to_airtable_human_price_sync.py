"""
One-way sync: Google Sheet human prices → Airtable.

Reads tab ``Customer Initiated Evaluation`` (same worksheet as CR-4 / pricing learner).
Uses column K ``Final Price Offered`` when present, otherwise J ``Internal Team Price``.
Patches Airtable ``In-House Evaluator Price (KES)`` only when the Sheet has a numeric
price and an Airtable row on the **same calendar day** still has a blank in-house field.

Matching (same day):
  1. Normalized Sheet ``Item`` vs Airtable ``Product Name``: substring/containment
     (either normalized key is contained in the other); catches ``Samsung RF28`` vs
     ``Samsung RF28 refrigerator``.
  2. If still unmatched: compare **first word** (brand token) of raw Item vs raw
     Product Name.

Sheet → Airtable only; never writes back to the Sheet.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.services.airtable_service import (
    RATE_LIMIT_DELAY,
    _get_config,
    list_records_paginated,
    patch_in_house_evaluator_price,
)
from greenbay_ai_evaluator.services.google_sheets_service import read_historical_data
from greenbay_ai_evaluator.services.pricing_learner import _normalize_key, _parse_date, _parse_price


def _inhouse_is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return float(value) <= 0
    except (TypeError, ValueError):
        return True


def _airtable_date_day_key(iso_val: str | None) -> str | None:
    if not iso_val or not str(iso_val).strip():
        return None
    s = str(iso_val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _sheet_row_human_price(row: dict[str, Any]) -> float | None:
    """Prefer Final Price Offered (K), then Internal Team Price (J)."""
    final_raw = (row.get("Final Price Offered") or "").strip()
    internal_raw = (row.get("Internal Team Price") or "").strip()
    final_p = _parse_price(final_raw) if final_raw else None
    if final_p is not None and final_p > 0:
        return final_p
    internal_p = _parse_price(internal_raw) if internal_raw else None
    if internal_p is not None and internal_p > 0:
        return internal_p
    return None


def _normalized_names_match_loose(sheet_norm: str, airtable_norm: str) -> bool:
    """True if keys are equal or one normalized key is a substring of the other."""
    if not sheet_norm or not airtable_norm:
        return False
    if sheet_norm == airtable_norm:
        return True
    if len(sheet_norm) < 2 or len(airtable_norm) < 2:
        return False
    return sheet_norm in airtable_norm or airtable_norm in sheet_norm


def _first_word_brand(text: str) -> str:
    """First whitespace-delimited token, lowercased (brand hint)."""
    parts = (text or "").strip().split()
    return parts[0].lower() if parts else ""


def sync_human_evaluator_prices_from_sheet_sync() -> int:
    """Blocking sync. Returns number of Airtable records patched this run."""
    cfg = _get_config()
    if cfg is None:
        logger.debug("Sheet→Airtable human price sync: Airtable not configured, skip")
        return 0

    rows = read_historical_data()
    if not rows:
        logger.info("Sheet→Airtable human price sync: no Sheet rows, skip")
        return 0

    fields = [
        "Date Submitted",
        "Product Name",
        "In-House Evaluator Price (KES)",
    ]
    records = list_records_paginated(cfg, fields=fields)
    if not records:
        logger.info("Sheet→Airtable human price sync: no Airtable records fetched")
        return 0

    pending_by_day: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for rec in records:
        rid = rec.get("id")
        f = rec.get("fields") or {}
        if not rid:
            continue
        day = _airtable_date_day_key(f.get("Date Submitted"))
        raw_name = (f.get("Product Name") or "").strip()
        if not day or not raw_name:
            continue
        name_key = _normalize_key(raw_name)
        if not name_key:
            continue
        if _inhouse_is_blank(f.get("In-House Evaluator Price (KES)")):
            pending_by_day[day].append((rid, name_key, raw_name))

    patched_ids: set[str] = set()
    patched_count = 0
    sheet_with_price = 0

    for row in rows:
        price = _sheet_row_human_price(row)
        if price is None:
            continue
        sheet_with_price += 1

        date_str = (row.get("Date") or "").strip()
        item = (row.get("Item") or "").strip()
        if not item:
            continue

        dt = _parse_date(date_str)
        if dt is None:
            continue
        day_key = dt.strftime("%Y-%m-%d")
        sheet_key = _normalize_key(item)
        if not sheet_key:
            continue

        bucket = pending_by_day.get(day_key, [])
        if not bucket:
            continue

        sheet_brand = _first_word_brand(item)

        # Pass 1: normalized substring / containment match
        for rid, at_norm, raw_at in bucket:
            if rid in patched_ids:
                continue
            if _normalized_names_match_loose(sheet_key, at_norm):
                if patch_in_house_evaluator_price(rid, price):
                    patched_count += 1
                    patched_ids.add(rid)
                    time.sleep(RATE_LIMIT_DELAY)

        # Pass 2: same calendar day, first-word brand match (secondary)
        for rid, at_norm, raw_at in bucket:
            if rid in patched_ids:
                continue
            if sheet_brand and _first_word_brand(raw_at) == sheet_brand:
                if patch_in_house_evaluator_price(rid, price):
                    patched_count += 1
                    patched_ids.add(rid)
                    time.sleep(RATE_LIMIT_DELAY)

    logger.info(
        f"Sheet→Airtable human price sync: patched {patched_count} record(s) "
        f"(sheet rows with J/K price: {sheet_with_price}, airtable rows fetched: {len(records)})"
    )
    return patched_count


async def sync_human_evaluator_prices_from_sheet() -> int:
    """Async entrypoint: runs blocking sync in the default executor."""
    loop = asyncio.get_event_loop()
    return int(await loop.run_in_executor(None, sync_human_evaluator_prices_from_sheet_sync))


if __name__ == "__main__":
    import sys

    n = sync_human_evaluator_prices_from_sheet_sync()
    print(f"Patched {n} record(s)")
    sys.exit(0 if n >= 0 else 1)
