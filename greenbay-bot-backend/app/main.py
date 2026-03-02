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
    
    # Start background payment polling service
    polling_task = asyncio.create_task(payment_polling_service.start_polling_loop())
    logger.info("Payment polling service started")
    
    yield
    
    # Shutdown
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass
    logger.info("Payment polling service stopped")
    logger.info("Shutting down GreenBay Market Chatbot...")


def create_app() -> FastAPI:
    """Create and configure FastAPI application."""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="WhatsApp E-commerce Chatbot for GreenBay Market using LangGraph React Agent",
        lifespan=lifespan,
        debug=settings.debug
    )
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
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
