"""
One-time backfill: copy every historical evaluation from Postgres (and,
where available, the Google Sheet) into Airtable.

Usage (from the EC2 host):
    # Initial backfill (idempotent - skips existing records)
    docker compose exec app python scripts/backfill_airtable.py

    # After enabling S3: PATCH existing records with fresh 7-day presigned
    # image URLs (does NOT create new rows, only updates Product Images +
    # Attachment Summary on rows that already exist in Airtable):
    docker compose exec app python scripts/backfill_airtable.py --update-images

The script is idempotent: before inserting a record it queries Airtable for
an existing row with the same Date Submitted + Product Name and skips it if
found. This makes it safe to re-run after adding more historical data.

Rate limited: batches of 10 rows, 250ms between batches (Airtable's documented
5 req/sec limit).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# Make sure the project root is on sys.path when run via `python scripts/...`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loguru import logger
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database.db import SessionLocal
from app.database.models import ExpertPriceFeedback, TradeInSession
from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
from greenbay_ai_evaluator.services.airtable_service import (
    _auth_headers,
    _base_url,
    _get_config,
    _post_record,
)


BATCH_SIZE = 10
BATCH_DELAY_SECONDS = 0.25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _product_name(vs: ValuationSession) -> str:
    parts = [
        (vs.brand or "").strip(),
        (vs.model or "").strip(),
        (vs.category or "").strip(),
    ]
    joined = " ".join(p for p in parts if p)
    return joined or (vs.category or "Unknown")


def _iso_date(dt: Optional[datetime]) -> str:
    if not dt:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _image_attachments_from_trade_in(
    tis: Optional[TradeInSession],
) -> list[dict]:
    """Build attachment list preferring 7-day re-presigned URLs from s3_keys."""
    if not tis:
        return []

    try:
        from app.services.s3_service import s3_client, settings as s3_settings
    except Exception:
        s3_client = None
        s3_settings = None

    if s3_client and s3_settings and tis.s3_keys:
        keys = tis.s3_keys if isinstance(tis.s3_keys, list) else []
        out: list[dict] = []
        for key in keys[:8]:
            try:
                url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": s3_settings.aws_s3_bucket, "Key": key},
                    ExpiresIn=7 * 24 * 3600,
                )
                if f".s3.{s3_settings.aws_region}.amazonaws.com" not in url:
                    url = url.replace(
                        ".s3.amazonaws.com",
                        f".s3.{s3_settings.aws_region}.amazonaws.com",
                    )
                out.append({"url": url})
            except Exception as e:
                logger.warning(f"Backfill: presign failed for {key}: {e}")
        if out:
            return out

    # Fallback: raw URLs from image_urls
    if tis.image_urls:
        urls = tis.image_urls if isinstance(tis.image_urls, list) else []
        return [{"url": u} for u in urls[:8] if u]

    return []


def _chunked(seq: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---------------------------------------------------------------------------
# Sheets merge (optional)
# ---------------------------------------------------------------------------
def _load_sheet_index() -> dict[tuple[str, str], dict[str, Any]]:
    """Read the Google Sheet and index rows by (brand+model lowercased, date).

    Returns empty dict if Sheets is not available. Each value dict may carry
    'internal_price' (column J) and 'final_price' (column K) parsed as floats.
    """
    try:
        from greenbay_ai_evaluator.services.google_sheets_service import (
            read_historical_data,
            _parse_sheet_price,
        )
    except Exception as e:
        logger.warning(f"Backfill: Sheets module unavailable ({e})")
        return {}

    try:
        rows = read_historical_data()
    except Exception as e:
        logger.warning(f"Backfill: could not read Sheet ({e})")
        return {}

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        item = (row.get("Item") or "").strip().lower()
        date_str = (row.get("Date") or "").strip()
        if not item:
            continue
        internal = _parse_sheet_price(row.get("Internal Team Price", ""))
        final_p = _parse_sheet_price(row.get("Final Price Offered", ""))
        index[(item, date_str)] = {
            "internal_price": internal,
            "final_price": final_p,
            "ai_price": _parse_sheet_price(row.get("AI Price", "")),
        }
    logger.info(f"Backfill: loaded {len(index)} rows from Google Sheet")
    return index


def _lookup_sheet_prices(
    sheet_index: dict[tuple[str, str], dict[str, Any]],
    vs: ValuationSession,
) -> tuple[Optional[float], Optional[float]]:
    """Return (internal_team_price, final_price) from the Sheet if matched."""
    if not sheet_index:
        return (None, None)
    item_key = f"{(vs.brand or '').strip()} {(vs.model or '').strip()}".strip().lower()
    if not item_key:
        item_key = f"{(vs.brand or '').strip()} {(vs.category or '').strip()}".strip().lower()

    candidates = [v for (i, _d), v in sheet_index.items() if i == item_key]
    if not candidates:
        return (None, None)

    internal = next((c["internal_price"] for c in candidates if c.get("internal_price")), None)
    final_p = next((c["final_price"] for c in candidates if c.get("final_price")), None)
    return (internal, final_p)


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------
def _find_in_airtable(
    cfg: dict[str, str], date_iso: str, product_name: str
) -> Optional[str]:
    """Return the Airtable record id for the first match, or None if not found.

    Matches on (Date Submitted prefix YYYY-MM-DDTHH:MM, Product Name exact).
    """
    import requests

    date_prefix = date_iso[:16]
    safe_name = product_name.replace("'", "\\'")
    formula = (
        f"AND(LEFT({{Date Submitted}}, 16) = '{date_prefix}',"
        f"{{Product Name}} = '{safe_name}')"
    )
    try:
        resp = requests.get(
            _base_url(cfg),
            headers=_auth_headers(cfg),
            params={"filterByFormula": formula, "maxRecords": 1},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        records = resp.json().get("records", [])
        return records[0].get("id") if records else None
    except Exception:
        return None


def _already_in_airtable(cfg: dict[str, str], date_iso: str, product_name: str) -> bool:
    """Back-compat wrapper: True if a record exists."""
    return _find_in_airtable(cfg, date_iso, product_name) is not None


def _patch_record(
    cfg: dict[str, str], record_id: str, fields: dict[str, Any]
) -> tuple[bool, Optional[str]]:
    """PATCH a single existing Airtable record. Returns (success, error)."""
    import requests

    url = f"{_base_url(cfg)}/{record_id}"
    body = {"fields": fields, "typecast": True}
    try:
        resp = requests.patch(
            url,
            headers=_auth_headers(cfg),
            json=body,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return True, None
        return False, f"HTTP {resp.status_code}: {resp.text[:240]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_record(
    vs: ValuationSession,
    tis: Optional[TradeInSession],
    expert: Optional[ExpertPriceFeedback],
    sheet_index: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    internal_from_sheet, final_from_sheet = _lookup_sheet_prices(sheet_index, vs)

    # In-House Evaluator Price = expert feedback if we have it, else Sheet
    # internal team price, else blank.
    in_house = None
    if expert and expert.expert_price:
        in_house = float(expert.expert_price)
    elif internal_from_sheet:
        in_house = float(internal_from_sheet)

    # Final price offered — if present in Sheet, record it as a note on the
    # Airtable side via In-House price fallback (Airtable base doesn't have
    # a separate "Final Offered" column per the spec we received).
    if final_from_sheet and not in_house:
        in_house = float(final_from_sheet)

    images = _image_attachments_from_trade_in(tis)
    img_count = len(images)
    attachment_summary = (
        f"{img_count} product image{'s' if img_count != 1 else ''} "
        f"(7-day presigned URL{'s' if img_count != 1 else ''})"
        if img_count
        else "No images attached (S3 unavailable or no keys on file)"
    )

    payload: dict[str, Any] = {
        "Date Submitted": _iso_date(vs.created_at),
        "Product Name": _product_name(vs),
        "Brand": vs.brand or "",
        "Model Number": vs.model or "",
        "Category": vs.category or "",
        "Age (Years)": float(vs.age_years or 0),
        "Condition": vs.condition_grade or "",
        "Product Images": images,
        "Customer Asking Price (KES)": float(vs.seller_asking_price or 0),
        "AI Evaluated Price (KES)": float(vs.opening_offer or 0),
        "Vertex AI Price (KES)": 0,  # historical records predate Vertex integration
        "Attachment Summary": attachment_summary,
        "Customer Name": vs.seller_name or "",
        "Customer Phone": vs.seller_phone or "",
        "Evaluation Status": vs.decision or "",
        "Notes": (vs.decision_reason or "")[:2000],
    }
    if in_house is not None:
        payload["In-House Evaluator Price (KES)"] = in_house

    # Strip empty / None / 0 for optional fields so Airtable doesn't flood with blanks
    clean = {k: v for k, v in payload.items() if v not in (None, "", [])}
    return clean


def run_update_images(cfg: dict[str, str]) -> int:
    """PATCH-only pass: attach fresh 7-day presigned URLs to already-backfilled
    rows. Useful when the initial backfill ran before S3 was configured.
    """
    logger.info("=" * 60)
    logger.info("Airtable image-update pass starting (PATCH-only, no inserts)")
    logger.info("=" * 60)

    try:
        from app.services.s3_service import s3_client
    except Exception:
        s3_client = None
    if not s3_client:
        logger.error(
            "S3 client unavailable - set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
            "in the EC2 .env and restart before running this."
        )
        return 3

    db: Session = SessionLocal()
    try:
        sessions: list[ValuationSession] = (
            db.query(ValuationSession)
            .order_by(ValuationSession.created_at.asc())
            .all()
        )
        trade_in_ids = {vs.trade_in_session_id for vs in sessions if vs.trade_in_session_id}
        trade_in_map: dict[int, TradeInSession] = {}
        if trade_in_ids:
            tis_rows = (
                db.query(TradeInSession)
                .filter(TradeInSession.id.in_(trade_in_ids))
                .all()
            )
            trade_in_map = {t.id: t for t in tis_rows}

        counters = {"found": len(sessions), "patched": 0, "no_images": 0, "not_in_airtable": 0, "failed": 0}

        for batch in _chunked(sessions, BATCH_SIZE):
            for vs in batch:
                tis = trade_in_map.get(vs.trade_in_session_id)
                images = _image_attachments_from_trade_in(tis)
                if not images:
                    counters["no_images"] += 1
                    continue

                date_iso = _iso_date(vs.created_at)
                prod_name = _product_name(vs)
                record_id = _find_in_airtable(cfg, date_iso, prod_name)
                if not record_id:
                    counters["not_in_airtable"] += 1
                    logger.debug(f"Skip (not in Airtable): {prod_name} @ {date_iso}")
                    continue

                img_count = len(images)
                summary = (
                    f"{img_count} product image{'s' if img_count != 1 else ''} "
                    f"(7-day presigned URL{'s' if img_count != 1 else ''})"
                )
                patch_fields: dict[str, Any] = {
                    "Product Images": images,
                    "Attachment Summary": summary,
                }
                # While we're patching, also backfill the enriched metadata
                # fields that earlier runs never populated.
                if vs.seller_name:
                    patch_fields["Customer Name"] = vs.seller_name
                if vs.seller_phone:
                    patch_fields["Customer Phone"] = vs.seller_phone
                if vs.decision:
                    patch_fields["Evaluation Status"] = vs.decision
                if vs.decision_reason:
                    patch_fields["Notes"] = str(vs.decision_reason)[:2000]

                ok, err = _patch_record(cfg, record_id, patch_fields)
                if ok:
                    counters["patched"] += 1
                    logger.info(
                        f"Patched {counters['patched']}: {prod_name} (+{img_count} images)"
                    )
                else:
                    counters["failed"] += 1
                    logger.warning(f"Patch failed for {prod_name}: {err}")
            time.sleep(BATCH_DELAY_SECONDS)

        logger.info("=" * 60)
        logger.info(
            "Image-update summary: "
            f"found={counters['found']} | "
            f"patched={counters['patched']} | "
            f"no_images={counters['no_images']} | "
            f"not_in_airtable={counters['not_in_airtable']} | "
            f"failed={counters['failed']}"
        )
        logger.info("=" * 60)
        return 0 if counters["failed"] == 0 else 2
    finally:
        db.close()


def run() -> int:
    parser = argparse.ArgumentParser(description="Airtable backfill/updater")
    parser.add_argument(
        "--update-images",
        action="store_true",
        help=(
            "PATCH-only mode: refresh Product Images + Attachment Summary on rows "
            "that already exist in Airtable. Run this after S3 is configured to "
            "backfill the image attachments that earlier runs left blank."
        ),
    )
    args = parser.parse_args()

    settings = get_settings()
    cfg = _get_config()
    if cfg is None:
        logger.error(
            "Airtable is not configured (AIRTABLE_API_TOKEN / AIRTABLE_BASE_ID missing)."
        )
        return 1

    if args.update_images:
        return run_update_images(cfg)

    logger.info("=" * 60)
    logger.info(f"Airtable backfill starting (base={cfg['base_id']}, table={cfg['table']})")
    logger.info("=" * 60)

    sheet_index = _load_sheet_index()

    db: Session = SessionLocal()
    try:
        sessions: list[ValuationSession] = (
            db.query(ValuationSession)
            .order_by(ValuationSession.created_at.asc())
            .all()
        )
        logger.info(f"Backfill: found {len(sessions)} valuation sessions in DB")

        trade_in_ids = {vs.trade_in_session_id for vs in sessions if vs.trade_in_session_id}
        trade_in_map: dict[int, TradeInSession] = {}
        if trade_in_ids:
            tis_rows = (
                db.query(TradeInSession)
                .filter(TradeInSession.id.in_(trade_in_ids))
                .all()
            )
            trade_in_map = {t.id: t for t in tis_rows}

        session_ids = [vs.id for vs in sessions]
        expert_map: dict[str, ExpertPriceFeedback] = {}
        if session_ids:
            expert_rows = (
                db.query(ExpertPriceFeedback)
                .filter(ExpertPriceFeedback.valuation_session_id.in_(session_ids))
                .order_by(ExpertPriceFeedback.created_at.desc())
                .all()
            )
            # Most recent expert price per valuation_session wins
            for row in expert_rows:
                if row.valuation_session_id not in expert_map:
                    expert_map[row.valuation_session_id] = row

        counters = {"found": len(sessions), "written": 0, "skipped": 0, "failed": 0}

        for batch in _chunked(sessions, BATCH_SIZE):
            for vs in batch:
                tis = trade_in_map.get(vs.trade_in_session_id)
                expert = expert_map.get(vs.id)
                payload = build_record(vs, tis, expert, sheet_index)
                date_iso = payload["Date Submitted"]
                prod_name = payload["Product Name"]

                if _already_in_airtable(cfg, date_iso, prod_name):
                    counters["skipped"] += 1
                    logger.debug(f"Skip existing: {prod_name} @ {date_iso}")
                    continue

                ok, err = _post_record(cfg, payload)
                if ok:
                    counters["written"] += 1
                    logger.info(
                        f"Wrote {counters['written']}/{counters['found']}: "
                        f"{prod_name} @ {date_iso}"
                    )
                else:
                    counters["failed"] += 1
                    logger.warning(f"Failed to write {prod_name}: {err}")

            time.sleep(BATCH_DELAY_SECONDS)

        logger.info("=" * 60)
        logger.info(
            "Backfill summary: "
            f"found={counters['found']} | "
            f"written={counters['written']} | "
            f"skipped={counters['skipped']} | "
            f"failed={counters['failed']}"
        )
        logger.info("=" * 60)
        return 0 if counters["failed"] == 0 else 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(run())
