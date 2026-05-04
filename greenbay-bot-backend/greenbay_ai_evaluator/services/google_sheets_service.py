"""
Google Sheets integration service (CR-4).

Reads from and writes to the GreenBay evaluation tracking spreadsheet.
Uses gspread with a Google Cloud service account for authentication.

IMPORTANT: Columns J (Internal Team Price) and K (Final Price Offered)
are NEVER written to by this service — they are managed by the human team.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger


# ---------------------------------------------------------------------------
# Lazy imports — gspread/google-auth may not be installed
# ---------------------------------------------------------------------------
_gspread_client = None
_worksheet = None


def _get_worksheet():
    """Lazily initialize the gspread client and return the worksheet."""
    global _gspread_client, _worksheet

    if _worksheet is not None:
        return _worksheet

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning(
            "gspread or google-auth not installed. "
            "Install with: pip install gspread google-auth"
        )
        return None

    try:
        from app.config import get_settings
        settings = get_settings()

        # Primary: dedicated Sheets credentials; fallback: Vertex service account
        # (same project, same service account email, already mounted in Docker).
        creds_file = getattr(settings, "google_sheets_credentials_file", "") or ""
        vertex_file = getattr(settings, "google_vertex_credentials_file", "") or ""

        chosen_file: str | None = None
        source_label = ""
        if creds_file and Path(creds_file).exists():
            chosen_file = creds_file
            source_label = "GOOGLE_SHEETS_CREDENTIALS_FILE"
        elif vertex_file and Path(vertex_file).exists():
            chosen_file = vertex_file
            source_label = "GOOGLE_VERTEX_CREDENTIALS_FILE (fallback)"

        if not chosen_file:
            logger.warning(
                "Google Sheets: no credentials file found — tried "
                f"GOOGLE_SHEETS_CREDENTIALS_FILE={creds_file!r} and "
                f"GOOGLE_VERTEX_CREDENTIALS_FILE={vertex_file!r}. "
                "Sheets integration disabled."
            )
            return None

        creds_path = Path(chosen_file)
        logger.info(f"Google Sheets: using credentials from {source_label} ({creds_path})")

        from greenbay_ai_evaluator.services.google_sheets_config import SHEET_ID, TAB_NAME

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        _gspread_client = gspread.authorize(credentials)

        sheet = _gspread_client.open_by_key(
            getattr(settings, "google_sheets_id", SHEET_ID)
        )
        _worksheet = sheet.worksheet(TAB_NAME)
        logger.info(f"Google Sheets connected: {sheet.title} / {TAB_NAME}")
        return _worksheet

    except Exception as e:
        logger.error(f"Failed to connect to Google Sheets: {e}")
        return None


def append_evaluation_row(
    *,
    category: str,
    brand: str,
    model: str = "",
    condition: str = "",
    condition_grade: str = "",
    age_years: float = 0,
    retail_price_estimate: float = 0,
    wants_trade_in: bool = True,
    ai_price: float = 0,
    ai_confidence: float = 0,
    customer_asking_price: float | None = None,
    status: str = "",
    notes: str = "",
    decision: str = "",
) -> bool:
    """Append a new evaluation row to the Google Sheet.

    NEVER writes to columns J (Internal Team Price) or K (Final Price Offered).

    Returns True if successful, False otherwise.
    """
    from greenbay_ai_evaluator.services.google_sheets_config import (
        CONDITION_TO_NUMERIC,
        age_to_display,
        format_price_range,
    )

    ws = _get_worksheet()
    if ws is None:
        return False

    try:
        # Build the item description (e.g., "Samsung 350L Fridge")
        item = f"{brand} {model}".strip() if model else f"{brand} {category}"

        # Map condition to 1-5 numeric
        cond_numeric = CONDITION_TO_NUMERIC.get(
            condition, CONDITION_TO_NUMERIC.get(condition_grade, 3)
        )

        # Format date as DD/MM/YY to match existing rows
        date_str = datetime.now().strftime("%d/%m/%y")

        # Format prices
        def _fmt_price(val: float) -> str:
            if not val:
                return ""
            if val >= 1000:
                k = val / 1000
                return f"{k:.0f}k" if k == int(k) else f"{k:.1f}k"
            return str(int(val))

        # Determine status text
        if not status:
            if decision == "accept":
                status = "Accepted"
            elif decision in ("reject", "redirect_to_agents"):
                status = "Rejected"
            elif decision == "negotiate":
                status = "Pending"
            elif decision == "review":
                status = "Under Review"
            else:
                status = "Pending"

        # Build the row — 13 columns (A through M)
        # Columns J (index 9) and K (index 10) are left empty
        row = [
            date_str,                                          # A: Date
            item,                                              # B: Item
            str(cond_numeric),                                 # C: Condition (1-5)
            age_to_display(age_years),                         # D: Age
            _fmt_price(retail_price_estimate),                 # E: New price (estimate)
            "Yes" if wants_trade_in else "No",                 # F: Trade in?
            _fmt_price(ai_price),                              # G: AI Price
            f"{ai_confidence:.0f}%" if ai_confidence else "",  # H: AI Confidence
            _fmt_price(customer_asking_price) if customer_asking_price else "",  # I: Customer Price
            "",                                                # J: Internal Team Price (DO NOT WRITE)
            "",                                                # K: Final Price Offered (DO NOT WRITE)
            status,                                            # L: Accepted / Rejected
            notes or "",                                       # M: Notes
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")

        logger.info(
            f"Google Sheet: appended row — {item}, AI Price: {_fmt_price(ai_price)}, "
            f"Status: {status}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to append row to Google Sheet: {e}")
        return False


async def append_evaluation_row_async(**kwargs) -> bool:
    """Async wrapper for append_evaluation_row — runs in thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: append_evaluation_row(**kwargs))


def read_historical_data() -> list[dict[str, Any]]:
    """Read all evaluation rows from the Google Sheet for self-learning.

    Returns a list of dicts with column values. Includes columns J and K
    (Internal Team Price and Final Price Offered) for learning from
    human-assessed prices.
    """
    ws = _get_worksheet()
    if ws is None:
        return []

    try:
        all_rows = ws.get_all_values()

        if len(all_rows) <= 1:
            return []  # Only header row

        headers = all_rows[0]
        data = []

        for row in all_rows[1:]:
            if not any(cell.strip() for cell in row):
                continue  # Skip empty rows

            entry = {}
            for i, header in enumerate(headers):
                entry[header.strip()] = row[i].strip() if i < len(row) else ""
            data.append(entry)

        logger.info(f"Google Sheet: read {len(data)} historical rows")
        return data

    except Exception as e:
        logger.error(f"Failed to read Google Sheet: {e}")
        return []


async def read_historical_data_async() -> list[dict[str, Any]]:
    """Async wrapper for read_historical_data."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, read_historical_data)


def get_team_prices() -> dict[str, list[float]]:
    """Extract internal team prices grouped by item category+brand for learning.

    Returns a dict mapping "category|brand" keys to lists of team-assessed prices.
    Only includes rows where column J (Internal Team Price) is filled.
    """
    data = read_historical_data()
    team_prices: dict[str, list[float]] = {}

    for row in data:
        internal_price_str = row.get("Internal Team Price", "").strip()
        if not internal_price_str:
            continue

        # Parse price (handle "25k", "25,000", etc.)
        price = _parse_sheet_price(internal_price_str)
        if price is None or price <= 0:
            continue

        item = row.get("Item", "").strip()
        if not item:
            continue

        # Create a normalized key
        key = item.lower()
        if key not in team_prices:
            team_prices[key] = []
        team_prices[key].append(price)

    logger.info(f"Google Sheet: extracted team prices for {len(team_prices)} items")
    return team_prices


def _parse_sheet_price(text: str) -> float | None:
    """Parse a price string from the Google Sheet.

    Handles formats like: "25k", "25,000", "KES 25000", "25k to 28k" (takes avg).
    """
    if not text:
        return None

    text = text.strip().lower()
    text = text.replace("kes", "").replace(",", "").strip()

    # Handle range: "25k to 28k" -> average
    if " to " in text:
        parts = text.split(" to ")
        prices = [_parse_sheet_price(p.strip()) for p in parts]
        valid = [p for p in prices if p is not None]
        return sum(valid) / len(valid) if valid else None

    # Handle "k" suffix
    if text.endswith("k"):
        try:
            return float(text[:-1]) * 1000
        except ValueError:
            return None

    try:
        return float(text)
    except ValueError:
        return None
