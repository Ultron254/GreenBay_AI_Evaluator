"""Configuration management for GreenBay Market Chatbot."""

import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # OpenAI Configuration
    openai_api_key: str = Field(default="test_key", env="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", env="OPENAI_MODEL")
    
    # Anthropic (Claude) Configuration — for AI Evaluator vision & chat
    anthropic_api_key: Optional[str] = Field(default=None, env="ANTHROPIC_API_KEY")
    anthropic_primary_model: str = Field(default="claude-opus-4-20250514", env="ANTHROPIC_PRIMARY_MODEL")
    anthropic_fallback_model: str = Field(default="claude-sonnet-4-20250514", env="ANTHROPIC_FALLBACK_MODEL")
    
    # Flowcart (WhatsApp Integration) Configuration
    flowcart_webhook_secret: Optional[str] = Field(default=None, env="FLOWCART_WEBHOOK_SECRET")
    flowcart_api_key: Optional[str] = Field(default=None, env="FLOWCART_API_KEY")
    flowcart_api_url: str = Field(default="https://api.flowcart.io/v1", env="FLOWCART_API_URL")
    
    # WhatsApp Business API Configuration
    whatsapp_api_token: str = Field(default="test_token", validation_alias="ACCESS_TOKEN")
    whatsapp_phone_number_id: str = Field(default="test_id", validation_alias="PHONE_NUMBER_ID")
    whatsapp_verify_token: str = Field(default="test_verify_token", validation_alias="VERIFY_TOKEN")
    whatsapp_catalog_id: str = Field(default="test_catalog", env="CATALOG_ID")
    whatsapp_app_id: str = Field(default="test_app_id", env="APP_ID")
    whatsapp_app_secret: str = Field(default="test_app_secret", env="APP_SECRET")
    whatsapp_version: str = Field(default="v23.0", env="VERSION")
    
    # Qdrant Vector Database Configuration
    qdrant_url: str = Field(default="http://localhost:6333", env="QDRANT_URL")
    qdrant_api_key: Optional[str] = Field(default=None, env="QDRANT_API_KEY")
    qdrant_collection_name: str = Field(default="products", env="QDRANT_COLLECTION")
    embedding_dim: int = Field(default=768, env="EMBEDDING_DIM")
    similarity_threshold: float = Field(default=0.5, env="SIMILARITY_THRESHOLD")
    search_limit: int = Field(default=20, env="SEARCH_LIMIT")
    qdrant_use_reranking: bool = Field(default=True, env="QDRANT_USE_RERANKING")
    qdrant_rerank_top_k: int = Field(default=20, env="QDRANT_RERANK_TOP_K")
    qdrant_rerank_model: str = Field(default="cohere-rerank-v3", env="QDRANT_RERANK_MODEL")
    
    # M-Pesa Daraja API Configuration
    mpesa_consumer_key: str = Field(default="test_consumer_key", env="MPESA_CONSUMER_KEY")
    mpesa_consumer_secret: str = Field(default="test_consumer_secret", env="MPESA_CONSUMER_SECRET")
    mpesa_shortcode: str = Field(default="test_shortcode", env="MPESA_BUSINESS_SHORTCODE")
    mpesa_passkey: str = Field(default="test_passkey", env="MPESA_PASSKEY")
    mpesa_environment: str = Field(default="sandbox", env="MPESA_ENVIRONMENT")
    mpesa_phone_number: str = Field(default="test_phone", env="MPESA_PHONE_NUMBER")
    mpesa_callback_url: str = Field(default="https://test.com/callback", env="MPESA_CALLBACK_URL")
    
    # Database Configuration
    database_url: str = Field(default="sqlite:///./greenbay_chatbot.db", env="DATABASE_URL")
    postgres_host: str = Field(default="localhost", env="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, env="POSTGRES_PORT")
    postgres_db: str = Field(default="greenbay_market", env="POSTGRES_DB")
    postgres_user: str = Field(default="postgres", env="POSTGRES_USER")
    postgres_password: str = Field(default="", env="POSTGRES_PASSWORD")
    
    # Redis Configuration
    redis_host: str = Field(default="localhost", env="REDIS_HOST")
    redis_port: int = Field(default=6379, env="REDIS_PORT")
    redis_db: int = Field(default=0, env="REDIS_DB")
    redis_password: Optional[str] = Field(default=None, env="REDIS_PASSWORD")
    
    # AWS S3 Configuration
    aws_access_key_id: Optional[str] = Field(default=None, env="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(default=None, env="AWS_SECRET_ACCESS_KEY")
    aws_region: str = Field(default="eu-north-1", env="AWS_DEFAULT_REGION")
    aws_s3_bucket: str = Field(default="greenbay-bucket", env="S3_BUCKET")
    
    # Application Configuration
    app_name: str = Field(default="GreenBay Market Chatbot", env="APP_NAME")
    app_version: str = Field(default="1.0.0", env="APP_VERSION")
    debug: bool = Field(default=False, env="DEBUG")
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    
    # Security
    secret_key: str = Field(default="test_secret_key", env="SECRET_KEY")
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
    
    # LangSmith Configuration
    langsmith_api_key: Optional[str] = Field(default=None, env="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="greenbay-chatbot", env="LANGSMITH_PROJECT")
    langsmith_endpoint: str = Field(default="https://api.smith.langchain.com", env="LANGSMITH_ENDPOINT")
    langsmith_tracing: bool = Field(default=True, env="LANGSMITH_TRACING_V2")
    
    # Tavily Search Configuration (for general web search)
    tavily_api_key: Optional[str] = Field(default=None, env="TAVILY_API_KEY")
    
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore"  # Ignore extra fields from .env
    )


# Global settings instance (initialized on first call)
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get application settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
