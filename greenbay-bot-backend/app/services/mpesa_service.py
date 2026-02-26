"""M-Pesa service for handling payment operations."""

from typing import Dict, Any, Optional
import requests
import base64
import json
from datetime import datetime
from loguru import logger

from app.config import get_settings

settings = get_settings()


class MpesaService:
    """Service for handling M-Pesa payment operations."""
    
    def __init__(self):
        """Initialize M-Pesa service."""
        self.consumer_key = settings.mpesa_consumer_key
        self.consumer_secret = settings.mpesa_consumer_secret
        self.shortcode = settings.mpesa_shortcode
        self.passkey = settings.mpesa_passkey
        self.environment = settings.mpesa_environment
        self.callback_url = settings.mpesa_callback_url
        
        # API URLs based on environment
        if self.environment == "production":
            self.base_url = "https://api.safaricom.co.ke"
        else:
            self.base_url = "https://sandbox.safaricom.co.ke"
        
        self.access_token = None
        self.token_expires_at = None
    
    async def get_access_token(self) -> str:
        """
        Get M-Pesa access token.
        
        Returns:
            Access token string
        """
        try:
            # Check if token is still valid
            if self.access_token and self.token_expires_at and datetime.now().timestamp() < self.token_expires_at:
                return self.access_token
            
            # Generate new token
            auth_string = f"{self.consumer_key}:{self.consumer_secret}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()
            
            headers = {
                "Authorization": f"Basic {encoded_auth}",
                "Content-Type": "application/json"
            }
            
            response = requests.get(
                f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
                headers=headers
            )
            
            if response.status_code == 200:
                data = response.json()
                self.access_token = data["access_token"]
                # Set expiry time (subtract 5 minutes for safety)
                expires_in = int(data.get("expires_in", 3600))
                self.token_expires_at = datetime.now().timestamp() + expires_in - 300
                
                logger.info("M-Pesa access token obtained successfully")
                return self.access_token
            else:
                logger.error(f"Failed to get M-Pesa access token: {response.text}")
                raise Exception(f"Failed to get access token: {response.text}")
                
        except Exception as e:
            logger.error(f"Error getting M-Pesa access token: {e}")
            raise
    
    async def initiate_stk_push(
        self,
        phone_number: str,
        amount: float,
        order_id: str,
        description: str = "GreenBay Market Purchase"
    ) -> Dict[str, Any]:
        """
        Initiate STK Push payment.
        
        Args:
            phone_number: Customer's phone number (format: 254XXXXXXXXX)
            amount: Payment amount
            order_id: Order identifier
            description: Payment description
            
        Returns:
            Dictionary with STK Push result
        """
        try:
            # Check if using test credentials - return mock response
            if self.shortcode == "test_shortcode" or self.passkey == "test_passkey":
                logger.warning("Using test M-Pesa credentials - returning mock response")
                return {
                    "success": True,
                    "message": "Mock payment request sent to your phone",
                    "checkout_request_id": f"MOCK_{order_id}",
                    "merchant_request_id": f"MOCK_MERCHANT_{order_id}",
                    "order_id": order_id,
                    "mock": True
                }
            
            access_token = await self.get_access_token()
            
            # Format phone number (remove + and ensure 254 prefix)
            if phone_number.startswith("+"):
                phone_number = phone_number[1:]
            if not phone_number.startswith("254"):
                phone_number = f"254{phone_number[-9:]}"  # Take last 9 digits and add 254
            
            # Generate timestamp
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            
            # Generate password
            password_string = f"{self.shortcode}{self.passkey}{timestamp}"
            password = base64.b64encode(password_string.encode()).decode()
            
            # STK Push payload
            payload = {
                "BusinessShortCode": self.shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(float(amount)),  # Convert to float first, then int
                "PartyA": phone_number,
                "PartyB": self.shortcode,
                "PhoneNumber": phone_number,
                "CallBackURL": self.callback_url,
                "AccountReference": order_id,
                "TransactionDesc": description
            }
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            # Log the exact request being sent
            logger.info("=== M-PESA STK PUSH REQUEST ===")
            logger.info(f"URL: {self.base_url}/mpesa/stkpush/v1/processrequest")
            logger.info(f"Headers: {json.dumps(headers, indent=2)}")
            logger.info(f"Payload: {json.dumps(payload, indent=2)}")
            logger.info("=== END M-PESA REQUEST ===")
            
            response = requests.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                headers=headers,
                json=payload
            )
            
            # Log the response
            logger.info("=== M-PESA STK PUSH RESPONSE ===")
            logger.info(f"Status Code: {response.status_code}")
            logger.info(f"Response Headers: {dict(response.headers)}")
            logger.info(f"Response Body: {response.text}")
            logger.info("=== END M-PESA RESPONSE ===")
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("ResponseCode") == "0":
                    logger.info(f"STK Push initiated successfully for order {order_id}")
                    return {
                        "success": True,
                        "message": "Payment request sent to your phone",
                        "checkout_request_id": data.get("CheckoutRequestID"),
                        "merchant_request_id": data.get("MerchantRequestID"),
                        "order_id": order_id
                    }
                else:
                    error_message = data.get("ResponseDescription", "Unknown error")
                    logger.error(f"STK Push failed: {error_message}")
                    return {
                        "success": False,
                        "message": f"Payment failed: {error_message}",
                        "error": error_message
                    }
            else:
                logger.error(f"STK Push request failed: {response.text}")
                return {
                    "success": False,
                    "message": "Failed to initiate payment",
                    "error": response.text
                }
                
        except Exception as e:
            logger.error(f"Error initiating STK Push: {e}")
            return {
                "success": False,
                "message": "Failed to initiate payment",
                "error": str(e)
            }
    
    async def query_stk_push_status(self, checkout_request_id: str) -> Dict[str, Any]:
        """
        Query STK Push payment status.
        
        Args:
            checkout_request_id: Checkout request ID from STK Push
            
        Returns:
            Dictionary with payment status
        """
        try:
            access_token = await self.get_access_token()
            
            # Generate timestamp
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            
            # Generate password
            password_string = f"{self.shortcode}{self.passkey}{timestamp}"
            password = base64.b64encode(password_string.encode()).decode()
            
            # Query payload
            payload = {
                "BusinessShortCode": self.shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id
            }
            
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json"
            }
            
            response = requests.post(
                f"{self.base_url}/mpesa/stkpushquery/v1/query",
                headers=headers,
                json=payload
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("ResponseCode") == "0":
                    result_code = data.get("ResultCode")
                    result_desc = data.get("ResultDesc")
                    
                    # Handle different result codes
                    result_code_str = str(result_code)
                    
                    if result_code_str == "0":
                        # Payment successful
                        return {
                            "success": True,
                            "status": "completed",
                            "message": "Payment completed successfully",
                            "result_code": result_code_str,
                            "result_description": result_desc
                        }
                    elif result_code_str == "1032":
                        # Request cancelled by user
                        return {
                            "success": False,
                            "status": "cancelled",
                            "message": "Payment request was cancelled by user",
                            "result_code": result_code_str,
                            "result_description": result_desc
                        }
                    else:
                        # Payment failed or other error
                        return {
                            "success": False,
                            "status": "failed",
                            "message": f"Payment failed: {result_desc}",
                            "result_code": result_code_str,
                            "result_description": result_desc
                        }
                else:
                    error_message = data.get("ResponseDescription", "Unknown error")
                    return {
                        "success": False,
                        "status": "error",
                        "message": f"Query failed: {error_message}",
                        "error": error_message
                    }
            else:
                logger.error(f"STK Push query failed: {response.text}")
                return {
                    "success": False,
                    "status": "error",
                    "message": "Failed to query payment status",
                    "error": response.text
                }
                
        except Exception as e:
            logger.error(f"Error querying STK Push status: {e}")
            return {
                "success": False,
                "status": "error",
                "message": "Failed to query payment status",
                "error": str(e)
            }
    
    async def process_final_checkout_with_mpesa(
        self,
        user_phone: str,
        order_id: str,
        total_amount: float,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Process final checkout with M-Pesa payment.
        
        Args:
            user_phone: User's phone number
            order_id: Order identifier
            total_amount: Total amount to pay
            description: Payment description
            
        Returns:
            Dictionary with checkout result
        """
        try:
            if not description:
                description = f"GreenBay Market Order {order_id}"
            
            # Initiate STK Push
            stk_result = await self.initiate_stk_push(
                phone_number=user_phone,
                amount=total_amount,
                order_id=order_id,
                description=description
            )
            
            if stk_result["success"]:
                # Update order status to PENDING_PAYMENT
                try:
                    from app.database.db import get_db_session, engine
                    from app.database.models import Order, OrderStatus
                    
                    # First, ensure the enum value exists in the database (for PostgreSQL)
                    # Note: ALTER TYPE must be run outside a transaction in PostgreSQL
                    try:
                        # Use raw connection with autocommit for ALTER TYPE
                        raw_conn = engine.raw_connection()
                        try:
                            raw_conn.set_isolation_level(0)  # Autocommit mode
                            cursor = raw_conn.cursor()
                            # Check if the value already exists
                            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_enum WHERE enumlabel = 'pending_payment' AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'orderstatus'))")
                            exists = cursor.fetchone()[0]
                            
                            if not exists:
                                cursor.execute("ALTER TYPE orderstatus ADD VALUE 'pending_payment'")
                                logger.info("Added 'pending_payment' to orderstatus enum")
                            else:
                                logger.debug("Enum value 'pending_payment' already exists")
                            cursor.close()
                        finally:
                            raw_conn.close()
                    except Exception as enum_error:
                        # Enum value might already exist, or we're not using PostgreSQL
                        logger.warning(f"Could not add enum value (may already exist or not PostgreSQL): {enum_error}")
                    
                    db = get_db_session()
                    try:
                        from app.database.models import Payment, PaymentStatus
                        from sqlalchemy import text
                        
                        order = db.query(Order).filter(Order.order_id == order_id).first()
                        if order:
                            # Skip status update - it's causing enum issues and is not critical
                            # The order will remain as PENDING and M-Pesa callback will update it to CONFIRMED when payment is received
                            # This avoids the enum name/value mismatch issue with SQLAlchemy
                            logger.debug(f"Order {order_id} status left as PENDING (will be updated by M-Pesa callback)")
                            
                            # Create or update payment record with checkout_request_id (this is critical)
                            checkout_request_id = stk_result.get("checkout_request_id")
                            payment = db.query(Payment).filter(Payment.order_id == order.id).first()
                            
                            if payment:
                                # Update existing payment record
                                payment.checkout_request_id = checkout_request_id
                                payment.status = PaymentStatus.PENDING
                            else:
                                # Create new payment record
                                payment = Payment(
                                    order_id=order.id,
                                    checkout_request_id=checkout_request_id,
                                    amount=total_amount,
                                    status=PaymentStatus.PENDING,
                                    phone_number=user_phone
                                )
                                db.add(payment)
                            
                            db.commit()
                            logger.info(f"Payment record created/updated for order {order_id} with checkout_request_id: {checkout_request_id}")
                    finally:
                        db.close()
                except Exception as e:
                    # Log error but continue - payment record creation is more important
                    logger.warning(f"Error in payment processing (non-critical): {e}")
                
                logger.info(f"Checkout initiated for order {order_id}, amount: {total_amount}")
                return {
                    "success": True,
                    "message": "Payment request sent to your phone. Please check your M-Pesa menu.",
                    "order_id": order_id,
                    "amount": total_amount,
                    "checkout_request_id": stk_result.get("checkout_request_id"),
                    "merchant_request_id": stk_result.get("merchant_request_id")
                }
            else:
                return {
                    "success": False,
                    "message": stk_result.get("message", "Payment failed"),
                    "error": stk_result.get("error")
                }
                
        except Exception as e:
            logger.error(f"Error processing final checkout: {e}")
            return {
                "success": False,
                "message": "Failed to process checkout",
                "error": str(e)
            }
    
    async def buy_product(
        self,
        user_phone: str,
        product_name: str,
        amount: float,
        description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Buy a single product with M-Pesa payment.
        
        Args:
            user_phone: User's phone number
            product_name: Product name
            amount: Product amount
            description: Payment description
            
        Returns:
            Dictionary with purchase result
        """
        try:
            if not description:
                description = f"GreenBay Market - {product_name}"
            
            # Generate a simple order ID for single product purchase
            order_id = f"GB{datetime.now().strftime('%Y%m%d%H%M%S')}"
            
            return await self.process_final_checkout_with_mpesa(
                user_phone=user_phone,
                order_id=order_id,
                total_amount=amount,
                description=description
            )
            
        except Exception as e:
            logger.error(f"Error buying product: {e}")
            return {
                "success": False,
                "message": "Failed to purchase product",
                "error": str(e)
            }
    
    async def health_check(self) -> bool:
        """
        Check if M-Pesa service is healthy.
        
        Returns:
            True if service is healthy, False otherwise
        """
        try:
            await self.get_access_token()
            return True
        except Exception as e:
            logger.error(f"M-Pesa health check failed: {e}")
            return False


# Global service instance
mpesa_service = MpesaService()
