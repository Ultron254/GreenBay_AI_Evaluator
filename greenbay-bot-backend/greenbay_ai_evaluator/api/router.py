"""
FastAPI router for the GreenBay AI Evaluator.

Endpoints:
  POST  /evaluate              — Run deterministic valuation
  POST  /{session_id}/counter  — Process a seller counter-offer
  GET   /{session_id}          — Retrieve full session detail
"""

from __future__ import annotations

import asyncio
import base64
import io
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from loguru import logger
from sqlalchemy.orm import Session

from app.database.db import get_db
from greenbay_ai_evaluator.api.security import verify_admin_key
from greenbay_ai_evaluator.api.schemas import (
    AcceptOfferResponse,
    CounterRequest,
    CounterResponse,
    DecisionLedgerOut,
    EvaluateRequest,
    EvaluateResponse,
    ExpertFeedbackRequest,
    ExpertFeedbackResponse,
    InventoryStatsOut,
    NegotiationRoundOut,
    PickupNotifyRequest,
    PickupNotifyResponse,
    RejectionChoiceRequest,
    RejectionChoiceResponse,
    RelatedProductOut,
    SessionDetailResponse,
)
from greenbay_ai_evaluator.config import CONDITION_GRADE_MAP
# NEGOTIATION PAUSED -- imports kept for future re-enablement
# from greenbay_ai_evaluator.engine.negotiation_engine import (
#     NegotiationState,
#     process_counter,
# )
from greenbay_ai_evaluator.engine.offer_engine import (
    Comparable,
    PricingPolicyData,
    _round_kes_500,
    _round_price,
    compute_valuation,
    reconcile_retail_price,
)
from greenbay_ai_evaluator.services.reference_data_service import (
    lookup_matrix,
    lookup_sales_stock,
    get_category_acquisition_ratio,
)
from greenbay_ai_evaluator.models.evaluator_models import (
    DecisionLedger,
    NegotiationRound,
    PricingPolicy,
    ValuationSession,
)
from greenbay_ai_evaluator.services.comparables_service import get_comparables
from greenbay_ai_evaluator.services.image_quality_service import (
    score_images,
    score_images_with_rejections,
)
from greenbay_ai_evaluator.services.market_price_service import search_internet_price
from greenbay_ai_evaluator.services.marketplace_scraper import get_marketplace_prices
from greenbay_ai_evaluator.services.risk_service import assess_risk
from greenbay_ai_evaluator.services.vision_service import analyze_images
from greenbay_ai_evaluator.services.google_lens_service import identify_product_multi
# CR-2: Self-learning pricing from Google Sheet
from greenbay_ai_evaluator.services.pricing_learner import get_historical_price
# CR-3: Image duplicate detection
from greenbay_ai_evaluator.services.image_hash_service import (
    check_duplicates,
    store_session_hashes,
)

from app.database.models import ExpertPriceFeedback, PickupRequest, ShopifyProduct, TradeInSession
# Airtable backup data repository (write-only; never blocks the response)
from greenbay_ai_evaluator.services.airtable_service import (
    get_historical_accuracy_ratio,
    write_evaluation_async as airtable_write_async,
)

evaluator_router = APIRouter()


# ---------------------------------------------------------------------------
# Live service health-check (issue: "I don't know what's broken in prod")
# Key-gated. Makes REAL calls to each dependency, never raises.
# GET /tradein/health/services?key=...
# ---------------------------------------------------------------------------
@evaluator_router.get("/health/services")
def health_services(_: bool = Depends(verify_admin_key)):
    """Run live probes against every external dependency.

    Gated by DASHBOARD_KEY (header X-Admin-Key or ?key=) so the report —
    which can include error snippets — is not publicly exposed.
    """
    from greenbay_ai_evaluator.services.live_healthcheck_service import (
        run_live_healthcheck,
    )
    return run_live_healthcheck()


@evaluator_router.get("/dashboard/metrics")
def dashboard_metrics(
    days: int = 30,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_key),
):
    """Pricing/performance metrics for the ops dashboard (gated)."""
    from greenbay_ai_evaluator.services.evaluator_metrics_service import (
        compute_evaluator_metrics,
    )
    return compute_evaluator_metrics(db, days=max(1, min(days, 365)))


@evaluator_router.get("/dashboard/calibration")
def dashboard_calibration(
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_key),
):
    """Back-test the pricing policy against real sold prices (issue #12, gated)."""
    from greenbay_ai_evaluator.services.calibration_service import compute_calibration
    return compute_calibration(db)


@evaluator_router.get("/dashboard")
def dashboard_page():
    """Serve the single-page ops dashboard shell (NOT gated).

    The HTML itself contains no data — it asks for the access key and then
    fetches the gated /dashboard/metrics and /health/services endpoints with
    it. This lets the home page link to the dashboard without leaking the key.
    """
    from fastapi.responses import HTMLResponse
    from greenbay_ai_evaluator.api.dashboard_page import DASHBOARD_HTML
    return HTMLResponse(content=DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Airtable backfill (issue #2/#9): replay fallback queue + backfill DB rows
# that were never written to Airtable (e.g. the silent stop since May 22).
# dry_run=true previews; pass dry_run=false to actually write.
# ---------------------------------------------------------------------------
@evaluator_router.post("/admin/airtable-backfill")
def airtable_backfill(
    since: str = "2026-05-22",
    dry_run: bool = True,
    limit: int = 2000,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_key),
):
    """Backfill Airtable from ValuationSession rows + replay the fallback queue.

    De-dupes against existing Airtable rows via the 'Ref: <id8>' marker in Notes.
    """
    from datetime import datetime as _dt
    try:
        since_dt = _dt.fromisoformat(since)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")

    from greenbay_ai_evaluator.services.airtable_service import (
        _get_config, list_records_paginated, write_evaluation, retry_failed_writes,
    )

    cfg = _get_config()
    if cfg is None:
        raise HTTPException(status_code=400, detail="Airtable not configured")

    # 1. Existing refs already in Airtable (dedupe) — scan Notes for 'Ref: xxxxxxxx'.
    existing_refs: set[str] = set()
    try:
        for rec in list_records_paginated(cfg, fields=["Notes"]):
            note = (rec.get("fields", {}) or {}).get("Notes", "") or ""
            m = _re.search(r"Ref:\s*([0-9a-f]{8})", note)
            if m:
                existing_refs.add(m.group(1))
    except Exception as e:
        logger.warning(f"Backfill: could not list existing Airtable refs: {e}")

    # 2. Candidate sessions since the cutoff.
    sessions = (
        db.query(ValuationSession)
        .filter(ValuationSession.created_at >= since_dt)
        .order_by(ValuationSession.created_at.asc())
        .limit(max(1, min(limit, 10000)))
        .all()
    )

    to_write = [vs for vs in sessions if vs.id[:8] not in existing_refs]

    result: dict[str, Any] = {
        "since": since,
        "dry_run": dry_run,
        "sessions_found": len(sessions),
        "already_in_airtable": len(sessions) - len(to_write),
        "to_backfill": len(to_write),
        "written": 0,
        "failed": 0,
        "fallback_replayed": 0,
        "samples": [
            {"id": vs.id[:8], "product": f"{vs.brand} {vs.model} {vs.category}",
             "ai_price": vs.opening_offer, "date": str(getattr(vs, "created_at", ""))}
            for vs in to_write[:10]
        ],
    }

    if dry_run:
        return result

    # 3. Replay disk fallback queue first.
    try:
        result["fallback_replayed"] = retry_failed_writes()
    except Exception as e:
        logger.warning(f"Backfill: fallback replay failed: {e}")

    # 4. Write missing sessions.
    for vs in to_write:
        try:
            payload = _build_airtable_payload_from_session(vs)
            if write_evaluation(payload):
                result["written"] += 1
            else:
                result["failed"] += 1
        except Exception as e:
            logger.warning(f"Backfill: session {vs.id[:8]} failed: {e}")
            result["failed"] += 1

    logger.info(
        f"Airtable backfill complete: written={result['written']} "
        f"failed={result['failed']} replayed={result['fallback_replayed']}"
    )
    return result


@evaluator_router.post("/admin/airtable-patch-missing")
def airtable_patch_missing(
    since: str = "2026-04-01",
    dry_run: bool = True,
    limit: int = 5000,
    db: Session = Depends(get_db),
    _: bool = Depends(verify_admin_key),
):
    """Backfill EMPTY columns on existing Airtable rows from stored sessions.

    Matches a row to its session via the 'Ref: <id8>' marker in Notes (added to
    every live write). Only fields that are currently empty/zero are patched, so
    it is idempotent and safe to re-run. Fills: New Price (Estimate), Country,
    Currency, Customer Asking Price (KES), Customer Name, Customer Phone.
    """
    from datetime import datetime as _dt
    try:
        since_dt = _dt.fromisoformat(since)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")

    from greenbay_ai_evaluator.services.airtable_service import (
        _get_config, find_record_by_ref, find_record_by_model_price, patch_record_by_id,
    )
    if _get_config() is None:
        raise HTTPException(status_code=400, detail="Airtable not configured")

    sessions = (
        db.query(ValuationSession)
        .filter(ValuationSession.created_at >= since_dt)
        .order_by(ValuationSession.created_at.asc())
        .limit(max(1, min(limit, 20000)))
        .all()
    )

    result: dict[str, Any] = {
        "since": since, "dry_run": dry_run,
        "sessions_scanned": len(sessions),
        "rows_matched": 0, "rows_patched": 0, "no_match": 0, "already_full": 0,
        "samples": [],
    }

    def _empty(v) -> bool:
        return v is None or v == "" or v == 0

    for vs in sessions:
        # Prefer the exact Ref marker; fall back to Model+AI-price for older rows
        # that were written before the Ref marker existed.
        rec = find_record_by_ref(str(vs.id)[:8])
        if not rec and vs.model and vs.opening_offer:
            rec = find_record_by_model_price(vs.model, float(vs.opening_offer))
        if not rec:
            result["no_match"] += 1
            continue
        result["rows_matched"] += 1
        cur = rec["fields"]
        desired = {
            "New Price (Estimate)": float(getattr(vs, "retail_price", 0) or 0),
            "Country": getattr(vs, "country", "KE") or "KE",
            "Currency": getattr(vs, "currency_code", "KES") or "KES",
            "Customer Asking Price (KES)": float(vs.seller_asking_price or 0),
            "Customer Name": vs.seller_name or "",
            "Customer Phone": vs.seller_phone or "",
        }
        patch = {k: v for k, v in desired.items() if _empty(cur.get(k)) and not _empty(v)}
        if not patch:
            result["already_full"] += 1
            continue
        if len(result["samples"]) < 10:
            result["samples"].append({"id": str(vs.id)[:8], "patch": patch})
        if dry_run:
            continue
        if patch_record_by_id(rec["id"], patch):
            result["rows_patched"] += 1

    logger.info(
        f"Airtable patch-missing: matched={result['rows_matched']} "
        f"patched={result['rows_patched']} no_match={result['no_match']}"
    )
    return result


# ---------------------------------------------------------------------------
# Size resolution (v6.1) — drives accurate per-size pricing
# ---------------------------------------------------------------------------
import re as _re

_SIZE_UNIT_BY_CATEGORY: dict[str, str] = {
    "tv": "inch", "tv_monitor": "inch", "monitor": "inch",
    "washing_machine": "kg", "washer": "kg",
    "refrigerator": "litre", "fridge": "litre", "freezer": "litre", "chiller": "litre",
}


def _infer_size_from_text(category: str, *texts: str) -> tuple[float | None, str | None]:
    """Best-effort size inference from model number / vision text / titles.

    Returns (value, unit) or (None, None). Category-aware so we don't, e.g.,
    read a TV's '43' as litres.
    """
    cat = (category or "").lower().strip()
    unit = _SIZE_UNIT_BY_CATEGORY.get(cat)
    if not unit:
        return None, None

    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None, None

    if unit == "inch":
        # explicit "55 inch" / '55"' first
        m = _re.search(r'(\d{2,3})\s*(?:inch|inches|"|”|in\b)', blob)
        if m:
            v = float(m.group(1))
            if 14 <= v <= 120:
                return v, "inch"
        # leading 2-digit token in a model code, e.g. 43S5K, 55T6C, UA55
        for m in _re.finditer(r'\b[a-z]{0,3}(\d{2,3})[a-z0-9]*\b', blob):
            v = float(m.group(1))
            if 19 <= v <= 100:  # plausible TV diagonal
                return v, "inch"
    elif unit == "kg":
        m = _re.search(r'(\d{1,2}(?:\.\d)?)\s*kg', blob)
        if m:
            v = float(m.group(1))
            if 3 <= v <= 25:
                return v, "kg"
    elif unit == "litre":
        m = _re.search(r'(\d{2,4})\s*(?:litre|liter|litres|liters|ltr|l\b)', blob)
        if m:
            v = float(m.group(1))
            if 30 <= v <= 1000:
                return v, "litre"
    return None, None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _load_policy(category: str, db: Session) -> PricingPolicy:
    """Load active pricing policy for *category* or raise 400."""
    policy = (
        db.query(PricingPolicy)
        .filter(
            PricingPolicy.category == category.lower().strip(),
            PricingPolicy.is_active.is_(True),
        )
        .first()
    )
    if not policy:
        raise HTTPException(
            status_code=400,
            detail=f"No active pricing policy found for category '{category}'.",
        )
    return policy


def _policy_to_data(policy: PricingPolicy) -> PricingPolicyData:
    return PricingPolicyData(
        category=policy.category,
        margin_pct=policy.margin_pct,
        max_offer_pct=policy.max_offer_pct,
        walkaway_pct=policy.walkaway_pct,
        depreciation_year1=policy.depreciation_year1 or 0.25,
        depreciation_year2_3=policy.depreciation_year2_3 or 0.15,
        depreciation_year4_5=policy.depreciation_year4_5 or 0.10,
        depreciation_year6_plus=policy.depreciation_year6_plus or 0.07,
        round_step_pct=policy.round_step_pct or 0.05,
        max_negotiation_rounds=policy.max_negotiation_rounds or 3,
        brand_premium_json=policy.brand_premium_json or {},
        condition_multiplier_json=policy.condition_multiplier_json or {},
    )


def _eval_image_data_url_to_jpeg_bytes(data_url: str) -> bytes:
    """Decode a data-URL or raw base64 appliance photo and normalize to JPEG bytes."""
    from PIL import Image

    payload = data_url.strip()
    if "," in payload:
        payload = payload.split(",", 1)[1]
    raw = base64.b64decode(payload)
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _upload_eval_image_data_to_s3(image_data: list[str], folder_id: str) -> list[str]:
    """Upload evaluation photos to S3 at uploads/{folder_id}/{index}.jpg. Best-effort."""
    import concurrent.futures

    from app.services.s3_service import upload_bytes_to_s3

    async def _run() -> list[str]:
        keys_out: list[str] = []
        for i, data_url in enumerate(image_data[:8]):
            if not (data_url or "").strip():
                continue
            try:
                jpeg_bytes = _eval_image_data_url_to_jpeg_bytes(data_url)
                key = f"uploads/{folder_id}/{i}.jpg"
                await upload_bytes_to_s3(jpeg_bytes, key, content_type="image/jpeg")
                keys_out.append(key)
            except Exception as e:
                logger.warning(f"Evaluate: S3 upload failed for image index {i}: {e}")
        return keys_out

    def _in_thread() -> list[str]:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_in_thread).result()


def _repressign_images_for_airtable(
    req_image_urls: list[str],
    trade_in_session_id: int | None,
    db: Session,
    expires_in_seconds: int = 7 * 24 * 3600,
    uploaded_s3_keys: list[str] | None = None,
) -> list[dict]:
    """Build a list of `{"url": "..."}` dicts for Airtable attachments.

    Priority:
      1. ``uploaded_s3_keys`` from this request (anonymous valuations / fresh keys).
      2. Keys on ``TradeInSession`` when ``trade_in_session_id`` is set.
      3. Raw ``req_image_urls`` (HTTP URLs).

    When S3 is configured, re-presign each key for *expires_in_seconds* (default 7 days)
    so Airtable can fetch the image after short-lived URLs expire.
    """
    raw_urls = [u for u in (req_image_urls or []) if u]
    raw_attachments = [{"url": u} for u in raw_urls]

    keys_to_sign: list[str] = []
    if uploaded_s3_keys:
        keys_to_sign = [k for k in uploaded_s3_keys if k][:8]
    elif trade_in_session_id:
        try:
            tis = (
                db.query(TradeInSession)
                .filter(TradeInSession.id == trade_in_session_id)
                .first()
            )
            if tis and tis.s3_keys:
                keys_to_sign = (
                    list(tis.s3_keys) if isinstance(tis.s3_keys, list) else []
                )[:8]
        except Exception as e:
            logger.warning(f"TradeInSession lookup for Airtable images failed: {e}")

    if not keys_to_sign:
        return raw_attachments

    try:
        from app.services.s3_service import _ensure_s3_client, settings as s3_settings
    except Exception:
        return raw_attachments

    client = _ensure_s3_client()
    if not client or not getattr(s3_settings, "aws_s3_bucket", None):
        return raw_attachments

    fresh: list[dict] = []
    for key in keys_to_sign[:8]:
        try:
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": s3_settings.aws_s3_bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )
            if f".s3.{s3_settings.aws_region}.amazonaws.com" not in url:
                url = url.replace(
                    ".s3.amazonaws.com",
                    f".s3.{s3_settings.aws_region}.amazonaws.com",
                )
            fresh.append({"url": url})
        except Exception as e:
            logger.warning(f"Airtable: 7-day presign failed for {key}: {e}")

    return fresh or raw_attachments


def _build_airtable_payload(
    vs: ValuationSession,
    req: EvaluateRequest,
    grade: str,
    images_for_airtable: list[dict],
    vertex_result: dict | None,
    *,
    had_image_inputs: bool = False,
    s3_configured: bool = False,
    pricing_justification: str = "",
) -> dict:
    """Assemble the Airtable 'Appliance Evaluations' record from the evaluation.

    Field names match the live Airtable base. Submission ID and Price Variance (%)
    are auto-generated by Airtable (autoNumber and formula respectively) and are
    therefore omitted from the payload.
    """
    product_name = " ".join(
        part for part in [
            (req.brand or "").strip(),
            (req.model or "").strip(),
            (req.category or "").strip(),
        ] if part
    ) or (req.category or "Unknown")

    vertex_price = 0.0
    vertex_observations = ""
    if isinstance(vertex_result, dict):
        try:
            vertex_price = float(vertex_result.get("estimated_price_kes") or 0) or 0.0
        except (TypeError, ValueError):
            vertex_price = 0.0
        vertex_observations = str(vertex_result.get("observations") or "")[:300]

    # Attachment summary: short one-line description of what's attached
    img_count = len(images_for_airtable or [])
    if img_count:
        attachment_summary = (
            f"{img_count} product image{'s' if img_count != 1 else ''} "
            f"(7-day presigned URL{'s' if img_count != 1 else ''})"
        )
    elif not had_image_inputs:
        attachment_summary = "No images submitted"
    elif not s3_configured:
        attachment_summary = "No images attached (S3 not configured)"
    else:
        attachment_summary = "Images not attached (upload or presign failed)"

    # Notes: combine the decision reason with any Vertex AI observations.
    # The 'Ref:' marker lets accept-offer / backfill patches find this row later.
    note_parts: list[str] = []
    if vs.decision_reason:
        note_parts.append(str(vs.decision_reason))
    if vertex_observations:
        note_parts.append(f"Vertex AI: {vertex_observations}")
    note_parts.append(f"Ref: {str(vs.id)[:8]}")
    notes = " | ".join(p for p in note_parts if p)[:2000]

    payload: dict[str, Any] = {
        "Date Submitted": datetime.now(timezone.utc).isoformat(),
        "Product Name": product_name,
        "Brand": req.brand or "",
        "Model Number": req.model or "",
        "Category": req.category or "",
        "Age (Years)": float(req.age_years or 0),
        "Condition": grade,
        "Product Images": images_for_airtable,
        "Customer Asking Price (KES)": float(req.seller_asking_price or 0),
        "AI Evaluated Price (KES)": float(vs.opening_offer or 0),
        "New Price (Estimate)": float(getattr(vs, "retail_price", 0) or 0),
        "Vertex AI Price (KES)": vertex_price,
        "Attachment Summary": attachment_summary,
        "Customer Name": req.seller_name or "",
        "Customer Phone": req.seller_phone or "",
        "Evaluation Status": vs.decision or "",
        "Notes": notes,
        "AI Pricing Justification": pricing_justification[:10000] if pricing_justification else "",
        "Country": getattr(vs, "country", "KE") or "KE",
        "Currency": getattr(vs, "currency_code", "KES") or "KES",
        # In-House Evaluator Price (KES) is intentionally omitted — filled by humans.
        # Submission ID and Price Variance (%) are auto-generated by Airtable.
    }
    # Size (only when known) — harmless if the column doesn't exist (writer drops it).
    _size_v = getattr(vs, "size_value", None)
    _size_u = getattr(vs, "size_unit", None)
    if _size_v and _size_u:
        payload["Size"] = f"{_size_v:g} {_size_u}"
    return payload


def _build_airtable_payload_from_session(vs: ValuationSession) -> dict:
    """Reconstruct an Airtable record from a stored ValuationSession (for backfill).

    Uses only persisted columns — there is no live EvaluateRequest at backfill
    time. The session id is embedded in Notes so the backfill can be de-duped.
    """
    product_name = " ".join(
        part for part in [
            (vs.brand or "").strip(),
            (vs.model or "").strip(),
            (vs.category or "").strip(),
        ] if part
    ) or (vs.category or "Unknown")

    date_submitted = None
    if getattr(vs, "created_at", None):
        try:
            date_submitted = vs.created_at.isoformat()
        except Exception:
            date_submitted = None

    notes_parts: list[str] = []
    if vs.decision_reason:
        notes_parts.append(str(vs.decision_reason))
    notes_parts.append(f"Ref: {vs.id[:8]}")  # dedupe marker
    notes = " | ".join(notes_parts)[:2000]

    payload: dict[str, Any] = {
        "Date Submitted": date_submitted or datetime.now(timezone.utc).isoformat(),
        "Product Name": product_name,
        "Brand": vs.brand or "",
        "Model Number": vs.model or "",
        "Category": vs.category or "",
        "Age (Years)": float(vs.age_years or 0),
        "Condition": vs.condition_grade or "",
        "Customer Asking Price (KES)": float(vs.seller_asking_price or 0),
        "AI Evaluated Price (KES)": float(vs.opening_offer or 0),
        "New Price (Estimate)": float(getattr(vs, "retail_price", 0) or 0),
        "Customer Name": vs.seller_name or "",
        "Customer Phone": vs.seller_phone or "",
        "Evaluation Status": vs.decision or "",
        "Notes": notes,
        "Country": getattr(vs, "country", "KE") or "KE",
        "Currency": getattr(vs, "currency_code", "KES") or "KES",
    }
    sv = getattr(vs, "size_value", None)
    su = getattr(vs, "size_unit", None)
    if sv and su:
        payload["Size"] = f"{sv:g} {su}"
    return payload


def _enrich_justification(
    *,
    base_trace: str,
    price_verification: dict | None,
    internet_result,
    size_value: float | None,
    size_unit: str | None,
    size_source: str | None,
    reference_source: str,
) -> str:
    """Append real, auditable evidence to the engine's math trace (issue #8).

    Captures: the new-price source (verified vs estimate), Gemini/Google search
    source URLs, the size used and how it was obtained, and which reference data
    drove the base value. This is what gets stored in 'AI Pricing Justification'.
    """
    lines: list[str] = [base_trace or "", "", "EVIDENCE & SOURCES", "-" * 50]

    # Size
    if size_value and size_unit:
        lines.append(f"Size used: {size_value:g} {size_unit} (source: {size_source or 'user'})")
    else:
        lines.append("Size: UNKNOWN — could not be provided or inferred (lowers confidence)")

    # Reference data that drove the base value
    if reference_source:
        lines.append(f"Base value reference: {reference_source}")
    else:
        lines.append("Base value reference: external research / reconciled retail (no GreenBay match)")

    # New-price verification + Gemini sources
    pv = price_verification or {}
    verified = pv.get("new_price_verified")
    num_real = pv.get("num_real_sources", 0)
    reconciled = pv.get("reconciled_price")
    if verified:
        lines.append(
            f"New price: VERIFIED from {num_real} real source(s); "
            f"reconciled retail = KES {float(reconciled or 0):,.0f}"
        )
    else:
        lines.append(
            "New price: NOT VERIFIED — no live market source returned a price; "
            "value is a category estimate and this evaluation was routed for review."
        )

    # List the actual source URLs Gemini grounded on (real evidence)
    src_urls: list[str] = []
    if internet_result is not None:
        for s in (getattr(internet_result, "sources", None) or [])[:5]:
            url = s.get("url") if isinstance(s, dict) else None
            if url:
                src_urls.append(url)
    if src_urls:
        lines.append("Google/Gemini sources:")
        lines.extend(f"  - {u}" for u in src_urls)

    # Per-source price breakdown from reconciliation
    for s in (pv.get("sources") or []):
        tag = " (estimate)" if s.get("is_estimate") else ""
        lines.append(
            f"  · {s.get('source')}: KES {float(s.get('price') or 0):,.0f}{tag}"
        )

    return "\n".join(l for l in lines if l is not None)


def _policy_snapshot(policy: PricingPolicy) -> dict:
    return {
        "category": policy.category,
        "margin_pct": policy.margin_pct,
        "max_offer_pct": policy.max_offer_pct,
        "walkaway_pct": policy.walkaway_pct,
        "depreciation_year1": policy.depreciation_year1,
        "depreciation_year2_3": policy.depreciation_year2_3,
        "depreciation_year4_5": policy.depreciation_year4_5,
        "depreciation_year6_plus": policy.depreciation_year6_plus,
        "round_step_pct": policy.round_step_pct,
        "max_negotiation_rounds": policy.max_negotiation_rounds,
        "brand_premium_json": policy.brand_premium_json,
        "condition_multiplier_json": policy.condition_multiplier_json,
        "updated_at": str(policy.updated_at) if policy.updated_at else str(policy.created_at),
    }


# ---------------------------------------------------------------------------
# POST /evaluate
# ---------------------------------------------------------------------------
@evaluator_router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_trade_in(req: EvaluateRequest, db: Session = Depends(get_db)):
    """Create a deterministic valuation for a trade-in submission."""
    eval_s3_keys: list[str] = []
    s3_configured_for_airtable = False
    try:
        # 1. Load pricing policy
        policy = _load_policy(req.category, db)
        policy_data = _policy_to_data(policy)

        # 2. Normalize condition grade
        grade = CONDITION_GRADE_MAP.get(req.condition_grade.lower().strip(), "C")

        # 3. Gather comparables
        comp_result = get_comparables(
            category=req.category,
            brand=req.brand,
            model=req.model,
            db_session=db,
        )

        # 3b. Run vision analysis if photos were submitted
        vision_result = None
        if req.image_data:
            try:
                import asyncio
                import concurrent.futures
                from app.config import get_settings
                settings = get_settings()

                def _run_vision():
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(
                            analyze_images(
                                images_base64=req.image_data[:8],
                                category=req.category,
                                brand_hint=req.brand,
                                model_hint=req.model,
                                age_years=req.age_years,
                                api_key=settings.anthropic_api_key,
                            )
                        )
                    finally:
                        loop.close()

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    vision_result = pool.submit(_run_vision).result(timeout=120)
                logger.info(f"Vision analysis complete: grade={vision_result.get('condition_grade')}")
            except Exception as ve:
                logger.warning(f"Vision analysis skipped: {ve}")

        # 3b-ii. Vertex AI (Gemini) secondary evaluation — advisory only.
        # Runs in a separate thread with its own event loop, matches the vision
        # analysis pattern above. Never blocks; any failure is logged and
        # `vertex_result` stays None.
        vertex_result = None
        if req.image_data:
            try:
                import asyncio
                import concurrent.futures
                from greenbay_ai_evaluator.services.vertex_ai_service import vertex_evaluate

                def _run_vertex():
                    loop = asyncio.new_event_loop()
                    try:
                        return loop.run_until_complete(
                            vertex_evaluate(
                                images_base64=req.image_data[:3],
                                category=req.category,
                                brand=req.brand,
                                age=req.age_years,
                                working_status=req.condition_grade,
                            )
                        )
                    finally:
                        loop.close()

                with concurrent.futures.ThreadPoolExecutor() as pool:
                    vertex_result = pool.submit(_run_vertex).result(timeout=45)
                if vertex_result:
                    logger.info(
                        f"Vertex AI: grade={vertex_result.get('condition_grade')}, "
                        f"price={vertex_result.get('estimated_price_kes')}, "
                        f"observations={vertex_result.get('observations', '')[:100]}"
                    )
            except Exception as ve:
                logger.warning(f"Vertex AI skipped (timeout or error): {ve}")

        # 3c. Google Cloud Vision product identification (Google Lens equivalent)
        lens_result = None
        if req.image_data:
            try:
                from app.config import get_settings
                settings_for_lens = get_settings()
                lens_result = identify_product_multi(
                    images_b64=req.image_data[:3],  # Use first 3 images
                    category_hint=req.category,
                    brand_hint=req.brand,
                    model_hint=req.model,
                    google_cloud_api_key=settings_for_lens.google_cloud_api_key,
                )
                if lens_result and lens_result.confidence > 20:
                    logger.info(
                        f"Google Lens identified: {lens_result.product_name}, "
                        f"brand={lens_result.brand}, model={lens_result.model}, "
                        f"price={lens_result.estimated_retail_price_kes}"
                    )
                    # Enrich request data with Google Lens findings
                    if lens_result.brand and not req.brand:
                        req.brand = lens_result.brand
                    if lens_result.model and not req.model:
                        req.model = lens_result.model
                    # Use Google Lens retail price as a reference if we don't have one
                    if lens_result.estimated_retail_price_kes and (
                        not req.retail_price or req.retail_price <= 0
                    ):
                        req.retail_price = lens_result.estimated_retail_price_kes
                        req.retail_price_source = "google_lens"
                        logger.info(f"Using Google Lens retail price: {req.retail_price}")
            except Exception as le:
                logger.warning(f"Google Lens identification failed: {le}")

        # 3d. Resolve appliance size (v6.1) — user value wins; else infer.
        size_value = req.size_value if (req.size_value and req.size_value > 0) else None
        size_unit = req.size_unit or _SIZE_UNIT_BY_CATEGORY.get((req.category or "").lower().strip())
        size_source = "user" if size_value else None
        if not size_value:
            _vision_obs = ""
            _vision_model = ""
            if vision_result:
                _vision_obs = " ".join(str(x) for x in (vision_result.get("key_observations") or []))
                _vision_model = str(vision_result.get("model_detected") or "")
            _lens_name = lens_result.product_name if lens_result else ""
            inferred_v, inferred_u = _infer_size_from_text(
                req.category, req.model, _vision_model, _vision_obs, _lens_name,
            )
            if inferred_v:
                size_value, size_unit, size_source = inferred_v, inferred_u, "inferred"
                logger.info(f"Size inferred: {size_value} {size_unit} (from model/vision text)")
            else:
                logger.info("Size unknown: could not infer; pricing confidence will be lower")

        # 4. Score images (OpenCV if base64 data available)
        iq_result = score_images(
            image_urls=req.image_urls,
            image_data=req.image_data,
        )

        # 5. Assess risk (rule-based fraud detection)
        risk_result = assess_risk(
            category=req.category,
            image_urls=req.image_urls,
            image_data=req.image_data,
            seller_asking_price=req.seller_asking_price,
            retail_price=req.retail_price,
            condition_grade=req.condition_grade,
            age_years=req.age_years,
            db_session=db,
        )

        # 5b. Build defects list from request
        defects_dicts = [d.model_dump() for d in req.defects]

        # 5c. Merge vision results into scoring if available
        if vision_result:
            # Use vision condition score if higher confidence
            vision_cond = vision_result.get("condition_score")
            if vision_cond is not None:
                # Blend: 60% vision, 40% seller-reported
                req.condition_score = vision_cond * 0.6 + req.condition_score * 0.4
            # Add vision-detected defects to the list
            for defect in vision_result.get("defects", []):
                defects_dicts.append(defect)
            # Use vision photo quality if available
            viq = vision_result.get("photo_quality_score")
            if viq is not None:
                iq_result.score = viq

            # v6.1 FIX (issue #11): the vision grade was previously discarded,
            # so condition was driven entirely by the seller's wizard choice
            # (skewed to "A"). Reconcile: when vision is confident and grades
            # the item WORSE than the seller claimed, trust the photos.
            _grade_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
            vision_grade = str(vision_result.get("condition_grade") or "").upper().strip()
            vision_defects = vision_result.get("defects", [])
            has_visible_wear = any(
                str(d.get("severity", "")).lower() in ("medium", "high")
                for d in vision_defects
            )
            if vision_grade in _grade_rank:
                # Take the worse of seller-reported vs vision-reported grade.
                if _grade_rank[vision_grade] > _grade_rank.get(grade, 1):
                    logger.info(
                        f"Condition grade adjusted by vision: seller={grade} -> "
                        f"vision={vision_grade} (visible wear={has_visible_wear})"
                    )
                    grade = vision_grade
                elif has_visible_wear and grade == "A":
                    # Vision saw real wear but still said A — downgrade to B as a floor.
                    logger.info("Condition downgraded A->B: vision detected visible wear")
                    grade = "B"

        # 5d. Age-based condition grade cap (hard rule)
        # Products cannot be Grade A if old, regardless of what vision says
        if req.age_years >= 10 and grade in ("A", "B"):
            grade = "C"
            logger.info(f"Condition capped to C: product is {req.age_years:.0f} years old")
        elif req.age_years >= 5 and grade == "A":
            grade = "B"
            logger.info(f"Condition capped to B: product is {req.age_years:.0f} years old")

        # 6a. Quality rejection pre-screening
        HARD_REJECT_KEYWORDS = {
            "rust", "rusted", "corrosion", "heavy_dent", "major_crack",
            "missing_part", "broken", "shattered", "severe_damage",
        }
        defect_types = {d.get("type", "").lower() for d in defects_dicts}
        defect_descs = " ".join(d.get("description", "").lower() for d in defects_dicts)

        is_hard_reject = bool(defect_types & HARD_REJECT_KEYWORDS) or any(
            kw in defect_descs for kw in ["rust", "rusted", "heavy dent", "major crack"]
        )
        grade_lower = req.condition_grade.lower().strip()
        if grade_lower in ("d", "poor", "not working"):
            is_hard_reject = True

        is_review_flag = req.age_years > 5 or grade_lower in ("partially working",)

        # 6a. Multi-source price verification (5-point system)
        price_verification = None
        internet_price = None
        marketplace_avg = None
        shopify_avg = None
        expert_avg = None
        internet_result = None
        mkt_result = None

        # --- Source A: Internet price lookup (v6: Gemini with Google Search grounding) ---
        _search_country = getattr(req, "country", "KE") or "KE"
        try:
            internet_result = search_internet_price(
                brand=req.brand, model=req.model,
                category=req.category, condition=req.condition_grade,
                country=_search_country,
                size_value=size_value, size_unit=size_unit,
            )
            internet_price = internet_result.launch_price
            logger.info(f"Internet price: {internet_price} (confidence: {internet_result.confidence})")
        except Exception as e:
            logger.warning(f"Internet price lookup failed: {e}")

        # --- Source B: Live marketplace scraping (Jiji/Jumia) ---
        try:
            import asyncio
            import concurrent.futures

            def _run_marketplace():
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(
                        get_marketplace_prices(
                            brand=req.brand, model=req.model,
                            category=req.category,
                        )
                    )
                finally:
                    loop.close()

            with concurrent.futures.ThreadPoolExecutor() as pool:
                mkt_result = pool.submit(_run_marketplace).result(timeout=15)
            marketplace_avg = mkt_result.avg_price if mkt_result.count > 0 else None
            logger.info(f"Marketplace: avg={marketplace_avg}, count={mkt_result.count}")
        except Exception as e:
            logger.warning(f"Marketplace scraping failed: {e}")

        # --- Source C: Shopify inventory average ---
        try:
            if comp_result.sources_breakdown and comp_result.sources_breakdown.get("shopify", 0) > 0:
                shopify_avg = comp_result.weighted_average
            logger.info(f"Shopify inventory: avg={shopify_avg}")
        except Exception as e:
            logger.warning(f"Shopify lookup failed: {e}")

        # --- Source D: Expert feedback average (learning from past expert prices) ---
        try:
            from sqlalchemy import func as sqla_func, desc as sqla_desc
            # Query expert feedback for similar products (same category + brand)
            expert_query = (
                db.query(ExpertPriceFeedback)
                .filter(
                    ExpertPriceFeedback.product_category == req.category,
                    ExpertPriceFeedback.brand == req.brand,
                    ExpertPriceFeedback.expert_price > 0,
                )
            )
            # If we know the model, prefer exact model matches
            exact_model_prices = (
                expert_query
                .filter(ExpertPriceFeedback.model == req.model)
                .order_by(sqla_desc(ExpertPriceFeedback.created_at))
                .limit(10)
                .all()
            )
            if exact_model_prices:
                # Exact model match — highly relevant
                expert_avg = sum(f.expert_price for f in exact_model_prices) / len(exact_model_prices)
                logger.info(f"Expert feedback (exact model): avg={expert_avg}, count={len(exact_model_prices)}")
            else:
                # Fallback: same category + brand, weighted by recency
                brand_prices = (
                    expert_query
                    .order_by(sqla_desc(ExpertPriceFeedback.created_at))
                    .limit(20)
                    .all()
                )
                if brand_prices:
                    # Weight more recent feedback higher
                    total_weight = 0
                    weighted_sum = 0
                    for i, f in enumerate(brand_prices):
                        weight = 1.0 / (i + 1)  # Recency decay: 1, 0.5, 0.33, ...
                        weighted_sum += f.expert_price * weight
                        total_weight += weight
                    expert_avg = weighted_sum / total_weight if total_weight > 0 else None
                    logger.info(f"Expert feedback (brand-level): avg={expert_avg}, count={len(brand_prices)}")
                else:
                    logger.info("Expert feedback: no matching records found")
        except Exception as e:
            logger.warning(f"Expert feedback lookup failed: {e}")

        # --- Source E: Google Sheet historical prices (CR-2/CR-4) ---
        historical_avg = None
        try:
            hist_data = get_historical_price(
                brand=req.brand,
                model=req.model,
                category=req.category,
                condition_grade=grade,
                age_years=req.age_years,
            )
            if hist_data:
                historical_avg = hist_data["price"]
                logger.info(
                    f"Historical price (Sheet): avg={historical_avg}, "
                    f"confidence={hist_data['confidence']}, "
                    f"points={hist_data['data_points']}"
                )
        except Exception as e:
            logger.warning(f"Historical price lookup failed: {e}")

        # --- Reconcile all sources (60% human intelligence / 40% AI market) ---
        db_comparables_avg = (
            comp_result.weighted_average if comp_result.count > 0 else None
        )
        try:
            price_verification = reconcile_retail_price(
                frontend_price=req.retail_price,
                internet_price=internet_price,
                marketplace_avg=marketplace_avg,
                shopify_avg=shopify_avg,
                expert_avg=expert_avg,
                historical_avg=historical_avg,
                comparables_avg=db_comparables_avg,
                frontend_source=req.retail_price_source,
            )

            # Add detailed breakdown if sources responded
            if internet_result:
                price_verification["internet_data"] = {
                    "launch_price": internet_result.launch_price,
                    "resale_range": [internet_result.current_resale_low, internet_result.current_resale_high],
                    "sources": internet_result.sources[:3],
                    "confidence": internet_result.confidence,
                }
            if mkt_result:
                price_verification["marketplace_data"] = {
                    "avg_price": mkt_result.avg_price,
                    "price_range": [mkt_result.min_price, mkt_result.max_price],
                    "listing_count": mkt_result.count,
                    "confidence": mkt_result.confidence,
                }
            if lens_result and lens_result.confidence > 0:
                price_verification["google_lens_data"] = {
                    "product_name": lens_result.product_name,
                    "brand": lens_result.brand,
                    "model": lens_result.model,
                    "category": lens_result.category,
                    "estimated_retail_price": lens_result.estimated_retail_price_kes,
                    "release_year": lens_result.release_year,
                    "labels": lens_result.labels[:5],
                    "web_entities": [
                        {"description": e["description"], "score": e.get("score", 0)}
                        for e in lens_result.web_entities[:5]
                    ],
                    "confidence": lens_result.confidence,
                }

            if expert_avg:
                price_verification["expert_data"] = {
                    "avg_price": expert_avg,
                    "confidence": 80.0,
                }

            # Learning loop: Airtable-derived human/AI ratio (never mixed inside reconcile).
            try:
                learn_ratio = get_historical_accuracy_ratio(req.brand, req.category)
                if (
                    learn_ratio is not None
                    and price_verification.get("reconciled_price")
                ):
                    pre_airtable = float(price_verification["reconciled_price"])
                    adjusted = _round_kes_500(pre_airtable * learn_ratio)
                    price_verification["reconciled_price_pre_airtable_learning"] = pre_airtable
                    price_verification["airtable_accuracy_ratio_applied"] = learn_ratio
                    price_verification["reconciled_price"] = adjusted
                    logger.info(
                        f"Airtable accuracy learning: ratio={learn_ratio} "
                        f"reconciled KES {pre_airtable:,.0f} -> {adjusted:,.0f}"
                    )
            except Exception as ae:
                logger.warning(f"Airtable accuracy learning skipped: {ae}")

            logger.info(
                f"Price verification: {price_verification['num_sources']} sources, "
                f"reconciled={price_verification['reconciled_price']}, "
                f"frontend={req.retail_price}, expert_avg={expert_avg}"
            )
        except Exception as pve:
            logger.warning(f"Price reconciliation failed: {pve}")

        # Per-request diagnostic: prove multi-source pricing + learning-loop delta
        try:
            sb = comp_result.sources_breakdown or {}
            vpx = None
            if vertex_result and isinstance(vertex_result, dict):
                try:
                    _vp = float(vertex_result.get("estimated_price_kes") or 0)
                    vpx = _vp if _vp > 0 else None
                except (TypeError, ValueError):
                    vpx = None

            pv_without = None
            if price_verification:
                pv_without = reconcile_retail_price(
                    frontend_price=req.retail_price,
                    internet_price=internet_price,
                    marketplace_avg=marketplace_avg,
                    shopify_avg=shopify_avg,
                    expert_avg=expert_avg,
                    historical_avg=None,
                    comparables_avg=db_comparables_avg,
                    frontend_source=req.retail_price_source,
                )
            src_list = []
            if price_verification and price_verification.get("sources"):
                src_list = [x.get("source") for x in price_verification["sources"]]

            logger.info(
                "EVAL_PRICE_TRACE "
                f"brand={req.brand!r} category={req.category!r} "
                f"db_comparables_count={comp_result.count} db_sources={sb} "
                f"sheets_historical_kes={historical_avg} "
                "airtable_not_in_reconcile_blend=true "
                f"tavily_internet_kes={internet_price} marketplace_avg_kes={marketplace_avg} "
                f"shopify_inventory_kes={shopify_avg} expert_feedback_kes={expert_avg} "
                f"vertex_secondary_opinion_kes={vpx} "
                f"reconciled_retail_with_learner_kes={(price_verification or {}).get('reconciled_price')} "
                f"reconciled_retail_without_sheet_learner_kes={(pv_without or {}).get('reconciled_price')} "
                f"sources_used={src_list}"
            )
        except Exception as te:
            logger.warning(f"EVAL_PRICE_TRACE diagnostic failed: {te}")

        # 6b. Compute historical team average for floor/ceiling guardrails
        historical_team_avg_for_guardrails: float | None = None
        if historical_avg and historical_avg > 0:
            historical_team_avg_for_guardrails = historical_avg
        elif expert_avg and expert_avg > 0:
            historical_team_avg_for_guardrails = expert_avg

        # 6b-v6. Reference data resolution (priority order from WS3)
        reference_resale_value: float | None = None
        reference_source = ""
        v6_has_matrix_match = False
        v6_has_sales_stock_match = False

        try:
            # Priority 1: Exact model match in sales stock
            sales_result = lookup_sales_stock(
                brand=req.brand, model=req.model, category=req.category,
            )
            if sales_result and sales_result.get("match_type") == "exact_model":
                reference_resale_value = sales_result["median_selling_price"]
                reference_source = "sales_stock_exact"
                v6_has_sales_stock_match = True
                logger.info(
                    f"v6 ref: sales stock exact match — "
                    f"median resale KES {reference_resale_value:,.0f} "
                    f"({sales_result['count']} records)"
                )

            # Priority 2: Pricing matrix match
            if not reference_resale_value:
                matrix_result = lookup_matrix(
                    brand=req.brand, model=req.model, category=req.category,
                )
                if matrix_result:
                    rec_min = matrix_result.get("recommended_min") or 0
                    rec_max = matrix_result.get("recommended_max") or 0
                    if rec_min > 0 and rec_max > 0:
                        reference_resale_value = (rec_min + rec_max) / 2
                        reference_source = "matrix"
                        v6_has_matrix_match = True
                    elif matrix_result.get("new_price") and matrix_result["new_price"] > 0:
                        reference_resale_value = matrix_result["new_price"]
                        reference_source = "matrix"
                        v6_has_matrix_match = True
                    if reference_resale_value:
                        logger.info(
                            f"v6 ref: matrix match — KES {reference_resale_value:,.0f}"
                        )

            # Priority 3: Brand+category match in sales stock
            if not reference_resale_value and sales_result and sales_result.get("match_type") == "brand_category":
                reference_resale_value = sales_result["median_selling_price"]
                reference_source = "sales_stock_category"
                v6_has_sales_stock_match = True
                logger.info(
                    f"v6 ref: sales stock brand+category — "
                    f"median resale KES {reference_resale_value:,.0f} "
                    f"({sales_result['count']} records)"
                )

            # Priority 4: External research (handled by the engine as fallback)
            if not reference_resale_value:
                logger.info("v6 ref: no GreenBay data match — using external research")

        except Exception as ref_err:
            logger.warning(f"v6 reference data lookup failed: {ref_err}")

        # 6c. Run deterministic offer engine with price verification
        result = compute_valuation(
            category=req.category,
            brand=req.brand,
            model=req.model,
            age_years=req.age_years,
            condition_grade=grade,
            condition_score=req.condition_score,
            defects=defects_dicts,
            seller_asking_price=req.seller_asking_price,
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            comparables=comp_result.comparables,
            pricing_policy=policy_data,
            retail_price=req.retail_price,
            price_verification=price_verification,
            historical_team_avg=historical_team_avg_for_guardrails,
            reference_resale_value=reference_resale_value,
            reference_source=reference_source,
            has_matrix_match=v6_has_matrix_match,
            has_sales_stock_match=v6_has_sales_stock_match,
        )

        # CR-7: Check per-image rejections
        rejected_images_data = None
        try:
            iq_with_rejections = score_images_with_rejections(
                image_urls=req.image_urls,
                image_data=req.image_data,
            )
            if iq_with_rejections.any_rejected:
                rejected_images_data = [
                    {
                        "index": r.index,
                        "reasons": r.reasons,
                        "width": r.width,
                        "height": r.height,
                    }
                    for r in iq_with_rejections.rejections
                    if r.rejected
                ]
                logger.info(f"CR-7: {iq_with_rejections.rejected_count} images rejected")
        except Exception as e:
            logger.warning(f"CR-7 image rejection check failed: {e}")

        # CR-3: Check for duplicate/resubmitted images
        redirect_info = None
        try:
            dup_check = check_duplicates(
                image_data_list=req.image_data,
                current_session_id="",
            )
            if dup_check.is_duplicate:
                result.decision = "redirect_to_agents"
                result.decision_reason = dup_check.message
                redirect_info = {
                    "whatsapp_number": "+254705919099",
                    "message": dup_check.message,
                }
                logger.info(f"CR-3: Duplicate detected, redirecting to agents")
        except Exception as e:
            logger.warning(f"CR-3 duplicate check failed: {e}")

        # Override decision based on rejection pre-screening
        if is_hard_reject:
            reject_reasons = []
            if defect_types & HARD_REJECT_KEYWORDS:
                reject_reasons.append(f"Detected issues: {', '.join(defect_types & HARD_REJECT_KEYWORDS)}")
            if grade_lower in ("d", "poor", "not working"):
                reject_reasons.append(f"Condition grade too low: {req.condition_grade}")
            result.decision = "reject"
            result.decision_reason = "Product does not meet minimum quality standards. " + "; ".join(reject_reasons)
            logger.info(f"Hard reject: {reject_reasons}")
        # CR-3: Smart rejection — age > 2 years + extensive wear = redirect
        elif req.age_years > 2 and grade_lower in ("d", "poor", "not working", "partially working", "working_issues"):
            result.decision = "redirect_to_agents"
            result.decision_reason = (
                f"Your {req.brand} {req.category} is {req.age_years:.0f} years old "
                f"with condition '{req.condition_grade}'. Our team can give you "
                f"a more accurate assessment. Please contact us directly."
            )
            redirect_info = {
                "whatsapp_number": "+254705919099",
                "message": result.decision_reason,
            }
            logger.info(f"CR-3 Smart redirect: age={req.age_years}, grade={grade_lower}")
        elif is_review_flag and result.decision not in ("reject", "redirect_to_agents"):
            review_reasons = []
            if req.age_years > 5:
                review_reasons.append(f"Product age ({req.age_years:.0f} years) exceeds 5-year threshold")
            if grade_lower in ("partially working",):
                review_reasons.append("Product is only partially working")
            result.decision = "review"
            result.decision_reason = "Manual review required. " + "; ".join(review_reasons)
            logger.info(f"Flagged for review: {review_reasons}")

        # 7. Persist valuation session
        # v6: Resolve country + currency
        req_country = getattr(req, "country", "KE") or "KE"
        from greenbay_ai_evaluator.config import get_currency_config
        _cc = get_currency_config(req_country)
        req_currency = _cc["code"]

        vs = ValuationSession(
            trade_in_session_id=req.trade_in_session_id,
            category=req.category,
            brand=req.brand,
            model=req.model,
            age_years=req.age_years,
            condition_grade=grade,
            condition_score=req.condition_score,
            defects=defects_dicts,
            seller_asking_price=req.seller_asking_price,
            seller_name=req.seller_name,
            seller_phone=req.seller_phone,
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            retail_price=req.retail_price,
            estimated_resale_value=result.estimated_resale_value,
            confidence_score=result.confidence_score,
            acquisition_ceiling=result.acquisition_ceiling,
            opening_offer=result.opening_offer,
            walkaway_limit=result.walkaway_limit,
            decision=result.decision,
            decision_reason=result.decision_reason,
            country=req_country,
            currency_code=req_currency,
            size_value=size_value,
            size_unit=size_unit,
            size_source=size_source,
            pricing_policy_snapshot=_policy_snapshot(policy),
            comparable_data={
                "count": comp_result.count,
                "weighted_average": comp_result.weighted_average,
                "confidence": comp_result.confidence,
            },
        )
        db.add(vs)
        db.flush()  # Populate vs.id before creating ledger entries

        # 8. Decision ledger entry
        ledger = DecisionLedger(
            valuation_session_id=vs.id,
            event_type="evaluation_created",
            actor="system",
            data={
                "decision": result.decision,
                "opening_offer": result.opening_offer,
                "ceiling": result.acquisition_ceiling,
                "resale_value": result.estimated_resale_value,
            },
        )
        db.add(ledger)

        # 9. If decision is negotiate or accept, also log offer_made
        if result.decision in ("negotiate", "accept"):
            offer_ledger = DecisionLedger(
                valuation_session_id=vs.id,
                event_type="offer_made",
                actor="system",
                data={
                    "offer_amount": result.opening_offer,
                    "ceiling": result.acquisition_ceiling,
                },
            )
            db.add(offer_ledger)

            # Also persist round 1 (system opening offer)
            round1 = NegotiationRound(
                valuation_session_id=vs.id,
                round_number=1,
                actor="system",
                offer_amount=result.opening_offer,
                ceiling_at_time=result.acquisition_ceiling,
                decision=result.decision,
                reason=result.decision_reason,
            )
            db.add(round1)

        try:
            from app.config import get_settings as _get_s3_settings

            _st = _get_s3_settings()
            s3_configured_for_airtable = bool(
                (_st.aws_s3_bucket or "").strip()
                and (_st.aws_access_key_id or "").strip()
                and (_st.aws_secret_access_key or "").strip()
            )
        except Exception:
            s3_configured_for_airtable = False

        if req.image_data and s3_configured_for_airtable:
            try:
                folder_id = (
                    str(req.trade_in_session_id)
                    if req.trade_in_session_id is not None
                    else str(vs.id)
                )
                eval_s3_keys = _upload_eval_image_data_to_s3(req.image_data, folder_id)
                if req.trade_in_session_id is not None and eval_s3_keys:
                    tis_row = (
                        db.query(TradeInSession)
                        .filter(TradeInSession.id == req.trade_in_session_id)
                        .first()
                    )
                    if tis_row:
                        tis_row.s3_keys = eval_s3_keys
            except Exception as e:
                logger.warning(f"Evaluate: S3 persist for evaluation images failed: {e}")

        db.commit()
        db.refresh(vs)

        logger.info(
            f"Valuation created: session={vs.id}, decision={result.decision}, "
            f"offer={result.opening_offer}"
        )

        # CR-3: Store image hashes for rejected sessions
        try:
            store_session_hashes(
                image_data_list=req.image_data,
                session_id=str(vs.id),
                decision=result.decision,
            )
        except Exception as e:
            logger.warning(f"CR-3 hash storage failed: {e}")

        # Issue #8: enrich the justification with real evidence — Gemini source
        # links, new-price verification status, size used, and the formula.
        enriched_justification = _enrich_justification(
            base_trace=result.pricing_justification,
            price_verification=price_verification,
            internet_result=internet_result,
            size_value=size_value,
            size_unit=size_unit,
            size_source=size_source,
            reference_source=reference_source,
        )

        # Airtable: backup data repository (async, non-blocking, write-only)
        try:
            if not enriched_justification:
                logger.warning("Pricing justification is empty — this should not happen")

            images_for_airtable = _repressign_images_for_airtable(
                req_image_urls=req.image_urls,
                trade_in_session_id=req.trade_in_session_id,
                db=db,
                uploaded_s3_keys=eval_s3_keys if eval_s3_keys else None,
            )
            airtable_payload = _build_airtable_payload(
                vs=vs,
                req=req,
                grade=grade,
                images_for_airtable=images_for_airtable,
                vertex_result=vertex_result,
                had_image_inputs=bool(req.image_data or req.image_urls),
                s3_configured=s3_configured_for_airtable,
                pricing_justification=enriched_justification,
            )
            logger.info(
                f"Airtable payload: justification_len={len(result.pricing_justification)}, "
                f"vertex_price={airtable_payload.get('Vertex AI Price (KES)')}, "
                f"customer_name={airtable_payload.get('Customer Name')!r}"
            )
            airtable_write_async(airtable_payload)
            logger.info("Airtable: write dispatched (fire-and-forget)")
        except Exception as e:
            logger.warning(f"Airtable dispatch failed: {e}")

        # CR-4: Auto-populate Google Sheet (async, non-blocking)
        try:
            import asyncio
            import concurrent.futures

            def _append_sheet():
                from greenbay_ai_evaluator.services.google_sheets_service import append_evaluation_row
                append_evaluation_row(
                    category=req.category,
                    brand=req.brand,
                    model=req.model,
                    condition=req.condition_grade,
                    condition_grade=grade,
                    age_years=req.age_years,
                    retail_price_estimate=req.retail_price,
                    wants_trade_in=True,
                    ai_price=result.opening_offer,
                    ai_confidence=result.confidence_score,
                    customer_asking_price=req.seller_asking_price,
                    status="",
                    notes="",
                    decision=result.decision,
                )

            with concurrent.futures.ThreadPoolExecutor() as pool:
                pool.submit(_append_sheet)  # Fire and forget
            logger.info("CR-4: Google Sheet append queued")
        except Exception as e:
            logger.warning(f"CR-4 Google Sheet append failed: {e}")

        # v6: Build customer message for low-confidence reviews
        customer_message: str | None = None
        if result.decision == "review" and result.confidence_score < 80:
            customer_message = (
                "Thank you for submitting your appliance for evaluation. "
                "Our specialist team is reviewing your item to ensure you "
                "get the best possible offer. We'll be in touch shortly."
            )

        # v6 WS7: Internal WhatsApp notification (every outcome)
        try:
            from greenbay_ai_evaluator.services.internal_notification_service import (
                notify_internal_team,
            )
            _outcome = "NEEDS REVIEW" if result.decision == "review" else result.decision.upper()
            notify_internal_team({
                "outcome": _outcome,
                "product": f"{req.brand} {req.model} {req.category}".strip(),
                "ai_price": result.opening_offer,
                "currency": req_currency,
                "confidence": result.confidence_score,
                "customer_name": req.seller_name or "",
                "customer_phone": req.seller_phone or "",
                "session_id": vs.id,
            })
        except Exception as _notify_err:
            logger.warning(f"v6 internal notification failed: {_notify_err}")

        return EvaluateResponse(
            session_id=vs.id,
            estimated_resale_value=result.estimated_resale_value,
            confidence_score=result.confidence_score,
            acquisition_ceiling=result.acquisition_ceiling,
            opening_offer=result.opening_offer,
            walkaway_limit=result.walkaway_limit,
            decision=result.decision,
            decision_reason=result.decision_reason,
            condition_grade=grade,
            risk_score=risk_result.score,
            comparable_count=comp_result.count,
            pricing_policy_version=(
                str(policy.updated_at) if policy.updated_at else str(policy.created_at)
            ),
            price_verification=price_verification,
            currency_code=req_currency,
            country=req_country,
            customer_message=customer_message,
            rejected_images=rejected_images_data,
            redirect_info=redirect_info,
        )

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        import traceback
        tb = traceback.format_exc()
        logger.opt(raw=True).error(f"Evaluation failed: {tb}\n")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# v6 WS6: POST /{session_id}/accept-offer
# ---------------------------------------------------------------------------
@evaluator_router.post("/{session_id}/accept-offer")
def accept_offer(
    session_id: str,
    db: Session = Depends(get_db),
):
    """Customer accepts the AI offer. Updates DB + Airtable + sends notification."""
    vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
    if not vs:
        raise HTTPException(404, "Valuation session not found")

    vs.decision = "accepted"
    vs.final_decision = "accepted"
    vs.final_offer = vs.opening_offer
    db.commit()

    # Update Airtable record
    try:
        from greenbay_ai_evaluator.services.airtable_service import patch_record_field
        patch_record_field(session_id, "Evaluation Status", "accepted")
    except Exception as e:
        logger.warning(f"Airtable accept-offer patch failed: {e}")

    # Send internal notification
    try:
        from greenbay_ai_evaluator.services.internal_notification_service import notify_internal_team
        currency = vs.currency_code or "KES"
        notify_internal_team({
            "outcome": "ACCEPTED",
            "product": f"{vs.brand} {vs.model} {vs.category}".strip(),
            "ai_price": vs.opening_offer or 0,
            "currency": currency,
            "confidence": vs.confidence_score or 0,
            "customer_name": vs.seller_name or "",
            "customer_phone": vs.seller_phone or "",
            "session_id": session_id,
        })
    except Exception as e:
        logger.warning(f"Accept notification failed: {e}")

    # Issue #13: email the GreenBay team that the price was accepted.
    try:
        from greenbay_ai_evaluator.services.email_service import (
            snapshot_session, send_evaluation_accepted_email,
        )
        send_evaluation_accepted_email(snapshot_session(vs))
    except Exception as e:
        logger.warning(f"Accept email failed: {e}")

    return AcceptOfferResponse(
        session_id=session_id,
        decision="accepted",
        message=(
            "Your offer is accepted. Our team has been notified and will "
            "contact you to arrange collection and payment."
        ),
    )


# ---------------------------------------------------------------------------
# v6 WS8: POST /{session_id}/rejection-choice
# ---------------------------------------------------------------------------
@evaluator_router.post("/{session_id}/rejection-choice")
def rejection_choice(
    session_id: str,
    body: RejectionChoiceRequest,
    db: Session = Depends(get_db),
):
    """Customer rejects and picks an alternative option (A/B/C)."""
    vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
    if not vs:
        raise HTTPException(404, "Valuation session not found")

    option = body.option.upper()
    vs.decision = f"rejected_option_{option}"
    vs.final_decision = f"rejected_option_{option}"

    upfront: float | None = None
    balance: float | None = None
    currency = vs.currency_code or "KES"

    if option == "A":
        message = (
            "Your item will be listed on our marketplace at your desired price. "
            "We'll handle the listing, marketing, and customer inquiries. "
            "Our team will be in touch to arrange collection."
        )
    elif option == "B":
        offer = vs.opening_offer or 0
        upfront = round(offer * 0.10, -2)
        balance = round(offer * 0.90, -2)
        vs.final_offer = upfront
        message = (
            f"Great choice! You'll receive {currency} {upfront:,.0f} upfront today, "
            f"and {currency} {balance:,.0f} when the item sells (within 90 days). "
            f"Our team will contact you to arrange collection."
        )
    else:  # C
        message = (
            "A member of our team will reach out to you on WhatsApp shortly "
            "to discuss your options."
        )

    db.commit()

    # Update Airtable
    try:
        from greenbay_ai_evaluator.services.airtable_service import patch_record_field
        patch_record_field(session_id, "Evaluation Status", f"rejected_option_{option}")
    except Exception as e:
        logger.warning(f"Airtable rejection-choice patch failed: {e}")

    # Internal notification
    try:
        from greenbay_ai_evaluator.services.internal_notification_service import notify_internal_team
        option_labels = {"A": "Consignment", "B": "10/90 Split", "C": "Talk to Team"}
        notify_internal_team({
            "outcome": "REJECTED",
            "product": f"{vs.brand} {vs.model} {vs.category}".strip(),
            "ai_price": vs.opening_offer or 0,
            "currency": currency,
            "confidence": vs.confidence_score or 0,
            "customer_name": vs.seller_name or "",
            "customer_phone": vs.seller_phone or "",
            "session_id": session_id,
            "extra": f"Option {option}: {option_labels.get(option, option)}",
        })
    except Exception as e:
        logger.warning(f"Rejection notification failed: {e}")

    return RejectionChoiceResponse(
        session_id=session_id,
        option=option,
        message=message,
        upfront_amount=upfront,
        balance_amount=balance,
    )


# ---------------------------------------------------------------------------
# POST /upload-video
# ---------------------------------------------------------------------------
# Accept one MP4/MOV/WebM file (<=45 MB) from the frontend, store it in S3
# (7-day presigned GET url), and extract up to 5 evenly-spaced JPEG keyframes
# that the caller can then pass into /evaluate via `image_data`.

_VIDEO_ALLOWED_MIME = {
    "video/mp4",
    "video/quicktime",  # .mov
    "video/webm",
    "video/x-matroska",
    "application/octet-stream",  # some mobile browsers send this for .mov
}
_VIDEO_MAX_BYTES = 45 * 1024 * 1024  # keep below the 50 MB request limit
_VIDEO_KEYFRAME_COUNT = 5


@evaluator_router.post("/upload-video")
async def upload_video(
    video: UploadFile = File(..., description="MP4 / MOV / WebM video, max 45 MB"),
    user_phone: str = Form("", max_length=30),
):
    """Store a short appliance video in S3 and return keyframes for analysis.

    Response:
        {
          "video_s3_key": "videos/254.../....mp4",
          "video_url":    "<7-day presigned URL>",
          "keyframes_base64": ["<b64>", ...],
          "frame_count":  5,
          "duration_seconds": 12.3,
          "note": null | "<why frames are missing>"
        }

    The caller should append `keyframes_base64` to `image_data` when calling
    /evaluate so the vision models can factor the video into the assessment.
    Video storage in S3 is best-effort - if S3 is disabled we still return
    keyframes (from a transient temp copy) so the evaluation is not blocked.
    """
    import mimetypes
    import os as _os
    import tempfile
    import uuid as _uuid

    from app.services.video_service import extract_keyframes, frame_to_base64, video_duration_seconds

    mime = (video.content_type or "").lower()
    ext_guess = mimetypes.guess_extension(mime) or ""
    # Trust the filename extension as a fallback signal (browsers often lie about MIME for .mov)
    original_name = video.filename or ""
    _, file_ext = _os.path.splitext(original_name.lower())
    if not file_ext:
        file_ext = ext_guess or ".mp4"
    if mime and mime not in _VIDEO_ALLOWED_MIME and file_ext not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported video type: {mime or file_ext or 'unknown'}",
        )

    # Read into a temp file, streaming-capped so we don't eat memory.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=file_ext, prefix="gb_vid_")
    total = 0
    try:
        with _os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await video.read(1024 * 1024)  # 1 MB at a time
                if not chunk:
                    break
                total += len(chunk)
                if total > _VIDEO_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Video exceeds max size of {_VIDEO_MAX_BYTES // (1024*1024)} MB",
                    )
                out.write(chunk)
        await video.close()

        # Best-effort: upload original video to S3 (7-day presigned URL)
        video_s3_key: str | None = None
        video_url: str | None = None
        try:
            from app.config import get_settings as _gs
            from app.services.s3_service import s3_client, settings as s3_settings
            _s = _gs()
            if s3_client and _s.aws_s3_bucket:
                safe_phone = "".join(
                    c for c in (user_phone or "anonymous")
                    if c.isalnum() or c in "+-_"
                ) or "anonymous"
                ts = int(datetime.now(timezone.utc).timestamp())
                key = f"videos/{safe_phone}/{ts}_{_uuid.uuid4().hex[:10]}{file_ext}"
                content_type = mime or "video/mp4"
                with open(tmp_path, "rb") as fh:
                    s3_client.put_object(
                        Bucket=_s.aws_s3_bucket,
                        Key=key,
                        Body=fh.read(),
                        ContentType=content_type,
                    )
                try:
                    url = s3_client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": _s.aws_s3_bucket, "Key": key},
                        ExpiresIn=7 * 24 * 3600,
                    )
                    if f".s3.{_s.aws_region}.amazonaws.com" not in url:
                        url = url.replace(
                            ".s3.amazonaws.com",
                            f".s3.{_s.aws_region}.amazonaws.com",
                        )
                    video_url = url
                except Exception as e:
                    logger.warning(f"upload-video: presign failed: {e}")
                video_s3_key = key
                logger.info(f"upload-video: stored {key} ({total} bytes)")
        except Exception as e:
            logger.warning(f"upload-video: S3 store failed: {e}")

        # Extract keyframes (best-effort)
        duration = video_duration_seconds(tmp_path)
        frames = extract_keyframes(tmp_path, count=_VIDEO_KEYFRAME_COUNT)
        note: str | None = None
        if not frames:
            note = (
                "Video stored for human review; automatic keyframe extraction "
                "was unavailable (ffmpeg missing or corrupt input)."
            )

        return {
            "video_s3_key": video_s3_key,
            "video_url": video_url,
            "keyframes_base64": [frame_to_base64(b) for b in frames],
            "frame_count": len(frames),
            "duration_seconds": duration,
            "note": note,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.opt(exception=True).error(f"upload-video failed: {e}")
        raise HTTPException(status_code=500, detail="upload-video failed")
    finally:
        try:
            _os.remove(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /{session_id}/counter
# NEGOTIATION PAUSED -- returning direct price only
# ---------------------------------------------------------------------------
# @evaluator_router.post("/{session_id}/counter", response_model=CounterResponse)
# def counter_offer(session_id: str, req: CounterRequest, db: Session = Depends(get_db)):
#     """Process a seller counter-offer against an existing valuation session."""
#     try:
#         # 1. Load valuation session
#         vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
#         if not vs:
#             raise HTTPException(status_code=404, detail=f"Valuation session '{session_id}' not found.")
#
#         # 2. Check session is not already finalized
#         if vs.decision in ("accept", "decline"):
#             raise HTTPException(
#                 status_code=409,
#                 detail=f"Session already finalized with decision '{vs.decision}'.",
#             )
#
#         # 3. Load pricing policy for negotiation params
#         policy = _load_policy(vs.category, db)
#         policy_data = _policy_to_data(policy)
#
#         # 4. Determine current negotiation state
#         rounds = (
#             db.query(NegotiationRound)
#             .filter(NegotiationRound.valuation_session_id == session_id)
#             .order_by(NegotiationRound.round_number.desc())
#             .all()
#         )
#
#         last_system_offer = vs.opening_offer
#         current_round = 0
#         for r in rounds:
#             if r.actor == "system":
#                 last_system_offer = r.offer_amount
#                 break
#         current_round = len(rounds)
#
#         state = NegotiationState(
#             current_round=current_round,
#             previous_system_offer=last_system_offer,
#             acquisition_ceiling=vs.acquisition_ceiling,
#             walkaway_limit=vs.walkaway_limit,
#         )
#
#         # 5. Record seller's counter as a round
#         seller_round = NegotiationRound(
#             valuation_session_id=session_id,
#             round_number=current_round + 1,
#             actor="seller",
#             offer_amount=req.seller_counter,
#             ceiling_at_time=vs.acquisition_ceiling,
#             decision="counter",
#             reason=f"Seller counter-offer of KES {req.seller_counter:,.0f}",
#         )
#         db.add(seller_round)
#
#         # 6. Decision ledger: counter_received
#         db.add(DecisionLedger(
#             valuation_session_id=session_id,
#             event_type="counter_received",
#             actor="seller",
#             data={"seller_counter": req.seller_counter, "round": current_round + 1},
#         ))
#
#         # 7. Run negotiation engine
#         neg_result = process_counter(
#             seller_counter=req.seller_counter,
#             state=state,
#             max_negotiation_rounds=policy_data.max_negotiation_rounds,
#             round_step_pct=policy_data.round_step_pct,
#         )
#
#         # 8. Record system's response as a round
#         system_round = NegotiationRound(
#             valuation_session_id=session_id,
#             round_number=neg_result.round_number + 1,  # after seller's
#             actor="system",
#             offer_amount=neg_result.system_offer,
#             ceiling_at_time=vs.acquisition_ceiling,
#             decision=neg_result.decision,
#             reason=neg_result.reason,
#         )
#         db.add(system_round)
#
#         # 9. Decision ledger: system response
#         event_type = {
#             "accept": "accepted",
#             "counter": "counter_offered",
#             "decline": "declined",
#         }.get(neg_result.decision, neg_result.decision)
#
#         db.add(DecisionLedger(
#             valuation_session_id=session_id,
#             event_type=event_type,
#             actor="system",
#             data={
#                 "system_offer": neg_result.system_offer,
#                 "seller_counter": req.seller_counter,
#                 "round": neg_result.round_number,
#                 "decision": neg_result.decision,
#             },
#         ))
#
#         # 10. Update session decision if finalized
#         if neg_result.decision in ("accept", "decline"):
#             vs.decision = neg_result.decision
#             vs.decision_reason = neg_result.reason
#
#         db.commit()
#
#         logger.info(
#             f"Counter processed: session={session_id}, "
#             f"seller={req.seller_counter}, decision={neg_result.decision}, "
#             f"system_offer={neg_result.system_offer}"
#         )
#
#         return CounterResponse(
#             round_number=neg_result.round_number,
#             decision=neg_result.decision,
#             system_offer=neg_result.system_offer,
#             ceiling=neg_result.ceiling,
#             reason=neg_result.reason,
#             rounds_remaining=neg_result.rounds_remaining,
#         )
#
#     except HTTPException:
#         raise
#     except Exception as e:
#         db.rollback()
#         logger.error(f"Counter processing failed: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=f"Counter processing failed: {e}")


# ---------------------------------------------------------------------------
# POST /notify-pickup  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
NEWTON_PHONE = "254715284353"  # Newton's WhatsApp number


@evaluator_router.post("/notify-pickup", response_model=PickupNotifyResponse)
def notify_pickup(req: PickupNotifyRequest, db: Session = Depends(get_db)):
    """Save a pickup request and return a WhatsApp deep-link to notify Newton."""
    try:
        pickup = PickupRequest(
            valuation_session_id=req.valuation_session_id,
            seller_name=req.seller_name,
            seller_phone=req.seller_phone,
            appliance_description=req.appliance_description,
            condition_grade=req.condition_grade,
            agreed_price=req.agreed_price,
            pickup_address=req.pickup_address,
            preferred_day=req.preferred_day,
            photo_count=req.photo_count,
            status="pending",
        )
        db.add(pickup)
        db.commit()
        db.refresh(pickup)

        # Build WhatsApp deep-link message
        price_str = f"KES {req.agreed_price:,.0f}" if req.agreed_price else "TBD"
        msg = (
            f"\U0001f7e2 *NEW PICKUP REQUEST #{pickup.id}*\n\n"
            f"\U0001f4e6 *Item:* {req.appliance_description}\n"
            f"\U0001f464 *Seller:* {req.seller_name}\n"
            f"\U0001f4de *Phone:* {req.seller_phone}\n"
            f"\U0001f4b0 *Agreed Price:* {price_str}\n"
            f"\U0001f4cd *Address:* {req.pickup_address}\n"
            f"\U0001f4c5 *Preferred Day:* {req.preferred_day or 'Any'}\n"
            f"\U0001f4f8 *Photos:* {req.photo_count}\n"
            f"\u2b50 *Condition:* {req.condition_grade or 'N/A'}"
        )
        import urllib.parse
        wa_link = f"https://wa.me/{NEWTON_PHONE}?text={urllib.parse.quote(msg)}"

        logger.info(f"Pickup request #{pickup.id} created for {req.seller_name}")

        return PickupNotifyResponse(
            id=pickup.id,
            status="pending",
            whatsapp_link=wa_link,
            message="Pickup request saved. Newton has been notified.",
        )
    except Exception as e:
        logger.error(f"Pickup notification failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /pickup-requests  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/pickup-requests")
def list_pickup_requests(status: str = "pending", db: Session = Depends(get_db)):
    """List pickup requests, filtered by status."""
    q = db.query(PickupRequest)
    if status != "all":
        q = q.filter(PickupRequest.status == status)
    pickups = q.order_by(PickupRequest.created_at.desc()).all()
    return [
        {
            "id": p.id,
            "seller_name": p.seller_name,
            "seller_phone": p.seller_phone,
            "appliance": p.appliance_description,
            "agreed_price": p.agreed_price,
            "pickup_address": p.pickup_address,
            "preferred_day": p.preferred_day,
            "status": p.status,
            "created_at": str(p.created_at) if p.created_at else None,
        }
        for p in pickups
    ]


# ---------------------------------------------------------------------------
# GET /related-products  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
_CATEGORY_MAP = {
    "refrigerator": ["REFRIGERATORS"],
    "fridge": ["REFRIGERATORS"],
    "washing_machine": ["Washing Machine"],
    "washer": ["Washing Machine"],
    "tv": ["TV & Home Entertainment"],
    "television": ["TV & Home Entertainment"],
    "cooker": ["COOKERS"],
    "stove": ["COOKERS"],
    "microwave": ["MICROWAVE"],
    "freezer": ["FREEZER"],
    "chiller": ["CHILLERS"],
    "laptop": ["Computers & Laptops"],
    "computer": ["Computers & Laptops"],
}


@evaluator_router.get("/related-products", response_model=list[RelatedProductOut])
def get_related_products(
    category: str = "",
    limit: int = 6,
    db: Session = Depends(get_db),
):
    """Return related products from our Shopify inventory."""
    results: list[RelatedProductOut] = []

    # Map evaluator category to Shopify product_type
    shopify_types = _CATEGORY_MAP.get(category.lower().strip(), [])

    # Same-category products first
    if shopify_types:
        same_cat = (
            db.query(ShopifyProduct)
            .filter(
                ShopifyProduct.is_active.is_(True),
                ShopifyProduct.available.is_(True),
                ShopifyProduct.product_type.in_(shopify_types),
            )
            .order_by(ShopifyProduct.price.asc())
            .limit(limit)
            .all()
        )
        for p in same_cat:
            results.append(RelatedProductOut(
                title=p.title,
                price=p.price,
                compare_at_price=p.compare_at_price,
                image_url=p.image_url,
                product_url=p.product_url,
                product_type=p.product_type,
                available=p.available,
            ))

    # Fill remaining with popular items from other categories
    remaining = limit - len(results)
    if remaining > 0:
        existing_ids = {r.title for r in results}
        other = (
            db.query(ShopifyProduct)
            .filter(
                ShopifyProduct.is_active.is_(True),
                ShopifyProduct.available.is_(True),
            )
            .order_by(ShopifyProduct.price.asc())
            .limit(remaining + len(results))  # fetch extra to skip dupes
            .all()
        )
        for p in other:
            if p.title not in existing_ids and len(results) < limit:
                results.append(RelatedProductOut(
                    title=p.title,
                    price=p.price,
                    compare_at_price=p.compare_at_price,
                    image_url=p.image_url,
                    product_url=p.product_url,
                    product_type=p.product_type,
                    available=p.available,
                ))

    return results


# ---------------------------------------------------------------------------
# POST /expert-feedback  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.post("/expert-feedback", response_model=ExpertFeedbackResponse)
def submit_expert_feedback(req: ExpertFeedbackRequest, db: Session = Depends(get_db)):
    """Submit expert pricing feedback for a valuation session.

    Used by experienced sales agents during pilot phase to teach the AI.
    """
    try:
        # Look up the valuation session to get product details
        vs = db.query(ValuationSession).filter(
            ValuationSession.id == int(req.valuation_session_id)
        ).first()

        system_price = None
        category = None
        brand = None
        model_name = None
        condition = None
        age = None
        images = None

        if vs:
            system_price = vs.opening_offer
            category = vs.category
            brand = vs.brand
            model_name = vs.model
            condition = vs.condition_grade
            age = vs.age_years
            images = vs.defects  # JSON field that may contain image refs

        price_diff = (req.expert_price - system_price) if system_price else None

        feedback = ExpertPriceFeedback(
            valuation_session_id=req.valuation_session_id,
            expert_name=req.expert_name,
            expert_price=req.expert_price,
            expert_reasoning=req.expert_reasoning,
            product_category=category,
            brand=brand,
            model=model_name,
            condition_grade=condition,
            age_years=age,
            system_price=system_price,
            price_difference=price_diff,
            images_json=images,
            specs_json={
                "category": category,
                "brand": brand,
                "model": model_name,
                "condition": condition,
                "age_years": age,
            },
        )
        db.add(feedback)
        db.commit()
        db.refresh(feedback)

        logger.info(
            f"Expert feedback: session={req.valuation_session_id}, "
            f"expert={req.expert_name}, price={req.expert_price}, "
            f"system={system_price}, diff={price_diff}"
        )

        return ExpertFeedbackResponse(
            id=feedback.id,
            valuation_session_id=req.valuation_session_id,
            expert_name=req.expert_name,
            expert_price=req.expert_price,
            system_price=system_price,
            price_difference=price_diff,
            message=f"Thank you! Your pricing expertise has been recorded and will help improve future valuations.",
        )

    except Exception as e:
        db.rollback()
        logger.error(f"Expert feedback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /expert-feedback/{session_id}  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/expert-feedback/{session_id}")
def get_expert_feedback(session_id: str, db: Session = Depends(get_db)):
    """Retrieve expert feedback for a specific valuation session."""
    feedbacks = db.query(ExpertPriceFeedback).filter(
        ExpertPriceFeedback.valuation_session_id == session_id
    ).all()

    return [
        {
            "id": f.id,
            "expert_name": f.expert_name,
            "expert_price": f.expert_price,
            "expert_reasoning": f.expert_reasoning,
            "system_price": f.system_price,
            "price_difference": f.price_difference,
            "created_at": str(f.created_at),
        }
        for f in feedbacks
    ]


# ---------------------------------------------------------------------------
# GET /inventory-stats  (MUST be above /{session_id} wildcard)
# ---------------------------------------------------------------------------
@evaluator_router.get("/inventory-stats", response_model=InventoryStatsOut)
def get_inventory_stats(db: Session = Depends(get_db)):
    """Return summary of Shopify inventory in our DB."""
    from sqlalchemy import func as sqla_func

    total = db.query(ShopifyProduct).filter(ShopifyProduct.is_active.is_(True)).count()

    # Count by category
    cats = (
        db.query(ShopifyProduct.product_type, sqla_func.count())
        .filter(ShopifyProduct.is_active.is_(True))
        .group_by(ShopifyProduct.product_type)
        .all()
    )
    by_category = {cat or "Unknown": cnt for cat, cnt in cats}

    # Last scrape time
    latest = (
        db.query(sqla_func.max(ShopifyProduct.last_seen_at))
        .filter(ShopifyProduct.is_active.is_(True))
        .scalar()
    )

    return InventoryStatsOut(
        total_active=total,
        by_category=by_category,
        last_scrape=str(latest) if latest else None,
    )


# ---------------------------------------------------------------------------
# GET /{session_id}  — WILDCARD: must be LAST
# ---------------------------------------------------------------------------
@evaluator_router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(session_id: str, db: Session = Depends(get_db)):
    """Retrieve full valuation session with negotiation rounds and ledger."""
    vs = db.query(ValuationSession).filter(ValuationSession.id == session_id).first()
    if not vs:
        raise HTTPException(status_code=404, detail=f"Valuation session '{session_id}' not found.")

    rounds = (
        db.query(NegotiationRound)
        .filter(NegotiationRound.valuation_session_id == session_id)
        .order_by(NegotiationRound.round_number)
        .all()
    )

    ledger = (
        db.query(DecisionLedger)
        .filter(DecisionLedger.valuation_session_id == session_id)
        .order_by(DecisionLedger.created_at)
        .all()
    )

    return SessionDetailResponse(
        session_id=vs.id,
        trade_in_session_id=vs.trade_in_session_id,
        category=vs.category,
        brand=vs.brand,
        model=vs.model,
        age_years=vs.age_years,
        condition_grade=vs.condition_grade,
        condition_score=vs.condition_score,
        defects=vs.defects,
        seller_asking_price=vs.seller_asking_price,
        image_quality_score=vs.image_quality_score,
        risk_score=vs.risk_score,
        retail_price=vs.retail_price,
        estimated_resale_value=vs.estimated_resale_value,
        confidence_score=vs.confidence_score,
        acquisition_ceiling=vs.acquisition_ceiling,
        opening_offer=vs.opening_offer,
        walkaway_limit=vs.walkaway_limit,
        decision=vs.decision,
        decision_reason=vs.decision_reason,
        pricing_policy_snapshot=vs.pricing_policy_snapshot,
        comparable_data=vs.comparable_data,
        created_at=str(vs.created_at) if vs.created_at else None,
        negotiation_rounds=[
            NegotiationRoundOut(
                round_number=r.round_number,
                actor=r.actor,
                offer_amount=r.offer_amount,
                ceiling_at_time=r.ceiling_at_time,
                decision=r.decision,
                reason=r.reason,
                created_at=str(r.created_at) if r.created_at else "",
            )
            for r in rounds
        ],
        decision_ledger=[
            DecisionLedgerOut(
                event_type=entry.event_type,
                actor=entry.actor,
                data=entry.data,
                created_at=str(entry.created_at) if entry.created_at else "",
            )
            for entry in ledger
        ],
    )


# ---------------------------------------------------------------------------
# POST /model-lookup
# ---------------------------------------------------------------------------
from pydantic import BaseModel as PydanticBaseModel


class ModelLookupRequest(PydanticBaseModel):
    image_data: str  # base64 data URL of model label photo
    typed_model: str = ""
    category: str = ""
    brand: str = ""


class ModelLookupResponse(PydanticBaseModel):
    model_verified: bool = False
    model_number: str | None = None
    specs_summary: str | None = None
    retail_price: float | None = None
    release_year: int | None = None


@evaluator_router.post("/model-lookup", response_model=ModelLookupResponse)
def model_lookup(req: ModelLookupRequest):
    """
    Accept a photo of a model label, OCR the model number,
    and look up specs, retail price, and release date.
    """
    try:
        from greenbay_ai_evaluator.services.vision_service import analyze_model_label

        result = analyze_model_label(
            image_data=req.image_data,
            typed_model=req.typed_model,
            category=req.category,
            brand=req.brand,
        )

        logger.info(
            f"Model lookup: typed={req.typed_model}, "
            f"verified={result.get('model_verified')}, "
            f"found={result.get('model_number')}"
        )

        return ModelLookupResponse(
            model_verified=result.get("model_verified", False),
            model_number=result.get("model_number"),
            specs_summary=result.get("specs_summary"),
            retail_price=result.get("retail_price"),
            release_year=result.get("release_year"),
        )

    except Exception as e:
        logger.warning(f"Model lookup failed (non-critical): {e}")
        # Non-critical endpoint: return unverified response
        return ModelLookupResponse(
            model_verified=False,
            model_number=req.typed_model or None,
            specs_summary=None,
        )

