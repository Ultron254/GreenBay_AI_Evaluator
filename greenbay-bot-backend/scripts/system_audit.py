#!/usr/bin/env python3
"""Non-interactive end-to-end probe of GreenBay integrations.

Run from repo root:
  cd greenbay-bot-backend && python scripts/system_audit.py

Does not print secrets. Exit code 0 if script ran; component status is JSON lines.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Minimal valid JPEG (1×1 px) — hex is more reliable than fragile b64 literals.
_MIN_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010101004800480000ffdb004300"
    "080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a"
    "1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432"
    "ffdb0043010909090c0b0c180d0d1823211c213232323232323232323232323232"
    "323232323232323232323232323232323232323232323232323232323232323232"
    "ffc00011080001000103011100021101031101ffc4001f00000105010101010101"
    "00000000000000000102030405060708090a0bffc400b510000201030303040305"
    "04050706040700010211000321041231410551611322710614324191b1082342a1"
    "b115c152d1f02433627282090a161718191a25262728292a3435363738393a4344"
    "45464748494a535455565758595a636465666768696a737475767778797a838485"
    "868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2"
    "c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8"
    "f9faffda000c03010102110311003f00faa28a280affd9"
)


def _emit(component: str, ok: bool, detail: dict) -> None:
    print(json.dumps({"component": component, "ok": ok, **detail}, default=str))


def _backend_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(_backend_root()))
    os.chdir(str(_backend_root()))

    from app.config import get_settings, invalidate_settings_cache

    invalidate_settings_cache()
    settings = get_settings()

    # --- 1 Anthropic (vision-capable Messages call) ---
    try:
        import httpx

        key = (settings.anthropic_api_key or "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY empty")
        b64 = base64.standard_b64encode(_MIN_JPEG).decode("ascii")
        payload = {
            "model": settings.anthropic_primary_model,
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": 'Reply with exactly one word: "pong".'},
                    ],
                }
            ],
        }
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60.0,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        _emit(
            "anthropic_vision",
            True,
            {"model": settings.anthropic_primary_model, "response_preview": text[:80]},
        )
    except Exception as e:
        _emit("anthropic_vision", False, {"error": str(e)})

    # --- 2 Vertex AI ---
    async def _vertex():
        from greenbay_ai_evaluator.services.vertex_ai_service import vertex_evaluate

        b64 = base64.standard_b64encode(_MIN_JPEG).decode("ascii")
        out = await vertex_evaluate(
            images_base64=[b64],
            category="refrigerator",
            brand="TestBrand",
            age=2.0,
            working_status="good",
            timeout_seconds=45.0,
        )
        return out

    try:
        vout = asyncio.run(_vertex())
        if vout and isinstance(vout, dict):
            _emit(
                "vertex_ai",
                True,
                {
                    "estimated_price_kes": vout.get("estimated_price_kes"),
                    "condition_grade": vout.get("condition_grade"),
                    "latency_ms": vout.get("latency_ms"),
                },
            )
        else:
            _emit("vertex_ai", False, {"error": "vertex_evaluate returned None or invalid"})
    except Exception as e:
        _emit("vertex_ai", False, {"error": str(e)})

    # --- 3 Airtable read / write test record / delete ---
    cfg_token = (settings.airtable_api_token or "").strip()
    cfg_base = (settings.airtable_base_id or "").strip()
    cfg_table = (settings.airtable_table_name or "").strip()
    if not cfg_token or not cfg_base:
        _emit("airtable", False, {"error": "AIRTABLE_API_TOKEN or AIRTABLE_BASE_ID missing"})
    else:
        try:
            import urllib.parse

            import httpx

            tbl = urllib.parse.quote(cfg_table)
            base_url = f"https://api.airtable.com/v0/{cfg_base}/{tbl}"
            headers = {
                "Authorization": f"Bearer {cfg_token}",
                "Content-Type": "application/json",
            }
            rid_delete = None
            with httpx.Client(timeout=30.0) as client:
                lr = client.get(base_url, headers=headers, params={"maxRecords": 1})
                if lr.status_code != 200:
                    raise RuntimeError(f"list HTTP {lr.status_code}: {lr.text[:200]}")
                records = lr.json().get("records") or []
                read_ok = len(records) >= 1

                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                fields = {
                    "Product Name": f"AUDIT_DELETE_ME_{stamp}",
                    "Brand": "Audit",
                    "Category": "test",
                    "AI Evaluated Price (KES)": 1,
                    "Notes": "Automated audit row — safe to delete.",
                }
                wr = client.post(base_url, headers=headers, json={"fields": fields})
                if wr.status_code not in (200, 201):
                    raise RuntimeError(f"create HTTP {wr.status_code}: {wr.text[:200]}")
                rid_delete = (wr.json().get("id") or "").strip()
                if not rid_delete:
                    raise RuntimeError("create missing record id")

                dr = client.delete(f"{base_url}/{rid_delete}", headers=headers)
                delete_ok = dr.status_code == 200

                _emit(
                    "airtable",
                    bool(read_ok and rid_delete and delete_ok),
                    {
                        "read_one_record": read_ok,
                        "wrote_test_id": rid_delete[:8] + "…",
                        "deleted": delete_ok,
                    },
                )
        except Exception as e:
            _emit("airtable", False, {"error": str(e)})

    # --- 4 Google Sheets ---
    try:
        from greenbay_ai_evaluator.services.google_sheets_service import read_historical_data

        rows = read_historical_data()
        sample = None
        for row in rows:
            hp = (row.get("Internal Team Price") or "").strip()
            if hp:
                sample = {
                    "Item": (row.get("Item") or "")[:60],
                    "Internal Team Price": hp[:40],
                    "Date": row.get("Date"),
                }
                break
        _emit(
            "google_sheets",
            True,
            {"row_count": len(rows), "sample_human_price_row": sample},
        )
    except Exception as e:
        _emit("google_sheets", False, {"error": str(e)})

    # --- 5 Pricing learner ---
    try:
        from greenbay_ai_evaluator.services import pricing_learner as pl

        async def _refresh():
            return await pl.refresh_from_sheet()

        n = asyncio.run(_refresh())
        snap = pl.learning_loop_diagnostic_snapshot()
        # one sample key
        keys = list(pl._price_cache.keys())[:3]  # type: ignore[attr-defined]
        sample_key = keys[0] if keys else None
        sample_entry = None
        if sample_key:
            e = pl._price_cache.get(sample_key)  # type: ignore[attr-defined]
            if e:
                sample_entry = {
                    "key": sample_key[:80],
                    "datapoints": e.get("count"),
                    "first_price": (e.get("prices") or [None])[0],
                }
        _emit(
            "pricing_learner",
            True,
            {
                "points_after_refresh": n,
                "snapshot": snap,
                "sample_cache_entry": sample_entry,
            },
        )
    except Exception as e:
        _emit("pricing_learner", False, {"error": str(e)})

    # --- 6 Tavily ---
    try:
        from tavily import TavilyClient

        tk = (settings.tavily_api_key or "").strip()
        if not tk:
            raise RuntimeError("TAVILY_API_KEY empty")
        client = TavilyClient(api_key=tk)
        res = client.search("LG refrigerator Nairobi price KES", max_results=3)
        results = res.get("results") if isinstance(res, dict) else []
        _emit(
            "tavily",
            bool(results),
            {"result_count": len(results), "first_title": (results[0] or {}).get("title") if results else None},
        )
    except Exception as e:
        _emit("tavily", False, {"error": str(e)})

    # --- 7 S3 ---
    try:
        import boto3
        from botocore.exceptions import ClientError

        from app.services.s3_service import _ensure_s3_client

        client = _ensure_s3_client()
        bucket = settings.aws_s3_bucket
        if not client or not bucket:
            raise RuntimeError("S3 client or bucket not configured")
        key = f"audit/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_ping.txt"
        client.put_object(Bucket=bucket, Key=key, Body=b"audit", ContentType="text/plain")
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=60,
        )
        client.delete_object(Bucket=bucket, Key=key)
        _emit("s3", True, {"bucket": bucket, "test_key": key, "presigned_ok": bool(url)})
    except Exception as e:
        _emit("s3", False, {"error": str(e)})

    # --- 8 Database ---
    try:
        from sqlalchemy import func

        from app.database.db import SessionLocal
        from app.database.models import ExpertPriceFeedback
        from greenbay_ai_evaluator.models.evaluator_models import ValuationSession

        d = SessionLocal()
        try:
            vc = d.query(func.count(ValuationSession.id)).scalar() or 0
            ec = d.query(func.count(ExpertPriceFeedback.id)).scalar() or 0
            _emit("database", True, {"valuation_sessions": int(vc), "expert_price_feedback": int(ec)})
        finally:
            d.close()
    except Exception as e:
        _emit("database", False, {"error": str(e)})

    # --- 9 Redis ---
    try:
        import redis as redis_mod

        rc = redis_mod.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        rc.ping()
        # dbsize is total keys in this DB
        nkeys = rc.dbsize()
        _emit("redis", True, {"host": settings.redis_host, "port": settings.redis_port, "key_count": int(nkeys)})
    except Exception as e:
        _emit("redis", False, {"error": str(e)})

    # --- 10 Qdrant ---
    try:
        import httpx

        coll = settings.qdrant_collection_name
        url = f"{settings.qdrant_url.rstrip('/')}/collections/{coll}"
        r = httpx.get(url, timeout=5.0)
        info = {}
        if r.status_code == 200:
            info = r.json().get("result") or {}
        pts = int(info.get("points_count") or info.get("vectors_count") or 0)
        _emit(
            "qdrant",
            r.status_code == 200,
            {
                "http_status": r.status_code,
                "collection": coll,
                "points_count": pts,
            },
        )
    except Exception as e:
        _emit("qdrant", False, {"error": str(e)})

    # --- 12 Dashboard HTTP ---
    try:
        import httpx

        dk = os.environ.get("DASHBOARD_KEY", "greenbay-admin-2026")
        r = httpx.get(
            f"http://127.0.0.1:9100/dashboard/status",
            params={"key": dk},
            timeout=5.0,
        )
        js = r.json() if r.status_code == 200 else None
        _emit(
            "dashboard_status_endpoint",
            r.status_code == 200 and isinstance(js, dict) and "services" in js,
            {"http_status": r.status_code, "has_services_key": isinstance(js, dict) and "services" in js},
        )
    except Exception as e:
        _emit("dashboard_status_endpoint", False, {"error": str(e)})

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
