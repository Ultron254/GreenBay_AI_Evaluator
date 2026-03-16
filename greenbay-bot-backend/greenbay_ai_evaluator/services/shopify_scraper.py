"""
Shopify inventory scraper for greenbay.market.

Fetches the full product catalog via the public /products.json endpoint,
upserts into the shopify_products table, and marks removed products inactive.
Designed to be run on startup and then every 12 hours via a background task.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from app.database.models import ShopifyProduct

SHOPIFY_BASE = "https://greenbay.market"
PRODUCTS_URL = f"{SHOPIFY_BASE}/products.json"
PAGE_SIZE = 250  # Shopify max


async def _fetch_all_products() -> list[dict[str, Any]]:
    """Paginate through Shopify products.json and return all products."""
    all_products: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            url = f"{PRODUCTS_URL}?limit={PAGE_SIZE}&page={page}"
            logger.info(f"Fetching Shopify page {page}: {url}")
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products", [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < PAGE_SIZE:
                break
            page += 1
    logger.info(f"Fetched {len(all_products)} products from Shopify")
    return all_products


def _extract_product_data(product: dict) -> dict:
    """Extract relevant fields from a Shopify product dict."""
    # Price from first variant
    variants = product.get("variants", [])
    price = None
    compare_at = None
    sku = None
    available = False
    if variants:
        v = variants[0]
        price = float(v["price"]) if v.get("price") else None
        compare_at = float(v["compare_at_price"]) if v.get("compare_at_price") else None
        sku = v.get("sku")
        available = v.get("available", False)

    # First image
    images = product.get("images", [])
    image_url = images[0]["src"] if images else None

    handle = product.get("handle", "")
    product_url = f"{SHOPIFY_BASE}/products/{handle}" if handle else None

    return {
        "shopify_id": str(product["id"]),
        "title": product.get("title", ""),
        "handle": handle,
        "product_type": product.get("product_type", ""),
        "vendor": product.get("vendor", ""),
        "tags": product.get("tags", []),
        "price": price,
        "compare_at_price": compare_at,
        "sku": sku,
        "available": available,
        "image_url": image_url,
        "product_url": product_url,
    }


def scrape_full_inventory_sync(db: Session) -> dict[str, int]:
    """Synchronous wrapper that runs the async scrape and upserts to DB."""
    loop = asyncio.new_event_loop()
    try:
        products = loop.run_until_complete(_fetch_all_products())
    finally:
        loop.close()
    return _upsert_products(db, products)


async def scrape_full_inventory_async() -> dict[str, int]:
    """Async version — fetches products and upserts using a fresh DB session."""
    products = await _fetch_all_products()
    from app.database.db import get_db_session
    db = get_db_session()
    try:
        result = _upsert_products(db, products)
        return result
    finally:
        db.close()


def _upsert_products(db: Session, products: list[dict]) -> dict[str, int]:
    """Upsert Shopify products into the DB. Returns counts."""
    now = datetime.now(timezone.utc)
    seen_ids: set[str] = set()
    new_count = 0
    updated_count = 0

    for raw in products:
        data = _extract_product_data(raw)
        shopify_id = data["shopify_id"]
        seen_ids.add(shopify_id)

        existing = db.query(ShopifyProduct).filter(
            ShopifyProduct.shopify_id == shopify_id
        ).first()

        if existing:
            # Update existing
            for key, val in data.items():
                if key != "shopify_id":
                    setattr(existing, key, val)
            existing.last_seen_at = now
            existing.is_active = True
            updated_count += 1
        else:
            # Insert new
            sp = ShopifyProduct(
                **data,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
            db.add(sp)
            new_count += 1

    # Mark products no longer in the feed as inactive
    deactivated = 0
    if seen_ids:
        stale = db.query(ShopifyProduct).filter(
            ShopifyProduct.is_active.is_(True),
            ShopifyProduct.shopify_id.notin_(seen_ids),
        ).all()
        for sp in stale:
            sp.is_active = False
            deactivated += 1

    db.commit()
    total = db.query(ShopifyProduct).filter(ShopifyProduct.is_active.is_(True)).count()

    result = {
        "new": new_count,
        "updated": updated_count,
        "deactivated": deactivated,
        "total_active": total,
    }
    logger.info(f"Shopify inventory sync: {result}")
    return result
