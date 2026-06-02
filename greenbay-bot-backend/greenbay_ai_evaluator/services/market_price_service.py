"""
Internet price lookup service (v6).

Primary: Gemini 2.5 Flash with Google Search grounding.
Fallback: Tavily web search (commented out, kept for rollback).

Searches for retail prices in the target country's local market and currency.
"""

from __future__ import annotations

import json
import re
import time
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
    confidence: float = 0.0
    raw_snippets: list[str] = field(default_factory=list)
    currency: str = "KES"


# ---------------------------------------------------------------------------
# Price sanity ranges by category (in KES-equivalent)
# ---------------------------------------------------------------------------
_PRICE_SANITY: dict[str, tuple[float, float]] = {
    "refrigerator": (8_000, 300_000),
    "fridge": (8_000, 300_000),
    "freezer": (8_000, 250_000),
    "cooker": (5_000, 200_000),
    "cooker_oven": (5_000, 200_000),
    "tv": (5_000, 500_000),
    "tv_monitor": (5_000, 500_000),
    "washing_machine": (10_000, 300_000),
    "microwave": (3_000, 80_000),
    "water_dispenser": (5_000, 80_000),
    "soundbar": (3_000, 100_000),
    "woofer": (2_000, 80_000),
}
_DEFAULT_SANITY = (1_000, 500_000)

_COUNTRY_NAMES = {"KE": "Kenya", "UG": "Uganda", "NG": "Nigeria"}
_COUNTRY_CURRENCIES = {"KE": "KES", "UG": "UGX", "NG": "NGN"}


# ---------------------------------------------------------------------------
# Gemini Google Search grounding (v6 primary)
# ---------------------------------------------------------------------------
def _extract_json_from_text(text: str) -> dict | None:
    """Parse a JSON object from model output, tolerant of fences."""
    if not text:
        return None
    stripped = text.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _format_size_hint(size_value: float | None, size_unit: str | None) -> str:
    """Build a human phrase like '55 inch' / '8 kg' / '250 litre' for the prompt."""
    if not size_value or size_value <= 0 or not size_unit:
        return ""
    unit = size_unit.lower().strip()
    if unit in ("inch", "inches", '"', "in"):
        return f"{size_value:g} inch"
    if unit in ("kg", "kgs", "kilogram", "kilograms"):
        return f"{size_value:g} kg"
    if unit in ("litre", "litres", "liter", "liters", "l"):
        return f"{size_value:g} litre"
    return f"{size_value:g} {unit}"


def gemini_price_research(
    *,
    brand: str,
    model: str,
    category: str,
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Call Gemini 2.5 Flash with Google Search grounding for retail price.

    Uses the existing Vertex AI service account — no new credentials needed.
    The size hint (inches / kg / litres) is critical: without it a 43" and a
    55" TV produce identical prompts and identical (wrong) prices.
    """
    result = InternetPriceResult()
    country_name = _COUNTRY_NAMES.get(country, "Kenya")
    currency = _COUNTRY_CURRENCIES.get(country, "KES")
    result.currency = currency

    try:
        from greenbay_ai_evaluator.services.vertex_ai_service import (
            _get_access_token,
            _build_endpoint,
        )
        from app.config import get_settings
        settings = get_settings()
    except Exception as e:
        logger.warning(f"Gemini price research: import/config failed: {e}")
        return result

    if not settings.google_vertex_credentials_file:
        logger.warning("Gemini price research: no Vertex credentials")
        return result

    size_hint = _format_size_hint(size_value, size_unit)
    # Build the most specific product description we can.
    parts = [p for p in (brand, model, size_hint) if p]
    product_desc = " ".join(parts).strip() or category

    prompt = (
        f"You are a pricing researcher. Using Google Search, find the current "
        f"NEW retail price of this exact appliance in {country_name}:\n"
        f"  Brand: {brand or 'unknown'}\n"
        f"  Model: {model or 'unknown'}\n"
        f"  Size/capacity: {size_hint or 'unknown'}\n"
        f"  Category: {category}\n\n"
        f"Search mainstream {country_name} retailers (e.g. Jumia, Kilimall, "
        f"brand stores, electronics shops). The size/capacity MUST match — a "
        f"43-inch TV and a 55-inch TV have very different prices, so do not "
        f"return a generic category price. "
        f"Return ONLY a JSON object with these exact fields: "
        f'{{"new_price": <number in {currency}>, "currency": "{currency}", '
        f'"matched_product": "<the exact product/title you priced>", '
        f'"sources": [<list of source URLs>]}}. '
        f"If you cannot find a price for this specific size/model, return "
        f'{{"new_price": null}} rather than guessing.'
    )

    try:
        token = _get_access_token()
    except Exception as e:
        logger.warning(f"Gemini price research: auth failed: {e}")
        return result

    # Build endpoint for the search-grounded model
    s = settings
    endpoint = (
        f"https://{s.google_vertex_region}-aiplatform.googleapis.com/v1/"
        f"projects/{s.google_vertex_project}/locations/{s.google_vertex_region}/"
        f"publishers/google/models/{s.google_vertex_model}:generateContent"
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}],
        "generationConfig": {
            "temperature": 0.1,
            # Raised from 512: with Google Search grounding the grounded answer
            # plus citations needs headroom, otherwise the JSON gets truncated.
            "maxOutputTokens": 2048,
            # gemini-2.5-flash has "thinking" ON by default; thinking tokens
            # count against maxOutputTokens and were eating the entire budget,
            # leaving NO visible text (root cause of "returned NO price").
            # Disable it for this factual lookup.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    for attempt in range(2):
        try:
            import requests as _req
            resp = _req.post(endpoint, headers=headers, json=body, timeout=30)
        except Exception as e:
            logger.warning(f"Gemini price research: request failed (attempt {attempt+1}): {e}")
            continue

        if resp.status_code != 200:
            logger.warning(
                f"Gemini price research: HTTP {resp.status_code} "
                f"(attempt {attempt+1}): {resp.text[:200]}"
            )
            # Self-heal: some Vertex API versions reject thinkingConfig with a
            # 400. Strip it and let the next attempt run without it.
            if resp.status_code == 400 and "thinking" in resp.text.lower():
                body["generationConfig"].pop("thinkingConfig", None)
                logger.warning("Gemini price research: removed thinkingConfig and will retry")
            continue

        finish_reason = ""
        try:
            data = resp.json()
            candidate = (data.get("candidates") or [{}])[0]
            finish_reason = candidate.get("finishReason", "")
            parts = (candidate.get("content") or {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        except Exception:
            text = ""

        if not text:
            logger.warning(
                f"Gemini price research: empty text (attempt {attempt+1}, "
                f"finishReason={finish_reason!r}) — raising token budget if MAX_TOKENS"
            )
        result.raw_snippets.append(text[:500])

        # Extract grounding sources if available
        grounding = candidate.get("groundingMetadata", {})
        for chunk in grounding.get("groundingChunks", []):
            web = chunk.get("web", {})
            if web.get("uri"):
                result.sources.append({
                    "title": web.get("title", ""),
                    "url": web["uri"],
                })

        parsed = _extract_json_from_text(text)
        if parsed and parsed.get("new_price"):
            try:
                price = float(parsed["new_price"])
            except (TypeError, ValueError):
                logger.warning(f"Gemini price research: unparsable price: {parsed['new_price']}")
                continue

            cat_key = category.lower().strip()
            lo, hi = _PRICE_SANITY.get(cat_key, _DEFAULT_SANITY)
            if lo <= price <= hi:
                result.launch_price = price
                result.current_resale_low = price * 0.7
                result.current_resale_high = price * 0.95
                result.confidence = min(75.0, 40.0 + len(result.sources) * 10.0)
                logger.info(
                    f"Gemini price research: {product_desc} → "
                    f"{currency} {price:,.0f} (confidence {result.confidence})"
                )
                return result
            else:
                logger.warning(
                    f"Gemini price research: price {price} outside sanity "
                    f"range [{lo}-{hi}] for {cat_key}, retrying..."
                )
                continue
        else:
            logger.warning(
                f"Gemini price research: no JSON/price in response "
                f"(attempt {attempt+1}, finishReason={finish_reason!r}); "
                f"text snippet: {text[:200]!r}"
            )

    return result


# ---------------------------------------------------------------------------
# Public entry point (v6 — Gemini primary)
# ---------------------------------------------------------------------------
def search_internet_price(
    *,
    brand: str,
    model: str,
    category: str,
    condition: str = "",
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Search for retail and resale prices using Gemini with Google Search grounding."""
    return gemini_price_research(
        brand=brand, model=model, category=category, country=country,
        size_value=size_value, size_unit=size_unit,
    )


# ---------------------------------------------------------------------------
# TAVILY FALLBACK — kept for rollback, not called in v6
# ---------------------------------------------------------------------------
# def _extract_kes_prices(text: str) -> list[float]:
#     patterns = [
#         r'KES\s*[\d,]+(?:\.\d+)?',
#         r'Ksh\s*[\d,]+(?:\.\d+)?',
#         r'KSh\s*[\d,]+(?:\.\d+)?',
#         r'Kshs?\s*[\d,]+(?:\.\d+)?',
#         r'[\d,]+(?:\.\d+)?\s*KES',
#         r'[\d,]+(?:\.\d+)?\s*Ksh',
#     ]
#     prices = []
#     for pat in patterns:
#         for match in re.finditer(pat, text, re.IGNORECASE):
#             digits = re.sub(r'[^0-9.]', '', match.group())
#             try:
#                 val = float(digits)
#                 if 500 <= val <= 2_000_000:
#                     prices.append(val)
#             except ValueError:
#                 pass
#     return prices
#
# def search_internet_price_tavily(*, brand, model, category, condition=""):
#     """TAVILY FALLBACK — uncomment to re-enable."""
#     result = InternetPriceResult()
#     try:
#         from app.config import get_settings
#         settings = get_settings()
#         api_key = settings.tavily_api_key
#         if not api_key:
#             return result
#         from tavily import TavilyClient
#         client = TavilyClient(api_key=api_key)
#         product_desc = f"{brand} {model}".strip() or category
#         queries = [
#             f"{product_desc} price Kenya KES",
#             f"{product_desc} {category} retail price",
#         ]
#         all_prices = []
#         for query in queries:
#             try:
#                 response = client.search(query=query, search_depth="basic", max_results=5, include_answer=True)
#                 answer = response.get("answer", "")
#                 if answer:
#                     result.raw_snippets.append(answer)
#                     all_prices.extend(_extract_kes_prices(answer))
#                 for r in response.get("results", []):
#                     content = r.get("content", "")
#                     title = r.get("title", "")
#                     url = r.get("url", "")
#                     snippet = f"{title}: {content}"
#                     prices_found = _extract_kes_prices(snippet)
#                     all_prices.extend(prices_found)
#                     if prices_found:
#                         result.sources.append({"title": title, "url": url, "price": str(prices_found[0])})
#                     result.raw_snippets.append(snippet[:200])
#             except Exception as e:
#                 continue
#         if all_prices:
#             all_prices.sort()
#             median = all_prices[len(all_prices) // 2]
#             filtered = [p for p in all_prices if p <= median * 3]
#             if filtered:
#                 result.launch_price = max(filtered)
#                 result.current_resale_low = min(filtered)
#                 result.current_resale_high = filtered[int(len(filtered) * 0.75)] if len(filtered) > 2 else max(filtered)
#                 result.confidence = min(90.0, len(filtered) * 15.0)
#     except ImportError:
#         pass
#     except Exception as e:
#         logger.error(f"Tavily price lookup failed: {e}")
#     return result
