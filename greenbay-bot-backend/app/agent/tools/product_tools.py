"""Agent tools for Product Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *

@tool
@traceable(name="search_product")
def search_product(query: str, limit: int = 5) -> Dict[str, Any]:
    """
    Search for products using semantic search in Qdrant.
    
    IMPORTANT: This tool fetches up to 30 results but returns only the first 5 for display.
    All results are cached for pagination. When user asks for "more", use show_more_products() instead.
    
    WORKFLOW:
    1. Validate query is relevant to electronics/home appliances (GreenBay Market only sells these)
    2. Search Qdrant for products matching query (fetches 30, shows 5)
    3. Use LLM to validate and filter results to ensure relevance
    4. Format results: Name, Price, Vendor, Stock, Condition
    5. Filter by condition if user specified (e.g., "used" → only Pre-Loved/Refurbished)
    6. Highlight low stock items (<3 units)
    7. Cache all results for pagination
    8. Wait for user to select specific item or ask for more
    
    WHEN TO USE THIS TOOL:
    - User is searching for NEW products (e.g., "do you have freezers?", "looking for washing machine")
    - User specifies a brand/category (e.g., "I want from Ramtons only", "samsung only")
    - This is the FIRST search for a query
    
    WHEN NOT TO USE THIS TOOL:
    - User asks for "more" after seeing results → Use show_more_products() instead
    - User asks "only 3?" or "any more?" → Use show_more_products() instead
    - User is asking a question about a product → Use get_product_details() instead
    
    Args:
        query: Search query (e.g., "iPhone 13", "laptop", "4K TV", "Ramtons freezers", "used cooker")
        limit: Number of results to show initially (default 5, but 30 are fetched and cached)
        
    CONDITION FILTERING:
    The tool automatically filters by condition if user specifies:
    - "used", "pre-loved", "second hand" → Shows only Pre-Loved & Certified Refurbished products
    - "brand new", "new", "sealed" → Shows only Brand New products
    - "refurbished", "certified refurbished" → Shows only Certified Refurbished products
        
    RESPONSE FORMATTING (Generate engaging, personalized responses):
    - ALWAYS directly answer the user's question first, then provide results
    - Check the "has_exact_match" field in the result:
      a. If has_exact_match=False and user asked for specific size/model (e.g., "130L fridge"):
         - FIRST say: "I don't have exactly [what they asked for], but I have some close alternatives:"
         - THEN show the closest matches with their actual sizes clearly visible
         - Explain why they're good alternatives (e.g., "138L is very close to 130L and offers similar storage capacity")
      b. If has_exact_match=True:
         - Say: "Yes! Here are the [what they asked for] options we have:"
    - If user asks "do you have X?" or "you don't have X?":
      a. Answer directly: "No, we don't have [exact X], but we have [alternatives]" OR "Yes, we have [X]!"
      b. Be honest and transparent - don't just show similar products without acknowledging the difference
      c. Always acknowledge what they asked for before showing alternatives
    - Start with an engaging, context-aware opening (e.g., "Great choice! Here are some excellent freezers..." instead of "Here are some freezers")
    - Mention the total count if more than 5: "I found X products for you. Here are the top 5:"
    - If has_more is True, mention: "I have more options available - just ask if you'd like to see them!"
    - Format each product exactly like this:
      "1. *Samsung Air Fryer* (Brand New)
      • Price: KES 12,500
      • Stock: 5 units  
      • Link: https://greenbay.market/products/samsung-air-fryer-xyz"
    - Use *single asterisk* for bold product names
    - ALWAYS include condition in parentheses after product name (e.g., "(Brand New)", "(Pre-Loved)", "(Certified Refurbished)")
    - If condition is not available, omit it (don't show "(None)")
    - Format prices as "KES X,XXX" with commas
    - ALWAYS include the product URL
    - For low stock (<3): add "Only X left!"
    - End with a helpful call-to-action: "Which one catches your eye?" or "Would you like to see more options?"
        
    Returns:
        Dictionary with:
        - success: bool
        - products: List of first 5 products (formatted with condition from metafields)
        - total_found: Total number of products found (may be > 5)
        - has_more: Whether more results are available
        - query: The search query used
        
    Note: Products include condition field extracted from metafields (Brand New, Pre-Loved, etc.)
    """
    try:
        from app.services.qdrant_service import qdrant_service
        from app.services.search_cache import search_cache
        from app.config import get_settings
        from langchain_openai import ChatOpenAI
        import asyncio
        import json
        import re
        
        settings = get_settings()
        
        # STEP 1: Quick validation - check if query is obviously not relevant (furniture, food, etc.)
        # Use simple keyword check first to avoid slow LLM calls
        query_lower = query.lower()
        non_relevant_keywords = ['chair', 'table', 'sofa', 'bed', 'furniture', 'food', 'clothing', 'clothes', 'shoes', 'book']
        if any(keyword in query_lower for keyword in non_relevant_keywords):
            return {
                "success": False,
                "message": f"I'm sorry, but GreenBay Market specializes in electronics and home appliances only. {query} is not available in our store. Would you like to search for electronics or home appliances instead?",
                "products": [],
                "query": query
            }
        
        # STEP 1b: Optional LLM validation (with timeout) - only for ambiguous queries
        # Skip LLM validation for common electronics/appliance terms to speed up
        common_terms = ['fridge', 'refrigerator', 'freezer', 'washing', 'machine', 'tv', 'television', 'laptop', 'phone', 'tablet', 'gaming', 'stove', 'cooker', 'microwave', 'air fryer', 'oven']
        needs_validation = not any(term in query_lower for term in common_terms)
        
        if needs_validation:
            try:
                llm_validator = ChatOpenAI(
                    model="gpt-4o-mini",
                    temperature=0.1,
                    api_key=settings.openai_api_key,
                    timeout=5,  # 5 second timeout
                    max_retries=1
                )
                
                validation_prompt = f"""Is "{query}" relevant to electronics or home appliances? Return ONLY JSON: {{"is_relevant": true/false, "reason": "brief"}}"""
                
                validation_response = llm_validator.invoke(validation_prompt)
                validation_text = validation_response.content
                
                json_match = re.search(r'\{.*\}', validation_text, re.DOTALL)
                if json_match:
                    validation_result = json.loads(json_match.group())
                    if not validation_result.get("is_relevant", True):
                        reason = validation_result.get("reason", "This item is not available")
                        return {
                            "success": False,
                            "message": f"I'm sorry, but GreenBay Market specializes in electronics and home appliances only. {reason}. Would you like to search for something else?",
                            "products": [],
                            "query": query
                        }
            except Exception as e:
                logger.warning(f"Query validation failed (timeout?), proceeding with search: {e}")
                # Continue with search if validation fails
        
        # STEP 2: Fetch more results than needed (30) to have enough for pagination
        all_results = asyncio.run(qdrant_service.search_products(query, limit=30, use_reranking=True))
        
        if not all_results:
            return {
                "success": False,
                "message": f"No products found for '{query}'. Would you like to try a different search term?",
                "products": []
            }
        
        # STEP 3: Format ALL results for display (filter out out-of-stock items)
        all_formatted_products = []
        for result in all_results:
            payload = result.get("payload", {})
            
            # Extract price from variants (Shopify format)
            price = 0.0
            stock = 0
            if payload.get("variants") and len(payload["variants"]) > 0:
                variant = payload["variants"][0]
                price = float(variant.get("price", 0))
                stock = variant.get("inventory_quantity", 0)
            
            # Skip out-of-stock products
            if stock <= 0:
                continue
            
            # Extract product condition from metafields
            # Check multiple possible metafield keys (Shopify uses different keys)
            condition = None
            metafields = payload.get("metafields", [])
            for metafield in metafields:
                namespace = metafield.get("namespace", "")
                key = metafield.get("key", "")
                
                # Check all possible condition metafield keys
                if ((namespace == "custom" and key == "product_condition_badge") or
                    (namespace == "custom" and key == "custom_badge") or
                    (namespace == "wk_custom_field" and key == "cus_product_condition")):
                    condition = metafield.get("value", "").strip()
                    break
            
            all_formatted_products.append({
                "id": result.get("id"),
                "name": payload.get("title", "Unknown Product"),
                "price": price,
                "vendor": payload.get("vendor", "Unknown Vendor"),
                "stock": stock,
                "condition": condition,  # Brand New, Pre-Loved, Certified Refurbished, etc.
                "score": result.get("score", 0.0),
                "product_type": payload.get("product_type", ""),
                "handle": payload.get("handle", ""),
                "description": payload.get("body_html", "")[:200] if payload.get("body_html") else ""
            })
        
        if not all_formatted_products:
            return {
                "success": False,
                "message": f"No in-stock products found for '{query}'. Would you like to try a different search term?",
                "products": []
            }
        
        # STEP 4: Use LLM to validate and filter results for relevance (with timeout)
        # Only do this if we have many results and query might be ambiguous
        if len(all_formatted_products) > 10:
            try:
                llm_filter = ChatOpenAI(
                    model="gpt-4o-mini",
                    temperature=0.1,
                    api_key=settings.openai_api_key,
                    timeout=8,  # 8 second timeout
                    max_retries=1
                )
                
                # Prepare product list for LLM validation (only top 15 to keep prompt short)
                products_for_validation = []
                for i, product in enumerate(all_formatted_products[:15]):
                    products_for_validation.append({
                        "index": i,
                        "name": product["name"],
                        "product_type": product.get("product_type", "")
                    })
                
                filter_prompt = f"""User searched: "{query}". Which products are relevant? Return ONLY JSON: {{"relevant_indices": [0,2,5]}}"""
                
                filter_response = llm_filter.invoke(filter_prompt)
                filter_text = filter_response.content
                
                # Parse JSON response
                json_match = re.search(r'\{.*\}', filter_text, re.DOTALL)
                if json_match:
                    filter_result = json.loads(json_match.group())
                    relevant_indices = set(filter_result.get("relevant_indices", []))
                    
                    # Filter products based on LLM validation
                    if relevant_indices:
                        filtered_products = []
                        for i, product in enumerate(all_formatted_products):
                            if i < 15:  # Only filter the first 15 that were validated
                                if i in relevant_indices:
                                    filtered_products.append(product)
                            else:
                                # Include products beyond validation range
                                filtered_products.append(product)
                        
                        all_formatted_products = filtered_products
                        
                        # If no relevant products after filtering
                        if not all_formatted_products:
                            return {
                                "success": False,
                                "message": f"I couldn't find products that match '{query}'. Would you like to try a different search term?",
                                "products": []
                            }
            except Exception as e:
                logger.warning(f"LLM filtering failed (timeout?), using all results: {e}")
                # Continue with all results if filtering fails
        
        # STEP 5: Filter by condition if user specified (used, pre-loved, brand new, etc.)
        query_lower = query.lower()
        requested_condition = None
        
        # Detect condition keywords in query
        if any(keyword in query_lower for keyword in ['used', 'pre-loved', 'preloved', 'pre loved', 'second hand', 'secondhand']):
            requested_condition = "Pre-Loved"
        elif any(keyword in query_lower for keyword in ['brand new', 'brandnew', 'new', 'sealed']):
            requested_condition = "Brand New"
        elif any(keyword in query_lower for keyword in ['refurbished', 'certified refurbished', 'restored']):
            requested_condition = "Certified Refurbished"
        
        # Filter products by condition if user specified
        if requested_condition:
            filtered_by_condition = []
            for product in all_formatted_products:
                product_condition = product.get("condition", "")
                
                # Match the requested condition
                if requested_condition == "Pre-Loved":
                    # Include Pre-Loved and Certified Refurbished (both are used)
                    if product_condition in ["Pre-Loved", "Certified Refurbished"]:
                        filtered_by_condition.append(product)
                elif requested_condition == "Brand New":
                    # Only brand new products
                    if product_condition == "Brand New":
                        filtered_by_condition.append(product)
                elif requested_condition == "Certified Refurbished":
                    # Only certified refurbished
                    if product_condition == "Certified Refurbished":
                        filtered_by_condition.append(product)
            
            # Use filtered list if we found matching products
            if filtered_by_condition:
                all_formatted_products = filtered_by_condition
                logger.info(f"Filtered to {len(filtered_by_condition)} products matching condition: {requested_condition}")
            else:
                # No products match the requested condition - keep all and let agent explain
                logger.info(f"No products found matching condition: {requested_condition}, showing all results")
        
        # STEP 6: Check for exact match vs close match
        # Extract numeric values from query (e.g., "130L" -> 130)
        import re
        query_numbers = re.findall(r'\d+', query)
        has_exact_match = False
        if query_numbers:
            # Check if any product name contains the exact number
            target_number = query_numbers[0]  # Use first number found
            for product in all_formatted_products:
                if target_number in product["name"]:
                    has_exact_match = True
                    break
        
        # STEP 7: Return only first batch for initial display
        first_batch = all_formatted_products[:limit]
        has_more = len(all_formatted_products) > limit
        
        # Store ALL results in search cache for pagination
        # Set offset to the number of products we're showing now
        try:
            user_phone = _get_current_user_phone()
            if user_phone:
                # Store all results with offset=limit (we're showing 'limit' products now)
                search_cache.set_results(user_phone, all_formatted_products, query=query, offset=len(first_batch))
        except Exception:
            # Cache is best-effort; do not block on errors
            pass
        
        return {
            "success": True,
            "message": f"Found {len(all_formatted_products)} in-stock products",
            "products": first_batch,  # Show first batch only
            "total_found": len(all_formatted_products),  # Total available
            "has_more": has_more,  # Whether more results available
            "query": query,
            "has_exact_match": has_exact_match  # Whether exact match was found
        }
        
    except Exception as e:
        logger.error(f"Error in search_product tool: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        # CRITICAL: Always return a result, never raise an exception
        # This ensures the tool call completes and returns a ToolMessage
        return {
            "success": False,
            "message": f"I encountered an error while searching. Please try again with a different search term.",
            "error": str(e),
            "products": [],
            "query": query if 'query' in locals() else ""
        }


@tool
@traceable(name="select_product_from_last_search")
def select_product_from_last_search(position: int) -> Dict[str, Any]:
    """
    Select a product by position (1-based) from the user's most recent search results.

    This prevents LLM ordinal mapping mistakes by using cached results saved by search_product.

    Args:
        position: 1-based index corresponding to what the user said (e.g., 5 for "5th one")

    Returns:
        Product info dict or an error if unavailable.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        if position < 1:
            return {"success": False, "message": "Position must be 1 or greater"}

        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Could not determine user phone"}

        products = search_cache.get_results(user_phone)
        if not products:
            return {
                "success": False,
                "message": "No recent search results found. Please search again."
            }

        if position > len(products):
            return {
                "success": False,
                "message": f"Only {len(products)} products available. Please choose between 1 and {len(products)}."
            }

        product = products[position - 1]
        return {
            "success": True,
            "product": product,
            "message": f"Selected product {position}: {product.get('name')}"
        }
    except Exception as e:
        logger.error(f"Error selecting product from last search: {e}")
        return {"success": False, "message": "Failed to select product", "error": str(e)}




@tool
@traceable(name="select_last_product_from_last_search")
def select_last_product_from_last_search() -> Dict[str, Any]:
    """
    Select the last product from the user's most recent search results.

    Returns:
        Product info dict or an error if unavailable.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Could not determine user phone"}

        products = search_cache.get_results(user_phone)
        if not products:
            return {
                "success": False,
                "message": "No recent search results found. Please search again."
            }

        product = products[-1]
        position = len(products)
        return {
            "success": True,
            "product": product,
            "message": f"Selected last product {position}: {product.get('name')}"
        }
    except Exception as e:
        logger.error(f"Error selecting last product from last search: {e}")
        return {"success": False, "message": "Failed to select last product", "error": str(e)}




@tool
@traceable(name="show_more_products")
def show_more_products(batch_size: int = 5) -> Dict[str, Any]:
    """
    Show more products from the last search results (pagination).
    
    CRITICAL: Use this tool when user asks for MORE products after seeing initial search results.
    This ensures consistent, non-duplicate results by continuing from cached search.
    
    WHEN TO USE THIS TOOL:
    - User says: "more", "do you have more?", "show more", "any more?", "only 3?", "only these 3?"
    - User asks for additional results after seeing initial search
    - User seems to want to see additional options
    
    WHEN NOT TO USE THIS TOOL:
    - User is doing a NEW search → Use search_product() instead
    - No previous search exists → Use search_product() instead
    - User is asking about a specific product → Use get_product_details() instead
    
    Args:
        batch_size: Number of products to show (default 5)
        
    RESPONSE FORMATTING (Generate engaging, accurate responses):
    - If has_more is True: "Here are {count} more options for you:" (engaging, not just "Here are more products")
    - If has_more is False: "You've now seen all {total} products we have in stock. That's everything we have available!"
    - Always mention progress: "Showing {shown} of {total} products" when appropriate
    - Format products same as search_product tool
    - Be accurate: If only 3 total products exist, don't say "here are more" - say "That's all we have!"
    - Use engaging language: "Here are some additional great options:" instead of "Here are more products"
        
    Returns:
        Dictionary with:
        - success: bool
        - products: List of next batch products (empty if all shown)
        - has_more: Whether more results are available
        - total: Total number of products in the search
        - shown: Number of products shown so far
        - query: The original search query
    """
    try:
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {
                "success": False,
                "message": "Could not determine user phone",
                "products": []
            }
        
        # Get next batch from cache
        batch_info = search_cache.get_next_batch(user_phone, batch_size)
        
        if not batch_info.get("results"):
            # No more results or no cached search
            if batch_info.get("total", 0) == 0:
                return {
                    "success": False,
                    "message": "No recent search found. Please search for products first.",
                    "products": []
                }
            else:
                # All results have been shown
                return {
                    "success": True,
                    "message": f"You've seen all {batch_info.get('total', 0)} products from your search.",
                    "products": [],
                    "has_more": False,
                    "total": batch_info.get("total", 0),
                    "shown": batch_info.get("shown", 0)
                }
        
        products = batch_info["results"]
        has_more = batch_info.get("has_more", False)
        total = batch_info.get("total", 0)
        shown = batch_info.get("shown", 0)
        query = batch_info.get("query", "")
        
        return {
            "success": True,
            "message": f"Here are {len(products)} more products" + (f" (showing {shown} of {total})" if total > 0 else ""),
            "products": products,
            "has_more": has_more,
            "total": total,
            "shown": shown,
            "query": query
        }
        
    except Exception as e:
        logger.error(f"Error showing more products: {e}")
        return {
            "success": False,
            "message": "Failed to load more products",
            "error": str(e),
            "products": []
        }




@tool
@traceable(name="filter_last_search_by_budget")
def filter_last_search_by_budget(max_price_kes: float) -> Dict[str, Any]:
    """
    Filter the last shown search results by a maximum price and update the cached results.

    This should be used when the user says things like "my budget is less than 30k".

    Args:
        max_price_kes: Maximum price in KES

    Returns:
        Dictionary with filtered products; also updates the last search cache for correct ordinal selection.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        if max_price_kes <= 0:
            return {"success": False, "message": "Budget must be greater than 0"}

        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Could not determine user phone"}

        products = search_cache.get_results(user_phone)
        if not products:
            return {
                "success": False,
                "message": "No recent search results found to filter. Please search again."
            }

        filtered = [p for p in products if (p.get("price") or 0) <= max_price_kes]

        # If nothing matches, keep original to avoid empty cache
        if filtered:
            try:
                search_cache.set_results(user_phone, filtered)
            except Exception:
                pass

        return {
            "success": True,
            "message": f"Found {len(filtered)} product(s) under KES {max_price_kes:,.0f}",
            "products": filtered,
            "max_price": max_price_kes
        }
    except Exception as e:
        logger.error(f"Error filtering last search by budget: {e}")
        return {"success": False, "message": "Failed to filter by budget", "error": str(e)}



@tool
@traceable(name="find_upsell_product")
def find_upsell_product(product_name: str, product_price: float) -> Dict[str, Any]:
    """
    Find an upsell product that costs more than the selected product.
    
    WORKFLOW:
    1. Use LLM to identify the product's main category
    2. Search for products in that category
    3. Filter for products with higher price than selected product
    4. If no results, search again with larger limit (scrolling technique)
    5. Return the best upsell product (highest price but reasonable upgrade)
    
    Args:
        product_name: Name of the product user added to cart (e.g., "Samsung 4K TV")
        product_price: Price of the product in KES (e.g., 90000)
        
    Returns:
        Dictionary with upsell product:
        {
            "success": bool,
            "upsell_product": {
                "id": str,
                "name": str,
                "price": float,
                "vendor": str,
                "stock": int
            },
            "price_difference": float,
            "upsell_reason": str,
            "message": str
        }
        IMPORTANT: If success=False, the message will be empty or generic. NEVER mention to the user that no upsell was found - just silently skip it and proceed with normal response.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        logger.info(f"🛍️ find_upsell_product tool called: product_name={product_name}, product_price={product_price}")
        
        from openai import OpenAI
        from app.config import get_settings
        import asyncio
        
        settings = get_settings()
        client = OpenAI(api_key=settings.openai_api_key)
        
        # Step 1: Identify category using LLM
        category_prompt = f"""Identify the MAIN PRODUCT CATEGORY for this product: "{product_name}"

The category should be a single, broad term that would be used for product search (e.g., "TV", "laptop", "refrigerator", "speaker", "phone", "gaming mouse", "solar torch").

Return ONLY the category name, nothing else. Example responses:
- "Samsung 4K TV" -> "TV"
- "MacBook Pro" -> "laptop"
- "iPhone 13" -> "phone"
- "Logitech Mouse" -> "mouse" or "gaming mouse"
"""
        
        category_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": category_prompt}],
            temperature=0.1,
            max_tokens=50
        )
        
        category = category_response.choices[0].message.content.strip().lower()
        logger.info(f"Identified category '{category}' for product '{product_name}'")
        
        # Step 2: Search for products in this category (using raw category from LLM)
        search_query = category
        
        upsell_products = []
        search_limit = 20  # Start with larger limit
        
        try:
            results = asyncio.run(qdrant_service.search_products(search_query, limit=search_limit))
            
            for result in results:
                payload = result.get("payload", {})
                
                # Extract price from variants
                price = 0.0
                stock = 0
                if payload.get("variants") and len(payload["variants"]) > 0:
                    variant = payload["variants"][0]
                    price = float(variant.get("price", 0))
                    stock = variant.get("inventory_quantity", 0)
                
                # Only consider products with higher price and in stock
                # Also exclude the same product (by name similarity)
                product_title = payload.get("title", "Unknown Product")
                if price > product_price and stock > 0:
                    # Don't suggest the same product
                    if product_title.lower() != product_name.lower():
                        # Don't go too high (reasonable upgrade, max 50% more)
                        max_reasonable_price = product_price * 1.5
                        if price <= max_reasonable_price:
                            upsell_products.append({
                                "id": result.get("id"),
                                "name": product_title,
                                "price": price,
                                "vendor": payload.get("vendor", "Unknown Vendor"),
                                "stock": stock,
                                "score": result.get("score", 0.0)
                            })
                        
                        # Stop if we have enough candidates
                        if len(upsell_products) >= 5:
                            break
                            
        except Exception as e:
            logger.warning(f"Error searching with query '{search_query}': {e}")
        
        # If no results found, try scrolling with even larger limit
        if not upsell_products:
            logger.info(f"No upsell products found, trying with scroll technique (limit=50)")
            try:
                results = asyncio.run(qdrant_service.search_products(category, limit=50))
                
                for result in results:
                    payload = result.get("payload", {})
                    
                    price = 0.0
                    stock = 0
                    if payload.get("variants") and len(payload["variants"]) > 0:
                        variant = payload["variants"][0]
                        price = float(variant.get("price", 0))
                        stock = variant.get("inventory_quantity", 0)
                    
                    product_title = payload.get("title", "Unknown Product")
                    if price > product_price and stock > 0:
                        # Don't suggest the same product
                        if product_title.lower() != product_name.lower():
                            max_reasonable_price = product_price * 1.5
                            if price <= max_reasonable_price:
                                upsell_products.append({
                                    "id": result.get("id"),
                                    "name": product_title,
                                    "price": price,
                                    "vendor": payload.get("vendor", "Unknown Vendor"),
                                    "stock": stock,
                                    "score": result.get("score", 0.0)
                                })
            except Exception as e:
                logger.warning(f"Error with scroll search: {e}")
        
        # If no results found, return failure silently (no message to user)
        if not upsell_products:
            logger.info(f"✗ find_upsell_product: No suitable upsell products found for {product_name} (price: KES {product_price:,})")
            return {
                "success": False,
                "upsell_product": None,
                "price_difference": 0.0,
                "upsell_reason": "",
                "message": ""  # Empty message - never mention to user that no upsell was found
            }
        
        # Step 3: Select best upsell product (highest price but reasonable upgrade)
        # Sort by price and pick one that's 10-30% more expensive (sweet spot)
        upsell_products.sort(key=lambda x: x["price"])
        
        target_price_range = (product_price * 1.1, product_price * 1.3)
        best_upsell = None
        
        # Try to find product in 10-30% range first
        for product in reversed(upsell_products):
            if target_price_range[0] <= product["price"] <= target_price_range[1]:
                best_upsell = product
                break
        
        # If no product in ideal range, use the cheapest upgrade (10%+ more)
        if not best_upsell:
            for product in upsell_products:
                if product["price"] >= product_price * 1.1:
                    best_upsell = product
                    break
        
        # If still none, use the first (cheapest) higher-priced option
        if not best_upsell and upsell_products:
            best_upsell = upsell_products[0]
        
        # Final check - if still no best_upsell, return failure
        if not best_upsell:
            logger.info(f"✗ find_upsell_product: No suitable upsell products found for {product_name} (price: KES {product_price:,})")
            return {
                "success": False,
                "upsell_product": None,
                "price_difference": 0.0,
                "upsell_reason": "",
                "message": ""  # Empty message - never mention to user that no upsell was found
            }
        
        # Step 4: Generate upsell reason using LLM
        reason_prompt = f"""Compare these two products and provide a brief (1-2 sentences) reason why the second is a better option:

Product 1: {product_name} - KES {product_price:,.0f}
Product 2: {best_upsell['name']} - KES {best_upsell['price']:,.0f}

Focus on specific benefits/features (e.g., better quality, more features, newer model, higher resolution, etc.).
Keep it concise and natural, like you're explaining to a customer.
"""
        
        reason_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": reason_prompt}],
            temperature=0.3,
            max_tokens=100
        )
        
        upsell_reason = reason_response.choices[0].message.content.strip()
        price_difference = best_upsell["price"] - product_price
        
        logger.info(f"Found upsell: {best_upsell['name']} (KES {best_upsell['price']:,}) for {product_name} (KES {product_price:,})")
        
        logger.info(f"✓ find_upsell_product SUCCESS: Found {best_upsell['name']} as upsell option")
        
        return {
            "success": True,
            "upsell_product": {
                "id": str(best_upsell["id"]),
                "name": best_upsell["name"],
                "price": best_upsell["price"],
                "vendor": best_upsell["vendor"],
                "stock": best_upsell["stock"]
            },
            "price_difference": price_difference,
            "upsell_reason": upsell_reason,
            "message": f"Found premium option: {best_upsell['name']} at KES {best_upsell['price']:,.0f} (KES {price_difference:,.0f} more)"
        }
        
    except Exception as e:
        logger.error(f"✗ Error finding upsell product: {e}")
        return {
            "success": False,
            "upsell_product": None,
            "price_difference": 0.0,
            "upsell_reason": "",
            "message": ""  # Empty message - never mention to user that no upsell was found
        }


@tool
@traceable(name="find_cross_sell_product")
def find_cross_sell_product(product_name: str, main_product_price: Optional[float] = None) -> Dict[str, Any]:
    """
    Find a complimentary product that goes well with the main product.
    
    WHEN TO CALL THIS:
    - Call ONCE after `calculate_checkout_total_with_delivery(location)` during checkout
    - ONLY call if cross-sell has NOT been offered yet in this checkout session
    - If cart already has 2+ items, skip calling this (cross-sell likely already added)
    - NEVER call this tool multiple times in the same checkout flow
    
    WORKFLOW:
    1. Use LLM to identify what type of complimentary product would be useful (e.g., "smartphone" → "charger", "adapter", "earphone")
    2. Search Qdrant for products in that complimentary category
    3. Return the first suitable product found that is CHEAPER than the main product (price must be less than main_product_price)
    4. This is for cross-selling, not upselling - focus on accessories/complementary items
    5. If no suitable product is found, return success=False silently (don't mention it to the user)
    
    EXAMPLES:
    - Smartphone → Fast charging adapter, Earphone, Phone case
    - Laptop → Laptop bag, Mouse, USB hub
    - TV → HDMI cable, TV stand, Soundbar
    - Washing machine → Detergent, Fabric softener
    
    Args:
        product_name: Name of the main product (e.g., "Samsung Galaxy S21", "MacBook Pro")
        main_product_price: Optional price of the main product. If provided, cross-sell product must be cheaper than this.
        
    Returns:
        Dictionary with cross-sell product:
        {
            "success": bool,
            "cross_sell_product": {
                "id": str,
                "name": str,
                "price": float,
                "vendor": str,
                "stock": int,
                "url": str
            },
            "reason": str,  # Why this product complements the main product
            "message": str
        }
        If success=False, the message should be empty or generic - don't mention that no product was found.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        logger.info(f"🛒 find_cross_sell_product tool called: product_name={product_name}")
        
        from openai import OpenAI
        import asyncio
        
        settings = get_settings()
        client = OpenAI(api_key=settings.openai_api_key)
        
        # Get main product's category/type to avoid suggesting same category
        main_product_category = None
        try:
            # Try to get product from cart or search to find its category
            user_phone = _get_current_user_phone()
            if user_phone:
                cart_result = cart_service.get_cart(user_phone)
                if cart_result.get("success") and cart_result.get("cart_items"):
                    # Find the main product in cart by name
                    for item in cart_result["cart_items"]:
                        if product_name.lower() in item.get("product_name", "").lower() or item.get("product_name", "").lower() in product_name.lower():
                            # Try to get product details from Qdrant
                            try:
                                product_details = asyncio.run(qdrant_service.get_product_by_id(item.get("product_id", "")))
                                if product_details:
                                    main_product_category = product_details.get("payload", {}).get("product_type", "").lower()
                                    logger.info(f"Found main product category: {main_product_category}")
                                    break
                            except Exception:
                                pass
        except Exception as e:
            logger.debug(f"Could not determine main product category: {e}")
        
        # Step 1: Use LLM to identify complimentary product category
        cross_sell_prompt = f"""Given this product: "{product_name}"

Identify ONE type of COMPLIMENTARY/ACCESSORY product that would be useful with this product.
IMPORTANT: The complimentary product MUST be a DIFFERENT category/type than the main product.
For example:
- Dishwasher → detergent, rinse aid, dishwasher cleaner (NOT another dishwasher)
- Smartphone → charger, case, screen protector (NOT another smartphone)
- Laptop → bag, mouse, USB hub (NOT another laptop)

Return 2-3 complimentary product type/category names (1-2 words), nothing else. Don't repeat the same product type/category name.
Focus on practical accessories that enhance the main product's use.
"""
        
        category_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": cross_sell_prompt}],
            temperature=0.3,
            max_tokens=50
        )
        
        complimentary_category = category_response.choices[0].message.content.strip().lower()
        logger.info(f"Identified complimentary category '{complimentary_category}' for product '{product_name}'")
        
        # Step 2: Search Qdrant for products in this category
        search_queries = [
            complimentary_category,
            f"{complimentary_category} for {product_name.split()[0] if product_name.split() else ''}",  # e.g., "charger for samsung"
        ]
        
        cross_sell_product = None
        
        for search_query in search_queries:
            try:
                results = asyncio.run(qdrant_service.search_products(search_query, limit=20))  # Search more products to find cheaper ones
                
                for result in results:
                    payload = result.get("payload", {})
                    
                    # Get price and stock
                    price = 0.0
                    stock = 0
                    if payload.get("variants") and len(payload["variants"]) > 0:
                        variant = payload["variants"][0]
                        price = float(variant.get("price", 0))
                        stock = variant.get("inventory_quantity", 0)
                    
                    # Must be in stock and have valid price
                    if stock > 0 and price > 0:
                        # If main_product_price is provided, cross-sell product must be cheaper
                        if main_product_price is not None and price >= main_product_price:
                            continue  # Skip products that are equal or more expensive
                        
                        # CRITICAL: Never suggest same category product
                        cross_sell_product_type = payload.get("product_type", "").lower()
                        if main_product_category and cross_sell_product_type:
                            # Check if categories are similar (avoid same category)
                            if main_product_category == cross_sell_product_type:
                                logger.debug(f"Skipping cross-sell product '{payload.get('title')}' - same category as main product ({main_product_category})")
                                continue
                            # Also check if product title contains main product category keywords
                            product_title_lower = payload.get("title", "").lower()
                            if main_product_category in product_title_lower or any(keyword in product_title_lower for keyword in main_product_category.split() if len(keyword) > 3):
                                # Likely same category, skip
                                logger.debug(f"Skipping cross-sell product '{payload.get('title')}' - appears to be same category")
                                continue
                        
                        product_title = payload.get("title", "Unknown Product")
                        product_id = result.get("id")
                        
                        # Get product URL
                        from app.services.qdrant_service import build_product_url
                        handle = payload.get("handle", "")
                        product_url = build_product_url(handle) if handle else None
                        
                        cross_sell_product = {
                            "id": str(product_id),
                            "name": product_title,
                            "price": price,
                            "vendor": payload.get("vendor", "Unknown Vendor"),
                            "stock": stock,
                            "url": product_url
                        }
                        break  # Found a suitable product
                        
            except Exception as e:
                logger.warning(f"Error searching with query '{search_query}': {e}")
                continue
        
        if not cross_sell_product:
            # Silently skip - don't mention that no product was found
            logger.debug(f"No suitable cross-sell products found for {product_name} (cheaper than {main_product_price if main_product_price else 'N/A'})")
            return {
                "success": False,
                "cross_sell_product": None,
                "reason": "",
                "message": ""  # Empty message - silent skip
            }
        
        # Step 3: Generate reason using LLM
        reason_prompt = f"""Explain in 1 sentence why "{cross_sell_product['name']}" is a good complementary product for "{product_name}".

Keep it brief and practical. Example: "A fast charger ensures your phone charges quickly and efficiently."
"""
        
        reason_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": reason_prompt}],
            temperature=0.3,
            max_tokens=50
        )
        
        reason = reason_response.choices[0].message.content.strip()
        
        logger.info(f"✓ find_cross_sell_product SUCCESS: Found {cross_sell_product['name']} as cross-sell option")
        
        return {
            "success": True,
            "cross_sell_product": cross_sell_product,
            "reason": reason,
            "message": f"Found complementary product: {cross_sell_product['name']} at KES {cross_sell_product['price']:,.0f}"
        }
        
    except Exception as e:
        logger.error(f"✗ Error finding cross-sell product: {e}")
        return {
            "success": False,
            "cross_sell_product": None,
            "reason": "",
            "message": f"Failed to find cross-sell product: {str(e)}"
        }


@tool
@traceable(name="get_product_details")
def get_product_details(product_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific product.
    
    Args:
        product_id: Product identifier
        
    Returns:
        Dictionary with product details
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        import asyncio
        product = asyncio.run(qdrant_service.get_product_by_id(product_id))
        
        if not product:
            return {
                "success": False,
                "message": f"Product {product_id} not found"
            }
        
        payload = product.get("payload", {})
        
        # Extract price from variants (Shopify format)
        price = 0.0
        stock = 0
        if payload.get("variants") and len(payload["variants"]) > 0:
            variant = payload["variants"][0]
            price = float(variant.get("price", 0))
            stock = variant.get("inventory_quantity", 0)
        
        # Extract image URL from images (Shopify format)
        image_url = ""
        if payload.get("images") and len(payload["images"]) > 0:
            image_url = payload["images"][0].get("src", "")
        
        # Extract product condition from metafields
        # Check multiple possible metafield keys (Shopify uses different keys)
        condition = None
        metafields = payload.get("metafields", [])
        for metafield in metafields:
            namespace = metafield.get("namespace", "")
            key = metafield.get("key", "")
            
            # Check all possible condition metafield keys
            if ((namespace == "custom" and key == "product_condition_badge") or
                (namespace == "custom" and key == "custom_badge") or
                (namespace == "wk_custom_field" and key == "cus_product_condition")):
                condition = metafield.get("value", "").strip()
                break
        
        return {
            "success": True,
            "product": {
                "id": product.get("id"),
                "name": payload.get("title", "Unknown Product"),
                "price": price,
                "vendor": payload.get("vendor", "Unknown Vendor"),
                "stock": stock,
                "condition": condition,  # Brand New, Pre-Loved, Certified Refurbished, etc.
                "description": payload.get("body_html", ""),
                "category": payload.get("product_type", ""),
                "image_url": image_url,
                "handle": payload.get("handle", ""),
                "tags": payload.get("tags", "")
            }
        }
        
    except Exception as e:
        logger.error(f"Error in get_product_details tool: {e}")
        return {
            "success": False,
            "message": "Failed to get product details",
            "error": str(e)
        }


# Cart Management Tools



