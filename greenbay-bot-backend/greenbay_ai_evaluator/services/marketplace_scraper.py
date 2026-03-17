"""
Marketplace scraper for second-hand appliance prices.

Scrapes Jiji.co.ke and Jumia Deals for live listings of used appliances
to provide real market price benchmarks during trade-in evaluation.
Results are cached in Redis for 6 hours to avoid excessive requests.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from loguru import logger


@dataclass
class MarketListing:
    """A single listing from a marketplace."""
    title: str
    price: float
    condition: str = ""
    url: str = ""
    source: str = ""  # "jiji" | "jumia"


@dataclass
class MarketplaceResult:
    """Combined result from marketplace scraping."""
    listings: list[MarketListing] = field(default_factory=list)
    avg_price: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    count: int = 0
    confidence: float = 0.0  # 0-100


def _cache_key(brand: str, model: str, category: str) -> str:
    """Generate a Redis cache key for marketplace results."""
    raw = f"marketplace:{category}:{brand}:{model}".lower().strip()
    return f"mktplace:{hashlib.md5(raw.encode()).hexdigest()[:12]}"


def _get_redis():
    """Get Redis client, returns None if unavailable."""
    try:
        import redis
        from app.config import get_settings
        settings = get_settings()
        return redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password,
            decode_responses=True,
            socket_timeout=2,
        )
    except Exception:
        return None


def _extract_prices_from_html(html: str) -> list[dict[str, Any]]:
    """Extract prices from HTML content using regex."""
    results = []

    # KES price patterns
    price_patterns = [
        r'KES\s*([\d,]+)',
        r'Ksh\.?\s*([\d,]+)',
        r'KSh\s*([\d,]+)',
        r'"price":\s*"?([\d,]+)"?',
        r'data-price="([\d,]+)"',
    ]

    for pat in price_patterns:
        for match in re.finditer(pat, html, re.IGNORECASE):
            try:
                price = float(match.group(1).replace(',', ''))
                if 500 <= price <= 500_000:
                    results.append({"price": price, "raw": match.group(0)})
            except ValueError:
                pass

    return results


async def scrape_jiji_prices(
    brand: str,
    model: str,
    category: str,
) -> list[MarketListing]:
    """Scrape Jiji.co.ke for second-hand listings.

    Uses Jiji search API endpoint to find matching products.
    """
    listings: list[MarketListing] = []

    # Map categories to Jiji search terms
    cat_map = {
        "refrigerator": "refrigerator",
        "washing_machine": "washing machine",
        "tv_monitor": "television",
        "tv": "television",
        "cooker_oven": "cooker",
        "cooker": "cooker",
        "microwave": "microwave",
        "small_kitchen": "blender",
    }

    search_term = f"{brand} {model}".strip()
    if not search_term:
        search_term = cat_map.get(category.lower(), category)
    else:
        cat_term = cat_map.get(category.lower(), "")
        if cat_term:
            search_term = f"{search_term} {cat_term}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            url = f"https://jiji.co.ke/api_web/v1/listing"
            params = {
                "query": search_term,
                "region_item_id": "2",  # Nairobi
                "page": "1",
                "webp": "true",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            }

            resp = await client.get(url, params=params, headers=headers)

            if resp.status_code == 200:
                data = resp.json()
                adverts = data.get("adverts_list", {}).get("adverts", [])

                for ad in adverts[:15]:
                    try:
                        price = float(ad.get("price_obj", {}).get("value", 0))
                        if price < 500:
                            continue
                        title = ad.get("title", "")
                        ad_url = f"https://jiji.co.ke{ad.get('url', '')}"

                        listings.append(MarketListing(
                            title=title,
                            price=price,
                            condition="used",
                            url=ad_url,
                            source="jiji",
                        ))
                    except (ValueError, TypeError):
                        continue

        logger.info(f"Jiji scrape: '{search_term}' → {len(listings)} listings")

    except httpx.TimeoutException:
        logger.warning(f"Jiji scrape timed out for '{search_term}'")
    except Exception as e:
        logger.warning(f"Jiji scrape failed: {e}")

    return listings


async def scrape_jumia_prices(
    brand: str,
    model: str,
    category: str,
) -> list[MarketListing]:
    """Scrape Jumia Kenya for appliance listings.

    Uses Jumia search page to find matching products.
    """
    listings: list[MarketListing] = []

    search_term = f"{brand} {model}".strip()
    if not search_term:
        search_term = category.replace("_", " ")

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            url = "https://www.jumia.co.ke/catalog/"
            params = {"q": search_term}
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html",
            }

            resp = await client.get(url, params=params, headers=headers)

            if resp.status_code == 200:
                html = resp.text

                # Extract product cards using regex
                # Jumia uses data-attributes and structured content
                card_pattern = r'class="[^"]*prd[^"]*"[^>]*>.*?</article>'
                title_pattern = r'class="[^"]*name[^"]*"[^>]*>(.*?)</(?:h3|div|span)'
                price_pattern = r'class="[^"]*prc[^"]*"[^>]*>\s*KSh\s*([\d,]+)'
                link_pattern = r'href="(/[^"]*\.html)"'

                # Simple extraction of prices and titles
                titles = re.findall(title_pattern, html, re.DOTALL | re.IGNORECASE)
                prices = re.findall(price_pattern, html, re.IGNORECASE)
                links = re.findall(link_pattern, html)

                for i, (title, price_str) in enumerate(zip(titles[:10], prices[:10])):
                    try:
                        price = float(price_str.replace(',', ''))
                        if price < 500:
                            continue
                        link = f"https://www.jumia.co.ke{links[i]}" if i < len(links) else ""

                        listings.append(MarketListing(
                            title=title.strip(),
                            price=price,
                            condition="new/refurbished",
                            url=link,
                            source="jumia",
                        ))
                    except (ValueError, TypeError):
                        continue

        logger.info(f"Jumia scrape: '{search_term}' → {len(listings)} listings")

    except httpx.TimeoutException:
        logger.warning(f"Jumia scrape timed out for '{search_term}'")
    except Exception as e:
        logger.warning(f"Jumia scrape failed: {e}")

    return listings


async def get_marketplace_prices(
    *,
    brand: str,
    model: str,
    category: str,
) -> MarketplaceResult:
    """Get marketplace prices from Jiji and Jumia with Redis caching.

    Results are cached for 6 hours (21600 seconds).
    """
    result = MarketplaceResult()

    # Check Redis cache
    r = _get_redis()
    cache_k = _cache_key(brand, model, category)

    if r:
        try:
            cached = r.get(cache_k)
            if cached:
                data = json.loads(cached)
                result.listings = [MarketListing(**l) for l in data["listings"]]
                result.avg_price = data["avg_price"]
                result.min_price = data["min_price"]
                result.max_price = data["max_price"]
                result.count = data["count"]
                result.confidence = data["confidence"]
                logger.info(f"Marketplace cache hit: {cache_k} → {result.count} listings")
                return result
        except Exception as e:
            logger.debug(f"Cache read failed: {e}")

    # Scrape both marketplaces concurrently
    import asyncio
    jiji_task = scrape_jiji_prices(brand, model, category)
    jumia_task = scrape_jumia_prices(brand, model, category)

    jiji_listings, jumia_listings = await asyncio.gather(
        jiji_task, jumia_task, return_exceptions=True
    )

    all_listings: list[MarketListing] = []
    if isinstance(jiji_listings, list):
        all_listings.extend(jiji_listings)
    if isinstance(jumia_listings, list):
        all_listings.extend(jumia_listings)

    if all_listings:
        prices = [l.price for l in all_listings]
        result.listings = all_listings
        result.avg_price = round(sum(prices) / len(prices), 2)
        result.min_price = min(prices)
        result.max_price = max(prices)
        result.count = len(all_listings)
        result.confidence = min(85.0, len(all_listings) * 10.0)

    # Cache result in Redis (6 hours)
    if r and all_listings:
        try:
            cache_data = {
                "listings": [
                    {"title": l.title, "price": l.price, "condition": l.condition,
                     "url": l.url, "source": l.source}
                    for l in all_listings[:20]
                ],
                "avg_price": result.avg_price,
                "min_price": result.min_price,
                "max_price": result.max_price,
                "count": result.count,
                "confidence": result.confidence,
            }
            r.setex(cache_k, 21600, json.dumps(cache_data))
            logger.debug(f"Marketplace result cached: {cache_k}")
        except Exception as e:
            logger.debug(f"Cache write failed: {e}")

    logger.info(
        f"Marketplace prices: {brand} {model} ({category}) → "
        f"avg={result.avg_price}, range=[{result.min_price}-{result.max_price}], "
        f"count={result.count}"
    )

    return result
