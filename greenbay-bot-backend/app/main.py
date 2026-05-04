"""Main FastAPI application for GreenBay Market WhatsApp E-commerce Chatbot."""

import os
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import uvicorn
from loguru import logger

# Configure structured file logging with rotation
# SECURITY: diagnose=False prevents leaking local variable values in stack traces
_log_dir = Path(__file__).resolve().parent.parent / "logs"
_log_dir.mkdir(exist_ok=True)
logger.add(
    str(_log_dir / "greenbay.log"),
    rotation="10 MB",
    retention="30 days",
    compression="gz",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
    level="INFO",
    backtrace=True,
    diagnose=False,
)

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    HAS_SLOWAPI = True
except ImportError:
    HAS_SLOWAPI = False
    logger.warning("slowapi not installed, rate limiting disabled")

from app.config import get_settings, invalidate_settings_cache, runtime_s3_bucket_name
from app.database.db import init_db
from app.webhooks.whatsapp import whatsapp_router
# CR-5: M-Pesa backend disabled — uncomment to re-enable
# from app.webhooks.mpesa import mpesa_router
from app.webhooks.shopify import shopify_router
from app.webhooks.flowcart import flowcart_router
from app.monitoring.dashboard import langsmith_monitor
# CR-5: Payment polling disabled (depends on M-Pesa)
# from app.services.payment_polling_service import payment_polling_service
from greenbay_ai_evaluator.api.router import evaluator_router
from greenbay_ai_evaluator.api.chat_router import chat_router


def _run_startup_health_check(settings) -> None:
    """Probe every external dependency at boot and log a tidy banner.

    Pure diagnostics — never raises. Each probe is wrapped individually so
    one failing check cannot mask the rest of the report.
    """
    logger.info("=" * 60)
    logger.info("STARTUP HEALTH CHECK")
    logger.info("=" * 60)

    # Database
    try:
        from app.database.db import SessionLocal
        from sqlalchemy import text
        d = SessionLocal()
        try:
            d.execute(text("SELECT 1"))
            logger.info("[OK]   Database: connected")
        finally:
            d.close()
    except Exception as e:
        logger.error(f"[FAIL] Database: {e}")

    # Redis
    try:
        import redis as _redis
        rc = _redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        rc.ping()
        logger.info(f"[OK]   Redis: connected ({settings.redis_host}:{settings.redis_port})")
    except Exception as e:
        logger.warning(f"[WARN] Redis: {e}")

    # Anthropic
    if settings.anthropic_api_key:
        logger.info(
            f"[OK]   Anthropic: key configured (model: {settings.anthropic_primary_model})"
        )
    else:
        logger.error("[FAIL] Anthropic: API key not configured — vision analysis will fail")

    # Airtable
    if settings.airtable_api_token and settings.airtable_base_id:
        logger.info(
            f"[OK]   Airtable: token configured (base: {settings.airtable_base_id})"
        )
    else:
        logger.warning("[WARN] Airtable: not configured — data repository disabled")

    # Vertex AI
    vertex_creds = settings.google_vertex_credentials_file or ""
    if vertex_creds and Path(vertex_creds).exists():
        logger.info(
            f"[OK]   Vertex AI: credentials found (project: {settings.google_vertex_project})"
        )
    else:
        logger.warning(
            f"[WARN] Vertex AI: credentials not found at {vertex_creds or '<unset>'}"
        )

    # Google Sheets
    sheets_creds = settings.google_sheets_credentials_file or ""
    if sheets_creds and Path(sheets_creds).exists():
        logger.info("[OK]   Google Sheets: credentials found")
    elif vertex_creds and Path(vertex_creds).exists():
        logger.info("[OK]   Google Sheets: using Vertex AI credentials as fallback")
    else:
        logger.warning("[WARN] Google Sheets: no credentials found")

    # Tavily
    if settings.tavily_api_key:
        logger.info("[OK]   Tavily: API key configured")
    else:
        logger.warning("[WARN] Tavily: not configured — internet price lookup disabled")

    # AWS S3 — use runtime resolution so the banner matches Docker env, not a
    # stale singleton from an early import.
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        logger.info(
            f"[OK]   AWS S3: configured (bucket: {runtime_s3_bucket_name()})"
        )
    else:
        logger.warning("[WARN] AWS S3: not configured — image storage disabled")

    # Flowcart
    if settings.flowcart_webhook_secret:
        logger.info("[OK]   Flowcart: webhook secret configured")
    else:
        logger.warning("[WARN] Flowcart: webhook secret not configured")

    # Airtable fallback replay
    try:
        from greenbay_ai_evaluator.services.airtable_service import retry_failed_writes
        recovered = retry_failed_writes()
        if recovered > 0:
            logger.info(f"[OK]   Airtable recovery: {recovered} pending records recovered")
        else:
            logger.info("[OK]   Airtable recovery: no pending records")
    except Exception as e:
        logger.warning(f"[WARN] Airtable recovery: {e}")

    logger.info("=" * 60)
    logger.info("STARTUP HEALTH CHECK COMPLETE")
    logger.info("=" * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting GreenBay Market Chatbot...")

    # Drop any settings snapshot from import-time so env matches runtime (S3 bucket, etc.).
    invalidate_settings_cache()
    try:
        import app.services.s3_service as _s3mod

        _s3mod.s3_client = None
        _s3mod._s3_client_bucket = None
    except Exception:
        pass

    # Initialize LangSmith tracing
    settings = get_settings()
    if settings.langsmith_api_key and settings.langsmith_tracing:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_ENDPOINT"] = settings.langsmith_endpoint
        os.environ["LANGCHAIN_API_KEY"] = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        logger.info("LangSmith tracing initialized")
    else:
        logger.info("LangSmith tracing disabled")
    
    await init_db()
    logger.info("Database initialized successfully")

    # ------------------------------------------------------------------
    # Startup health check — purely diagnostic, never raises.
    # ------------------------------------------------------------------
    try:
        _run_startup_health_check(settings)
    except Exception as e:
        logger.warning(f"Startup health check encountered an error: {e}")

    # CR-5: M-Pesa payment polling disabled — uncomment to re-enable
    # async def _resilient_polling():
    #     """Wrapper that restarts the polling loop if it crashes."""
    #     while True:
    #         try:
    #             await payment_polling_service.start_polling_loop()
    #         except asyncio.CancelledError:
    #             raise  # propagate cancellation
    #         except Exception as e:
    #             logger.error(f"Payment polling crashed, restarting in 5s: {e}")
    #             await asyncio.sleep(5)
    #
    # polling_task = asyncio.create_task(_resilient_polling())
    # logger.info("Payment polling service started (with auto-restart)")
    logger.info("M-Pesa payment polling DISABLED (CR-5)")

    # Start background Shopify inventory scraper (runs on startup + every 12 hours)
    async def _shopify_scraper_loop():
        """Scrape greenbay.market inventory into DB every 12 hours."""
        from greenbay_ai_evaluator.services.shopify_scraper import scrape_full_inventory_async
        while True:
            try:
                logger.info("Starting Shopify inventory scrape...")
                result = await scrape_full_inventory_async()
                logger.info(f"Shopify scrape complete: {result}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Shopify scraper error: {e}")
            await asyncio.sleep(43200)  # 12 hours

    scraper_task = asyncio.create_task(_shopify_scraper_loop())
    logger.info("Shopify inventory scraper started (12-hour cycle)")

    # Start pricing learner: initial load + hourly refresh from Google Sheet.
    # Uses the module's existing start_refresh_loop() (no wrapper needed).
    from greenbay_ai_evaluator.services.pricing_learner import (
        start_refresh_loop as pricing_learner_loop,
    )
    learner_task = asyncio.create_task(pricing_learner_loop())
    logger.info("Pricing learner started (initial load + 1-hour refresh)")

    # Sheet → Airtable: human evaluator prices (J/K) into In-House Evaluator Price (30 min).
    async def _sheet_human_price_sync_loop():
        from greenbay_ai_evaluator.services.sheet_to_airtable_human_price_sync import (
            sync_human_evaluator_prices_from_sheet,
        )

        while True:
            try:
                await sync_human_evaluator_prices_from_sheet()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Sheet→Airtable human price sync loop error: {e}")
            await asyncio.sleep(1800)  # 30 minutes

    human_price_sync_task = asyncio.create_task(_sheet_human_price_sync_loop())
    logger.info(
        "Sheet→Airtable human price sync started (30-minute cycle; Sheet → Airtable only)"
    )

    yield

    # Shutdown — cancel any background tasks we started above.
    # Note: M-Pesa polling task is disabled (CR-5), so it's NOT cancelled here.
    for task_name, task in (
        ("Shopify scraper", scraper_task),
        ("Pricing learner", learner_task),
        ("Human price Sheet→Airtable sync", human_price_sync_task),
    ):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"{task_name} shutdown error: {e}")
        logger.info(f"{task_name} stopped")
    logger.info("Shutting down GreenBay Market Chatbot...")


def create_app() -> FastAPI:
    """Create and configure FastAPI application with security hardening."""
    settings = get_settings()

    # SECURITY: Disable interactive docs in production (OWASP API Security)
    docs_url = "/docs" if settings.debug else None
    redoc_url = "/redoc" if settings.debug else None

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="WhatsApp E-commerce Chatbot for GreenBay Market using LangGraph React Agent",
        lifespan=lifespan,
        debug=False,  # SECURITY: Never run debug=True in production
        docs_url=docs_url,
        redoc_url=redoc_url,
    )

    # -----------------------------------------------------------------
    # CORS middleware — SECURITY: restrict methods and headers (OWASP)
    # -----------------------------------------------------------------
    allowed_origins = [
        "http://localhost:9100",
        "http://127.0.0.1:9100",
        "https://greenbay.market",
        "https://www.greenbay.market",
        "http://3.217.166.244",
        "https://3.217.166.244",
    ]
    # Allow custom origins from environment
    extra = os.environ.get("CORS_ORIGINS", "")
    if extra:
        allowed_origins.extend([o.strip() for o in extra.split(",") if o.strip()])

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],  # Only methods we actually use
        allow_headers=["Content-Type", "Authorization", "Accept", "X-Requested-With"],
    )

    # -----------------------------------------------------------------
    # Rate limiting middleware with graceful 429 responses
    # -----------------------------------------------------------------
    if HAS_SLOWAPI:
        limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
        app.state.limiter = limiter

        # Custom 429 handler with Retry-After header
        async def _custom_rate_limit_handler(request: Request, exc: RateLimitExceeded):
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "message": "Too many requests. Please slow down.",
                    "retry_after": 60,
                },
                headers={"Retry-After": "60"},
            )

        app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)
        logger.info("Rate limiting enabled: 60 requests/minute per IP (default)")
    else:
        logger.warning("Rate limiting disabled (install slowapi to enable)")
    
    # Include routers
    app.include_router(whatsapp_router, tags=["WhatsApp"])
    # CR-5: M-Pesa router disabled — uncomment to re-enable
    # app.include_router(mpesa_router, tags=["M-Pesa"])
    app.include_router(shopify_router, tags=["Shopify"])
    app.include_router(flowcart_router, prefix="/webhook", tags=["Flowcart WhatsApp"])
    app.include_router(evaluator_router, prefix="/tradein", tags=["Trade-In Evaluator"])
    app.include_router(chat_router, prefix="/tradein", tags=["Trade-In Chat"])
    
    # Mount frontend static files
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.is_dir():
        app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
        logger.info(f"Frontend mounted at /app from {frontend_dir}")
    else:
        logger.warning(f"Frontend directory not found: {frontend_dir}")

    # -----------------------------------------------------------------
    # SECURITY HEADERS MIDDLEWARE (OWASP best practices)
    # -----------------------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Add OWASP-recommended security headers to all responses."""
        # SECURITY: Reject oversized request bodies (50MB limit for image uploads)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 50 * 1024 * 1024:
            return JSONResponse(
                status_code=413,
                content={"error": "Request body too large", "max_size_mb": 50},
            )

        response = await call_next(request)

        # OWASP security headers on ALL responses
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # HSTS — tell browsers to always use HTTPS
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

        # No-cache for frontend files
        if request.url.path.startswith("/app/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response
    
    @app.get("/")
    async def root():
        """Root endpoint."""
        return {
            "message": "GreenBay Market WhatsApp E-commerce Chatbot",
            "version": settings.app_version,
            "status": "running"
        }
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {
            "status": "healthy",
            "service": settings.app_name,
            "version": settings.app_version
        }
    
    @app.get("/monitoring/metrics")
    async def get_metrics(hours: int = 24):
        """Get LangSmith monitoring metrics."""
        return langsmith_monitor.get_conversation_metrics(hours)
    
    @app.get("/monitoring/tools")
    async def get_tool_performance(hours: int = 24):
        """Get tool performance metrics."""
        return langsmith_monitor.get_tool_performance(hours)
    
    @app.get("/monitoring/conversations")
    async def get_conversation_flow(hours: int = 24):
        """Get conversation flow analytics."""
        return langsmith_monitor.get_conversation_flow(hours)
    
    @app.get("/monitoring/health")
    async def monitoring_health():
        """Check LangSmith monitoring health."""
        return langsmith_monitor.get_health_status()

    # -----------------------------------------------------------------
    # Hidden Ops Dashboard (/app/dashboard.html?key=...)
    # -----------------------------------------------------------------
    _DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "greenbay-admin-2026")

    def _require_dashboard_key(
        key: str = Query("", alias="key", max_length=120),
    ) -> str:
        """Simple query-param gate. Not high security — prevents casual access."""
        if not key or key != _DASHBOARD_KEY:
            raise HTTPException(status_code=403, detail="forbidden")
        return key

    @app.get("/dashboard/status")
    async def dashboard_status(_: str = Depends(_require_dashboard_key)):
        """Aggregate status for every subsystem. Never raises per-subsystem errors."""
        from app.config import get_settings as _gs
        s = _gs()

        # Anthropic
        anthropic = {
            "status": "online" if s.anthropic_api_key else "disabled",
            "configured": bool(s.anthropic_api_key),
            "model": s.anthropic_primary_model,
        }
        # Vertex AI
        try:
            from greenbay_ai_evaluator.services.vertex_ai_service import (
                get_service_status as vertex_status,
            )
            vertex = vertex_status()
        except Exception as e:
            vertex = {"status": "offline", "error": str(e)}
        # Airtable
        try:
            from greenbay_ai_evaluator.services.airtable_service import (
                get_service_status as airtable_status,
            )
            airtable = airtable_status()
        except Exception as e:
            airtable = {"status": "offline", "error": str(e)}
        # Google Sheets
        sheets_creds_file = s.google_sheets_credentials_file or ""
        vertex_creds_file = s.google_vertex_credentials_file or ""
        sheets_source = None
        if sheets_creds_file and Path(sheets_creds_file).exists():
            sheets_source = "GOOGLE_SHEETS_CREDENTIALS_FILE"
            sheets_status = "online"
        elif vertex_creds_file and Path(vertex_creds_file).exists():
            sheets_source = "GOOGLE_VERTEX_CREDENTIALS_FILE (fallback)"
            sheets_status = "online"
        else:
            sheets_source = None
            sheets_status = "disabled"
        sheets = {
            "status": sheets_status,
            "credentials_source": sheets_source,
            "sheet_id": s.google_sheets_id,
        }
        # Tavily
        tavily = {
            "status": "online" if s.tavily_api_key else "disabled",
            "configured": bool(s.tavily_api_key),
        }
        # S3
        from app.config import runtime_s3_bucket_name as _s3_bucket_resolved

        s3_info: dict[str, Any] = {
            "status": "disabled",
            "configured": False,
            "bucket": _s3_bucket_resolved() or None,
            "region": s.aws_region,
        }
        if s.aws_access_key_id and s.aws_secret_access_key and s.aws_s3_bucket:
            s3_info["status"] = "online"
            s3_info["configured"] = True
        # Database
        db_info: dict[str, Any] = {"status": "offline"}
        try:
            from app.database.db import SessionLocal
            from sqlalchemy import text
            from greenbay_ai_evaluator.models.evaluator_models import ValuationSession

            d = SessionLocal()
            try:
                d.execute(text("SELECT 1"))
                valuation_count = d.query(ValuationSession).count()
                db_info = {
                    "status": "online",
                    "dialect": d.bind.dialect.name if d.bind else "unknown",
                    "valuation_count": valuation_count,
                }
            finally:
                d.close()
        except Exception as e:
            db_info = {"status": "offline", "error": str(e)}
        # Redis
        redis_info: dict[str, Any] = {"host": s.redis_host, "status": "offline"}
        try:
            import redis as _redis
            rc = _redis.Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=s.redis_db,
                password=s.redis_password,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            rc.ping()
            redis_info["status"] = "online"
        except Exception as e:
            redis_info["status"] = "offline"
            redis_info["error"] = str(e)
        # Qdrant
        qdrant_info: dict[str, Any] = {"url": s.qdrant_url, "status": "offline"}
        try:
            import httpx as _httpx
            resp = _httpx.get(s.qdrant_url, timeout=3)
            qdrant_info["status"] = "online" if resp.status_code < 500 else "degraded"
        except Exception as e:
            qdrant_info["status"] = "offline"
            qdrant_info["error"] = str(e)
        # Flowcart
        flowcart = {
            "status": "online" if s.flowcart_webhook_secret else "disabled",
            "configured": bool(s.flowcart_webhook_secret),
        }

        return {
            "app": s.app_name,
            "version": s.app_version,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "services": {
                "anthropic": anthropic,
                "vertex": vertex,
                "airtable": airtable,
                "sheets": sheets,
                "tavily": tavily,
                "s3": s3_info,
                "database": db_info,
                "redis": redis_info,
                "qdrant": qdrant_info,
                "flowcart": flowcart,
            },
        }

    @app.get("/dashboard/resources")
    async def dashboard_resources(_: str = Depends(_require_dashboard_key)):
        """Resource usage metrics for the ops dashboard.

        Returns CPU, memory, disk, Redis memory, Qdrant vectors, database
        size, Docker/process uptime, S3 storage usage, and counters for AI
        and Airtable API calls. Each numeric metric is returned as both a
        raw value and a 0-100 percent where applicable, plus a human-friendly
        display string. Never raises - individual probe failures land as
        `null` fields with an `error` key.
        """
        from app.config import get_settings as _gs
        s = _gs()

        def _fmt_bytes(n: Optional[int]) -> str:
            if n is None:
                return "—"
            units = ["B", "KB", "MB", "GB", "TB", "PB"]
            size = float(n)
            u = 0
            while size >= 1024 and u < len(units) - 1:
                size /= 1024
                u += 1
            return f"{size:.1f} {units[u]}" if u else f"{int(size)} {units[u]}"

        def _fmt_duration(seconds: Optional[float]) -> str:
            if seconds is None:
                return "—"
            s_int = int(seconds)
            d, rem = divmod(s_int, 86400)
            h, rem = divmod(rem, 3600)
            m, sec = divmod(rem, 60)
            if d:
                return f"{d}d {h}h"
            if h:
                return f"{h}h {m}m"
            if m:
                return f"{m}m {sec}s"
            return f"{sec}s"

        # ---- System (container) ----
        cpu_info: dict[str, Any] = {}
        memory_info: dict[str, Any] = {}
        disk_info: dict[str, Any] = {}
        uptime_info: dict[str, Any] = {}
        try:
            import psutil
            # cpu_percent with a short interval gives a meaningful sample.
            cpu_pct = psutil.cpu_percent(interval=0.25)
            cpu_info = {
                "percent": round(cpu_pct, 1),
                "cores": psutil.cpu_count(logical=True),
                "display": f"{cpu_pct:.1f}%",
            }
            vm = psutil.virtual_memory()
            memory_info = {
                "percent": round(vm.percent, 1),
                "used_bytes": int(vm.used),
                "total_bytes": int(vm.total),
                "display": f"{_fmt_bytes(vm.used)} / {_fmt_bytes(vm.total)}",
            }
            du = psutil.disk_usage("/app")
            disk_info = {
                "percent": round(du.percent, 1),
                "used_bytes": int(du.used),
                "total_bytes": int(du.total),
                "display": f"{_fmt_bytes(du.used)} / {_fmt_bytes(du.total)}",
            }
            # Process uptime from our own process
            try:
                p = psutil.Process(os.getpid())
                proc_uptime_s = (
                    datetime.now(timezone.utc).timestamp() - p.create_time()
                )
                uptime_info = {
                    "seconds": int(proc_uptime_s),
                    "display": _fmt_duration(proc_uptime_s),
                    "started_at": datetime.fromtimestamp(
                        p.create_time(), timezone.utc
                    ).isoformat(),
                }
            except Exception:
                uptime_info = {"seconds": None, "display": "—"}
        except Exception as e:
            cpu_info = {"error": str(e), "display": "unavailable"}
            memory_info = {"error": str(e), "display": "unavailable"}
            disk_info = {"error": str(e), "display": "unavailable"}

        # ---- Database size ----
        db_size: dict[str, Any] = {}
        try:
            from app.database.db import SessionLocal
            from sqlalchemy import text
            d = SessionLocal()
            try:
                dialect = d.bind.dialect.name if d.bind else ""
                if dialect == "postgresql":
                    row = d.execute(
                        text("SELECT pg_database_size(current_database())")
                    ).scalar()
                    db_size = {
                        "bytes": int(row or 0),
                        "display": _fmt_bytes(int(row or 0)),
                    }
                else:
                    db_size = {"bytes": None, "display": f"n/a ({dialect})"}
            finally:
                d.close()
        except Exception as e:
            db_size = {"error": str(e), "display": "unavailable"}

        # ---- Redis memory ----
        redis_mem: dict[str, Any] = {}
        try:
            import redis as _redis
            rc = _redis.Redis(
                host=s.redis_host,
                port=s.redis_port,
                db=s.redis_db,
                password=s.redis_password,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            info = rc.info(section="memory")
            used = int(info.get("used_memory", 0) or 0)
            max_mem = int(info.get("maxmemory", 0) or 0) or None
            redis_mem = {
                "used_bytes": used,
                "max_bytes": max_mem,
                "percent": round(100 * used / max_mem, 1) if max_mem else None,
                "display": (
                    f"{_fmt_bytes(used)} / {_fmt_bytes(max_mem)}"
                    if max_mem else _fmt_bytes(used)
                ),
            }
        except Exception as e:
            redis_mem = {"error": str(e), "display": "unavailable"}

        # ---- Qdrant vectors ----
        qdrant_stats: dict[str, Any] = {}
        try:
            import httpx as _httpx
            coll = s.qdrant_collection_name
            resp = _httpx.get(f"{s.qdrant_url}/collections/{coll}", timeout=3)
            if resp.status_code == 200:
                result = resp.json().get("result", {}) or {}
                pts = int(result.get("points_count") or result.get("vectors_count") or 0)
                qdrant_stats = {
                    "collection": coll,
                    "points": pts,
                    "display": f"{pts:,} vectors",
                }
            else:
                qdrant_stats = {
                    "collection": coll,
                    "display": f"http {resp.status_code}",
                }
        except Exception as e:
            qdrant_stats = {"error": str(e), "display": "unavailable"}

        # ---- S3 storage (bucket-wide list; lazy-init client first) ----
        s3_usage: dict[str, Any] = {}
        from app.config import runtime_s3_bucket_name as _bucket_rt

        bucket_rt = _bucket_rt()
        if s.aws_access_key_id and s.aws_secret_access_key and bucket_rt:
            try:
                from app.services.s3_service import _ensure_s3_client

                client = _ensure_s3_client()
                if client:
                    total_bytes = 0
                    total_objects = 0
                    video_bytes = 0
                    video_objects = 0
                    paginator = client.get_paginator("list_objects_v2")
                    for page in paginator.paginate(
                        Bucket=bucket_rt, PaginationConfig={"MaxItems": 5000}
                    ):
                        for obj in page.get("Contents", []) or []:
                            size = int(obj.get("Size") or 0)
                            total_bytes += size
                            total_objects += 1
                            key = str(obj.get("Key") or "")
                            if key.startswith("videos/"):
                                video_bytes += size
                                video_objects += 1
                    s3_usage = {
                        "bucket": bucket_rt,
                        "total_bytes": total_bytes,
                        "total_objects": total_objects,
                        "videos_bytes": video_bytes,
                        "videos_objects": video_objects,
                        "display": f"{_fmt_bytes(total_bytes)} ({total_objects:,} objects)",
                    }
                else:
                    s3_usage = {"display": "client not initialised"}
            except Exception as e:
                s3_usage = {"error": str(e), "display": "unavailable"}
        else:
            s3_usage = {"display": "disabled (no credentials)"}

        # ---- API call counters (from module state) ----
        api_counters: dict[str, Any] = {}
        try:
            from greenbay_ai_evaluator.services.vertex_ai_service import (
                get_service_status as vertex_status,
            )
            vstate = vertex_status()
            api_counters["vertex"] = {
                "calls_today": vstate.get("calls_today", 0),
                "total_calls": vstate.get("total_calls", 0),
                "total_failures": vstate.get("total_failures", 0),
                "last_success_at": vstate.get("last_success_at"),
                "display": f"{vstate.get('calls_today', 0)} today · {vstate.get('total_calls', 0)} total",
            }
        except Exception as e:
            api_counters["vertex"] = {"error": str(e)}
        try:
            from greenbay_ai_evaluator.services.airtable_service import (
                get_service_status as at_status,
            )
            astate = at_status()
            api_counters["airtable"] = {
                "writes_today": astate.get("writes_today", 0),
                "total_writes": astate.get("total_writes", 0),
                "total_failures": astate.get("total_failures", 0),
                "pending_fallback": astate.get("pending_fallback_records", 0),
                "display": (
                    f"{astate.get('writes_today', 0)} writes today · "
                    f"{astate.get('total_writes', 0)} total"
                ),
            }
        except Exception as e:
            api_counters["airtable"] = {"error": str(e)}

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "cpu": cpu_info,
            "memory": memory_info,
            "disk": disk_info,
            "uptime": uptime_info,
            "database": db_size,
            "redis": redis_mem,
            "qdrant": qdrant_stats,
            "s3": s3_usage,
            "api_counters": api_counters,
        }

    @app.get("/dashboard/recent")
    async def dashboard_recent(
        _: str = Depends(_require_dashboard_key),
        limit: int = Query(20, ge=1, le=100),
    ):
        """Return the last N valuation sessions for the ops dashboard."""
        try:
            from app.database.db import SessionLocal
            from greenbay_ai_evaluator.models.evaluator_models import ValuationSession

            d = SessionLocal()
            try:
                rows = (
                    d.query(ValuationSession)
                    .order_by(ValuationSession.created_at.desc())
                    .limit(limit)
                    .all()
                )
                sessions = [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "brand": r.brand,
                        "model": r.model,
                        "category": r.category,
                        "decision": r.decision,
                        "opening_offer": r.opening_offer,
                        "confidence_score": r.confidence_score,
                    }
                    for r in rows
                ]
                return {"sessions": sessions}
            finally:
                d.close()
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": "recent_query_failed", "message": str(e)},
            )

    @app.get("/dashboard/analytics")
    async def dashboard_analytics(_: str = Depends(_require_dashboard_key)):
        """Aggregate stats over the last 90 days."""
        try:
            from app.database.db import SessionLocal
            from greenbay_ai_evaluator.models.evaluator_models import ValuationSession
            from sqlalchemy import func as _func

            since = datetime.now(timezone.utc) - timedelta(days=90)

            d = SessionLocal()
            try:
                base = d.query(ValuationSession).filter(
                    ValuationSession.created_at >= since
                )
                total = base.count()

                mean_conf = (
                    d.query(_func.avg(ValuationSession.confidence_score))
                    .filter(ValuationSession.created_at >= since)
                    .scalar()
                    or 0
                )
                mean_offer = (
                    d.query(_func.avg(ValuationSession.opening_offer))
                    .filter(ValuationSession.created_at >= since)
                    .scalar()
                    or 0
                )

                today_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                evaluations_today = (
                    d.query(ValuationSession)
                    .filter(ValuationSession.created_at >= today_start)
                    .count()
                )

                def _count_by(col):
                    return (
                        d.query(col, _func.count(ValuationSession.id))
                        .filter(ValuationSession.created_at >= since)
                        .group_by(col)
                        .order_by(_func.count(ValuationSession.id).desc())
                        .limit(10)
                        .all()
                    )

                decisions = [
                    {"label": (row[0] or "—"), "count": int(row[1])}
                    for row in _count_by(ValuationSession.decision)
                ]
                brands = [
                    {"label": (row[0] or "—"), "count": int(row[1])}
                    for row in _count_by(ValuationSession.brand)
                ][:5]
                categories = [
                    {"label": (row[0] or "—"), "count": int(row[1])}
                    for row in _count_by(ValuationSession.category)
                ][:5]

                return {
                    "total_evaluations": total,
                    "mean_confidence": float(mean_conf or 0),
                    "mean_offer": float(mean_offer or 0),
                    "evaluations_today": evaluations_today,
                    "decisions": decisions,
                    "top_brands": brands,
                    "top_categories": categories,
                }
            finally:
                d.close()
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": "analytics_query_failed", "message": str(e)},
            )

    @app.get("/dashboard/learning-diagnostic")
    async def dashboard_learning_diagnostic(
        _: str = Depends(_require_dashboard_key),
        brand: str = Query("LG", max_length=120),
        category: str = Query("refrigerator", max_length=120),
    ):
        """Prove the Sheets pricing-learner cache is loaded and sample Airtable history.

        ``mean_human_to_ai_price_ratio`` is mean(In-House / AI) on Airtable rows
        with both prices; ``None`` if there are fewer than three such rows.
        """
        from greenbay_ai_evaluator.services.airtable_service import (
            get_historical_accuracy_ratio,
            read_comparables,
        )
        from greenbay_ai_evaluator.services.pricing_learner import (
            learning_loop_diagnostic_snapshot,
        )

        b = brand.strip()
        c = category.strip()
        comps = read_comparables(brand=b, category=c, limit=25)
        ratio = get_historical_accuracy_ratio(brand=b, category=c)

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "google_sheets_pricing_learner": learning_loop_diagnostic_snapshot(),
            "airtable_sample": {
                "brand": b,
                "category": c,
                "records_with_human_price": len(comps),
                "mean_human_to_ai_price_ratio": ratio,
                "notes": (
                    "Airtable rows matched by exact Brand + Category with "
                    "In-House Evaluator Price > 0. Ratio mean(human/ai); "
                    "needs ≥3 pairs."
                ),
            },
            "logs": (
                "Each POST /evaluate emits one INFO line prefixed EVAL_PRICE_TRACE "
                "(Sheets learner in/out reconciled retail, DB comps, Tavily, Shopify, "
                "expert, Vertex advisory)."
            ),
        }

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Global exception handler."""
        logger.error(f"Unhandled exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "message": "An unexpected error occurred"
            }
        )
    
    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=9100,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )
