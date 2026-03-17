"""
Internet price lookup service using Tavily web search.

Searches the web for retail launch prices, current resale values,
and product age indicators for appliances in the Kenyan market.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass
class InternetPriceResult:
    """Result from internet price lookup."""

    launch_price: float | None = None
    current_resale_low: float | None = None
    current_resale_high: float | None = None
    sources: list[dict[str, str]] = field(default_factory=list)
    confidence: float = 0.0  # 0-100
    raw_snippets: list[str] = field(default_factory=list)


def _extract_kes_prices(text: str) -> list[float]:
    """Extract KES prices from text."""
    patterns = [
        r'KES\s*[\d,]+(?:\.\d+)?',
        r'Ksh\s*[\d,]+(?:\.\d+)?',
        r'KSh\s*[\d,]+(?:\.\d+)?',
        r'Kshs?\s*[\d,]+(?:\.\d+)?',
        r'[\d,]+(?:\.\d+)?\s*KES',
        r'[\d,]+(?:\.\d+)?\s*Ksh',
    ]
    prices = []
    for pat in patterns:
        for match in re.finditer(pat, text, re.IGNORECASE):
            digits = re.sub(r'[^0-9.]', '', match.group())
            try:
                val = float(digits)
                if 500 <= val <= 2_000_000:  # Reasonable KES range
                    prices.append(val)
            except ValueError:
                pass
    return prices


def search_internet_price(
    *,
    brand: str,
    model: str,
    category: str,
    condition: str = "",
) -> InternetPriceResult:
    """Search the internet for retail and resale prices.

    Uses Tavily API for web search, targeting Kenyan retailers and
    second-hand marketplaces.
    """
    result = InternetPriceResult()

    try:
        from app.config import get_settings
        settings = get_settings()

        api_key = settings.tavily_api_key
        if not api_key:
            logger.warning("Tavily API key not set — skipping internet price lookup")
            return result

        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)

        # Build search queries
        product_desc = f"{brand} {model}".strip()
        if not product_desc:
            product_desc = category

        queries = [
            f"{product_desc} price Kenya KES",
            f"{product_desc} {category} retail price",
        ]

        all_prices: list[float] = []

        for query in queries:
            try:
                response = client.search(
                    query=query,
                    search_depth="basic",
                    max_results=5,
                    include_answer=True,
                )

                # Extract prices from answer
                answer = response.get("answer", "")
                if answer:
                    result.raw_snippets.append(answer)
                    all_prices.extend(_extract_kes_prices(answer))

                # Extract prices from results
                for r in response.get("results", []):
                    content = r.get("content", "")
                    title = r.get("title", "")
                    url = r.get("url", "")
                    snippet = f"{title}: {content}"

                    prices_found = _extract_kes_prices(snippet)
                    all_prices.extend(prices_found)

                    if prices_found:
                        result.sources.append({
                            "title": title,
                            "url": url,
                            "price": str(prices_found[0]),
                        })
                    result.raw_snippets.append(snippet[:200])

            except Exception as e:
                logger.warning(f"Tavily search failed for query '{query}': {e}")
                continue

        if all_prices:
            all_prices.sort()
            # Remove extreme outliers (> 3x median)
            median = all_prices[len(all_prices) // 2]
            filtered = [p for p in all_prices if p <= median * 3]

            if filtered:
                result.launch_price = max(filtered)  # Highest = likely retail
                result.current_resale_low = min(filtered)
                result.current_resale_high = filtered[int(len(filtered) * 0.75)] if len(filtered) > 2 else max(filtered)
                result.confidence = min(90.0, len(filtered) * 15.0)

        logger.info(
            f"Internet price lookup: {product_desc} → "
            f"launch={result.launch_price}, "
            f"resale=[{result.current_resale_low}-{result.current_resale_high}], "
            f"confidence={result.confidence}, sources={len(result.sources)}"
        )

    except ImportError:
        logger.warning("tavily package not installed — skipping internet price lookup")
    except Exception as e:
        logger.error(f"Internet price lookup failed: {e}")

    return result
