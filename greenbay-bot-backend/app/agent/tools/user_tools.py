"""Agent tools for User Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *

@tool
@traceable(name="get_user_info")
def get_user_info(user_phone: str = "") -> Dict[str, Any]:
    """
    Get comprehensive user information and activity context for personalized greetings.
    
    CRITICAL: Call this FIRST in EVERY conversation (new or returning) to:
    1. Get user's name for personalization
    2. Understand conversation context (new vs returning user)
    3. Check for pending orders, cart items, or recent activity
    4. Tailor your greeting accordingly
    
    CONTEXT-AWARE GREETING GUIDE:
    - is_new_user=True → Use welcoming first-time greeting
    - has_pending_orders=True → Mention their pending order
    - cart_items_count>0 → Reference their cart
    - is_returning=True → Use "welcome back" style greeting
    - mid_conversation=True → Use casual acknowledgment, don't re-introduce
    
    Returns:
        {
            "success": bool,
            "name": str,
            "phone_number": str,
            "email": str,
            "default_address": str,
            "is_new_user": bool,           # First time user
            "is_returning": bool,          # Has previous activity
            "has_pending_orders": bool,    # Has incomplete orders
            "pending_orders_count": int,
            "cart_items_count": int,
            "total_orders": int,           # Lifetime orders
            "last_order_date": str,        # When they last ordered
            "context": str                 # Suggested greeting context
        }
    """
    try:
        from app.database.db import get_db
        from app.database.models import User, Order, Cart
        from sqlalchemy import func
        from datetime import datetime, timedelta
        
        # If no user_phone provided, get from context
        if not user_phone:
            user_phone = _get_current_user_phone()
        
        db = next(get_db())
        try:
            user = db.query(User).filter(User.phone_number == user_phone).first()
            
            if not user:
                return {
                    "success": False,
                    "name": None,
                    "phone_number": user_phone,
                    "email": None,
                    "default_address": None,
                    "is_new_user": True,
                    "is_returning": False,
                    "has_pending_orders": False,
                    "pending_orders_count": 0,
                    "cart_items_count": 0,
                    "total_orders": 0,
                    "last_order_date": None,
                    "context": "first_time_visitor",
                    "message": f"New user - no profile found"
                }
            
            # Get order statistics
            total_orders = db.query(func.count(Order.id)).filter(
                Order.user_id == user.id
            ).scalar() or 0
            
            pending_orders = db.query(func.count(Order.id)).filter(
                Order.user_id == user.id,
                Order.status.in_(["pending", "processing"])
            ).scalar() or 0
            
            # Get last order date
            last_order = db.query(Order).filter(
                Order.user_id == user.id
            ).order_by(Order.created_at.desc()).first()
            
            last_order_date = None
            if last_order:
                last_order_date = last_order.created_at.isoformat()
            
            # Get cart items count
            cart_items = db.query(func.count(Cart.id)).filter(
                Cart.user_id == user.id
            ).scalar() or 0
            
            # Determine user context
            is_new_user = total_orders == 0 and cart_items == 0
            is_returning = total_orders > 0 or cart_items > 0
            has_pending_orders = pending_orders > 0
            
            # Determine context for greeting
            context = "first_time"
            if has_pending_orders:
                context = "has_pending_orders"
            elif cart_items > 0:
                context = "has_cart_items"
            elif total_orders > 0:
                # Check if last order was recent (within 24 hours)
                if last_order and (datetime.now() - last_order.created_at) < timedelta(days=1):
                    context = "recent_customer"
                else:
                    context = "returning_customer"
            elif is_returning:
                context = "returning_visitor"
            else:
                context = "new_visitor"
            
            return {
                "success": True,
                "name": user.name,
                "phone_number": user.phone_number,
                "email": user.email,
                "default_address": user.default_address,
                "is_new_user": is_new_user,
                "is_returning": is_returning,
                "has_pending_orders": has_pending_orders,
                "pending_orders_count": pending_orders,
                "cart_items_count": cart_items,
                "total_orders": total_orders,
                "last_order_date": last_order_date,
                "context": context,
                "message": f"User context: {context}, Orders: {total_orders}, Cart: {cart_items}, Pending: {pending_orders}"
            }
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Error getting user info: {e}")
        return {
            "success": False,
            "name": None,
            "phone_number": user_phone,
            "email": None,
            "default_address": None,
            "is_new_user": True,
            "is_returning": False,
            "has_pending_orders": False,
            "pending_orders_count": 0,
            "cart_items_count": 0,
            "total_orders": 0,
            "last_order_date": None,
            "context": "error",
            "message": f"Error retrieving user info: {str(e)}"
        }


@tool
def get_store_info() -> str:
    """Get GreenBay Market store information including location, hours, and contact details."""
    return """🏪 *GreenBay Market Store Information*

📍 *Location:*
Amani Gardens, 71 Church Road, Nairobi, Kenya

📞 *Contact:*
Phone: +2540205002173
Email: info@greenbay.market

🕒 *Store Hours:*
Monday - Friday: 9:00 AM - 6:00 PM
Saturday: 9:00 AM - 4:00 PM
Sunday: Closed

🚚 *Services:*
• Electronics & Appliances
• Delivery Nationwide
• Installation Services
• Customer Support

Visit us for the best deals on quality electronics! 💚"""


# Image Processing Tools



