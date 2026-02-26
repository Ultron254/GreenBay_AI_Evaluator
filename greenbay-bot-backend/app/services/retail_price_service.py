"""Service for finding retail prices of products in Kenya."""

import requests
from typing import Dict, Any, Optional
from loguru import logger
import asyncio

from app.services.qdrant_service import qdrant_service


async def search_retail_price(product_name: str, model: str = "") -> Dict[str, Any]:
    """
    Search for retail price of a product in Kenya.
    
    This searches Qdrant for similar products to estimate retail price.
    
    Args:
        product_name: Product name (e.g., "Logitech G102")
        model: Model/variant (e.g., "Gaming Mouse")
        
    Returns:
        Dictionary with price information
    """
    try:
        # Search in Qdrant for the product
        search_query = f"{product_name} {model}".strip()
        results = await qdrant_service.search_products(search_query, limit=5)
        
        if not results:
            logger.warning(f"No retail prices found for: {search_query}")
            return {
                "success": False,
                "message": f"Could not find retail price for {product_name}",
                "estimated_price": None
            }
        
        # Extract prices from top results
        prices = []
        for result in results:
            payload = result.get("payload", {})
            if payload.get("variants") and len(payload["variants"]) > 0:
                variant = payload["variants"][0]
                price = float(variant.get("price", 0))
                if price > 0:
                    prices.append({
                        "product": payload.get("title", "Unknown"),
                        "price": price,
                        "vendor": payload.get("vendor", "Unknown")
                    })
        
        if not prices:
            return {
                "success": False,
                "message": f"No valid prices found for {product_name}",
                "estimated_price": None
            }
        
        # Get average price from top 3 results
        avg_price = sum(p["price"] for p in prices[:3]) / min(len(prices), 3)
        
        logger.info(f"Found retail price for {product_name}: KES {avg_price:,.2f}")
        
        return {
            "success": True,
            "product_name": product_name,
            "estimated_retail_price": avg_price,
            "currency": "KES",
            "similar_products": prices[:3],
            "message": "Product information retrieved successfully"  # DO NOT reveal retail price to user
        }
        
    except Exception as e:
        logger.error(f"Error searching retail price: {e}")
        return {
            "success": False,
            "message": "Failed to search retail price",
            "error": str(e),
            "estimated_price": None
        }


# Synchronous wrapper for tool compatibility
def get_retail_price(product_name: str, model: str = "") -> Dict[str, Any]:
    """Synchronous wrapper for search_retail_price."""
    return asyncio.run(search_retail_price(product_name, model))

