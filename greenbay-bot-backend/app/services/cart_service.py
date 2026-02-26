"""Cart service for managing shopping cart operations."""

from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime, timedelta
from loguru import logger

from app.database.db import get_db_session
from app.database.models import Cart, User


class CartService:
    """Service for managing shopping cart operations."""
    
    def __init__(self):
        """Initialize cart service."""
        pass
    
    def add_to_cart(
        self,
        user_phone: str,
        product_id: str,
        product_name: str,
        price: float,
        quantity: int = 1,
        retailer_id: Optional[str] = None,
        variant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Add a product to the user's cart.
        
        Args:
            user_phone: User's phone number
            product_id: Product identifier
            product_name: Product name
            price: Product price
            quantity: Quantity to add
            retailer_id: Retailer identifier (for catalog products)
            variant_id: Product variant identifier
            
        Returns:
            Dictionary with operation result
        """
        db = get_db_session()
        try:
            # Get or create user
            user = self._get_or_create_user(db, user_phone)
            
            # Check if product already exists in cart
            existing_item = db.query(Cart).filter(
                and_(
                    Cart.user_id == user.id,
                    Cart.product_id == product_id,
                    Cart.variant_id == variant_id
                )
            ).first()
            
            if existing_item:
                # Update quantity
                existing_item.quantity += quantity
                existing_item.updated_at = datetime.utcnow()
                db.commit()
                
                return {
                    "success": True,
                    "message": f"Updated quantity for {product_name}",
                    "cart_item": {
                        "id": existing_item.id,
                        "product_name": existing_item.product_name,
                        "quantity": existing_item.quantity,
                        "price": existing_item.price
                    }
                }
            else:
                # Add new item
                cart_item = Cart(
                    user_id=user.id,
                    product_id=product_id,
                    product_name=product_name,
                    price=price,
                    quantity=quantity,
                    retailer_id=retailer_id,
                    variant_id=variant_id
                )
                db.add(cart_item)
                db.commit()
                
                return {
                    "success": True,
                    "message": f"Added {product_name} to cart",
                    "cart_item": {
                        "id": cart_item.id,
                        "product_name": cart_item.product_name,
                        "quantity": cart_item.quantity,
                        "price": cart_item.price
                    }
                }
                
        except Exception as e:
            db.rollback()
            logger.error(f"Error adding to cart: {e}")
            return {
                "success": False,
                "message": f"Failed to add {product_name} to cart",
                "error": str(e)
            }
        finally:
            db.close()
    
    def remove_from_cart(self, user_phone: str, item_number: int) -> Dict[str, Any]:
        """
        Remove an item from the cart by item number.
        
        Args:
            user_phone: User's phone number
            item_number: Item number in cart (1-based)
            
        Returns:
            Dictionary with operation result
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            # Get cart items ordered by added_at
            cart_items = db.query(Cart).filter(
                Cart.user_id == user.id
            ).order_by(Cart.added_at.asc()).all()
            
            if item_number < 1 or item_number > len(cart_items):
                return {
                    "success": False,
                    "message": f"Invalid item number. Cart has {len(cart_items)} items."
                }
            
            # Remove the specified item
            item_to_remove = cart_items[item_number - 1]
            product_name = item_to_remove.product_name
            
            db.delete(item_to_remove)
            db.commit()
            
            return {
                "success": True,
                "message": f"Removed {product_name} from cart"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error removing from cart: {e}")
            return {
                "success": False,
                "message": "Failed to remove item from cart",
                "error": str(e)
            }
        finally:
            db.close()
    
    def remove_from_cart_by_product_id(self, user_phone: str, product_id: str) -> Dict[str, Any]:
        """
        Remove an item from the cart by product ID (more reliable than item_number).
        
        Args:
            user_phone: User's phone number
            product_id: Product ID to remove
            
        Returns:
            Dictionary with operation result
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            # Find and remove the item by product_id
            item_to_remove = db.query(Cart).filter(
                Cart.user_id == user.id,
                Cart.product_id == product_id
            ).first()
            
            if not item_to_remove:
                return {
                    "success": False,
                    "message": f"Product {product_id} not found in cart"
                }
            
            product_name = item_to_remove.product_name
            db.delete(item_to_remove)
            db.commit()
            
            return {
                "success": True,
                "message": f"Removed {product_name} from cart"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error removing from cart by product_id: {e}")
            return {
                "success": False,
                "message": "Failed to remove item from cart",
                "error": str(e)
            }
        finally:
            db.close()
    
    def clear_cart(self, user_phone: str) -> Dict[str, Any]:
        """
        Clear all items from the user's cart.
        
        Args:
            user_phone: User's phone number
            
        Returns:
            Dictionary with operation result
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            # Delete all cart items
            deleted_count = db.query(Cart).filter(
                Cart.user_id == user.id
            ).delete()
            
            db.commit()
            
            return {
                "success": True,
                "message": f"Cleared {deleted_count} items from cart"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error clearing cart: {e}")
            return {
                "success": False,
                "message": "Failed to clear cart",
                "error": str(e)
            }
        finally:
            db.close()
    
    def get_cart(self, user_phone: str) -> Dict[str, Any]:
        """
        Get the user's cart contents.
        
        Args:
            user_phone: User's phone number
            
        Returns:
            Dictionary with cart contents
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found",
                    "cart_items": [],
                    "total": 0.0
                }
            
            cart_items = db.query(Cart).filter(
                Cart.user_id == user.id
            ).order_by(Cart.added_at.asc()).all()
            
            items = []
            total = 0.0
            
            for i, item in enumerate(cart_items, 1):
                item_total = item.price * item.quantity
                total += item_total
                
                items.append({
                    "item_number": i,
                    "id": item.id,
                    "product_id": item.product_id,
                    "product_name": item.product_name,
                    "price": item.price,
                    "quantity": item.quantity,
                    "total": item_total,
                    "retailer_id": item.retailer_id,
                    "variant_id": item.variant_id
                })
            
            return {
                "success": True,
                "cart_items": items,
                "total": total,
                "item_count": len(items)
            }
            
        except Exception as e:
            logger.error(f"Error getting cart: {e}")
            return {
                "success": False,
                "message": "Failed to get cart",
                "error": str(e),
                "cart_items": [],
                "total": 0.0
            }
        finally:
            db.close()
    
    def update_cart_quantity(
        self, 
        user_phone: str, 
        item_number: int, 
        new_quantity: int
    ) -> Dict[str, Any]:
        """
        Update the quantity of a cart item.
        
        Args:
            user_phone: User's phone number
            item_number: Item number in cart (1-based)
            new_quantity: New quantity
            
        Returns:
            Dictionary with operation result
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            # Get cart items ordered by added_at
            cart_items = db.query(Cart).filter(
                Cart.user_id == user.id
            ).order_by(Cart.added_at.asc()).all()
            
            if item_number < 1 or item_number > len(cart_items):
                return {
                    "success": False,
                    "message": f"Invalid item number. Cart has {len(cart_items)} items."
                }
            
            if new_quantity <= 0:
                # Remove item if quantity is 0 or negative
                return self.remove_from_cart(user_phone, item_number)
            
            # Update quantity
            item_to_update = cart_items[item_number - 1]
            item_to_update.quantity = new_quantity
            item_to_update.updated_at = datetime.utcnow()
            
            db.commit()
            
            return {
                "success": True,
                "message": f"Updated {item_to_update.product_name} quantity to {new_quantity}"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating cart quantity: {e}")
            return {
                "success": False,
                "message": "Failed to update cart quantity",
                "error": str(e)
            }
        finally:
            db.close()
    
    def check_cart_inventory_and_cleanup(self, user_phone: str) -> Dict[str, Any]:
        """
        Check cart items for inventory availability and clean up out-of-stock items.
        
        Args:
            user_phone: User's phone number
            
        Returns:
            Dictionary with cleanup results
        """
        db = get_db_session()
        try:
            user = self._get_user_by_phone(db, user_phone)
            if not user:
                return {
                    "success": False,
                    "message": "User not found"
                }
            
            cart_items = db.query(Cart).filter(
                Cart.user_id == user.id
            ).all()
            
            removed_items = []
            updated_items = []
            
            for item in cart_items:
                # TODO: Implement actual inventory check with Qdrant/product service
                # For now, we'll assume all items are available
                # In production, check stock levels and update accordingly
                
                # Example logic:
                # stock_level = await product_service.get_stock_level(item.product_id)
                # if stock_level == 0:
                #     removed_items.append(item.product_name)
                #     db.delete(item)
                # elif stock_level < item.quantity:
                #     item.quantity = stock_level
                #     updated_items.append(f"{item.product_name} (qty: {stock_level})")
                pass
            
            db.commit()
            
            return {
                "success": True,
                "message": "Cart inventory checked",
                "removed_items": removed_items,
                "updated_items": updated_items
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error checking cart inventory: {e}")
            return {
                "success": False,
                "message": "Failed to check cart inventory",
                "error": str(e)
            }
        finally:
            db.close()
    
    def _get_or_create_user(self, db: Session, phone_number: str) -> User:
        """Get or create user by phone number."""
        user = db.query(User).filter(User.phone_number == phone_number).first()
        if not user:
            user = User(phone_number=phone_number)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user
    
    def _get_user_by_phone(self, db: Session, phone_number: str) -> Optional[User]:
        """Get user by phone number."""
        return db.query(User).filter(User.phone_number == phone_number).first()


# Global service instance
cart_service = CartService()
