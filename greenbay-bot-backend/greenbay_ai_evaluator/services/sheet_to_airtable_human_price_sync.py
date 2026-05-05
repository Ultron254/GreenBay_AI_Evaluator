"""
One-way sync: Google Sheet human prices -> Airtable.

Reads tab ``Customer Initiated Evaluation``.
Uses column K ``Final Price Offered`` when present, otherwise J ``Internal Team Price``.

Strategy (v3 -- complete rewrite):
  1. Read ALL Airtable records.  Keep only those where In-House Evaluator
     Price is blank ("pending").
  2. Read ALL Sheet rows.  Keep only those with a numeric human price.
  3. For each Sheet row, match against pending Airtable records using:
       a. **Date**: Sheet calendar date (Nairobi) falls within +/-1 day of
          the Airtable ``Date Submitted`` (UTC) calendar day.
       b. **Brand**: the Airtable ``Product Name`` (lowered) **contains**
          the first word of the Sheet ``Item`` (the brand).
  4. If exactly **one** match -> patch it.
  5. If multiple matches -> log and skip (ambiguous).
  6. If zero matches -> log for manual review.

Verbose debug logging is emitted for every Sheet row so we can diagnose
mismatches row-by-row.

Sheet -> Airtable only; never writes back to the Sheet.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.services.airtable_service import (
    RATE_LIMIT_DELAY,
    _get_config,
    list_records_paginated,
    patch_in_house_evaluator_price,
)
from greenbay_ai_evaluator.services.google_sheets_service import read_historical_data
from greenbay_ai_evaluator.services.sheet_price_parser import parse_sheet_price

# Category keywords found in Sheet Item text -> Airtable slug (last word of Product Name)
_ITEM_TO_SLUG: list[tuple[str, str]] = [
    ("washing machine", "washing_machine"),
    ("washer", "washing_machine"),
    ("microwave", "microwave"),
    ("fridge", "refrigerator"),
    ("refrigerator", "refrigerator"),
    ("freezer", "refrigerator"),
    ("chest freezer", "other"),
    ("tv", "tv_monitor"),
    ("television", "tv_monitor"),
    ("inch", "tv_monitor"),
    ("cooker", "cooker_oven"),
    ("oven", "cooker_oven"),
]


def _infer_category_from_item(item: str) -> str | None:
    """Map free-text Sheet Item to Airtable Product Name's category slug."""
    low = " ".join((item or "").lower().split())
    for keyword, slug in _ITEM_TO_SLUG:
        if keyword in low:
            return slug
    return None


def _airtable_category_slug(product_name: str) -> str | None:
    """Last word of Airtable Product Name (e.g. 'refrigerator', 'tv_monitor')."""
    parts = (product_name or "").strip().split()
    return parts[-1].lower() if parts else None


# ---- date parsing ---------------------------------------------------------

def _parse_sheet_date(text: str) -> date | None:
    """Parse Sheet ``Date`` cell into a ``date``.

    Handles DD/MM/YY, DD/MM/YYYY, M/D/YY, M/D/YYYY, YYYY-MM-DD,
    and ``D-Mon-YYYY`` / ``DD-Mon-YYYY`` (e.g. ``4-May-2026``).
    Two-digit years: 00-68 -> 2000-2068, 69-99 -> 1969-1999.

    When DD/MM and M/D are ambiguous (both would be valid), DD/MM wins
    because the Nairobi team normally uses that format. When DD/MM fails
    (e.g. ``4/13/26`` -- month 13 is impossible), M/D is tried as fallback.
    """
    s = (text or "").strip()
    if not s:
        return None
    for fmt in (
        "%d/%m/%y", "%d/%m/%Y",
        "%m/%d/%y", "%m/%d/%Y",
        "%Y-%m-%d",
        "%d-%b-%Y", "%d-%b-%y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _airtable_utc_date(iso_val: str | None) -> date | None:
    """Extract the UTC calendar date from an Airtable ISO timestamp."""
    if not iso_val:
        return None
    s = str(iso_val).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        return dt.date()
    except (ValueError, TypeError):
        return None


def _dates_within_one_day(a: date, b: date) -> bool:
    return abs((a - b).days) <= 1


# ---- price parsing --------------------------------------------------------

def _sheet_row_human_price(row: dict[str, Any]) -> float | None:
    """Prefer Final Price Offered (K), then Internal Team Price (J)."""
    for col in ("Final Price Offered", "Internal Team Price"):
        raw = (row.get(col) or "").strip()
        if raw:
            p = parse_sheet_price(raw)
            if p is not None and p > 0:
                return p
    return None


# ---- brand helpers --------------------------------------------------------

def _first_word_lower(text: str) -> str:
    """First whitespace-delimited token, lowercased."""
    parts = (text or "").strip().split()
    return parts[0].lower() if parts else ""


def _inhouse_is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        return float(value) <= 0
    except (TypeError, ValueError):
        return True


# ---- main sync ------------------------------------------------------------

def sync_human_evaluator_prices_from_sheet_sync() -> int:
    """Blocking sync.  Returns number of Airtable records patched."""
    cfg = _get_config()
    if cfg is None:
        logger.debug("Sheet->Airtable sync: Airtable not configured, skip")
        return 0

    # Step 1: Read ALL Airtable records, keep those with blank In-House price
    airtable_fields = [
        "Date Submitted",
        "Product Name",
        "Brand",
        "In-House Evaluator Price (KES)",
    ]
    all_records = list_records_paginated(cfg, fields=airtable_fields)
    if not all_records:
        logger.info("Sheet->Airtable sync: no Airtable records fetched")
        return 0

    # Build list of pending Airtable records: (record_id, utc_date, product_name, brand)
    pending: list[tuple[str, date, str, str]] = []
    for rec in all_records:
        rid = rec.get("id")
        f = rec.get("fields") or {}
        if not rid:
            continue
        if not _inhouse_is_blank(f.get("In-House Evaluator Price (KES)")):
            continue
        utc_d = _airtable_utc_date(f.get("Date Submitted"))
        if utc_d is None:
            continue
        product_name = (f.get("Product Name") or "").strip().lower()
        brand = (f.get("Brand") or "").strip().lower()
        if not product_name:
            continue
        pending.append((rid, utc_d, product_name, brand))

    logger.info(
        f"Sheet->Airtable sync: {len(all_records)} total Airtable records, "
        f"{len(pending)} with blank In-House price"
    )

    # Step 2: Read Sheet rows
    rows = read_historical_data()
    if not rows:
        logger.info("Sheet->Airtable sync: no Sheet rows, skip")
        return 0

    patched_ids: set[str] = set()
    patched_count = 0
    sheet_with_price = 0

    # Step 3: For each Sheet row with a price, find matching Airtable records
    for row in rows:
        price = _sheet_row_human_price(row)
        if price is None:
            continue
        sheet_with_price += 1

        date_str = (row.get("Date") or "").strip()
        item = (row.get("Item") or "").strip()
        if not item:
            logger.debug(f"SYNC ROW SKIP: no Item | Date={date_str!r}")
            continue

        sheet_date = _parse_sheet_date(date_str)
        sheet_brand = _first_word_lower(item)
        if not sheet_date:
            logger.warning(
                f"SYNC ROW SKIP: unparseable date | "
                f"Date={date_str!r} Item={item!r} Price={price}"
            )
            continue
        if not sheet_brand:
            logger.debug(
                f"SYNC ROW SKIP: no brand extracted | "
                f"Date={date_str!r} Item={item!r}"
            )
            continue

        sheet_iso = sheet_date.isoformat()

        # Find candidates: date within +/-1 day AND product_name contains brand
        candidates: list[tuple[str, date, str, str]] = []
        for rid, at_date, at_product, at_brand in pending:
            if rid in patched_ids:
                continue
            if not _dates_within_one_day(sheet_date, at_date):
                continue
            # Match brand: Airtable Product Name contains the Sheet brand word
            if sheet_brand in at_product or sheet_brand == at_brand:
                candidates.append((rid, at_date, at_product, at_brand))

        date_window = [
            (sheet_date - timedelta(days=1)).isoformat(),
            sheet_iso,
            (sheet_date + timedelta(days=1)).isoformat(),
        ]

        if len(candidates) == 0:
            logger.warning(
                f"SYNC NO MATCH | Sheet: Date={date_str!r} -> {sheet_iso}, "
                f"Brand={sheet_brand!r}, Item={item!r}, Price={price} | "
                f"Checked +/-1 day window {date_window} against {len(pending)} "
                f"pending Airtable records"
            )
            continue

        if len(candidates) > 1:
            # Try narrowing by category slug inferred from Sheet Item text
            sheet_cat = _infer_category_from_item(item)
            if sheet_cat:
                narrowed = [
                    c for c in candidates
                    if _airtable_category_slug(c[2]) == sheet_cat
                ]
                if len(narrowed) == 1:
                    candidates = narrowed
                    logger.debug(
                        f"SYNC NARROWED by category={sheet_cat!r} | "
                        f"Sheet: {date_str!r}, Item={item!r}"
                    )
            if len(candidates) > 1:
                cand_names = [f"{c[0]}:{c[2]}" for c in candidates]
                logger.warning(
                    f"SYNC AMBIGUOUS ({len(candidates)} matches) | "
                    f"Sheet: Date={date_str!r} -> {sheet_iso}, "
                    f"Brand={sheet_brand!r}, Item={item!r}, Price={price} | "
                    f"Candidates: {cand_names}"
                )
                continue

        # Exactly one match
        match_rid, match_date, match_product, match_brand = candidates[0]
        logger.info(
            f"SYNC MATCH | Sheet: {date_str!r} -> {sheet_iso}, "
            f"Brand={sheet_brand!r}, Item={item!r}, Price={price} | "
            f"Airtable: id={match_rid}, date={match_date.isoformat()}, "
            f"product={match_product!r}"
        )
        if patch_in_house_evaluator_price(match_rid, price):
            patched_count += 1
            patched_ids.add(match_rid)
            time.sleep(RATE_LIMIT_DELAY)
        else:
            logger.warning(f"SYNC PATCH FAILED for {match_rid}")

    logger.info(
        f"Sheet->Airtable sync DONE: patched {patched_count} record(s) "
        f"(sheet rows with price: {sheet_with_price}, "
        f"pending Airtable records: {len(pending)}, "
        f"total Airtable: {len(all_records)})"
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
