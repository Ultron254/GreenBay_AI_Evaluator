"""Configuration management for GreenBay Market Chatbot.

Security notes:
- All secret fields use repr=False so they are not exposed in logs or __repr__.
- Default values are empty strings — production MUST provide real values via .env.
- get_settings() logs a warning if critical keys are missing.
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import AliasChoices, Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # OpenAI Configuration
    openai_api_key: str = Field(default="", env="OPENAI_API_KEY", repr=False)
    openai_model: str = Field(default="gpt-4o-mini", env="OPENAI_MODEL")
    
    # Anthropic (Claude) Configuration — for AI Evaluator vision & chat
    anthropic_api_key: Optional[str] = Field(default=None, env="ANTHROPIC_API_KEY", repr=False)
    anthropic_primary_model: str = Field(default="claude-opus-4-20250514", env="ANTHROPIC_PRIMARY_MODEL")
    anthropic_fallback_model: str = Field(default="claude-sonnet-4-20250514", env="ANTHROPIC_FALLBACK_MODEL")
    
    # Flowcart (WhatsApp Integration) Configuration
    flowcart_webhook_secret: Optional[str] = Field(default=None, env="FLOWCART_WEBHOOK_SECRET", repr=False)
    flowcart_api_key: Optional[str] = Field(default=None, env="FLOWCART_API_KEY", repr=False)
    flowcart_api_url: str = Field(default="https://api.flowcart.io/v1", env="FLOWCART_API_URL")
    
    # Tavily Web Search (for internet price verification)
    tavily_api_key: Optional[str] = Field(default=None, env="TAVILY_API_KEY", repr=False)
    
    # Google Cloud Vision API (for Google Lens product identification)
    google_cloud_api_key: Optional[str] = Field(default=None, env="GOOGLE_CLOUD_API_KEY", repr=False)
    
    # WhatsApp Business API Configuration
    whatsapp_api_token: str = Field(default="", validation_alias="ACCESS_TOKEN", repr=False)
    whatsapp_phone_number_id: str = Field(default="", validation_alias="PHONE_NUMBER_ID")
    whatsapp_verify_token: str = Field(default="", validation_alias="VERIFY_TOKEN", repr=False)
    whatsapp_catalog_id: str = Field(default="", env="CATALOG_ID")
    whatsapp_app_id: str = Field(default="", env="APP_ID")
    whatsapp_app_secret: str = Field(default="", env="APP_SECRET", repr=False)
    whatsapp_version: str = Field(default="v23.0", env="VERSION")
    
    # Qdrant Vector Database Configuration
    qdrant_url: str = Field(default="http://localhost:9400", env="QDRANT_URL")
    qdrant_api_key: Optional[str] = Field(default=None, env="QDRANT_API_KEY", repr=False)
    qdrant_collection_name: str = Field(default="products", env="QDRANT_COLLECTION")
    embedding_dim: int = Field(default=768, env="EMBEDDING_DIM")
    similarity_threshold: float = Field(default=0.5, env="SIMILARITY_THRESHOLD")
    search_limit: int = Field(default=20, env="SEARCH_LIMIT")
    qdrant_use_reranking: bool = Field(default=True, env="QDRANT_USE_RERANKING")
    qdrant_rerank_top_k: int = Field(default=20, env="QDRANT_RERANK_TOP_K")
    qdrant_rerank_model: str = Field(default="cohere-rerank-v3", env="QDRANT_RERANK_MODEL")
    
    # M-Pesa Daraja API Configuration
    mpesa_consumer_key: str = Field(default="", env="MPESA_CONSUMER_KEY", repr=False)
    mpesa_consumer_secret: str = Field(default="", env="MPESA_CONSUMER_SECRET", repr=False)
    mpesa_shortcode: str = Field(default="", env="MPESA_BUSINESS_SHORTCODE")
    mpesa_passkey: str = Field(default="", env="MPESA_PASSKEY", repr=False)
    mpesa_environment: str = Field(default="sandbox", env="MPESA_ENVIRONMENT")
    mpesa_phone_number: str = Field(default="test_phone", env="MPESA_PHONE_NUMBER")
    mpesa_callback_url: str = Field(default="https://test.com/callback", env="MPESA_CALLBACK_URL")
    
    # Database Configuration
    database_url: str = Field(default="sqlite:///./greenbay_chatbot.db", env="DATABASE_URL")
    postgres_host: str = Field(default="localhost", env="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, env="POSTGRES_PORT")
    postgres_db: str = Field(default="greenbay_market", env="POSTGRES_DB")
    postgres_user: str = Field(default="postgres", env="POSTGRES_USER")
    postgres_password: str = Field(default="", env="POSTGRES_PASSWORD", repr=False)
    
    # Redis Configuration
    redis_host: str = Field(default="localhost", env="REDIS_HOST")
    redis_port: int = Field(default=9300, env="REDIS_PORT")
    redis_db: int = Field(default=0, env="REDIS_DB")
    redis_password: Optional[str] = Field(default=None, env="REDIS_PASSWORD", repr=False)
    
    # AWS S3 Configuration
    aws_access_key_id: Optional[str] = Field(default=None, env="AWS_ACCESS_KEY_ID", repr=False)
    aws_secret_access_key: Optional[str] = Field(default=None, env="AWS_SECRET_ACCESS_KEY", repr=False)
    aws_region: str = Field(
        default="eu-north-1",
        validation_alias=AliasChoices("AWS_DEFAULT_REGION", "AWS_REGION"),
    )
    # Accept either S3_BUCKET (our convention) or AWS_S3_BUCKET (common convention).
    # Default updated to the real production bucket so the startup banner never
    # shows the legacy "greenbay-bucket" placeholder even if neither env var is set.
    aws_s3_bucket: str = Field(
        default="greenbay-evaluator-images",
        validation_alias=AliasChoices("S3_BUCKET", "AWS_S3_BUCKET"),
    )
    
    # Application Configuration
    app_name: str = Field(default="GreenBay Market Chatbot", env="APP_NAME")
    app_version: str = Field(default="1.0.0", env="APP_VERSION")
    debug: bool = Field(default=False, env="DEBUG")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    
    # Security
    secret_key: str = Field(default="", env="SECRET_KEY", repr=False)
    algorithm: str = Field(default="HS256", env="ALGORITHM")
    access_token_expire_minutes: int = Field(default=30, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    
    # GreenBay Market Store Information
    store_name: str = Field(default="GreenBay Market", env="STORE_NAME")
    store_address: str = Field(default="Amani Gardens, 71 Church Road, Nairobi, Kenya", env="STORE_ADDRESS")
    store_phone: str = Field(default="+2540205002173", env="STORE_PHONE")
    store_email: str = Field(default="info@greenbay.market", env="STORE_EMAIL")
    
    
    # Payment Configuration
    payment_timeout_minutes: int = Field(default=10, env="PAYMENT_TIMEOUT_MINUTES")
    payment_retry_attempts: int = Field(default=3, env="PAYMENT_RETRY_ATTEMPTS")
    
    # Session Configuration
    session_timeout_hours: int = Field(default=24, env="SESSION_TIMEOUT_HOURS")
    conversation_timeout_hours: int = Field(default=2, env="CONVERSATION_TIMEOUT_HOURS")
    
    # Rate Limiting
    rate_limit_per_minute: int = Field(default=60, env="RATE_LIMIT_PER_MINUTE")
    rate_limit_per_hour: int = Field(default=1000, env="RATE_LIMIT_PER_HOUR")
    
    # Google Sheets Integration (CR-4)
    google_sheets_credentials_file: Optional[str] = Field(default=None, env="GOOGLE_SHEETS_CREDENTIALS_FILE")
    google_sheets_id: str = Field(default="1DCKTWSxvGYQzuEPoJanQFF5ssM9oWnqVmEoEmh1MhAU", env="GOOGLE_SHEETS_ID")

    # Airtable Configuration (write-only backup data repository)
    airtable_api_token: Optional[str] = Field(default=None, env="AIRTABLE_API_TOKEN", repr=False)
    airtable_base_id: str = Field(default="", env="AIRTABLE_BASE_ID")
    airtable_table_name: str = Field(default="Appliance Evaluations", env="AIRTABLE_TABLE_NAME")

    # Google Vertex AI Configuration (secondary evaluation via Gemini)
    google_vertex_credentials_file: str = Field(default="", env="GOOGLE_VERTEX_CREDENTIALS_FILE")
    google_vertex_project: str = Field(default="greenbay-ai-evaluator", env="GOOGLE_VERTEX_PROJECT")
    google_vertex_region: str = Field(default="us-central1", env="GOOGLE_VERTEX_REGION")
    google_vertex_model: str = Field(
        default="gemini-2.5-flash",
        env="GOOGLE_VERTEX_MODEL",
    )

    # LangSmith Configuration
    langsmith_api_key: Optional[str] = Field(default=None, env="LANGSMITH_API_KEY", repr=False)
    langsmith_project: str = Field(default="greenbay-chatbot", env="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(default="https://api.smith.langchain.com", env="LANGSMITH_ENDPOINT")
    langsmith_tracing: bool = Field(default=True, env="LANGSMITH_TRACING_V2")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"  # Ignore extra fields from .env
    )


# Global settings instance (initialized on first call)
_settings: Optional[Settings] = None


def invalidate_settings_cache() -> None:
    """Clear the cached Settings singleton so the next get_settings() reloads env/.env.

    Called once at FastAPI lifespan startup so module import order cannot pin an
    outdated S3 bucket (or other fields) before the process environment is final.
    """
    global _settings
    _settings = None


def runtime_s3_bucket_name() -> str:
    """Resolved S3 bucket for logging and health checks.

    Explicit ``S3_BUCKET`` / ``AWS_S3_BUCKET`` environment variables win, then a
    fresh ``Settings()`` read (not the singleton) so the name matches Docker/env
    even if something called get_settings() very early during imports.
    """
    direct = (
        os.environ.get("S3_BUCKET")
        or os.environ.get("AWS_S3_BUCKET")
        or ""
    ).strip()
    if direct:
        return direct
    return Settings().aws_s3_bucket


def get_settings() -> Settings:
    """Get application settings.

    Logs warnings if critical API keys are missing (security best practice).
    """
    global _settings
    if _settings is None:
        _settings = Settings()
        # Warn about missing critical keys (but don't block startup)
        import logging
        _log = logging.getLogger(__name__)
        _critical_keys = [
            ("anthropic_api_key", "Anthropic Vision"),
            ("tavily_api_key", "Tavily Search"),
            ("google_cloud_api_key", "Google Cloud Vision"),
            ("airtable_api_token", "Airtable Data Repository"),
            ("google_vertex_credentials_file", "Google Vertex AI"),
        ]
        for attr, label in _critical_keys:
            val = getattr(_settings, attr, None)
            if not val:
                _log.warning(f"SECURITY: {label} key not configured ({attr}). Feature will be disabled.")
    return _settings
