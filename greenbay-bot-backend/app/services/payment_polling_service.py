"""Background service for polling M-Pesa payment status for long-running payments."""

import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any
from loguru import logger

from app.database.db import get_db_session
from app.database.models import Order, Payment, OrderStatus, PaymentStatus
from app.services.mpesa_service import mpesa_service


class PaymentPollingService:
    """Service for polling M-Pesa payment status for pending payments."""
    
    def __init__(self):
        """Initialize payment polling service."""
        self.polling_interval = 60  # Poll every 60 seconds
        self.min_age_minutes = 5  # Only poll payments older than 5 minutes
        self.max_age_minutes = 30  # Stop polling after 30 minutes
    
    async def poll_pending_payments(self) -> Dict[str, Any]:
        """
        Poll M-Pesa API for pending payments that are older than min_age_minutes.
        
        Returns:
            Dictionary with polling results
        """
        try:
            db = get_db_session()
            try:
                # Find pending payments with checkout_request_id that are older than min_age_minutes
                min_age = datetime.utcnow() - timedelta(minutes=self.min_age_minutes)
                max_age = datetime.utcnow() - timedelta(minutes=self.max_age_minutes)
                
                pending_payments = db.query(Payment).join(Order).filter(
                    Payment.status == PaymentStatus.PENDING,
                    Payment.checkout_request_id.isnot(None),
                    Payment.created_at >= max_age,  # Not too old
                    Payment.created_at <= min_age  # Old enough to poll
                ).all()
                
                if not pending_payments:
                    return {
                        "success": True,
                        "polled_count": 0,
                        "updated_count": 0,
                        "message": "No pending payments to poll"
                    }
                
                logger.info(f"Polling {len(pending_payments)} pending payments")
                
                updated_count = 0
                completed_count = 0
                failed_count = 0
                cancelled_count = 0
                
                for payment in pending_payments:
                    try:
                        # Query M-Pesa STK Push status
                        query_result = await mpesa_service.query_stk_push_status(payment.checkout_request_id)
                        
                        if query_result.get("success") and query_result.get("status") == "completed":
                            # Payment completed
                            payment.status = PaymentStatus.COMPLETED
                            payment.order.status = OrderStatus.CONFIRMED.value
                            db.commit()
                            completed_count += 1
                            updated_count += 1
                            logger.info(f"Payment {payment.id} for order {payment.order.order_id} completed via polling")
                            
                        elif query_result.get("status") == "cancelled":
                            # Payment cancelled
                            payment.status = PaymentStatus.CANCELLED
                            payment.failure_reason = query_result.get("result_description", "Cancelled by user")
                            db.commit()
                            cancelled_count += 1
                            updated_count += 1
                            logger.info(f"Payment {payment.id} for order {payment.order.order_id} cancelled via polling")
                            
                        elif query_result.get("status") == "failed":
                            # Payment failed
                            payment.status = PaymentStatus.FAILED
                            payment.failure_reason = query_result.get("result_description", "Payment failed")
                            db.commit()
                            failed_count += 1
                            updated_count += 1
                            logger.info(f"Payment {payment.id} for order {payment.order.order_id} failed via polling")
                        
                        # Add small delay between queries to avoid rate limiting
                        await asyncio.sleep(0.5)
                        
                    except Exception as payment_error:
                        logger.warning(f"Error polling payment {payment.id}: {payment_error}")
                        continue
                
                return {
                    "success": True,
                    "polled_count": len(pending_payments),
                    "updated_count": updated_count,
                    "completed_count": completed_count,
                    "failed_count": failed_count,
                    "cancelled_count": cancelled_count,
                    "message": f"Polled {len(pending_payments)} payments, updated {updated_count}"
                }
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"Error in poll_pending_payments: {e}")
            return {
                "success": False,
                "polled_count": 0,
                "updated_count": 0,
                "message": f"Error polling payments: {str(e)}"
            }
    
    async def start_polling_loop(self):
        """
        Start continuous polling loop for pending payments.
        This should be run as a background task.
        """
        logger.info("Starting payment polling service")
        
        while True:
            try:
                await self.poll_pending_payments()
                await asyncio.sleep(self.polling_interval)
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(self.polling_interval)


# Global service instance
payment_polling_service = PaymentPollingService()

