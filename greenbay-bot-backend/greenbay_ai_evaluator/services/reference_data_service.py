"""
Reference data service (v6 — Workstream 3).

Reads two GreenBay Google Sheets on startup + every 6 hours and caches
them in Postgres for fast lookup by the pricing engine.

Source A — Pricing Matrix (board-approved pricing guide):
  Sheet ID 1_6RqiRaGshsY-iVssXpyLR2avlHsV_riiwXHTKqMz3c

Source B — Outlet Stock Control (everything ever sold):
  Sheet ID 1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE  sheet "Final Data"

Lookup functions:
  lookup_matrix(brand, model, category, age_band)
  lookup_sales_stock(brand, model, category)
  get_category_acquisition_ratio(category)
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

PRICING_MATRIX_SHEET_ID = "1_6RqiRaGshsY-iVssXpyLR2avlHsV_riiwXHTKqMz3c"
SALES_STOCK_SHEET_ID = "1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE"
SALES_STOCK_TAB = "Final Data"

# AI Evaluation & Pricing Tracker — the team logs the human/internal price here
# (sometimes filled in later). We ingest the 'Internal Team Price' column for the
# AI-vs-human MONITORING panel ONLY. It is deliberately NEVER fed into the pricing
# engine (see SHEET_INTERNAL_MARKER usage in the router) so prices stay grounded
# in the math + sales/matrix/Gemini, with or without an internal price present.
EVAL_TRACKER_SHEET_ID = "1DCKTWSxvGYQzuEPoJanQFF5ssM9oWnqVmEoEmh1MhAU"
EVAL_TRACKER_TAB = "Customer Initiated Evaluation"
SHEET_INTERNAL_MARKER = "Sheet: Customer Initiated Eval"  # expert_name tag for synced rows

MATRIX_TAB_TO_CATEGORY: dict[str, str] = {
    "TV Pricing Matrix": "TV",
    "Cooker Pricing Matrix": "Cooker",
    "Fridge Pricing Matrix": "Refrigerator",
    "Freezers Pricing Matrix": "Freezer",
    "Washing Machine Pricing Matrix": "Washing Machine",
    "Microwave Pricing Matrix": "Microwave",
    "Home/ Kitchen Appliances Pricing Matrix": "Home Kitchen Appliances",
    "Other Appliances Pricing Matrix": "Other Appliances",
}

ACQUISITION_RATIOS: dict[str, float] = {
    "refrigerator": 0.73,
    "fridge": 0.73,
    "cooker": 0.72,
    "cooker_oven": 0.72,
    "tv": 0.81,
    "tv_monitor": 0.81,
    "washing_machine": 0.74,
    "washer": 0.74,
    "freezer": 0.77,
    "chiller": 0.77,
    # Small / low-value appliances: trimmed Jun 2026 after the AI-vs-internal
    # tracker showed consistent over-valuation here (AI ~35% of new vs internal
    # ~20%). These items are harder to resell and need a wider acquisition margin.
    "microwave": 0.55,        # was 0.72
    "soundbar": 0.55,         # was 0.72
    "woofer": 0.55,           # was 0.85
    "speaker": 0.55,
    "home_theatre": 0.55,
    "water_dispenser": 0.58,  # was 0.78
    "air_fryer": 0.55,
    "fan": 0.50,
    "blender": 0.50,
    # Calibrated from real purchase/sell data (>=20 deals each, Jun 2026):
    "kettle": 0.60,      # was defaulting to 0.70 -> overpriced (within-20% only 49%)
    "iron_box": 0.55,    # was 0.72 -> over-valued vs internal
}
DEFAULT_ACQUISITION_RATIO = 0.70

_last_refresh: datetime | None = None
_matrix_cache: list[dict[str, Any]] = []
_sales_cache: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Google Sheets helpers (reuse credential pattern from google_sheets_service)
# ---------------------------------------------------------------------------
def _get_gspread_client():
    """Return an authorized gspread client, or None."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning("gspread or google-auth not installed")
        return None

    try:
        from app.config import get_settings
        settings = get_settings()

        creds_file = getattr(settings, "google_sheets_credentials_file", "") or ""
        vertex_file = getattr(settings, "google_vertex_credentials_file", "") or ""

        chosen: str | None = None
        if creds_file and Path(creds_file).exists():
            chosen = creds_file
        elif vertex_file and Path(vertex_file).exists():
            chosen = vertex_file

        if not chosen:
            logger.warning("Reference data: no Google credentials found")
            return None

        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(chosen, scopes=scopes)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Reference data: gspread auth failed: {e}")
        return None


def _safe_float(val: Any) -> float | None:
    """Parse a value to float, returning None on failure."""
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("KES", "").replace("Ksh", "")
    if not s or s.lower() in ("n/a", "-", ""):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _sanitize_for_json(row: dict) -> str:
    """Serialize a gspread row dict to a JSON *string*.

    Returns a ``str`` (not a dict) so psycopg2 can pass it directly to
    PostgreSQL's ``json`` column without needing its own Json adapter.
    This sidesteps the ``InvalidTextRepresentation`` error that occurs
    when psycopg2's Json adapter chokes on edge-case Python values.
    """
    try:
        return json.dumps(row, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({str(k): str(v) for k, v in row.items()})


# ---------------------------------------------------------------------------
# Sheet readers
# ---------------------------------------------------------------------------
def _read_pricing_matrix(gc) -> list[dict]:
    """Read all tabs from the pricing matrix sheet."""
    rows_out: list[dict] = []
    try:
        spreadsheet = gc.open_by_key(PRICING_MATRIX_SHEET_ID)
    except Exception as e:
        logger.error(f"Reference data: cannot open pricing matrix sheet: {e}")
        return rows_out

    # Diagnostic: log actual worksheet titles so we can match them
    try:
        actual_titles = [ws.title for ws in spreadsheet.worksheets()]
        logger.info(f"Reference data: pricing matrix actual tabs: {actual_titles}")
    except Exception as e:
        logger.warning(f"Reference data: could not list worksheet titles: {e}")

    for tab_name, category_slug in MATRIX_TAB_TO_CATEGORY.items():
        try:
            ws = spreadsheet.worksheet(tab_name)
            raw_rows = ws.get_all_values()

            # Auto-detect header: row containing "brand" AND "product name" (substring, case-insensitive)
            header_idx = None
            headers: list[str] = []
            for i, row in enumerate(raw_rows[:15]):
                cells = [str(c).strip() for c in row]
                lower_cells = [c.lower() for c in cells]
                has_brand = "brand" in lower_cells
                has_product_name = any("product name" in c for c in lower_cells)
                if has_brand and has_product_name:
                    header_idx = i
                    headers = cells
                    break

            if header_idx is None:
                logger.warning(
                    f"Reference data: tab '{tab_name}' — no header row found in first 15 rows. "
                    f"Row samples: {[r[:8] for r in raw_rows[:6]]}"
                )
                continue

            # Filter out empty-name columns; build a lowercase lookup dict per row
            valid_cols = [(ci, h) for ci, h in enumerate(headers) if h]
            col_names = [h for _, h in valid_cols]
            col_indices = [ci for ci, _ in valid_cols]

            logger.info(
                f"Reference data: tab '{tab_name}' header at row {header_idx}: {col_names}"
            )

            data_rows = raw_rows[header_idx + 1:]
            tab_count = 0
            for row in data_rows:
                raw_rec = {col_names[j]: (str(row[ci]).strip() if ci < len(row) else "")
                           for j, ci in enumerate(col_indices)}
                if not any(raw_rec.values()):
                    continue
                # Normalize keys: lowercase, strip, remove trailing " kes"
                nr: dict[str, str] = {}
                for k, v in raw_rec.items():
                    nk = k.lower().strip()
                    nr[nk] = v
                    if nk.endswith(" kes"):
                        nr[nk[:-4]] = v  # e.g. "base min kes" -> also store as "base min"
                rows_out.append({
                    "brand": nr.get("brand", ""),
                    "model": nr.get("model number", nr.get("model", "")),
                    "product_name": nr.get("product name", ""),
                    "category": category_slug,
                    "age_band": nr.get("age of appliance", nr.get("age", nr.get("age band", ""))),
                    "specification": nr.get("specification/size", nr.get("specification/type", "")),
                    "base_min": _safe_float(nr.get("base min")),
                    "base_max": _safe_float(nr.get("base max")),
                    "condition_grade": nr.get("condition grade", nr.get("grade", "")),
                    "recommended_min": _safe_float(nr.get("recommended min")),
                    "recommended_max": _safe_float(nr.get("recommended max")),
                    "new_price": _safe_float(nr.get("new price")),
                    "sheet_tab": tab_name,
                    "raw_json": _sanitize_for_json(raw_rec),
                })
                tab_count += 1
            logger.info(
                f"Reference data: pricing matrix tab '{tab_name}' -> '{category_slug}' — {tab_count} rows"
            )
        except Exception as e:
            logger.warning(
                f"Reference data: pricing matrix tab '{tab_name}' failed: "
                f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            )

    return rows_out


def _read_sales_stock(gc) -> list[dict]:
    """Read the outlet stock control sheet."""
    rows_out: list[dict] = []
    try:
        spreadsheet = gc.open_by_key(SALES_STOCK_SHEET_ID)
        ws = spreadsheet.worksheet(SALES_STOCK_TAB)
        records = ws.get_all_records()
        for row in records:
            rows_out.append({
                "product_category": str(row.get("Product Category", "")).strip(),
                "brand_name": str(row.get("Brand Name", row.get("Brand", ""))).strip(),
                "product_name": str(row.get("Product Name", "")).strip(),
                "model_number": str(row.get("Model Number", row.get("Model", ""))).strip(),
                "product_quality": str(row.get("Product Quality", row.get("Quality", ""))).strip(),
                "purchase_cost": _safe_float(row.get("Purchase cost", row.get("Purchase Cost"))),
                "selling_price": _safe_float(row.get("Selling Price", row.get("Selling price"))),
                "raw_json": _sanitize_for_json(row),
            })
        logger.info(f"Reference data: sales stock — {len(records)} rows")
    except Exception as e:
        logger.error(
            f"Reference data: sales stock read failed: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
    return rows_out


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------
def _coerce_row_for_matrix(r: dict) -> dict:
    """Build a clean kwargs dict for PricingMatrixReference, coercing all types."""
    return {
        "brand": str(r.get("brand") or ""),
        "model": str(r.get("model") or ""),
        "category": str(r.get("category") or ""),
        "age_band": str(r.get("age_band") or ""),
        "base_min": _safe_float(r.get("base_min")),
        "base_max": _safe_float(r.get("base_max")),
        "condition_grade": str(r.get("condition_grade") or ""),
        "recommended_min": _safe_float(r.get("recommended_min")),
        "recommended_max": _safe_float(r.get("recommended_max")),
        "new_price": _safe_float(r.get("new_price")),
        "sheet_tab": str(r.get("sheet_tab") or ""),
        "raw_json": r.get("raw_json") if isinstance(r.get("raw_json"), str)
                    else json.dumps(r.get("raw_json", {}), default=str),
    }


def _coerce_row_for_sales(r: dict) -> dict:
    """Build a clean kwargs dict for SalesStockReference, coercing all types."""
    return {
        "product_category": str(r.get("product_category") or ""),
        "brand_name": str(r.get("brand_name") or ""),
        "product_name": str(r.get("product_name") or ""),
        "model_number": str(r.get("model_number") or ""),
        "product_quality": str(r.get("product_quality") or ""),
        "purchase_cost": _safe_float(r.get("purchase_cost")),
        "selling_price": _safe_float(r.get("selling_price")),
        "raw_json": r.get("raw_json") if isinstance(r.get("raw_json"), str)
                    else json.dumps(r.get("raw_json", {}), default=str),
    }


def _persist_to_db(matrix_rows: list[dict], sales_rows: list[dict]) -> None:
    """Replace reference tables in Postgres with fresh data.

    Pre-coerces every value so the INSERT never hits a type mismatch.
    Bad rows are logged individually and skipped.
    """
    try:
        from app.database.db import SessionLocal
        from greenbay_ai_evaluator.models.evaluator_models import (
            PricingMatrixReference,
            SalesStockReference,
        )
    except Exception as e:
        logger.error(f"Reference data: DB import failed: {e}")
        return

    # Phase 1: build validated ORM objects, logging any coercion failures
    matrix_objs: list = []
    for idx, r in enumerate(matrix_rows):
        try:
            kwargs = _coerce_row_for_matrix(r)
            matrix_objs.append(PricingMatrixReference(**kwargs))
        except Exception as e:
            logger.error(
                f"Reference data: matrix row {idx} coercion failed: "
                f"{type(e).__name__}: {e}\n"
                f"Row data: {json.dumps(r, default=str)}"
            )

    sales_objs: list = []
    for idx, r in enumerate(sales_rows):
        try:
            kwargs = _coerce_row_for_sales(r)
            sales_objs.append(SalesStockReference(**kwargs))
        except Exception as e:
            logger.error(
                f"Reference data: sales row {idx} coercion failed: "
                f"{type(e).__name__}: {e}\n"
                f"Row data: {json.dumps(r, default=str)}"
            )

    # Phase 2: single transaction — delete old + insert all valid rows
    db = SessionLocal()
    try:
        db.query(PricingMatrixReference).delete()
        db.query(SalesStockReference).delete()
        db.flush()

        for obj in matrix_objs:
            db.add(obj)
        for obj in sales_objs:
            db.add(obj)

        db.commit()
        logger.info(
            f"Reference data: persisted {len(matrix_objs)}/{len(matrix_rows)} matrix rows "
            f"and {len(sales_objs)}/{len(sales_rows)} sales rows to DB"
        )
    except Exception as e:
        db.rollback()
        logger.error(
            f"Reference data: DB persist transaction failed: "
            f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        # Fall back to per-row insert to identify the exact offending row
        logger.info("Reference data: retrying per-row to find bad row...")
        db2 = SessionLocal()
        try:
            db2.query(PricingMatrixReference).delete()
            db2.query(SalesStockReference).delete()
            db2.commit()
        except Exception:
            db2.rollback()
        _persist_per_row(db2, "matrix", matrix_objs, PricingMatrixReference)
        _persist_per_row(db2, "sales", sales_objs, SalesStockReference)
        db2.close()
    finally:
        db.close()


def _persist_per_row(db, label: str, objs: list, model_cls) -> int:
    """Insert rows one at a time, logging each failure individually."""
    from sqlalchemy.orm import make_transient
    ok = 0
    for idx, obj in enumerate(objs):
        try:
            make_transient(obj)
            obj.id = None
            db.add(obj)
            db.commit()
            ok += 1
        except Exception as e:
            db.rollback()
            row_data = {c.name: getattr(obj, c.name, None)
                        for c in obj.__table__.columns if c.name != "id"}
            logger.error(
                f"Reference data: {label} row {idx} INSERT failed: "
                f"{type(e).__name__}: {e}\n"
                f"Column values: {json.dumps(row_data, default=str)}\n"
                f"{traceback.format_exc()}"
            )
    logger.info(f"Reference data: per-row {label} insert: {ok}/{len(objs)} succeeded")
    return ok


# ---------------------------------------------------------------------------
# Internal/in-house price ingestion (monitoring only — NOT used for pricing)
# ---------------------------------------------------------------------------
_CATEGORY_KEYWORDS: list[tuple[str, str]] = [
    ("refrigerator", "refrigerator"), ("fridge", "refrigerator"),
    ("freezer", "freezer"), ("chiller", "chiller"),
    ("washing", "washing_machine"), ("washer", "washing_machine"),
    ("cooker", "cooker_oven"), ("oven", "cooker_oven"),
    ("microwave", "microwave"),
    ("tv", "tv_monitor"), ("television", "tv_monitor"), ("monitor", "tv_monitor"),
    ("woofer", "woofer"), ("subwoofer", "woofer"),
    ("soundbar", "soundbar"), ("sound bar", "soundbar"),
    ("home theatre", "soundbar"), ("home theater", "soundbar"),
    ("water dispenser", "water_dispenser"), ("dispenser", "water_dispenser"),
    ("kettle", "kettle"), ("iron", "iron_box"),
]


def _infer_category_from_text(text: str) -> str:
    t = (text or "").lower()
    for kw, slug in _CATEGORY_KEYWORDS:
        if kw in t:
            return slug
    return "other"


def _infer_brand_from_text(item: str) -> str:
    """First meaningful token of the item description is usually the brand."""
    tokens = [w for w in (item or "").strip().split() if w]
    return tokens[0].strip().title() if tokens else ""


def _find_header_index(
    values: list[list[str]], *, require_internal: bool = True
) -> tuple[int, dict[str, int]]:
    """Locate the header row + map our target columns to indices, by name.

    The sheet's columns are fixed even though casual rows look ragged, so we map
    by header text (separator/case-insensitive contains) rather than position.

    ``require_internal``: reads that pair AI vs internal prices need the
    human-managed "Internal Team Price" column. The WRITE path must NOT depend
    on it — renaming/removing that one column would silently stop all appends —
    so writers pass require_internal=False and only need "AI Price" + "Item".
    """
    targets = {
        "date": ["date"],
        "item": ["item"],
        "model": ["model"],
        "condition": ["condition"],
        "age": ["age"],
        "new_price": ["new price"],
        "ai_price": ["ai price"],
        "ai_confidence": ["confidence"],
        "customer_price": ["customer selling price"],
        "internal_price": ["internal team price"],
        "final_price": ["final price offered"],
        "accepted": ["accepted"],
        "notes": ["notes"],
        "rationale": ["rationale"],
    }
    for ridx, row in enumerate(values[:10]):
        norm = [_RE_SEP.sub(" ", str(c or "").lower()).strip() for c in row]
        has_ai = any("ai price" in c for c in norm)
        has_internal = any("internal team price" in c for c in norm)
        has_item = any("item" in c for c in norm)
        if has_ai and (has_internal if require_internal else (has_internal or has_item)):
            col_map: dict[str, int] = {}
            for key, needles in targets.items():
                for cidx, cell in enumerate(norm):
                    if any(n in cell for n in needles):
                        col_map[key] = cidx
                        break
            return ridx, col_map
    return -1, {}


def _read_customer_initiated_evals(gc) -> list[dict]:
    """Read AI-vs-internal price pairs from the evaluation tracker sheet.

    Returns dicts with system_price (AI) + expert_price (internal team). Only
    rows where BOTH parse to a real price (>=500) are kept."""
    out: list[dict] = []
    try:
        ss = gc.open_by_key(EVAL_TRACKER_SHEET_ID)
        try:
            ws = ss.worksheet(EVAL_TRACKER_TAB)
        except Exception:
            ws = ss.sheet1  # fall back to first tab
        values = ws.get_all_values()
    except Exception as e:  # noqa: BLE001
        logger.error(f"Internal price sync: read failed: {type(e).__name__}: {e}")
        return out

    hidx, cmap = _find_header_index(values)
    if hidx < 0 or "ai_price" not in cmap or "internal_price" not in cmap:
        logger.warning("Internal price sync: header row not found")
        return out

    def cell(row, key):
        i = cmap.get(key)
        return row[i] if i is not None and i < len(row) else ""

    for row in values[hidx + 1:]:
        if not any(str(c).strip() for c in row):
            continue
        ai = _safe_float(cell(row, "ai_price"))
        internal = _safe_float(cell(row, "internal_price"))
        if not ai or not internal or ai < 500 or internal < 500:
            continue
        item = str(cell(row, "item")).strip()
        model = str(cell(row, "model")).strip()
        # Model column sometimes holds a condition digit; ignore pure numbers.
        if model.replace(".", "").isdigit():
            model = ""
        out.append({
            "item": item,
            "brand": _infer_brand_from_text(item),
            "model": model,
            "category": _infer_category_from_text(item),
            "system_price": ai,
            "expert_price": internal,
            "date": str(cell(row, "date")).strip(),
            "accepted": str(cell(row, "accepted")).strip(),
            "notes": (str(cell(row, "notes")).strip() or str(cell(row, "rationale")).strip()),
        })
    logger.info(f"Internal price sync: parsed {len(out)} AI/internal pairs from sheet")
    return out


def sync_internal_prices_from_sheet() -> dict:
    """Upsert sheet internal prices into ExpertPriceFeedback (idempotent).

    Tagged with SHEET_INTERNAL_MARKER so the dashboard's AI-vs-human panel fills
    up while the PRICING path explicitly excludes these rows."""
    result = {"parsed": 0, "inserted": 0, "skipped_existing": 0}
    gc = _get_gspread_client()
    if gc is None:
        result["error"] = "no gspread client"
        return result

    rows = _read_customer_initiated_evals(gc)
    result["parsed"] = len(rows)
    if not rows:
        return result

    try:
        from app.database.db import SessionLocal
        from app.database.models import ExpertPriceFeedback
    except Exception as e:  # noqa: BLE001
        result["error"] = f"import failed: {e}"
        return result

    db = SessionLocal()
    try:
        for r in rows:
            # Idempotency: same marker + model + both prices already present.
            exists = (
                db.query(ExpertPriceFeedback)
                .filter(
                    ExpertPriceFeedback.expert_name == SHEET_INTERNAL_MARKER,
                    ExpertPriceFeedback.model == (r["model"] or ""),
                    ExpertPriceFeedback.expert_price == r["expert_price"],
                    ExpertPriceFeedback.system_price == r["system_price"],
                )
                .first()
            )
            if exists:
                result["skipped_existing"] += 1
                continue
            db.add(ExpertPriceFeedback(
                valuation_session_id=None,
                expert_name=SHEET_INTERNAL_MARKER,
                expert_price=r["expert_price"],
                system_price=r["system_price"],
                price_difference=r["expert_price"] - r["system_price"],
                expert_reasoning=(
                    f"[{r.get('date','')}] {r.get('item','')} "
                    f"{r.get('accepted','')} {r.get('notes','')}"
                ).strip(),
                product_category=r["category"],
                brand=r["brand"],
                model=r["model"] or None,
            ))
            result["inserted"] += 1
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        result["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"Internal price sync: persist failed: {e}")
    finally:
        db.close()

    logger.info(f"Internal price sync: {result}")
    return result


# ---------------------------------------------------------------------------
# Tracker-sheet New Price write-back (header-aware; matches Airtable 1:1)
# ---------------------------------------------------------------------------
_sheet_backfill_state: dict = {"running": False, "started_at": None, "progress": {}}
_tracker_write_layout: dict | None = None  # cached {cmap, ncols}


def _col_letter(idx: int) -> str:
    """0-indexed column -> A1 letter (handles A..ZZ)."""
    s = ""
    n = idx
    while True:
        s = chr(ord("A") + n % 26) + s
        n = n // 26 - 1
        if n < 0:
            break
    return s


def _service_account_email() -> str:
    try:
        import json as _json
        from app.config import get_settings
        s = get_settings()
        f = (getattr(s, "google_sheets_credentials_file", "") or
             getattr(s, "google_vertex_credentials_file", "") or "")
        if f and Path(f).exists():
            with open(f, "r", encoding="utf-8") as fh:
                return _json.load(fh).get("client_email", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _airtable_new_price_for(model: str) -> float | None:
    """Look up the same product in Airtable (well-structured, already Gemini-
    repriced) by Model Number and return its 'New Price (Estimate)'. Free — no
    Gemini call — so we use it as the preferred guide before searching."""
    if not model or not str(model).strip():
        return None
    try:
        from greenbay_ai_evaluator.services.airtable_service import list_records_by_model
        prices: list[float] = []
        for r in list_records_by_model(str(model).strip()):
            v = (r.get("fields", {}) or {}).get("New Price (Estimate)")
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if v and v > 0:
                prices.append(v)
        if prices:
            prices.sort()
            return prices[len(prices) // 2]  # median of matching Airtable rows
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Airtable new-price lookup failed for {model!r}: {e}")
    return None


def _gemini_new_price(item: str, model: str, category: str, country: str = "KE") -> float | None:
    """Best-effort Gemini-grounded NEW retail price for a tracker row."""
    try:
        from greenbay_ai_evaluator.services.market_price_service import gemini_price_research
        brand = _infer_brand_from_text(item)
        cat = category or _infer_category_from_text(item)
        res = gemini_price_research(
            brand=brand, model=(model or item), category=cat, country=country,
        )
        if res and res.launch_price and res.launch_price > 0:
            return float(res.launch_price)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Sheet backfill: price lookup failed for {item!r}: {e}")
    return None


def backfill_sheet_new_prices(dry_run: bool = True, limit: int = 1000,
                              force: bool = False) -> dict:
    """Fill the 'New price (estimate)' column in the tracker sheet with the right
    new price, matching what we did in Airtable.

    Per row, the price is resolved by:
      1) Airtable — same Model Number, its 'New Price (Estimate)' (free, trusted).
      2) Gemini grounded search — only when Airtable has no match (and a model/
         item is searchable).

    force=False -> only fill empty/non-numeric cells (idempotent, resumable).
    force=True  -> also overwrite existing numbers (the early values were wrong).

    Header-aware: writes to the column actually titled 'New price (estimate)'.
    Never touches the human-managed Internal/Final price columns.
    """
    out: dict = {"dry_run": dry_run, "force": force, "rows": 0, "priced": 0,
                 "written": 0, "not_found": 0, "skipped": 0,
                 "from_airtable": 0, "from_gemini": 0}
    _sheet_backfill_state["progress"] = out
    gc = _get_gspread_client()
    if gc is None:
        out["error"] = "no gspread client"
        return out
    try:
        ss = gc.open_by_key(EVAL_TRACKER_SHEET_ID)
        ws = ss.worksheet(EVAL_TRACKER_TAB)
        values = ws.get_all_values()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"read failed: {type(e).__name__}: {e}"
        return out

    hidx, cmap = _find_header_index(values)
    if hidx < 0 or "new_price" not in cmap:
        out["error"] = "could not locate 'New price (estimate)' column"
        return out
    np_col = cmap["new_price"]
    np_letter = _col_letter(np_col)

    def cell(row, key):
        i = cmap.get(key)
        return row[i] if i is not None and i < len(row) else ""

    price_cache: dict[str, float] = {}
    pending: list[dict] = []
    calls = 0

    def _flush() -> None:
        nonlocal pending
        if not pending:
            return
        try:
            ws.batch_update(pending, value_input_option="USER_ENTERED")
            out["written"] += len(pending)
        except Exception as e:  # noqa: BLE001
            out["error"] = (
                f"write failed: {type(e).__name__}: {e}. "
                f"Grant EDIT access to the service account ({_service_account_email()})."
            )
        pending = []

    src_cache: dict[str, str] = {}
    for ridx, row in enumerate(values[hidx + 1:], start=hidx + 2):  # 1-based sheet row
        if not any(str(c).strip() for c in row):
            continue
        out["rows"] += 1
        existing = _safe_float(cell(row, "new_price"))
        # Without force: skip rows that already hold a clean number (resumable).
        if existing and not force:
            out["skipped"] += 1
            continue
        item = str(cell(row, "item")).strip()
        model = str(cell(row, "model")).strip()
        if model.replace(".", "").isdigit():
            model = ""
        if not item and not model:
            out["skipped"] += 1
            continue
        key = f"{item}|{model}".lower()
        if key in price_cache:
            price = price_cache[key]
            source = src_cache.get(key, "")
        else:
            # 1) Airtable structured guide (free) — same model, repriced value.
            price = _airtable_new_price_for(model)
            source = "airtable" if price else ""
            # 2) Gemini fallback only when Airtable has nothing.
            if not price:
                if calls >= limit:
                    out["skipped"] += 1
                    continue
                if not dry_run:
                    calls += 1
                    price = _gemini_new_price(item, model, _infer_category_from_text(item))
                    source = "gemini" if price else ""
            price_cache[key] = price or 0.0
            src_cache[key] = source

        if dry_run:
            # Preview: count what we'd resolve (Airtable hits are known; Gemini
            # hits are assumed when a searchable model/item exists).
            if source == "airtable":
                out["priced"] += 1
                out["from_airtable"] += 1
            else:
                out["priced"] += 1
            continue

        if not price or price <= 0:
            out["not_found"] += 1
            continue
        # Cell already holds this exact value — nothing to write.
        if existing and round(existing) == round(price):
            out["skipped"] += 1
            continue
        out["priced"] += 1
        out["from_airtable" if source == "airtable" else "from_gemini"] += 1
        pending.append({"range": f"{np_letter}{ridx}", "values": [[round(price)]]})
        # Flush incrementally so a restart never loses completed work.
        if len(pending) >= 15:
            _flush()
            if out.get("error"):
                return out

    if dry_run:
        out["note"] = "Preview only. Run dry_run=false to price (Gemini) + write."
        return out

    _flush()
    return out


def delete_tracker_rows_by_ref(ref8: str) -> int:
    """Delete tracker-sheet rows whose Notes contain 'Ref: <ref8>'. Returns count.
    Used to clean up a test/probe row. Best-effort."""
    if not ref8:
        return 0
    gc = _get_gspread_client()
    if gc is None:
        return 0
    try:
        ss = gc.open_by_key(EVAL_TRACKER_SHEET_ID)
        ws = ss.worksheet(EVAL_TRACKER_TAB)
        values = ws.get_all_values()
        hidx, cmap = _find_header_index(values)
        if hidx < 0:
            return 0
        notes_col = cmap.get("notes")
        if notes_col is None:
            return 0
        marker = f"ref: {ref8.lower()}"
        # Collect 1-based row numbers, delete bottom-up to keep indices valid.
        targets = [
            ridx for ridx, row in enumerate(values[hidx + 1:], start=hidx + 2)
            if notes_col < len(row) and marker in str(row[notes_col]).lower()
        ]
        deleted = 0
        for ridx in sorted(targets, reverse=True):
            try:
                ws.delete_rows(ridx)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"delete_tracker_rows_by_ref: row {ridx} failed: {e}")
        return deleted
    except Exception as e:  # noqa: BLE001
        logger.warning(f"delete_tracker_rows_by_ref failed: {e}")
        return 0


# Tracker write health — surfaced in get_refresh_status() so the ops dashboard
# and health checks can see a broken mirror instead of it hiding in a thread.
_tracker_status: dict = {
    "last_write_ok": None,       # ISO timestamp of last successful append
    "last_write_error": None,    # ISO timestamp of last failed append
    "last_error": None,          # str(exception) of last failure
    "consecutive_failures": 0,
}

# Failed rows are preserved here (one JSON object per line) so they can be
# replayed after the cause (permissions/quota) is fixed. logs/ is bind-mounted.
_TRACKER_FALLBACK_FILE = "logs/tracker_failed_rows.jsonl"


def _tracker_dump_fallback(payload: dict) -> None:
    """Persist a failed tracker row to disk. Never raises."""
    try:
        import json as _json
        from datetime import datetime as _dt
        p = Path(_TRACKER_FALLBACK_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"failed_at": _dt.now().isoformat(), **payload}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(payload, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Tracker fallback dump failed: {e}")


def append_tracker_row(
    *, item: str, model: str = "", new_price: float | None = None,
    ai_price: float | None = None, ai_confidence: float | None = None,
    customer_price: float | None = None, condition: str = "", age: str = "",
    status: str = "", notes: str = "", date_str: str | None = None,
) -> bool:
    """Append one evaluation to the tracker sheet, placing each value in the
    column that actually carries that header (robust to layout).

    Hardened (Jul 2026): retries once with a fresh header layout (handles
    columns being reordered mid-process), records success/failure in
    ``_tracker_status`` for the dashboard, and dumps failed rows to a JSONL
    fallback file so no evaluation is lost if the sheet becomes unwritable
    (as happened when the service account was downgraded to Viewer)."""
    global _tracker_write_layout
    from datetime import datetime as _dt

    payload = {
        "item": item, "model": model, "new_price": new_price,
        "ai_price": ai_price, "ai_confidence": ai_confidence,
        "customer_price": customer_price, "condition": condition, "age": age,
        "status": status, "notes": notes, "date_str": date_str,
    }

    gc = _get_gspread_client()
    if gc is None:
        _tracker_status["last_write_error"] = _dt.now().isoformat()
        _tracker_status["last_error"] = "no gspread client"
        _tracker_status["consecutive_failures"] += 1
        _tracker_dump_fallback(payload)
        return False

    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            ss = gc.open_by_key(EVAL_TRACKER_SHEET_ID)
            ws = ss.worksheet(EVAL_TRACKER_TAB)
            if _tracker_write_layout is None:
                # Write-tolerant detection: must not depend on the human-managed
                # "Internal Team Price" column (renaming it would kill appends).
                hidx, cmap = _find_header_index(
                    ws.get_all_values(), require_internal=False
                )
                if hidx < 0 or not cmap:
                    raise RuntimeError(
                        f"header row not found in tab '{EVAL_TRACKER_TAB}'"
                    )
                _tracker_write_layout = {"cmap": cmap, "ncols": max(cmap.values()) + 1}
            cmap = _tracker_write_layout["cmap"]
            ncols = _tracker_write_layout["ncols"]
            row = [""] * ncols

            def setc(key, val):
                i = cmap.get(key)
                if i is not None and i < ncols and val not in (None, ""):
                    row[i] = val

            setc("date", date_str or _dt.now().strftime("%d/%m/%Y"))
            setc("item", item)
            setc("model", model)
            setc("condition", condition)
            setc("age", age)
            if new_price:
                setc("new_price", round(float(new_price)))
            if ai_price:
                setc("ai_price", round(float(ai_price)))
            if ai_confidence:
                setc("ai_confidence", f"{float(ai_confidence):.0f}%")
            if customer_price:
                setc("customer_price", round(float(customer_price)))
            setc("accepted", status)
            setc("notes", notes)
            # NEVER touch internal_price / final_price (human-managed columns).
            ws.append_row(row, value_input_option="USER_ENTERED")
            _tracker_status["last_write_ok"] = _dt.now().isoformat()
            _tracker_status["last_error"] = None
            _tracker_status["consecutive_failures"] = 0
            return True
        except Exception as e:  # noqa: BLE001
            last_exc = e
            # A stale cached layout (columns reordered) or a transient API blip
            # can fail once; drop the cache and retry a single time.
            _tracker_write_layout = None
            if attempt == 1:
                time.sleep(1.5)

    # ERROR (not warning): a failing tracker write means evaluations silently
    # stop mirroring to the sheet. A 403 here = the service account lost
    # Editor access on the sheet (re-share it as Editor). Surfaced loudly and
    # recorded in status + fallback file so nothing is lost.
    _tracker_status["last_write_error"] = _dt.now().isoformat()
    _tracker_status["last_error"] = f"{type(last_exc).__name__}: {last_exc}"
    _tracker_status["consecutive_failures"] += 1
    _tracker_dump_fallback(payload)
    logger.error(
        f"Tracker append FAILED after retry (evaluations not mirroring to "
        f"sheet; row saved to {_TRACKER_FALLBACK_FILE}): {last_exc}"
    )
    return False


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------
async def refresh_reference_data() -> tuple[int, int]:
    """Pull both sheets and update in-memory cache + DB. Returns (matrix_count, sales_count)."""
    global _matrix_cache, _sales_cache, _last_refresh

    gc = _get_gspread_client()
    if gc is None:
        return 0, 0

    matrix_rows = await asyncio.to_thread(_read_pricing_matrix, gc)
    sales_rows = await asyncio.to_thread(_read_sales_stock, gc)

    _matrix_cache = matrix_rows
    _sales_cache = sales_rows
    _last_refresh = datetime.now(timezone.utc)

    await asyncio.to_thread(_persist_to_db, matrix_rows, sales_rows)

    # Monitoring-only: pull internal/human prices from the eval tracker sheet.
    # Never raises into the refresh path.
    try:
        await asyncio.to_thread(sync_internal_prices_from_sheet)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Internal price sync skipped: {e}")

    # Learn: recompute calibrated acquisition ratios from the team's own
    # evaluations so pricing continuously tracks the internal trend.
    try:
        await asyncio.to_thread(compute_calibrated_ratios)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Ratio calibration skipped: {e}")

    return len(matrix_rows), len(sales_rows)


async def start_refresh_loop() -> None:
    """Background task: refresh on startup then every 6 hours."""
    m, s = await refresh_reference_data()
    logger.info(f"Reference data: initial load complete ({m} matrix, {s} sales)")

    while True:
        try:
            await asyncio.sleep(21600)  # 6 hours
            m, s = await refresh_reference_data()
            logger.info(f"Reference data: 6h refresh ({m} matrix, {s} sales)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Reference data refresh loop error: {e}")
            await asyncio.sleep(300)


# ---------------------------------------------------------------------------
# Lookup functions
# ---------------------------------------------------------------------------
def _normalize(s: str) -> str:
    return s.lower().strip() if s else ""


def lookup_matrix(
    brand: str,
    model: str = "",
    category: str = "",
    age_band: str = "",
) -> dict | None:
    """Find the best match in the pricing matrix cache.

    Returns the row dict or None.
    """
    brand_n = _normalize(brand)
    model_n = _normalize(model)
    cat_n = _normalize(category)

    best: dict | None = None
    best_score = 0

    for row in _matrix_cache:
        score = 0
        if brand_n and brand_n in _normalize(row.get("brand", "")):
            score += 2
        if model_n and model_n in _normalize(row.get("model", "")):
            score += 4
        if cat_n and cat_n in _normalize(row.get("category", "")):
            score += 1
        if age_band and _normalize(age_band) in _normalize(row.get("age_band", "")):
            score += 1

        if score > best_score:
            best_score = score
            best = row

    if best_score >= 3:
        return best
    return None


def lookup_sales_stock(
    brand: str,
    model: str = "",
    category: str = "",
) -> dict | None:
    """Find matching sales from the outlet stock cache.

    Returns a dict with 'matches' list and 'median_selling_price'.
    """
    brand_n = _normalize(brand)
    model_n = _normalize(model)
    cat_n = _normalize(category)

    exact_matches: list[dict] = []
    category_matches: list[dict] = []

    for row in _sales_cache:
        row_brand = _normalize(row.get("brand_name", ""))
        row_model = _normalize(row.get("model_number", "") or row.get("product_name", ""))
        row_cat = _normalize(row.get("product_category", ""))

        brand_match = brand_n and brand_n in row_brand
        model_match = model_n and model_n in row_model

        if brand_match and model_match:
            exact_matches.append(row)
        elif brand_match and cat_n and cat_n in row_cat:
            category_matches.append(row)

    if exact_matches:
        prices = [r["selling_price"] for r in exact_matches if r.get("selling_price")]
        median_price = statistics.median(prices) if prices else None
        return {
            "match_type": "exact_model",
            "matches": exact_matches,
            "count": len(exact_matches),
            "median_selling_price": median_price,
            "median_purchase_cost": (
                statistics.median([r["purchase_cost"] for r in exact_matches if r.get("purchase_cost")])
                if any(r.get("purchase_cost") for r in exact_matches) else None
            ),
        }

    if category_matches:
        prices = [r["selling_price"] for r in category_matches if r.get("selling_price")]
        median_price = statistics.median(prices) if prices else None
        return {
            "match_type": "brand_category",
            "matches": category_matches,
            "count": len(category_matches),
            "median_selling_price": median_price,
            "median_purchase_cost": (
                statistics.median([r["purchase_cost"] for r in category_matches if r.get("purchase_cost")])
                if any(r.get("purchase_cost") for r in category_matches) else None
            ),
        }

    return None


_RE_SEP = re.compile(r"[\s_\-/]+")


def _collapse(s: str) -> str:
    """Lower-case and strip ALL separators so 'washing machine', 'washing_machine'
    and 'washing-machine' all compare equal (fixes multi-word categories silently
    falling back to the default ratio)."""
    return _RE_SEP.sub("", (s or "").lower())


# ---------------------------------------------------------------------------
# Calibrated acquisition ratios (Jul 2026) — learned from the team's OWN
# evaluations so every offer tracks what GreenBay would actually pay.
#
# For each tracker row where the internal team recorded a price, we compute the
# residual the static engine cannot explain:
#     r = internal_price / (new_price * depreciation(age, cat) * condition)
# and set the category's calibrated ratio to a shrunken median of those r's
# (shrunk toward the static prior so a handful of noisy rows can't swing it).
# This works for EVERY evaluation — items with no internal price still get the
# category-level learned ratio; internal prices are only the training signal,
# never a per-item dependency. Refreshed automatically with the 6h data cycle.
# ---------------------------------------------------------------------------
_calibrated_ratios: dict[str, dict[str, Any]] = {}
_CALIB_SHRINK_K = 5          # pseudo-samples of the static prior
_CALIB_MIN_N = 4             # below this, stay fully on the static prior
_CALIB_CLAMP = (0.30, 0.90)  # sane bounds for any learned ratio


def _static_acquisition_ratio(category: str) -> float:
    cat_c = _collapse(category)
    if not cat_c:
        return DEFAULT_ACQUISITION_RATIO
    for key, ratio in ACQUISITION_RATIOS.items():
        key_c = _collapse(key)
        if key_c in cat_c or cat_c in key_c:
            return ratio
    return DEFAULT_ACQUISITION_RATIO


def compute_calibrated_ratios() -> dict[str, Any]:
    """Recompute per-category calibrated acquisition ratios from the tracker.

    Reads the Customer Initiated Evaluation sheet, pairs internal team prices
    with the recorded new price + age + condition, and updates
    ``_calibrated_ratios``. Never raises; returns a status summary."""
    from greenbay_ai_evaluator.engine.offer_engine import (
        CONDITION_FACTORS, depreciation_factor,
    )

    gc = _get_gspread_client()
    if gc is None:
        return {"ok": False, "reason": "no gspread client"}
    try:
        ws = gc.open_by_key(EVAL_TRACKER_SHEET_ID).worksheet(EVAL_TRACKER_TAB)
        values = ws.get_all_values()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Calibration: tracker read failed: {e}")
        return {"ok": False, "reason": str(e)}

    hidx, cmap = _find_header_index(values)
    if hidx < 0:
        return {"ok": False, "reason": "header not found"}

    def cell(row, key):
        i = cmap.get(key)
        return row[i] if i is not None and i < len(row) else ""

    samples: dict[str, list[float]] = {}
    for row in values[hidx + 1:]:
        internal = _safe_float(cell(row, "internal_price")) or _safe_float(cell(row, "final_price"))
        new = _safe_float(cell(row, "new_price"))
        if not internal or not new or internal < 500 or new < 1000:
            continue
        cat = _infer_category_from_text(cell(row, "item"))
        if cat == "other":
            continue
        age = _safe_float(cell(row, "age")) or 2.0
        grade = (str(cell(row, "condition")).strip().upper()[:1]) or "B"
        cond = CONDITION_FACTORS.get(grade, 0.70)
        depr = depreciation_factor(age, cat)
        denom = new * depr * cond
        if denom <= 0:
            continue
        r = internal / denom
        if 0.05 <= r <= 3.0:  # discard absurd rows (typos, wrong columns)
            samples.setdefault(cat, []).append(r)

    updated: dict[str, dict[str, Any]] = {}
    for cat, rs in samples.items():
        n = len(rs)
        if n < _CALIB_MIN_N:
            continue
        raw = statistics.median(rs)
        prior = _static_acquisition_ratio(cat)
        # Shrunken estimate: with few samples stay near the prior; with many,
        # converge to the team's observed median.
        blended = (n * raw + _CALIB_SHRINK_K * prior) / (n + _CALIB_SHRINK_K)
        lo, hi = _CALIB_CLAMP
        updated[cat] = {
            "ratio": round(min(hi, max(lo, blended)), 3),
            "raw_median": round(raw, 3),
            "static_prior": prior,
            "n": n,
        }

    # Atomic swap: rebind instead of clear()+update() so concurrent evaluation
    # threads iterating the dict never see it mid-mutation (RuntimeError) or
    # momentarily empty (silent fallback to the static ratio).
    global _calibrated_ratios
    _calibrated_ratios = updated
    logger.info(
        "Calibration: learned acquisition ratios updated: "
        + ", ".join(f"{c}={v['ratio']}(n={v['n']})" for c, v in updated.items())
    )
    return {"ok": True, "categories": updated}


def get_calibrated_ratios() -> dict[str, dict[str, Any]]:
    """Snapshot of the learned ratios (for the accuracy report / dashboard)."""
    return dict(_calibrated_ratios)


def get_category_acquisition_ratio(category: str) -> float:
    """Return the acquisition ratio for a category.

    Prefers the CALIBRATED ratio learned from the internal team's own closed
    evaluations (refreshed every 6h); falls back to the static real-data table
    when a category has too little training data. Separator-insensitive:
    'washing machine', 'washing_machine' and 'washing-machine' all match.
    """
    cat_c = _collapse(category)
    if not cat_c:
        return DEFAULT_ACQUISITION_RATIO
    for key, info in _calibrated_ratios.items():
        key_c = _collapse(key)
        if key_c in cat_c or cat_c in key_c:
            return info["ratio"]
    return _static_acquisition_ratio(category)


def get_refresh_status() -> dict[str, Any]:
    """Status snapshot for the ops dashboard."""
    return {
        "service": "reference_data",
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
        "matrix_rows_cached": len(_matrix_cache),
        "sales_rows_cached": len(_sales_cache),
        # Tracker-sheet mirror health: a broken write (e.g. service account
        # downgraded to Viewer) is visible here instead of hiding in a thread.
        "tracker_last_write_ok": _tracker_status["last_write_ok"],
        "tracker_last_write_error": _tracker_status["last_write_error"],
        "tracker_last_error": _tracker_status["last_error"],
        "tracker_consecutive_failures": _tracker_status["consecutive_failures"],
        "calibrated_ratio_categories": len(_calibrated_ratios),
    }
