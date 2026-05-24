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
import statistics
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

PRICING_MATRIX_SHEET_ID = "1_6RqiRaGshsY-iVssXpyLR2avlHsV_riiwXHTKqMz3c"
SALES_STOCK_SHEET_ID = "1k7Zw8psnjIw9BukH7vVwESDR-k1PU60wlERWjljk_fE"
SALES_STOCK_TAB = "Final Data"

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
    "microwave": 0.72,
    "soundbar": 0.72,
    "woofer": 0.85,
    "water_dispenser": 0.78,
    "chiller": 0.77,
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

            # Auto-detect header row: first row containing "Brand" and ("Model" or "Age")
            header_idx = None
            headers: list[str] = []
            for i, row in enumerate(raw_rows[:15]):
                cells = [str(c).strip() for c in row]
                lower_cells = [c.lower() for c in cells]
                if "brand" in lower_cells and ("model" in lower_cells or "age" in lower_cells):
                    header_idx = i
                    headers = cells
                    break

            if header_idx is None:
                logger.warning(
                    f"Reference data: tab '{tab_name}' — no header row found in first 15 rows. "
                    f"Row samples: {[r[:5] for r in raw_rows[:5]]}"
                )
                continue

            # Filter out empty-name columns
            valid_cols = [(ci, h) for ci, h in enumerate(headers) if h]
            col_names = [h for _, h in valid_cols]
            col_indices = [ci for ci, _ in valid_cols]

            logger.info(
                f"Reference data: tab '{tab_name}' header at row {header_idx}: {col_names}"
            )

            data_rows = raw_rows[header_idx + 1:]
            tab_count = 0
            for row in data_rows:
                rec = {col_names[j]: str(row[ci]).strip() if ci < len(row) else ""
                       for j, ci in enumerate(col_indices)}
                # Skip fully-blank rows
                if not any(rec.values()):
                    continue
                rows_out.append({
                    "brand": rec.get("Brand", ""),
                    "model": rec.get("Model", rec.get("Model Number", "")),
                    "category": category_slug,
                    "age_band": rec.get("Age", rec.get("Age Band", "")),
                    "base_min": _safe_float(rec.get("Base Min", rec.get("Base Min KES"))),
                    "base_max": _safe_float(rec.get("Base Max", rec.get("Base Max KES"))),
                    "condition_grade": rec.get("Condition Grade", rec.get("Grade", "")),
                    "recommended_min": _safe_float(rec.get("Recommended Min", rec.get("Rec Min KES"))),
                    "recommended_max": _safe_float(rec.get("Recommended Max", rec.get("Rec Max KES"))),
                    "new_price": _safe_float(rec.get("New Price", rec.get("New Price KES"))),
                    "sheet_tab": tab_name,
                    "raw_json": _sanitize_for_json(rec),
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


def get_category_acquisition_ratio(category: str) -> float:
    """Return the acquisition ratio for a category from the real-data table."""
    cat_n = _normalize(category)
    for key, ratio in ACQUISITION_RATIOS.items():
        if key in cat_n or cat_n in key:
            return ratio
    return DEFAULT_ACQUISITION_RATIO


def get_refresh_status() -> dict[str, Any]:
    """Status snapshot for the ops dashboard."""
    return {
        "service": "reference_data",
        "last_refresh": _last_refresh.isoformat() if _last_refresh else None,
        "matrix_rows_cached": len(_matrix_cache),
        "sales_rows_cached": len(_sales_cache),
    }
