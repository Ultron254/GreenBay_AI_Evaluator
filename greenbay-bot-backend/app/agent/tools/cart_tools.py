"""Agent tools for Cart Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *

@tool
@traceable(name="add_to_cart")
def add_to_cart(
    product_id: str,
    product_name: str,
    price: float,
    quantity: int = 1,
    retailer_id: Optional[str] = None,
    variant_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Add product to cart - MANDATORY UPSELLING AFTER THIS
    
    AFTER CALLING THIS (MANDATORY - DO NOT SKIP):
    
    STEP 1: UPSELL (Better version of same product)
    1. IMMEDIATELY call find_upsell_product(product_name, product_price)
    2. If tool returns success=True with upsell product:
       a. Present: "I found a better option - {upsell_name} for KES {upsell_price}. {upsell_reason}. Swap?"
       b. If user agrees ("yes", "swap", "ok"):
          - Save original_product_id (the one you just added)
          - Call add_to_cart() with upsell product details
          - Call remove_from_cart_by_product_id(original_product_id)
          - Call summarize_cart() to verify only upsell product remains
          - Tell user: "Perfect! Swapped to {upsell_name}"
       c. If user disagrees: proceed with original (don't remove anything)
    3. If tool returns success=False: SILENTLY SKIP - DO NOT mention to user that no upsell was found. Just proceed with normal response like "Added to cart! Would you like to proceed to checkout or continue shopping?"
    
    NOTE: Cross-sell feature is disabled - DO NOT call find_cross_sell_product
    
    UPSELL SWAP RULES:
    - After swap, ONLY upsell product should be in cart
    - Use remove_from_cart_by_product_id (most reliable, uses product_id not position)
    - Always verify with summarize_cart after removal
    - NEVER leave both products in cart after swap
    
    Args:
        product_id: Product identifier
        product_name: Product name
        price: Product price
        quantity: Quantity to add
        retailer_id: Retailer identifier (for catalog products)
        variant_id: Product variant identifier
        
    Returns:
        Dictionary with cart addition result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.services.cart_service import CartService
        
        cart_service = CartService()
        
        # Get user phone from context (set by agent before tool invocation)
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("✗ add_to_cart FAILED: User phone not available in context")
            return {
                "success": False,
                "message": "Unable to determine user phone. Please try again.",
                "error": "User phone not in context"
            }
        
        logger.info(f"🛒 add_to_cart tool called: user={user_phone}, product_id={product_id}, product_name={product_name}, price={price}, quantity={quantity}")
        result = cart_service.add_to_cart(
            user_phone=user_phone,
            product_id=product_id,
            product_name=product_name,
            price=price,
            quantity=quantity,
            retailer_id=retailer_id,
            variant_id=variant_id
        )
        
        if result.get("success"):
            logger.info(f"✓ add_to_cart SUCCESS: Added {product_name} to cart for {user_phone}")
        else:
            logger.warning(f"✗ add_to_cart FAILED: {result.get('message', 'Unknown error')} for {user_phone}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error in add_to_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to add product to cart",
            "error": str(e)
        }


@tool
@traceable(name="add_catalog_product_to_cart")
def add_catalog_product_to_cart(
    user_phone: str,
    retailer_id: str,
    quantity: int = 1
) -> Dict[str, Any]:
    """
    Add a product from WhatsApp catalog to cart.
    
    Args:
        user_phone: User's phone number
        retailer_id: Retailer identifier (e.g., shopify_9785621774612_50202732462356)
        quantity: Quantity to add
        
    Returns:
        Dictionary with cart addition result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # TODO: Implement catalog product lookup
        # For now, use a placeholder implementation
        return {
            "success": False,
            "message": "Catalog product addition not yet implemented",
            "error": "Catalog integration pending"
        }
        
    except Exception as e:
        logger.error(f"Error in add_catalog_product_to_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to add catalog product to cart",
            "error": str(e)
        }




@tool
def remove_from_cart(user_phone: str, item_number: int) -> Dict[str, Any]:
    """
    Remove an item from the cart by item number.
    
    Args:
        user_phone: User's phone number
        item_number: Item number in cart (1-based)
        
    Returns:
        Dictionary with removal result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = cart_service.remove_from_cart(user_phone, item_number)
        return result
        
    except Exception as e:
        logger.error(f"Error in remove_from_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to remove item from cart",
            "error": str(e)
        }




@tool
@traceable(name="remove_from_cart_by_product_id")
def remove_from_cart_by_product_id(product_id: str) -> Dict[str, Any]:
    """
    Remove an item from the cart by product ID (more reliable than item_number).
    
    This is the preferred method for removing items during swaps or when you know the exact product_id,
    as it doesn't depend on cart position which can change.
    
    Args:
        product_id: Product ID to remove from cart
        
    Returns:
        Dictionary with removal result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # Get user phone from context (set by agent before tool invocation)
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("✗ remove_from_cart_by_product_id FAILED: User phone not available in context")
            return {
                "success": False,
                "message": "Unable to determine user phone. Please try again.",
                "error": "User phone not in context"
            }
        
        logger.info(f"🗑️ remove_from_cart_by_product_id tool called: user={user_phone}, product_id={product_id}")
        result = cart_service.remove_from_cart_by_product_id(user_phone, product_id)
        
        if result.get("success"):
            logger.info(f"✓ remove_from_cart_by_product_id SUCCESS: Removed product_id={product_id} from cart for {user_phone}")
        else:
            logger.warning(f"✗ remove_from_cart_by_product_id FAILED: {result.get('message', 'Unknown error')} for {user_phone}, product_id={product_id}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error in remove_from_cart_by_product_id tool: {e}")
        return {
            "success": False,
            "message": "Failed to remove item from cart",
            "error": str(e)
        }




@tool
def clear_cart(user_phone: str) -> Dict[str, Any]:
    """
    Clear all items from the user's cart.
    
    Args:
        user_phone: User's phone number
        
    Returns:
        Dictionary with cart clearing result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = cart_service.clear_cart(user_phone)
        return result
        
    except Exception as e:
        logger.error(f"Error in clear_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to clear cart",
            "error": str(e)
        }




@tool
def summarize_cart(user_phone: str) -> Dict[str, Any]:
    """
    Get a summary of the user's cart contents.
    
    Args:
        user_phone: User's phone number
        
    Returns:
        Dictionary with cart summary
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = cart_service.get_cart(user_phone)
        return result
        
    except Exception as e:
        logger.error(f"Error in summarize_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to get cart summary",
            "error": str(e),
            "cart_items": [],
            "total": 0.0
        }




@tool
@traceable(name="show_cart_items")
def show_cart_items() -> Dict[str, Any]:
    """
    Show the user's cart items in a formatted, user-friendly way.
    
    WHEN TO CALL THIS:
    - User asks "show my cart", "what's in my cart", "cart items", "show cart", "what do I have in cart"
    - User asks "what's in my basket"
    - User wants to see their current cart contents
    - CRITICAL RULE: Anytime the user specifically asks about the cart, you MUST call this tool (do not invent cart summaries manually)
    
    WORKFLOW:
    1. Get cart from database for the current user
    2. Format items with position numbers, names, quantities, and prices
    3. Calculate and display total
    4. Return formatted message ready to display
    
    Returns:
        Dictionary with formatted cart display:
        {
            "success": bool,
            "message": str (formatted cart display),
            "cart_items": list,
            "total": float,
            "item_count": int
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        logger.info("🔍 show_cart_items tool called")
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.warning("show_cart_items: Unable to determine user phone")
            return {
                "success": False,
                "message": "Unable to determine user phone",
                "cart_items": [],
                "total": 0.0,
                "item_count": 0
            }
        
        logger.info(f"show_cart_items: Getting cart for user {user_phone}")
        # Get cart from database
        cart_result = cart_service.get_cart(user_phone)
        
        if not cart_result.get("success"):
            return {
                "success": False,
                "message": cart_result.get("message", "Failed to load cart"),
                "cart_items": [],
                "total": 0.0,
                "item_count": 0
            }
        
        cart_items = cart_result.get("cart_items", [])
        total = cart_result.get("total", 0.0)
        
        if not cart_items:
            logger.info(f"show_cart_items: Cart is empty for user {user_phone}")
            return {
                "success": True,
                "message": "Your cart is empty. Add some products to get started! 😊",
                "cart_items": [],
                "total": 0.0,
                "item_count": 0
            }
        
        logger.info(f"show_cart_items: Found {len(cart_items)} items in cart for user {user_phone}")
        
        # Format cart items for display
        formatted_items = []
        for item in cart_items:
            item_number = item.get("item_number", 0)
            product_name = item.get("product_name", "Unknown Product")
            quantity = item.get("quantity", 1)
            price = item.get("price", 0.0)
            item_total = item.get("total", price * quantity)
            
            formatted_items.append(
                f"{item_number}. *{product_name}*\n"
                f"   Quantity: {quantity} × KES {price:,.0f} = KES {item_total:,.0f}"
            )
        
        # Build formatted message
        items_text = "\n\n".join(formatted_items)
        message = f"Here's what's in your cart:\n\n{items_text}\n\n*Total: KES {total:,.0f}*"
        
        return {
            "success": True,
            "message": message,
            "cart_items": cart_items,
            "total": total,
            "item_count": len(cart_items)
        }
        
    except Exception as e:
        logger.error(f"Error in show_cart_items tool: {e}")
        return {
            "success": False,
            "message": "Failed to retrieve cart items. Please try again.",
            "error": str(e),
            "cart_items": [],
            "total": 0.0,
            "item_count": 0
        }




@tool
def check_cart_inventory_and_cleanup(user_phone: str) -> Dict[str, Any]:
    """
    Check cart items for inventory availability and clean up out-of-stock items.
    
    Args:
        user_phone: User's phone number
        
    Returns:
        Dictionary with cleanup results
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = cart_service.check_cart_inventory_and_cleanup(user_phone)
        return result
        
    except Exception as e:
        logger.error(f"Error in check_cart_inventory_and_cleanup tool: {e}")
        return {
            "success": False,
            "message": "Failed to check cart inventory",
            "error": str(e)
        }


# Product Upselling Tools



@tool
@traceable(name="retain_cart_items_by_position")
def retain_cart_items_by_position(user_phone: str, item_numbers: List[int]) -> Dict[str, Any]:
    """
    Keep only the specified cart positions (1-based) and remove all other items.

    Args:
        user_phone: User's phone number
        item_numbers: List of item positions to keep (e.g., [4] for the last item)

    Returns:
        Dictionary with operation result and updated cart
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        if not item_numbers:
            return {
                "success": False,
                "message": "Please specify which cart positions to keep."
            }

        cart = cart_service.get_cart(user_phone)
        if not cart.get("success"):
            return {
                "success": False,
                "message": cart.get("message", "Failed to get cart"),
            }

        items = cart.get("cart_items", [])
        if not items:
            return {
                "success": False,
                "message": "Cart is empty",
                "cart_items": [],
            }

        keep_set = {pos for pos in item_numbers if 1 <= pos <= len(items)}
        if not keep_set:
            return {
                "success": False,
                "message": f"Positions {item_numbers} are invalid. Cart has {len(items)} item(s).",
                "cart_items": items,
            }

        kept_items = []
        removed = []
        failed = []

        for idx, item in enumerate(items, 1):
            product_id = item.get("product_id")
            if idx in keep_set:
                kept_items.append(item)
                continue

            if not product_id:
                # Fallback to removal by position if product_id missing
                removal = cart_service.remove_from_cart(user_phone, idx)
            else:
                removal = cart_service.remove_from_cart_by_product_id(user_phone, product_id)

            if removal.get("success"):
                removed.append(item)
            else:
                failed.append(
                    {
                        "item": item,
                        "error": removal.get("message", "Unknown error"),
                    }
                )

        updated_cart = cart_service.get_cart(user_phone)

        return {
            "success": len(failed) == 0,
            "message": (
                f"Kept {len(kept_items)} item(s) and removed {len(removed)}."
                if not failed
                else f"Partial success. Kept {len(kept_items)} item(s), removed {len(removed)}, failed to remove {len(failed)}."
            ),
            "kept_items": kept_items,
            "removed_items": removed,
            "failed_items": failed,
            "cart_summary": updated_cart,
        }
    except Exception as e:
        logger.error(f"Error in retain_cart_items_by_position tool: {e}")
        return {
            "success": False,
            "message": "Failed to keep only selected cart items",
            "error": str(e),
        }




@tool
@traceable(name="retain_only_item_in_cart")
def retain_only_item_in_cart(user_phone: str, keep_query: str) -> Dict[str, Any]:
    """
    Keep only one item in the cart by matching its name to the user's query; remove all others.

    This tool is designed for intents like: "remove all items except the Samsung washing machine".

    Args:
        user_phone: User's phone number
        keep_query: Free-form text describing the item to keep (e.g., full or partial product name)

    Returns:
        Dictionary with the operation result and the retained item, if found
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        cart = cart_service.get_cart(user_phone)
        if not cart.get("success"):
            return {
                "success": False,
                "message": cart.get("message", "Failed to get cart"),
            }

        items = cart.get("cart_items", [])
        if not items:
            return {
                "success": False,
                "message": "Cart is empty",
                "cart_items": [],
            }

        # Find the best matching item to keep
        import difflib

        query_norm = keep_query.strip().lower()
        best_idx = -1
        best_score = -1.0
        for idx, item in enumerate(items):
            name = (item.get("product_name") or "").lower()
            # Basic containment score + similarity score
            containment = 1.0 if all(part in name for part in query_norm.split()) else 0.0
            similarity = difflib.SequenceMatcher(a=query_norm, b=name).ratio()
            score = containment * 2.0 + similarity  # prefer containment
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx == -1:
            return {
                "success": False,
                "message": "Could not identify which item to keep from the query",
                "cart_items": items,
            }

        item_to_keep = items[best_idx]
        keep_product_id = item_to_keep.get("product_id")

        # Remove all other items
        removed = []
        failed = []
        for idx, item in enumerate(items):
            product_id = item.get("product_id")
            if not product_id or product_id == keep_product_id:
                continue
            res = cart_service.remove_from_cart_by_product_id(user_phone, product_id)
            if res.get("success"):
                removed.append(product_id)
            else:
                failed.append({"product_id": product_id, "error": res.get("message")})

        summary_after = cart_service.get_cart(user_phone)

        response = {
            "success": True if not failed else False,
            "message": (
                f"Kept only '{item_to_keep.get('product_name')}'. Removed {len(removed)} other item(s)."
                if not failed else
                f"Partial success. Kept '{item_to_keep.get('product_name')}', removed {len(removed)} items, failed {len(failed)}."
            ),
            "kept_item": item_to_keep,
            "removed_count": len(removed),
            "failed": failed,
            "cart_summary": summary_after,
        }
        return response

    except Exception as e:
        logger.error(f"Error in retain_only_item_in_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to retain only one item in cart",
            "error": str(e),
        }



@tool
def calculate_price_difference(
    current_product_price: float,
    suggested_product_price: float,
    current_product_name: str,
    suggested_product_name: str,
    upsell_reason: str = ""
) -> Dict[str, Any]:
    """
    Calculate price difference for upselling a better product.
    
    CRITICAL RULES:
    - ONLY use when suggested price is EQUAL or HIGHER than current (no cheaper products)
    - Price difference MUST NOT exceed 20,000 KES
    - ALWAYS provide upsell_reason with specific benefits (e.g., "wireless Bluetooth and 20-hour battery")
    
    After calling this tool, present the upsell naturally with the reason:
    "I found a better option - [Product Name] for KES [price]. It has [upsell_reason]. 
    Would you like to swap?"
    
    Args:
        current_product_price: Price of current product in cart
        suggested_product_price: Price of better/premium product (MUST be >= current, max +20k)
        current_product_name: Name of current product
        suggested_product_name: Name of suggested better product
        upsell_reason: Specific benefits/features that make it better (REQUIRED)
        
    Returns:
        Dictionary with price difference and upsell information
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # Validate that suggested product is not cheaper
        if suggested_product_price < current_product_price:
            return {
                "success": False,
                "message": "Cannot upsell to cheaper product. Suggested price must be equal or higher.",
                "error": "Invalid upsell: cheaper product"
            }
        
        price_difference = suggested_product_price - current_product_price
        
        # Validate price difference doesn't exceed 20,000 KES
        if price_difference > 20000:
            return {
                "success": False,
                "message": f"Price difference of KES {price_difference:,.2f} exceeds maximum allowed (KES 20,000)",
                "error": "Invalid upsell: price difference too large"
            }
        
        percentage_increase = (price_difference / current_product_price) * 100 if current_product_price > 0 else 0
        
        # Validate upsell_reason is provided
        if not upsell_reason or upsell_reason.strip() == "":
            return {
                "success": False,
                "message": "Upsell reason is required. Provide specific benefits/features.",
                "error": "Invalid upsell: missing reason"
            }
        
        return {
            "success": True,
            "current_product": {
                "name": current_product_name,
                "price": current_product_price
            },
            "suggested_product": {
                "name": suggested_product_name,
                "price": suggested_product_price
            },
            "price_difference": price_difference,
            "percentage_increase": percentage_increase,
            "upsell_reason": upsell_reason,
            "upsell_message": f"I found a better option - {suggested_product_name} for KES {suggested_product_price:,.2f}. "
                             f"It has {upsell_reason}. Would you like to swap?"
        }
        
    except Exception as e:
        logger.error(f"Error in calculate_price_difference tool: {e}")
        return {
            "success": False,
            "message": "Failed to calculate price difference",
            "error": str(e)
        }


# Checkout & Delivery Tools



