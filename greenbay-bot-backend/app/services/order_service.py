"""Order service for managing order operations."""

from typing import Dict, Any, Optional, List
from datetime import datetime
import uuid
from loguru import logger
from sqlalchemy.orm import Session

from app.database.db import get_db_session
from app.database.models import Order, OrderItem, Delivery, OrderStatus, DeliveryStatus, User
from app.services.cart_service import CartService

cart_service = CartService()


class OrderService:
    """Service for managing order operations."""
    
    def __init__(self):
        """Initialize order service."""
        pass
    
    def _get_user_by_phone(self, db: Session, user_phone: str) -> Optional[User]:
        """Get user by phone number."""
        return db.query(User).filter(User.phone_number == user_phone).first()
    
    def create_order(
        self,
        user_phone: str,
        delivery_address: str,
        delivery_cost: float = 0.0,
        installation_cost: float = 0.0,
        notes: Optional[str] = None,
        selected_product_ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Create a new order from the user's cart.
        
        Args:
            user_phone: User's phone number
            delivery_address: Delivery address
            delivery_cost: Delivery cost
            installation_cost: Installation cost
            notes: Order notes
            selected_product_ids: Optional list of product IDs to include in order. If provided, only these items will be ordered and removed from cart.
            
        Returns:
            Dictionary with order creation result
        """
        db = get_db_session()
        try:
            # Get user
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            # Get cart contents
            cart_result = cart_service.get_cart(user_phone)
            if not cart_result["success"] or not cart_result["cart_items"]:
                return {
                    "success": False,
                    "message": "Cart is empty"
                }
            
            cart_items = cart_result["cart_items"]
            
            # If selected_product_ids provided, filter cart items
            if selected_product_ids:
                cart_items = [item for item in cart_items if item["product_id"] in selected_product_ids]
                if not cart_items:
                    return {
                        "success": False,
                        "message": "None of the selected products were found in cart"
                    }
            
            # Calculate subtotal from selected items
            subtotal = sum(item["price"] * item["quantity"] for item in cart_items)
            
            # Calculate total
            total_amount = subtotal + delivery_cost + installation_cost
            
            # Generate order ID
            order_id = f"GB{datetime.now().strftime('%Y%m%d')}{str(uuid.uuid4())[:8].upper()}"
            
            # Create order with PENDING status (waiting for M-Pesa confirmation)
            order = Order(
                order_id=order_id,
                user_id=user.id,
                status=OrderStatus.PENDING,
                subtotal=subtotal,
                delivery_cost=delivery_cost,
                installation_cost=installation_cost,
                total_amount=total_amount,
                notes=notes
            )
            db.add(order)
            db.flush()  # Get order ID
            
            # Create order items
            for cart_item in cart_items:
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=cart_item["product_id"],
                    product_name=cart_item["product_name"],
                    price=cart_item["price"],
                    quantity=cart_item["quantity"],
                    retailer_id=cart_item["retailer_id"],
                    variant_id=cart_item["variant_id"]
                )
                db.add(order_item)
            
            # Create delivery record
            delivery = Delivery(
                order_id=order.id,
                delivery_address=delivery_address,
                delivery_cost=delivery_cost,
                installation_requested=installation_cost > 0,
                installation_cost=installation_cost,
                status=DeliveryStatus.PENDING
            )
            db.add(delivery)
            
            # Remove only the ordered items from cart (not all items)
            if selected_product_ids:
                # Remove only selected items
                for product_id in selected_product_ids:
                    cart_service.remove_from_cart_by_product_id(user_phone, product_id)
            else:
                # Clear all items from cart (default behavior)
                cart_service.clear_cart(user_phone)
            
            db.commit()
            
            return {
                "success": True,
                "message": "Order created successfully",
                "order": {
                    "order_id": order.order_id,
                    "total_amount": order.total_amount,
                    "status": order.status.value,
                    "created_at": order.created_at.isoformat()
                }
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error creating order: {e}")
            return {
                "success": False,
                "message": "Failed to create order",
                "error": str(e)
            }
        finally:
            db.close()
    
    def get_user_orders(
        self, 
        user_phone: str, 
        status_filter: Optional[str] = None,
        limit: int = 10
    ) -> Dict[str, Any]:
        
        """Get user's order history."""
        db = get_db_session()
        try:
            user = db.query(User).filter(User.phone_number == user_phone).first()
            if not user:
                return {
                    "success": False,
                    "message": "User not found",
                    "orders": []
                }
            
            query = db.query(Order).filter(Order.user_id == user.id)
            
            # Validate status_filter if provided
            if status_filter:
                # Handle special case: wishlist (not a valid order status)
                if status_filter.lower() == "wishlist":
                    return {
                        "success": False,
                        "message": "Wishlist feature is not available. You can save items to your cart instead. Would you like to see your cart?",
                        "orders": [],
                        "suggestion": "cart"
                    }
                
                # Validate that status_filter is a valid OrderStatus enum value
                try:
                    # Try to convert string to OrderStatus enum
                    valid_status = OrderStatus(status_filter.lower())
                    query = query.filter(Order.status == valid_status)
                except ValueError:
                    # Invalid status value
                    valid_statuses = [status.value for status in OrderStatus]
                    return {
                        "success": False,
                        "message": f"Invalid order status '{status_filter}'. Valid statuses are: {', '.join(valid_statuses)}",
                        "orders": [],
                        "valid_statuses": valid_statuses
                    }
            
            orders = query.order_by(Order.created_at.desc()).limit(limit).all()
            
            orders_list = []
            for order in orders:
                orders_list.append({
                    "order_id": order.order_id,
                    "status": order.status.value,
                    "total_amount": order.total_amount,
                    "created_at": order.created_at.isoformat() if order.created_at else None
                })
            
            return {
                "success": True,
                "orders": orders_list
            }
            
        except Exception as e:
            logger.error(f"Error getting user orders: {e}")
            return {
                "success": False,
                "message": "Failed to get orders",
                "orders": [],
                "error": str(e)
            }
        finally:
            db.close()
    
    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        """Get detailed information about a specific order."""
        db = get_db_session()
        try:
            order = db.query(Order).filter(Order.order_id == order_id).first()
            
            if not order:
                return {
                    "success": False,
                    "message": "Order not found"
                }
            
            # Get order items
            items = []
            for item in order.items:
                items.append({
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "price": item.price,
                    "quantity": item.quantity
                })
            
            return {
                "success": True,
                "order": {
                    "order_id": order.order_id,
                    "status": order.status.value,
                    "subtotal": order.subtotal,
                    "delivery_cost": order.delivery_cost,
                    "installation_cost": order.installation_cost,
                    "total_amount": order.total_amount,
                    "created_at": order.created_at.isoformat() if order.created_at else None,
                    "items": items
                }
            }
            
        except Exception as e:
            logger.error(f"Error getting order details: {e}")
            return {
                "success": False,
                "message": "Failed to get order details"
            }
        finally:
            db.close()
    
    def get_user_pending_orders_fresh(self, user_phone: str) -> Dict[str, Any]:
        """
        Get user's pending orders (fresh query from database).
        
        Args:
            user_phone: User's phone number
            
        Returns:
            Dictionary with pending orders
        """
        return self.get_user_orders(
            user_phone=user_phone,
            status_filter="pending",
            limit=10
        )
    
    def get_order_by_id(self, order_id: str) -> Dict[str, Any]:
        """
        Get order by order ID (alias for get_order_details).
        
        Args:
            order_id: Order ID
            
        Returns:
            Dictionary with order details
        """
        return self.get_order_details(order_id)
    
    def cancel_order(self, user_phone: str, order_id: str) -> Dict[str, Any]:
        """
        Cancel an order.
        
        Args:
            user_phone: User's phone number (for verification)
            order_id: Order ID to cancel
            
        Returns:
            Dictionary with cancellation result
        """
        db = get_db_session()
        try:
            # Get the order
            order = db.query(Order).filter(Order.order_id == order_id).first()
            
            if not order:
                return {
                    "success": False,
                    "message": f"Order {order_id} not found"
                }
            
            # Verify the order belongs to this user
            user = self._get_user_by_phone(db, user_phone)
            if not user or order.user_id != user.id:
                return {
                    "success": False,
                    "message": "Order not found or does not belong to you"
                }
            
            # Check if order can be cancelled
            if order.status == OrderStatus.CANCELLED:
                return {
                    "success": False,
                    "message": "Order is already cancelled"
                }
            
            if order.status == OrderStatus.DELIVERED:
                return {
                    "success": False,
                    "message": "Cannot cancel a delivered order. Please contact customer support for returns."
                }
            
            if order.status == OrderStatus.SHIPPED:
                return {
                    "success": False,
                    "message": "Order is already shipped. Please contact customer support for assistance."
                }
            
            if order.status == OrderStatus.REFUNDED:
                return {
                    "success": False,
                    "message": "Order has already been refunded."
                }
            
            # Cancel the order
            order.status = OrderStatus.CANCELLED
            
            # Update delivery status if exists
            # Note: DeliveryStatus doesn't have CANCELLED, use FAILED for cancelled orders
            if order.delivery:
                order.delivery.status = DeliveryStatus.FAILED
            
            db.commit()
            
            logger.info(f"Order {order_id} cancelled by user {user_phone}")
            
            return {
                "success": True,
                "message": f"Order {order_id} has been cancelled successfully",
                "order": {
                    "order_id": order.order_id,
                    "status": order.status.value,
                    "total_amount": order.total_amount
                }
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error cancelling order: {e}")
            return {
                "success": False,
                "message": "Failed to cancel order",
                "error": str(e)
            }
        finally:
            db.close()


# Create service instance
order_service = OrderService()
