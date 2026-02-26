"""Common utilities and shared imports for agent tools."""

import os
from typing import Dict, Any, List, Optional
from contextvars import ContextVar
from loguru import logger

from app.services.qdrant_service import qdrant_service
from app.services.search_cache import search_cache
from app.services.cart_service import cart_service
from app.services.order_service import order_service
from app.services.delivery_service import delivery_service
from app.services.mpesa_service import mpesa_service
from app.services.receipt_service import receipt_generator
from app.config import get_settings

settings = get_settings()

# Context-aware storage for user context (works across async boundaries)
_current_user_phone: ContextVar[str] = ContextVar('current_user_phone', default=None)

# Explicitly export functions for import *
__all__ = [
    'set_current_user_phone',
    '_get_current_user_phone',
    'qdrant_service',
    'search_cache',
    'cart_service',
    'order_service',
    'delivery_service',
    'mpesa_service',
    'receipt_generator',
    'settings',
    'logger',
]


def set_current_user_phone(user_phone: str):
    """Set the current user phone in context variable (async-safe)."""
    _current_user_phone.set(user_phone)
    logger.info(f"✓ Set current user phone in context: {user_phone}")


def _get_current_user_phone() -> str:
    """
    Get the current user phone from context variable.
    The agent must call set_current_user_phone() before invoking tools.
    """
    user_phone = _current_user_phone.get()
    if user_phone:
        logger.debug(f"✓ Retrieved user phone from context: {user_phone}")
        return user_phone
    
    # Fallback: try to extract from database (recent session)
    # This is a last resort and may not always work
    logger.warning("✗ User phone not set in context variable, attempting database fallback")
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        from datetime import datetime, timedelta
        
        db = next(get_db())
        try:
            # Get the most recent active session created in the last 5 minutes
            five_minutes_ago = datetime.utcnow() - timedelta(minutes=5)
            recent_session = db.query(TradeInSession).filter(
                TradeInSession.status == "active",
                TradeInSession.user_phone != "user_phone",
                TradeInSession.user_phone.like("88%"),
                TradeInSession.created_at >= five_minutes_ago
            ).order_by(TradeInSession.created_at.desc()).first()
            
            if recent_session:
                logger.warning(f"Using phone from recent session: {recent_session.user_phone}")
                return recent_session.user_phone
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Database fallback failed: {e}")
    
    # If all methods fail, error out
    logger.error("✗ CRITICAL: Could not get user phone from context variable or database.")
    raise ValueError("User phone not available - agent must call set_current_user_phone() first")

