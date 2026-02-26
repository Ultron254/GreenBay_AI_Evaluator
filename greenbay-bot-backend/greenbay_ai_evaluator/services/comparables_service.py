"""
Comparables service — Phase 1: historical DB lookup.

Queries the existing ``trade_in_valuations`` table for products in the same
category/brand.  Returns a weighted average, count, and confidence score.

# EXTENSION_POINT: Phase 2 — Add external API integration (Jiji scraper, Jumia API)
# EXTENSION_POINT: Phase 3 — ML-based comparable scoring and weighting
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from greenbay_ai_evaluator.engine.offer_engine import Comparable


@dataclass
class ComparablesResult:
    """Result from the comparables lookup."""

    comparables: list[Comparable]
    count: int
    weighted_average: float
    confidence: float  # 0-100


def get_comparables(
    *,
    category: str,
    brand: str,
    model: str = "",
    db_session: Any = None,
) -> ComparablesResult:
    """Query historical trade-in valuations for comparable sales.

    Parameters
    ----------
    category : str
        Product category (e.g. "refrigerator").
    brand : str
        Brand name.
    model : str, optional
        Model identifier for tighter matching.
    db_session : sqlalchemy.orm.Session, optional
        An active DB session.  If ``None``, returns an empty result.

    Returns
    -------
    ComparablesResult
    """
    if db_session is None:
        return ComparablesResult(
            comparables=[], count=0, weighted_average=0.0, confidence=0.0
        )

    try:
        from app.database.models import TradeInSession

        # Build query: completed sessions for same category with a final_offer
        query = db_session.query(TradeInSession).filter(
            TradeInSession.status.in_(["completed", "active"]),
            TradeInSession.final_offer.isnot(None),
            TradeInSession.final_offer > 0,
        )

        # Category match (case-insensitive)
        cat_lower = category.lower().strip() if category else ""
        if cat_lower:
            query = query.filter(
                TradeInSession.detected_category.ilike(f"%{cat_lower}%")
            )

        # Brand match — loosen to product_name contains brand
        brand_clean = brand.strip() if brand else ""
        if brand_clean:
            query = query.filter(
                TradeInSession.product_name.ilike(f"%{brand_clean}%")
            )

        # Order by recency, limit to 20
        rows = (
            query.order_by(TradeInSession.created_at.desc()).limit(20).all()
        )

        if not rows:
            return ComparablesResult(
                comparables=[], count=0, weighted_average=0.0, confidence=0.0
            )

        comparables: list[Comparable] = []
        for row in rows:
            resale = float(row.final_offer)
            # Weight: exact model match = 2.0, same brand = 1.5, else 1.0
            weight = 1.0
            if model and row.product_model and model.lower() in row.product_model.lower():
                weight = 2.0
            elif brand_clean and row.product_name and brand_clean.lower() in row.product_name.lower():
                weight = 1.5
            comparables.append(Comparable(resale_price=resale, weight=weight))

        total_weight = sum(c.weight for c in comparables)
        weighted_avg = (
            sum(c.resale_price * c.weight for c in comparables) / total_weight
            if total_weight > 0
            else 0.0
        )

        # Confidence: more matches = higher confidence (cap at 100)
        confidence = min(100.0, len(comparables) * 20.0)

        return ComparablesResult(
            comparables=comparables,
            count=len(comparables),
            weighted_average=round(weighted_avg, 2),
            confidence=round(confidence, 1),
        )

    except Exception as e:
        logger.warning(f"Comparables lookup failed: {e}")
        return ComparablesResult(
            comparables=[], count=0, weighted_average=0.0, confidence=0.0
        )
