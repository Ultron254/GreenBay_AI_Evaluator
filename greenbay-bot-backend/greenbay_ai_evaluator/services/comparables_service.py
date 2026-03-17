"""
Comparables service — Multi-source price comparison.

Phase 1: Historical DB lookup (trade_in_sessions)
Phase 2: Shopify inventory cross-reference (shopify_products)
Phase 3: Expert feedback integration (expert_price_feedback)
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
    sources_breakdown: dict[str, int] | None = None


def get_comparables(
    *,
    category: str,
    brand: str,
    model: str = "",
    db_session: Any = None,
) -> ComparablesResult:
    """Query all comparable sources: historical, Shopify, and expert feedback."""
    if db_session is None:
        return ComparablesResult(
            comparables=[], count=0, weighted_average=0.0, confidence=0.0
        )

    comparables: list[Comparable] = []
    sources = {"historical": 0, "shopify": 0, "expert": 0}

    # --- Source 1: Historical trade-in sessions ---
    try:
        from app.database.models import TradeInSession

        query = db_session.query(TradeInSession).filter(
            TradeInSession.status.in_(["completed", "active"]),
            TradeInSession.final_offer.isnot(None),
            TradeInSession.final_offer > 0,
        )

        cat_lower = category.lower().strip() if category else ""
        if cat_lower:
            query = query.filter(
                TradeInSession.detected_category.ilike(f"%{cat_lower}%")
            )

        brand_clean = brand.strip() if brand else ""
        if brand_clean:
            query = query.filter(
                TradeInSession.product_name.ilike(f"%{brand_clean}%")
            )

        rows = query.order_by(TradeInSession.created_at.desc()).limit(20).all()

        for row in rows:
            resale = float(row.final_offer)
            weight = 1.0
            if model and row.product_model and model.lower() in row.product_model.lower():
                weight = 2.0
            elif brand_clean and row.product_name and brand_clean.lower() in row.product_name.lower():
                weight = 1.5
            comparables.append(Comparable(resale_price=resale, weight=weight))
            sources["historical"] += 1

    except Exception as e:
        logger.warning(f"Historical comparables lookup failed: {e}")

    # --- Source 2: Shopify inventory (our own store prices) ---
    try:
        from app.database.models import ShopifyProduct

        cat_map = {
            "refrigerator": ["REFRIGERATORS"],
            "fridge": ["REFRIGERATORS"],
            "washing_machine": ["Washing Machine"],
            "washer": ["Washing Machine"],
            "tv": ["TV & Home Entertainment"],
            "tv_monitor": ["TV & Home Entertainment"],
            "television": ["TV & Home Entertainment"],
            "cooker": ["COOKERS"],
            "cooker_oven": ["COOKERS"],
            "stove": ["COOKERS"],
            "microwave": ["MICROWAVE"],
            "freezer": ["FREEZER"],
            "chiller": ["CHILLERS"],
            "small_kitchen": ["Home Appliances", "BLENDING JUICING"],
        }

        shopify_types = cat_map.get(cat_lower, [])
        if shopify_types:
            sq = db_session.query(ShopifyProduct).filter(
                ShopifyProduct.is_active.is_(True),
                ShopifyProduct.available.is_(True),
                ShopifyProduct.product_type.in_(shopify_types),
                ShopifyProduct.price.isnot(None),
                ShopifyProduct.price > 0,
            )

            # Brand matching
            if brand_clean:
                sq = sq.filter(ShopifyProduct.title.ilike(f"%{brand_clean}%"))

            shopify_rows = sq.order_by(ShopifyProduct.price.asc()).limit(10).all()

            for sp in shopify_rows:
                # Shopify price is our own selling price — use it as a strong
                # market anchor (weight 1.8 for brand match, 1.2 for category)
                weight = 1.8 if brand_clean and brand_clean.lower() in sp.title.lower() else 1.2
                comparables.append(Comparable(resale_price=float(sp.price), weight=weight))
                sources["shopify"] += 1

    except Exception as e:
        logger.warning(f"Shopify comparables lookup failed: {e}")

    # --- Source 3: Expert price feedback (highest weight) ---
    try:
        from app.database.models import ExpertPriceFeedback

        eq = db_session.query(ExpertPriceFeedback).filter(
            ExpertPriceFeedback.product_category.ilike(f"%{cat_lower}%") if cat_lower else True,
        )
        if brand_clean:
            eq = eq.filter(ExpertPriceFeedback.brand.ilike(f"%{brand_clean}%"))

        expert_rows = eq.order_by(ExpertPriceFeedback.created_at.desc()).limit(10).all()

        for ef in expert_rows:
            # Expert feedback has the highest weight (3.0 for exact model, 2.5 for brand)
            weight = 3.0 if model and ef.model and model.lower() in ef.model.lower() else 2.5
            comparables.append(Comparable(resale_price=float(ef.expert_price), weight=weight))
            sources["expert"] += 1

    except Exception as e:
        # ExpertPriceFeedback table might not exist yet
        logger.debug(f"Expert feedback lookup: {e}")

    # --- Aggregate ---
    if not comparables:
        return ComparablesResult(
            comparables=[], count=0, weighted_average=0.0, confidence=0.0,
            sources_breakdown=sources,
        )

    total_weight = sum(c.weight for c in comparables)
    weighted_avg = (
        sum(c.resale_price * c.weight for c in comparables) / total_weight
        if total_weight > 0
        else 0.0
    )

    # Confidence: more matches = higher confidence (cap at 100)
    confidence = min(100.0, len(comparables) * 12.0)
    # Boost confidence for expert data
    if sources["expert"] > 0:
        confidence = min(100.0, confidence + 15.0)

    logger.info(
        f"Comparables: {len(comparables)} total "
        f"(historical={sources['historical']}, shopify={sources['shopify']}, "
        f"expert={sources['expert']}), avg={weighted_avg:.0f}, conf={confidence:.0f}"
    )

    return ComparablesResult(
        comparables=comparables,
        count=len(comparables),
        weighted_average=round(weighted_avg, 2),
        confidence=round(confidence, 1),
        sources_breakdown=sources,
    )

