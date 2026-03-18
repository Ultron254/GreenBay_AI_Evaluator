"""Main FastAPI application for GreenBay Market WhatsApp E-commerce Chatbot."""

import os
import asyncio
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
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

from app.config import get_settings
from app.database.db import init_db
from app.webhooks.whatsapp import whatsapp_router
from app.webhooks.mpesa import mpesa_router
from app.webhooks.shopify import shopify_router
from app.webhooks.flowcart import flowcart_router
from app.monitoring.dashboard import langsmith_monitor
from app.services.payment_polling_service import payment_polling_service
from greenbay_ai_evaluator.api.router import evaluator_router
from greenbay_ai_evaluator.api.chat_router import chat_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting GreenBay Market Chatbot...")
    
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
    
    # Start background payment polling service with auto-restart
    async def _resilient_polling():
        """Wrapper that restarts the polling loop if it crashes."""
        while True:
            try:
                await payment_polling_service.start_polling_loop()
            except asyncio.CancelledError:
                raise  # propagate cancellation
            except Exception as e:
                logger.error(f"Payment polling crashed, restarting in 5s: {e}")
                await asyncio.sleep(5)

    polling_task = asyncio.create_task(_resilient_polling())
    logger.info("Payment polling service started (with auto-restart)")

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
    
    yield
    
    # Shutdown
    polling_task.cancel()
    scraper_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    try:
        await scraper_task
    except asyncio.CancelledError:
        pass
    logger.info("Payment polling service stopped")
    logger.info("Shopify scraper stopped")
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
    app.include_router(mpesa_router, tags=["M-Pesa"])
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
