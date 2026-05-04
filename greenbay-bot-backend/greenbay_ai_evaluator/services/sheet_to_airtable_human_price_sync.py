"""
One-way sync: Google Sheet human prices → Airtable.

Reads tab ``Customer Initiated Evaluation`` (same worksheet as CR-4 / pricing learner).
Uses column K ``Final Price Offered`` when present, otherwise J ``Internal Team Price``.
Prices are parsed with ``parse_sheet_price`` (shared with the pricing learner).

Patches ``In-House Evaluator Price (KES)`` only when an Airtable row still has a
blank in-house field.

**Timezone:** Sheet ``Date`` is the evaluation **calendar day in Africa/Nairobi
(EAT, UTC+3)**. ``Date Submitted`` in Airtable is UTC. The same Nairobi day spans
two UTC calendar days (e.g. Nairobi ``2026-03-19`` includes submissions shown as
``2026-03-18`` UTC late evening). Matching therefore considers **both** UTC date
``D-1`` and ``D`` when the Sheet calendar day is ``D`` (``YYYY-MM-DD``).

Matching is intentionally fuzzy — Sheet Item text (human) rarely equals machine-built
Airtable ``Product Name``:

  1. **Date** (primary): Sheet Nairobi calendar day vs Airtable ``Date Submitted``
     **UTC** calendar day, allowing the previous UTC day as above.
  2. **Brand**: first whitespace-separated word of Item vs Product Name (case-insensitive).
     If exactly **one** pending Airtable row shares that brand on that date → patch it.
  3. If **multiple** same-brand rows on that date → narrow with a **category hint**
     inferred from Sheet wording vs the **last token** of Product Name (evaluator slug),
     e.g. ``refrigerator``, ``washing_machine``, ``tv_monitor``.

Sheet → Airtable only; never writes back to the Sheet.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.config import DEFECT_DEDUCTIONS
from greenbay_ai_evaluator.services.airtable_service import (
    RATE_LIMIT_DELAY,
    _get_config,
    list_records_paginated,
    patch_in_house_evaluator_price,
)
from greenbay_ai_evaluator.services.google_sheets_service import read_historical_data
from greenbay_ai_evaluator.services.pricing_learner import _parse_date
from greenbay_ai_evaluator.services.sheet_price_parser import parse_sheet_price

# Last-token category slugs seen on machine-built Product Name values ("Samsung Unknown refrigerator").
_AIRTABLE_CATEGORY_TOKENS: frozenset[str] = frozenset(DEFECT_DEDUCTIONS.keys())


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
        else:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _nairobi_sheet_calendar_date(date_str: str) -> date | None:
    """Parse Sheet ``Date`` cell as a **calendar date** (Nairobi / team-local).

    Accepts ``DD/MM/YY``, ``DD/MM/YYYY``, ``YYYY-MM-DD``. Two-digit years use
    Python ``strptime`` rules (``00``–``68`` → 2000–2068, ``69``–``99`` → 1969–1999),
    so ``19/3/26`` → ``2026-03-19``, not 1926.
    """
    dt = _parse_date(date_str.strip())
    return dt.date() if dt else None


def _utc_day_keys_for_nairobi_calendar_day(cal: date) -> tuple[str, str]:
    """UTC calendar days that can correspond to Nairobi calendar day ``cal``."""
    prev = cal - timedelta(days=1)
    return (prev.isoformat(), cal.isoformat())


def _merge_pending_for_utc_days(
    pending_by_day: dict[str, list[tuple[str, str, str, str | None]]],
    utc_day_a: str,
    utc_day_b: str,
) -> list[tuple[str, str, str, str | None]]:
    """Union of pending rows for two UTC dates, stable order, deduped by record id."""
    seen: set[str] = set()
    out: list[tuple[str, str, str, str | None]] = []
    for key in (utc_day_a, utc_day_b):
        for entry in pending_by_day.get(key, []):
            rid = entry[0]
            if rid in seen:
                continue
            seen.add(rid)
            out.append(entry)
    return out


def _sheet_row_human_price(row: dict[str, Any]) -> float | None:
    """Prefer Final Price Offered (K), then Internal Team Price (J)."""
    final_raw = (row.get("Final Price Offered") or "").strip()
    internal_raw = (row.get("Internal Team Price") or "").strip()
    final_p = parse_sheet_price(final_raw) if final_raw else None
    if final_p is not None and final_p > 0:
        return final_p
    internal_p = parse_sheet_price(internal_raw) if internal_raw else None
    if internal_p is not None and internal_p > 0:
        return internal_p
    return None


def _first_word_brand(text: str) -> str:
    """First whitespace-delimited token, lowercased."""
    parts = (text or "").strip().split()
    return parts[0].lower() if parts else ""


def _extract_airtable_category_slug(product_name: str) -> str | None:
    """Category slug is typically the last token (e.g. ``tv_monitor``, ``refrigerator``)."""
    parts = (product_name or "").strip().split()
    if not parts:
        return None
    last = parts[-1].strip().lower()
    if last in _AIRTABLE_CATEGORY_TOKENS:
        return last
    return None


def _sheet_hint_matches_airtable_slug(hint: str, airtable_slug: str | None) -> bool:
    """TV/cooker naming differs between Sheet wording and Airtable last token."""
    if airtable_slug is None:
        return False
    if airtable_slug == hint:
        return True
    if hint == "tv_monitor" and airtable_slug == "tv":
        return True
    if hint == "cooker_oven" and airtable_slug == "cooker":
        return True
    return False


def _infer_sheet_category_slug(item: str) -> str | None:
    """Map free-text Sheet Item to evaluator category slug (must match Airtable last token)."""
    low = " ".join((item or "").lower().split())

    # Longer / more specific phrases first
    if "washing machine" in low or re.search(r"\bwasher\b", low):
        return "washing_machine"
    if "microwave" in low:
        return "microwave"
    if "fridge" in low or "refrigerator" in low or "freezer" in low:
        return "refrigerator"
    if "tv" in low or "television" in low or re.search(r"\d+\s*inch", low):
        return "tv_monitor"
    if "cooker" in low or "oven" in low:
        return "cooker_oven"

    return None


def _pick_airtable_record_for_sheet_row(
    item: str,
    bucket: list[tuple[str, str, str, str | None]],
) -> str | None:
    """At most one record id: same-day bucket entries are (rid, raw_name, brand, airtable_cat)."""
    sheet_brand = _first_word_brand(item)
    if not sheet_brand:
        return None

    same_brand = [e for e in bucket if e[2] == sheet_brand]
    if len(same_brand) == 1:
        return same_brand[0][0]
    if len(same_brand) == 0:
        return None

    hint = _infer_sheet_category_slug(item)
    if not hint:
        logger.debug(
            f"Sheet→Airtable: ambiguous brand={sheet_brand!r} ({len(same_brand)} rows), "
            f"no category hint from item={item!r}"
        )
        return None

    narrowed = [e for e in same_brand if _sheet_hint_matches_airtable_slug(hint, e[3])]
    if len(narrowed) == 1:
        return narrowed[0][0]
    logger.debug(
        f"Sheet→Airtable: brand={sheet_brand!r} hint={hint!r} "
        f"narrowed to {len(narrowed)} (need exactly 1)"
    )
    return None


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

    pending_by_day: dict[str, list[tuple[str, str, str, str | None]]] = defaultdict(list)
    for rec in records:
        rid = rec.get("id")
        f = rec.get("fields") or {}
        if not rid:
            continue
        day = _airtable_date_day_key(f.get("Date Submitted"))
        raw_name = (f.get("Product Name") or "").strip()
        if not day or not raw_name:
            continue
        abrand = _first_word_brand(raw_name)
        if not abrand:
            continue
        acat = _extract_airtable_category_slug(raw_name)
        if _inhouse_is_blank(f.get("In-House Evaluator Price (KES)")):
            pending_by_day[day].append((rid, raw_name, abrand, acat))

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

        cal = _nairobi_sheet_calendar_date(date_str)
        if cal is None:
            continue
        utc_a, utc_b = _utc_day_keys_for_nairobi_calendar_day(cal)
        bucket = _merge_pending_for_utc_days(pending_by_day, utc_a, utc_b)
        if not bucket:
            continue

        rid = _pick_airtable_record_for_sheet_row(item, bucket)
        if rid is None or rid in patched_ids:
            continue

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
