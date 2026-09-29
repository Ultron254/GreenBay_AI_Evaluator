"""Agent tools for Checkout Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *
from app.agent.tools.user_tools import get_user_info

@tool
@traceable(name="get_or_set_delivery_address")
def get_or_set_delivery_address(delivery_address: Optional[str] = None) -> Dict[str, Any]:
    """
    Get or set the user's delivery address for checkout.
    
    WHEN TO CALL THIS:
    - Call this tool IMMEDIATELY after the user agrees to checkout (says "yes", "checkout", "proceed", etc.)
    - This tool MUST be called BEFORE `calculate_checkout_total_with_delivery()`
    - This tool handles both checking for existing address and asking for new one if needed
    
    WORKFLOW:
    1. If `delivery_address` parameter is provided (user wants to set/update address):
       a. Store/update the address in the database
       b. Return the new address immediately
       c. Use this address for delivery zone and cost calculation
    2. If `delivery_address` parameter is NOT provided:
       a. Check if user has a delivery_address stored in the database
       b. If address exists: Return the stored address immediately
       c. If address does NOT exist: Return a message asking the user for their address
       d. Wait for user to provide address, then call this tool again WITH the address parameter to store it
    
    IMPORTANT: If user says "change to [location]" or "update to [location]", you MUST call this tool with delivery_address="[location]" to update the stored address.
    
    CRITICAL:
    - This tool automatically gets user phone from context - do NOT pass user_phone parameter
    - Only ask for address ONCE - if user provides it, store it immediately
    - After this tool returns a valid address, proceed to call `calculate_checkout_total_with_delivery(location)`
    
    Args:
        delivery_address: Optional delivery address provided by the user. If provided, it will be stored in the database.
                         If not provided and no address exists in DB, the tool will ask for it.
    
    Returns:
        Dictionary with:
        {
            "success": bool,
            "delivery_address": str (the address to use, or None if needs to be asked),
            "was_stored": bool (whether address was just stored),
            "was_existing": bool (whether address was retrieved from DB),
            "message": str (message to show user - either the address or a prompt to provide it)
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db_session
        from app.database.models import User
        
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {
                "success": False,
                "delivery_address": None,
                "was_stored": False,
                "was_existing": False,
                "message": "Unable to determine user phone. Please try again."
            }
        
        db = get_db_session()
        try:
            user = db.query(User).filter(User.phone_number == user_phone).first()
            
            if not user:
                return {
                    "success": False,
                    "delivery_address": None,
                    "was_stored": False,
                    "was_existing": False,
                    "message": "User not found. Please try again."
                }
            
            # Case 1: User provided a new address (either setting for first time or updating existing)
            if delivery_address and delivery_address.strip():
                old_address = user.delivery_address.strip() if user.delivery_address else None
                user.delivery_address = delivery_address.strip()
                db.commit()
                
                if old_address:
                    logger.info(f"✓ Updated delivery address for user {user_phone}: {old_address} → {delivery_address.strip()}")
                    return {
                        "success": True,
                        "delivery_address": delivery_address.strip(),
                        "was_stored": True,
                        "was_existing": False,
                        "message": f"Perfect! I've updated your delivery address to *{delivery_address.strip()}*. Proceeding with checkout..."
                    }
                else:
                    logger.info(f"✓ Stored delivery address for user {user_phone}: {delivery_address.strip()}")
                    return {
                        "success": True,
                        "delivery_address": delivery_address.strip(),
                        "was_stored": True,
                        "was_existing": False,
                        "message": f"Great! I've saved your delivery address: *{delivery_address.strip()}*. Proceeding with checkout..."
                    }
            
            # Case 2: User has existing delivery address stored and no new address provided
            if user.delivery_address and user.delivery_address.strip():
                logger.info(f"✓ Found stored delivery address for user {user_phone}: {user.delivery_address}")
                return {
                    "success": True,
                    "delivery_address": user.delivery_address.strip(),
                    "was_stored": False,
                    "was_existing": True,
                    "message": f"Using your saved delivery address: *{user.delivery_address.strip()}*. Proceeding with checkout..."
                }
            
            # Case 3: No address stored and user hasn't provided one yet - ask for it
            logger.info(f"⚠ No delivery address stored for user {user_phone}, asking user to provide it")
            return {
                "success": False,
                "delivery_address": None,
                "was_stored": False,
                "was_existing": False,
                "message": "I don't have a delivery address saved for you. Where would you like this delivered? Please provide your delivery address or city (e.g., 'Nairobi', 'Kakamega', 'Mombasa').",
                "needs_address": True
            }
            
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error in get_or_set_delivery_address tool: {e}")
        return {
            "success": False,
            "delivery_address": None,
            "was_stored": False,
            "was_existing": False,
            "message": f"Error processing delivery address: {str(e)}"
        }


@tool
def proceed_to_delivery_setup(user_phone: str) -> Dict[str, Any]:
    """
    Proceed to delivery setup and check for saved addresses.
    
    Args:
        user_phone: User's phone number
        
    Returns:
        Dictionary with delivery setup result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # TODO: Implement saved address lookup
        # For now, ask for delivery address
        return {
            "success": True,
            "message": "Great! Where would you like this delivered?",
            "has_saved_address": False,
            "saved_addresses": []
        }
        
    except Exception as e:
        logger.error(f"Error in proceed_to_delivery_setup tool: {e}")
        return {
            "success": False,
            "message": "Failed to proceed to delivery setup",
            "error": str(e)
        }




@tool
def process_address_response(user_phone: str, address: str) -> Dict[str, Any]:
    """
    Process user's delivery address response.
    
    Args:
        user_phone: User's phone number
        address: Delivery address
        
    Returns:
        Dictionary with address processing result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # TODO: Implement address validation and formatting
        return {
            "success": True,
            "message": f"Delivery address confirmed: {address}",
            "formatted_address": address
        }
        
    except Exception as e:
        logger.error(f"Error in process_address_response tool: {e}")
        return {
            "success": False,
            "message": "Failed to process address",
            "error": str(e)
        }




@tool
def calculate_delivery_options(
    location: str,
    weight_kg: float,
    product_name: str
) -> Dict[str, Any]:
    """
    Calculate delivery options for a location.
    
    Args:
        location: Delivery location
        weight_kg: Product weight in kg
        product_name: Product name for size classification
        
    Returns:
        Dictionary with delivery options
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = delivery_service.calculate_delivery_options(location, weight_kg, product_name)
        return result
        
    except Exception as e:
        logger.error(f"Error in calculate_delivery_options tool: {e}")
        return {
            "success": False,
            "message": "Failed to calculate delivery options",
            "error": str(e)
        }




@tool
def get_delivery_quote(location: str, product_name: str) -> Dict[str, Any]:
    """
    Get quick delivery quote.
    
    Args:
        location: Delivery location
        product_name: Product name
        
    Returns:
        Dictionary with delivery quote
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = delivery_service.get_delivery_quote(location, product_name)
        return result
        
    except Exception as e:
        logger.error(f"Error in get_delivery_quote tool: {e}")
        return {
            "success": False,
            "message": "Failed to get delivery quote",
            "error": str(e)
        }




@tool
def delivery_zones_info() -> Dict[str, Any]:
    """
    Get information about delivery zones.
    
    Returns:
        Dictionary with delivery zones information
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = delivery_service.delivery_zones_info()
        return result
        
    except Exception as e:
        logger.error(f"Error in delivery_zones_info tool: {e}")
        return {
            "success": False,
            "message": "Failed to get delivery zones info",
            "error": str(e)
        }




@tool
def compare_delivery_options(location: str) -> Dict[str, Any]:
    """
    Compare delivery options for different package sizes.
    
    Args:
        location: Delivery location
        
    Returns:
        Dictionary with delivery comparison
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        result = delivery_service.compare_delivery_options(location)
        return result
        
    except Exception as e:
        logger.error(f"Error in compare_delivery_options tool: {e}")
        return {
            "success": False,
            "message": "Failed to compare delivery options",
            "error": str(e)
        }




@tool
@traceable(name="create_order_from_cart")
def create_order_from_cart(
    delivery_address: str,
    delivery_cost: float = 0.0,
    installation_cost: float = 0.0,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an order from the current cart for the active user.
    Critical: Call this tool when the user wants to checkout from the cart and proceed to payment.

    Args:
        delivery_address: Delivery address/location provided by the user
        delivery_cost: Delivery cost (from calculate_checkout_total_with_delivery result) - REQUIRED
        installation_cost: Optional installation cost
        notes: Optional order notes

    Returns:
        Dictionary with {success, message, order: {order_id, total_amount, status, created_at}}
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Unable to determine user phone"}

        logger.info(f"📦 create_order_from_cart: user={user_phone}, address={delivery_address}, delivery_cost={delivery_cost}, installation_cost={installation_cost}")
        
        result = order_service.create_order(
            user_phone=user_phone,
            delivery_address=delivery_address,
            delivery_cost=delivery_cost,
            installation_cost=installation_cost,
            notes=notes,
            selected_product_ids=None  # All items by default
        )
        
        if result.get("success"):
            logger.info(f"✓ Order created: {result.get('order', {}).get('order_id')} for {user_phone}")
        else:
            logger.warning(f"✗ Order creation failed: {result.get('message')} for {user_phone}")

        return result
    except Exception as e:
        logger.error(f"Error in create_order_from_cart tool: {e}")
        return {
            "success": False,
            "message": "Failed to create order from cart",
            "error": str(e)
        }



@tool
@traceable(name="checkout_with_selected_items")
def checkout_with_selected_items(
    product_ids: List[str],
    delivery_address: str,
    delivery_cost: float = 0.0,
    installation_cost: float = 0.0,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an order from the cart with ONLY the selected items (by product IDs).
    
    WHEN TO CALL THIS:
    - When user says "checkout with the last item", "checkout with item 1 and 3", "checkout with [product name]", etc.
    - Use this instead of `create_order_from_cart` when user wants to checkout only specific items
    
    WORKFLOW:
    1. Get product IDs from cart items that match user's selection
    2. Call `calculate_checkout_total_with_delivery(location)` FIRST to get delivery_cost for selected items
    3. Call this tool with the selected product_ids and delivery_cost
    4. The sourcing team settles with the customer offline after inspection.
    
    HOW TO GET PRODUCT IDs:
    - If user says "last item" or "last one": Get product_id from the last item in cart (items[-1].product_id)
    - If user says "item 1", "first one": Get product_id from items[0].product_id
    - If user says product name: Search cart items by name and get product_id
    - If user says "item 1 and 3": Get product_ids from items[0] and items[2]
    
    CRITICAL:
    - You MUST pass the delivery_cost from calculate_checkout_total_with_delivery result
    - Only the selected items will be removed from cart, other items remain
    
    Args:
        product_ids: List of product IDs to include in order (e.g., ["9785669353748", "9857829896468"])
        delivery_address: Delivery address/location provided by the user
        delivery_cost: Delivery cost (from calculate_checkout_total_with_delivery result) - REQUIRED
        installation_cost: Optional installation cost
        notes: Optional order notes
    
    Returns:
        Dictionary with {success, message, order: {order_id, total_amount, status, created_at}}
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Unable to determine user phone"}
        
        if not product_ids:
            return {"success": False, "message": "No product IDs provided. Please specify which items to checkout."}
        
        logger.info(f"📦 checkout_with_selected_items: user={user_phone}, product_ids={product_ids}, address={delivery_address}, delivery_cost={delivery_cost}")
        
        result = order_service.create_order(
            user_phone=user_phone,
            delivery_address=delivery_address,
            delivery_cost=delivery_cost,
            installation_cost=installation_cost,
            notes=notes,
            selected_product_ids=product_ids
        )
        
        if result.get("success"):
            logger.info(f"✓ Order created with selected items: {result.get('order', {}).get('order_id')} for {user_phone}")
        else:
            logger.warning(f"✗ Order creation failed: {result.get('message')} for {user_phone}")
        
        return result
    except Exception as e:
        logger.error(f"Error in checkout_with_selected_items tool: {e}")
        return {
            "success": False,
            "message": "Failed to create order with selected items",
            "error": str(e)
        }



@tool
@traceable(name="calculate_checkout_total_with_delivery")
def calculate_checkout_total_with_delivery(location: str) -> Dict[str, Any]:
    """
    Calculate the checkout total including delivery cost based on the current cart and delivery location.
    
    CRITICAL: DELIVERY ADDRESS WORKFLOW (MANDATORY):
    - BEFORE calling this tool, you MUST FIRST call `get_or_set_delivery_address()`
    - `get_or_set_delivery_address()` will:
      a. Check if user has a saved delivery address
      b. If yes, return it immediately
      c. If no, ask the user for their address and store it
    - After `get_or_set_delivery_address()` returns a valid address (success=True), use that address as the `location` parameter for this tool
    - DO NOT call this tool until `get_or_set_delivery_address()` returns success=True with a delivery_address
    - The `location` parameter MUST be a valid city/area name (e.g., "Nairobi", "Kakamega", "Mombasa")

    WORKFLOW:
    1. Validate that location is provided (if not, return error asking for address)
    2. Load user's cart from DB
    3. Estimate total shipment weight from product metadata (Qdrant payload) with fallbacks by category
    4. Call delivery_service.calculate_delivery_options to get cost
    5. Return full breakdown and grand total
    
    NOTE: Cross-sell feature is disabled - DO NOT call find_cross_sell_product. After calling this tool, proceed directly to payment confirmation.

    Args:
        location: Delivery location/city/area

    Returns:
        {
          success, cart_subtotal, delivery_cost, grand_total, items:[{name, price, qty, product_id, weight_kg}],
          delivery_option: {zone, cost, timeline, size}
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        import asyncio
        user_phone = _get_current_user_phone()
        if not user_phone:
            return {"success": False, "message": "Unable to determine user phone"}

        # CRITICAL: Validate location is provided
        if not location or not location.strip():
            return {
                "success": False,
                "message": "Please provide your delivery address or city. Where would you like this delivered?",
                "needs_address": True
            }

        # Load cart
        cart = cart_service.get_cart(user_phone)
        if not cart.get("success"):
            return {"success": False, "message": cart.get("message", "Failed to load cart")}

        items = cart.get("cart_items", [])
        if not items:
            return {"success": False, "message": "Your cart is empty"}

        # Fetch product payloads to get weights
        product_ids = [str(i["product_id"]) for i in items if i.get("product_id")]
        products = asyncio.run(qdrant_service.get_products_by_ids(product_ids)) if product_ids else []
        id_to_payload = {str(p.get("id")): p.get("payload", {}) for p in products}

        def infer_weight_kg(name: str, payload: Dict[str, Any]) -> float:
            # Try payload first (variants[0].weight_kg or weight)
            try:
                variants = payload.get("variants") or []
                if variants:
                    w = variants[0].get("weight_kg") or variants[0].get("weight")
                    if w:
                        return float(w)
            except Exception:
                pass
            n = (name or "").lower()
            # Category heuristics (conservative)
            if "freezer" in n or "fridge" in n or "refrigerator" in n:
                return 35.0
            if "cooker" in n or "stove" in n or "oven" in n:
                return 28.0
            if "speaker" in n:
                return 7.0
            if "tv" in n:
                return 12.0
            if "phone" in n or "mouse" in n or "torch" in n:
                return 0.5
            return 3.0

        enriched_items = []
        subtotal = 0.0
        total_weight = 0.0
        first_name = items[0].get("product_name")
        for it in items:
            price = float(it.get("price", 0.0))
            qty = int(it.get("quantity", 1))
            subtotal += price * qty
            pid = str(it.get("product_id")) if it.get("product_id") is not None else ""
            payload = id_to_payload.get(pid, {})
            w = infer_weight_kg(it.get("product_name"), payload)
            total_weight += w * qty
            enriched_items.append({
                "product_id": pid,
                "product_name": it.get("product_name"),
                "price": price,
                "quantity": qty,
                "weight_kg": w
            })

        # Delivery options and cost
        delivery = delivery_service.calculate_delivery_options(location, total_weight, first_name or "items")
        if not delivery.get("success", True):
            return {"success": False, "message": delivery.get("message", "Failed to calculate delivery")}
        # Pick the recommended/cheapest
        delivery_cost = float(delivery.get("best_option", {}).get("cost", delivery.get("cost", 0.0)))
        option = delivery.get("best_option") or {"cost": delivery_cost}

        grand_total = subtotal + delivery_cost

        return {
            "success": True,
            "cart_subtotal": round(subtotal, 2),
            "delivery_cost": round(delivery_cost, 2),
            "grand_total": round(grand_total, 2),
            "items": enriched_items,
            "delivery_option": option,
            "message": f"Total {grand_total:.2f} (subtotal {subtotal:.2f} + delivery {delivery_cost:.2f})"
        }
    except Exception as e:
        logger.error(f"Error calculating checkout total: {e}")
        return {"success": False, "message": f"Failed to calculate total: {str(e)}"}


@tool
def generate_and_send_receipt(
    order_id: str
) -> Dict[str, Any]:
    """
    Generate PDF receipt for an order and send it directly via WhatsApp.
    
    WORKFLOW:
    1. Generate PDF receipt using reportlab
    2. Upload PDF to S3 (if configured) to get a publicly accessible URL
    3. Send the PDF file directly to the user via WhatsApp as a document
    4. The user will receive the actual PDF file, not a fake URL
    
    NOTE: Receipts are generated on request; settlement happens offline.
    Do NOT call this manually unless the receipt generation failed.
    
    This tool automatically gets user phone from context - do NOT pass user_phone parameter.
    
    Args:
        order_id: Order identifier
        
    Returns:
        Dictionary with receipt status (success, message, receipt_path, receipt_url)
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # Get user phone from context (set by agent before tool invocation)
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("✗ generate_and_send_receipt FAILED: User phone not available in context")
            return {
                "success": False,
                "message": "Unable to determine user phone. Please try again.",
                "error": "User phone not in context"
            }
        
        logger.info(f"🧾 generate_and_send_receipt tool called: user={user_phone}, order_id={order_id}")
        
        # Get order details
        order_result = order_service.get_order_details(order_id)
        
        if not order_result.get("success"):
            logger.warning(f"✗ generate_and_send_receipt FAILED: Order not found for order_id={order_id}")
            return {
                "success": False,
                "message": "Order not found"
            }
        
        order = order_result["order"]
        logger.info(f"✓ Order found: {order.get('order_id')}, total={order.get('total_amount')}")
        
        # Safely extract delivery and payment info (they can be None)
        delivery = order.get("delivery") or {}
        payment = order.get("payment") or {}
        
        # Get customer name if available
        customer_name = None
        try:
            user_info_result = get_user_info(user_phone)
            if user_info_result.get("success") and user_info_result.get("name"):
                customer_name = user_info_result.get("name")
                logger.info(f"✓ Found customer name: {customer_name}")
        except Exception as e:
            logger.warning(f"Could not retrieve customer name: {e}")
        
        # Generate PDF receipt
        receipt_path = receipt_generator.generate_receipt(
            order_id=order["order_id"],
            customer_phone=user_phone,
            items=order.get("items", []),
            subtotal=order.get("subtotal", 0),
            delivery_cost=delivery.get("cost", 0) if delivery else 0,
            installation_cost=order.get("installation_cost", 0),
            total_amount=order.get("total_amount", 0),
            delivery_address=delivery.get("address", "") if delivery else "",
            delivery_timeline=delivery.get("status", "") if delivery else "",
            payment_method="Settled by sourcing team",
            customer_name=customer_name
        )
        
        logger.info(f"✓ Receipt generated successfully: {receipt_path} for order {order_id}")
        
        # Upload PDF to S3 and get public URL
        import asyncio
        import os
        from app.services.s3_service import upload_bytes_to_s3, generate_s3_key
        
        try:
            # Read PDF file
            with open(receipt_path, "rb") as f:
                pdf_bytes = f.read()
            
            # Generate S3 key for receipt
            filename = os.path.basename(receipt_path)
            s3_key = generate_s3_key(user_phone, filename)
            
            # Upload to S3
            s3_result = asyncio.run(upload_bytes_to_s3(
                bytes_data=pdf_bytes,
                key=s3_key,
                content_type="application/pdf"
            ))
        
            receipt_url = s3_result["url"]
            logger.info(f"✓ Receipt uploaded to S3: {s3_key}, URL: {receipt_url}")
            
            # Send receipt via WhatsApp
            from app.webhooks.whatsapp import whatsapp_service
            send_result = asyncio.run(whatsapp_service.send_receipt(
                to=user_phone,
                receipt_url=receipt_url
            ))
            
            if send_result.get("success"):
                logger.info(f"✓ Receipt sent successfully to {user_phone} via WhatsApp")
                return {
                    "success": True,
                    "message": "Receipt generated and sent successfully",
                    "receipt_path": receipt_path
                    # Note: receipt_url is not returned to avoid showing it to user - PDF is sent directly via WhatsApp
                }
            else:
                logger.warning(f"✗ Failed to send receipt via WhatsApp: {send_result.get('error')}")
                return {
                    "success": True,  # Receipt was generated, just failed to send
                    "message": "Receipt generated successfully, but failed to send via WhatsApp",
                    "receipt_path": receipt_path,
                    "send_error": send_result.get("error")
                }
                
        except ImportError:
            logger.error("S3 service not available - cannot upload receipt")
            return {
                "success": False,
                "message": "Receipt generated but S3 service not configured. Please configure S3 to send receipts.",
                "receipt_path": receipt_path
            }
        except Exception as e:
            logger.error(f"✗ Error uploading/sending receipt: {e}")
            return {
                "success": True,  # Receipt was generated
                "message": f"Receipt generated but failed to upload/send: {str(e)}",
                "receipt_path": receipt_path
            }
        
    except Exception as e:
        logger.error(f"✗ Error in generate_and_send_receipt tool: {e}")
        return {
            "success": False,
            "message": "Failed to generate receipt",
            "error": str(e)
        }

