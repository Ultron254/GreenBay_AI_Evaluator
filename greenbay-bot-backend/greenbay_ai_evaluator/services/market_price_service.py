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

# Approx KES per 1 unit of local currency. Used ONLY to scale the KES-denominated
# sanity bands into the target currency so a legitimate local-currency price is
# not falsely rejected. (Not used for customer-facing conversion.)
_KES_PER_LOCAL = {"KES": 1.0, "UGX": 0.0357, "NGN": 0.085}


def _sanity_band_for(cat_key: str, currency: str) -> tuple[float, float]:
    """KES sanity band scaled into the evaluation's local currency."""
    lo, hi = _PRICE_SANITY.get(cat_key, _DEFAULT_SANITY)
    rate = _KES_PER_LOCAL.get(currency, 1.0)
    if rate and rate != 1.0:
        return lo / rate, hi / rate
    return lo, hi


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

    _GEMINI_BACKOFFS = (2, 6, 12)  # seconds; used for 429/transient retries
    _MAX_ATTEMPTS = 3
    for attempt in range(_MAX_ATTEMPTS):
        try:
            import requests as _req
            # Grounded search has been observed at ~35-38s; a 30s timeout was
            # killing the first attempt every time. Give it real headroom.
            resp = _req.post(endpoint, headers=headers, json=body, timeout=50)
        except Exception as e:
            logger.warning(f"Gemini price research: request failed (attempt {attempt+1}): {e}")
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(_GEMINI_BACKOFFS[attempt])
            continue

        if resp.status_code != 200:
            logger.warning(
                f"Gemini price research: HTTP {resp.status_code} "
                f"(attempt {attempt+1}): {resp.text[:200]}"
            )
            # Rate limited / quota exhausted — honor Retry-After then back off.
            if (resp.status_code == 429 or "RESOURCE_EXHAUSTED" in resp.text) and attempt < _MAX_ATTEMPTS - 1:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else _GEMINI_BACKOFFS[attempt]
                except (TypeError, ValueError):
                    delay = _GEMINI_BACKOFFS[attempt]
                logger.warning(f"Gemini price research: rate limited — backing off {delay}s")
                time.sleep(delay)
                continue
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

        # Extract grounding sources. Vertex has shipped these under a few
        # different shapes; check all known locations so issue #8's
        # justification gets the real URLs the price was grounded on.
        grounding = candidate.get("groundingMetadata", {}) or {}
        seen_urls: set[str] = set()

        def _add_src(title: str, url: str) -> None:
            if url and url not in seen_urls:
                seen_urls.add(url)
                result.sources.append({"title": title or "", "url": url})

        for chunk in grounding.get("groundingChunks", []) or []:
            web = (chunk.get("web") or {}) if isinstance(chunk, dict) else {}
            _add_src(web.get("title", ""), web.get("uri", ""))
        # Alternate/legacy shapes
        for attr in grounding.get("groundingAttributions", []) or []:
            web = (attr.get("web") or {}) if isinstance(attr, dict) else {}
            _add_src(web.get("title", ""), web.get("uri", "") or web.get("url", ""))
        # citationMetadata shape (cites the URLs a passage was drawn from)
        cm = candidate.get("citationMetadata", {}) or {}
        for c in (cm.get("citationSources") or cm.get("citations") or []):
            if isinstance(c, dict):
                _add_src(c.get("title", ""), c.get("uri", "") or c.get("url", ""))
        # searchEntryPoint.renderedContent embeds source <a href="..."> links
        sep = grounding.get("searchEntryPoint") or {}
        rendered = sep.get("renderedContent", "") if isinstance(sep, dict) else ""
        if rendered:
            for href in re.findall(r'href="(https?://[^"]+)"', rendered):
                _add_src("", href)
        for q in grounding.get("webSearchQueries", []) or []:
            result.raw_snippets.append(f"search_query: {q}")

        if not result.sources:
            # Surface the actual metadata keys so we can map the real shape
            # from the next health-check without guessing.
            logger.warning(
                f"Gemini price research: price found but 0 sources captured; "
                f"groundingMetadata keys={list(grounding.keys())}"
            )

        parsed = _extract_json_from_text(text)
        if parsed and parsed.get("new_price"):
            try:
                price = float(parsed["new_price"])
            except (TypeError, ValueError):
                logger.warning(f"Gemini price research: unparsable price: {parsed['new_price']}")
                continue

            cat_key = category.lower().strip()
            lo, hi = _sanity_band_for(cat_key, currency)
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
# Perplexity Sonar (v6.3) — second independent grounded new-price source
# ---------------------------------------------------------------------------
def sonar_price_research(
    *,
    brand: str,
    model: str,
    category: str,
    country: str = "KE",
    size_value: float | None = None,
    size_unit: str | None = None,
) -> InternetPriceResult:
    """Look up the NEW retail price via the Perplexity Sonar API (live web
    search with citations). Used as an independent cross-check on Gemini —
    single-source lookups were the main cause of new-price noise (same model
    priced 35,500 one day and 63,999 the next).

    Requires PERPLEXITY_API_KEY; silently returns an empty result when it is
    not configured, so the system degrades to Gemini-only."""
    result = InternetPriceResult()
    country_name = _COUNTRY_NAMES.get(country, "Kenya")
    currency = _COUNTRY_CURRENCIES.get(country, "KES")
    result.currency = currency

    try:
        from app.config import get_settings
        api_key = getattr(get_settings(), "perplexity_api_key", None) or ""
    except Exception:  # noqa: BLE001
        api_key = ""
    if not api_key:
        return result

    size_hint = _format_size_hint(size_value, size_unit)
    prompt = (
        f"Find the current NEW retail price in {country_name} of this exact "
        f"appliance: Brand: {brand or 'unknown'}; Model: {model or 'unknown'}; "
        f"Size/capacity: {size_hint or 'unknown'}; Category: {category}. "
        f"Check mainstream {country_name} retailers (Jumia, Kilimall, brand "
        f"stores, electronics shops). The size/capacity MUST match exactly. "
        f"Respond with ONLY a JSON object: "
        f'{{"new_price": <number in {currency}>, "currency": "{currency}", '
        f'"matched_product": "<exact product you priced>"}}. '
        f'If no price for this specific model/size exists, return {{"new_price": null}}.'
    )

    try:
        import requests as _req
        resp = _req.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "sonar",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 500,
            },
            timeout=45,
        )
        if resp.status_code != 200:
            logger.warning(f"Sonar price research: HTTP {resp.status_code}: {resp.text[:200]}")
            return result
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        parsed = _extract_json_from_text(text) or {}
        price = parsed.get("new_price")
        if price is None:
            return result
        price = float(str(price).replace(",", ""))
        lo, hi = _sanity_band_for(category.lower().strip(), currency)
        if not (lo <= price <= hi):
            logger.warning(
                f"Sonar price research: {price} {currency} outside sanity band "
                f"[{lo:,.0f}, {hi:,.0f}] for {category} — discarded"
            )
            return result
        result.launch_price = price
        result.confidence = 60.0
        for url in (data.get("citations") or [])[:6]:
            result.sources.append({"title": "sonar_citation", "url": str(url), "price": str(price)})
        if parsed.get("matched_product"):
            result.raw_snippets.append(f"sonar_matched: {parsed['matched_product']}")
        logger.info(f"Sonar price research: {price} {currency} for {brand} {model}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Sonar price research failed: {e}")
    return result


def combine_new_price_signals(
    gemini_price: float | None, sonar_price: float | None
) -> tuple[float | None, str]:
    """Merge two independent grounded new-price lookups into one estimate.

    - Both agree (within 40%): average them — two independent confirmations.
    - Both present but conflicting: take the LOWER one. Wrong-variant errors
      are almost always inflations (premium bundle/washer-dryer priced instead
      of the base model), and the matrix cross-check downstream still corrects
      a too-low pick when curated data exists.
    - One present: use it. Neither: None.

    Pure function — unit-testable.
    """
    g = float(gemini_price) if gemini_price and gemini_price > 0 else None
    s = float(sonar_price) if sonar_price and sonar_price > 0 else None
    if g and s:
        if max(g, s) / min(g, s) <= 1.4:
            return round((g + s) / 2.0, 2), "gemini+sonar agree"
        return min(g, s), "gemini/sonar conflict -> lower"
    if g:
        return g, "gemini only"
    if s:
        return s, "sonar only"
    return None, "none"


# ---------------------------------------------------------------------------
# Public entry point (v6.3 — Gemini primary + Sonar cross-check)
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
    """Search for the NEW retail price using two independent grounded sources
    (Gemini Google-Search grounding + Perplexity Sonar) and merge them."""
    gemini = gemini_price_research(
        brand=brand, model=model, category=category, country=country,
        size_value=size_value, size_unit=size_unit,
    )
    sonar = sonar_price_research(
        brand=brand, model=model, category=category, country=country,
        size_value=size_value, size_unit=size_unit,
    )

    combined, how = combine_new_price_signals(gemini.launch_price, sonar.launch_price)
    if combined is None:
        return gemini  # preserves gemini's currency/snippets even when empty

    # Base the result on the richer Gemini payload, overridden with the merged
    # price + the union of sources for full transparency.
    result = gemini if gemini.launch_price else sonar
    result.launch_price = combined
    result.sources = (gemini.sources or []) + (sonar.sources or [])
    if gemini.launch_price and sonar.launch_price:
        result.confidence = min(95.0, max(gemini.confidence, sonar.confidence) + 15.0)
        result.raw_snippets.append(
            f"dual_source: gemini={gemini.launch_price} sonar={sonar.launch_price} -> {combined} ({how})"
        )
    logger.info(f"New-price lookup [{how}]: {combined} for {brand} {model}")
    return result


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
