"""Agent tools for Order Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *

@tool
def get_user_orders(
    user_phone: str = "",
    status_filter: Optional[str] = None,
    limit: int = 10
) -> Dict[str, Any]:
    """
    Get user's order history.
    
    IMPORTANT: This tool is for ORDERS only, not wishlist. If user asks for "wishlist":
    - Wishlist feature is not available
    - Suggest showing cart instead (use show_cart_items tool)
    - Do NOT call this tool with status_filter="wishlist"
    
    Args:
        user_phone: User's phone number (optional, will use context if not provided)
        status_filter: Filter by order status (valid values: pending, pending_payment, confirmed, processing, shipped, delivered, cancelled, refunded)
        limit: Maximum number of orders to return
        
    Returns:
        Dictionary with user's orders or error message if invalid status
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.services.order_service import OrderService
        
        # Get user phone from context if not provided
        if not user_phone:
            user_phone = _get_current_user_phone()
            if not user_phone:
                return {
                    "success": False,
                    "message": "User phone number is required",
                    "orders": []
                }
        
        order_service = OrderService()
        result = order_service.get_user_orders(
            user_phone=user_phone,
            status_filter=status_filter,
            limit=limit
        )
        
        # If wishlist was requested, provide helpful message
        if not result.get("success") and result.get("suggestion") == "cart":
            result["message"] = "Wishlist feature is not available. Would you like to see your cart instead? You can use your cart to save items you're interested in."
        
        return result
        
    except Exception as e:
        logger.error(f"Error in get_user_orders tool: {e}")
        return {
            "success": False,
            "message": "Failed to get orders",
            "error": str(e),
            "orders": []
        }


@tool
def get_user_pending_orders_fresh(user_phone: str) -> Dict[str, Any]:
    """
    Get user's pending orders with fresh status.
    
    Args:
        user_phone: User's phone number
        
    Returns:
        Dictionary with pending orders
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.services.order_service import OrderService
        
        order_service = OrderService()
        result = order_service.get_user_pending_orders_fresh(user_phone)
        return result
        
    except Exception as e:
        logger.error(f"Error in get_user_pending_orders_fresh tool: {e}")
        return {
            "success": False,
            "message": "Failed to get pending orders",
            "error": str(e),
            "orders": []
        }




@tool
def cancel_order(user_phone: str, order_id: str) -> Dict[str, Any]:
    """
    Cancel an order (only if payment is pending).
    
    Args:
        user_phone: User's phone number
        order_id: Order identifier
        
    Returns:
        Dictionary with cancellation result
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.services.order_service import OrderService
        
        order_service = OrderService()
        result = order_service.cancel_order(user_phone, order_id)
        return result
        
    except Exception as e:
        logger.error(f"Error in cancel_order tool: {e}")
        return {
            "success": False,
            "message": "Failed to cancel order",
            "error": str(e)
        }




@tool
@traceable(name="get_order_details")
def get_order_details(order_id: str) -> Dict[str, Any]:
    """
    Get full order details by order_id, including payment and delivery statuses.

    Args:
        order_id: Public order identifier (e.g., GB20251028233959)

    Returns:
        Dictionary with order details {success, order: {...}}
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.services.order_service import OrderService
        
        order_service = OrderService()
        result = order_service.get_order_by_id(order_id)
        return result
    except Exception as e:
        logger.error(f"Error in get_order_details tool: {e}")
        return {
            "success": False,
            "message": "Failed to get order details",
            "error": str(e)
        }




