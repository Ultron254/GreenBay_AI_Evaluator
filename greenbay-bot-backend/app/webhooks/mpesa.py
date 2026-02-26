"""M-Pesa Daraja API webhook handlers."""

from typing import Dict, Any
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
import json
from datetime import datetime
from loguru import logger

from app.config import get_settings
from app.services.mpesa_service import mpesa_service
from app.services.order_service import order_service
from app.services.receipt_service import receipt_generator
from app.database.db import get_db_session
from app.database.models import Payment, PaymentStatus, Order, OrderStatus, Delivery, OrderItem, User

settings = get_settings()

# Create router
mpesa_router = APIRouter()


@mpesa_router.post("/webhook/mpesa/callback")
async def handle_mpesa_callback(request: Request):
    """Handle M-Pesa payment callbacks."""
    try:
        # Parse callback data
        callback_data = await request.json()
        logger.info(f"Received M-Pesa callback: {json.dumps(callback_data, indent=2)}")
        
        # Process callback based on type
        if "Body" in callback_data:
            # STK Push callback
            await process_stk_push_callback(callback_data["Body"])
        elif "Result" in callback_data:
            # STK Push query callback
            await process_stk_query_callback(callback_data["Result"])
        else:
            logger.warning("Unknown M-Pesa callback format")
        
        return JSONResponse(content={"ResultCode": 0, "ResultDesc": "Success"})
        
    except Exception as e:
        logger.error(f"Error handling M-Pesa callback: {e}")
        return JSONResponse(
            content={"ResultCode": 1, "ResultDesc": "Error processing callback"},
            status_code=500
        )


async def process_stk_push_callback(callback_body: Dict[str, Any]):
    """
    Process STK Push callback.
    
    Args:
        callback_body: STK Push callback body
    """
    try:
        stk_callback = callback_body.get("stkCallback", {})
        merchant_request_id = stk_callback.get("MerchantRequestID")
        checkout_request_id = stk_callback.get("CheckoutRequestID")
        result_code = stk_callback.get("ResultCode")
        result_desc = stk_callback.get("ResultDesc")
        
        logger.info(f"STK Push callback - ResultCode: {result_code}, ResultDesc: {result_desc}")
        
        if result_code == 0:
            # Payment successful
            callback_metadata = stk_callback.get("CallbackMetadata", {})
            items = callback_metadata.get("Item", [])
            
            # Extract payment details
            payment_details = {}
            for item in items:
                name = item.get("Name")
                value = item.get("Value")
                if name and value:
                    payment_details[name] = value
            
            # Process successful payment
            await process_successful_payment(
                merchant_request_id=merchant_request_id,
                checkout_request_id=checkout_request_id,
                payment_details=payment_details
            )
        else:
            # Payment failed
            await process_failed_payment(
                merchant_request_id=merchant_request_id,
                checkout_request_id=checkout_request_id,
                result_code=result_code,
                result_desc=result_desc
            )
            
    except Exception as e:
        logger.error(f"Error processing STK Push callback: {e}")


async def process_stk_query_callback(callback_result: Dict[str, Any]):
    """
    Process STK Push query callback.
    
    Args:
        callback_result: STK Push query callback result
    """
    try:
        result_code = callback_result.get("ResultCode")
        result_desc = callback_result.get("ResultDesc")
        merchant_request_id = callback_result.get("MerchantRequestID")
        checkout_request_id = callback_result.get("CheckoutRequestID")
        
        logger.info(f"STK Query callback - ResultCode: {result_code}, ResultDesc: {result_desc}")
        
        if result_code == 0:
            # Payment successful
            callback_metadata = callback_result.get("CallbackMetadata", {})
            items = callback_metadata.get("Item", [])
            
            # Extract payment details
            payment_details = {}
            for item in items:
                name = item.get("Name")
                value = item.get("Value")
                if name and value:
                    payment_details[name] = value
            
            # Process successful payment
            await process_successful_payment(
                merchant_request_id=merchant_request_id,
                checkout_request_id=checkout_request_id,
                payment_details=payment_details
            )
        else:
            # Payment failed
            await process_failed_payment(
                merchant_request_id=merchant_request_id,
                checkout_request_id=checkout_request_id,
                result_code=result_code,
                result_desc=result_desc
            )
            
    except Exception as e:
        logger.error(f"Error processing STK Query callback: {e}")


async def process_successful_payment(
    merchant_request_id: str,
    checkout_request_id: str,
    payment_details: Dict[str, Any]
):
    """
    Process successful payment and generate receipt.
    
    Args:
        merchant_request_id: Merchant request ID
        checkout_request_id: Checkout request ID
        payment_details: Payment details from callback
    """
    try:
        db = get_db_session()
        
        # Extract payment information
        amount = float(payment_details.get("Amount", 0))
        mpesa_receipt_number = payment_details.get("MpesaReceiptNumber")
        transaction_date = payment_details.get("TransactionDate")
        phone_number = payment_details.get("PhoneNumber")
        
        logger.info(f"Payment successful - Amount: {amount}, Receipt: {mpesa_receipt_number}")
        
        # Find the order by checkout request ID (AccountReference in STK Push)
        # We need to find recent pending order for this phone number
        order = db.query(Order).filter(
            Order.user.has(phone_number=phone_number),
            Order.status == OrderStatus.PENDING_PAYMENT
        ).order_by(Order.created_at.desc()).first()
        
        if order:
            # Update payment record
            payment = db.query(Payment).filter(Payment.order_id == order.id).first()
            if payment:
                payment.mpesa_transaction_id = mpesa_receipt_number
                payment.mpesa_receipt_number = mpesa_receipt_number
                payment.status = PaymentStatus.COMPLETED
                payment.payment_date = datetime.utcnow()
            else:
                # Create new payment record
                payment = Payment(
                    order_id=order.id,
                    mpesa_transaction_id=mpesa_receipt_number,
                    mpesa_receipt_number=mpesa_receipt_number,
                    amount=amount,
                    status=PaymentStatus.COMPLETED,
                    phone_number=phone_number,
                    payment_date=datetime.utcnow()
                )
                db.add(payment)
            
            # Update order status
            order.status = OrderStatus.CONFIRMED
            db.commit()
            
            logger.info(f"Order {order.order_id} payment confirmed")
            
            # Generate PDF receipt
            try:
                # Get customer name
                customer_name = None
                user = db.query(User).filter(User.phone_number == phone_number).first()
                if user and user.name:
                    customer_name = user.name
                
                # Get order items
                order_items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
                items = [
                    {
                        "product_name": item.product_name,
                        "quantity": item.quantity,
                        "price": item.price
                    }
                    for item in order_items
                ]
                
                # Get delivery info
                delivery = db.query(Delivery).filter(Delivery.order_id == order.id).first()
                delivery_address = delivery.delivery_address if delivery else ""
                delivery_timeline = delivery.status.value if delivery else ""
                
                # Generate receipt
                receipt_path = receipt_generator.generate_receipt(
                    order_id=order.order_id,
                    customer_phone=phone_number,
                    items=items,
                    subtotal=order.subtotal,
                    delivery_cost=order.delivery_cost,
                    installation_cost=order.installation_cost,
                    total_amount=order.total_amount,
                    delivery_address=delivery_address,
                    delivery_timeline=delivery_timeline,
                    payment_method="M-Pesa",
                    mpesa_transaction_id=mpesa_receipt_number,
                    customer_name=customer_name
                )
                
                logger.info(f"Receipt generated successfully: {receipt_path}")
                
                # Upload receipt to S3 and send via WhatsApp
                try:
                    import asyncio
                    import os
                    from app.services.s3_service import upload_bytes_to_s3, generate_s3_key
                    from app.webhooks.whatsapp import WhatsAppService
                    
                    # Read PDF file
                    with open(receipt_path, "rb") as f:
                        pdf_bytes = f.read()
                    
                    # Generate S3 key for receipt
                    filename = os.path.basename(receipt_path)
                    s3_key = generate_s3_key(phone_number, filename)
                    
                    # Upload to S3
                    s3_result = asyncio.run(upload_bytes_to_s3(
                        bytes_data=pdf_bytes,
                        key=s3_key,
                        content_type="application/pdf"
                    ))
                    
                    receipt_url = s3_result["url"]
                    logger.info(f"Receipt uploaded to S3: {s3_key}, URL: {receipt_url}")
                    
                    # Initialize WhatsApp service
                    whatsapp_service = WhatsAppService()
                    
                    # Send receipt via WhatsApp (customer_name already retrieved above in receipt generation)
                    greeting = f"Hi {customer_name}!" if customer_name else "Hi!"
                    message = f"{greeting} Your payment has been confirmed! 🎉\n\nYour receipt has been sent below."
                    
                    # Send message first
                    asyncio.run(whatsapp_service.send_message(
                        to=phone_number,
                        message=message,
                        message_type="text"
                    ))
                    
                    # Then send receipt PDF
                    send_result = asyncio.run(whatsapp_service.send_receipt(
                        to=phone_number,
                        receipt_url=receipt_url
                    ))
                    
                    if send_result.get("success"):
                        logger.info(f"Receipt sent successfully to {phone_number} via WhatsApp")
                    else:
                        logger.warning(f"Failed to send receipt via WhatsApp: {send_result.get('error')}")
                        
                except Exception as send_error:
                    logger.error(f"Error sending receipt via WhatsApp: {send_error}")
                
            except Exception as receipt_error:
                logger.error(f"Error generating receipt: {receipt_error}")
        else:
            logger.warning(f"No pending order found for phone {phone_number}")
            # Still create payment record for tracking
            payment_record = Payment(
                mpesa_transaction_id=mpesa_receipt_number,
                mpesa_receipt_number=mpesa_receipt_number,
                amount=amount,
                status=PaymentStatus.COMPLETED,
                phone_number=phone_number,
                payment_date=datetime.utcnow()
            )
            db.add(payment_record)
            db.commit()
        
    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")
    finally:
        db.close()


async def process_failed_payment(
    merchant_request_id: str,
    checkout_request_id: str,
    result_code: int,
    result_desc: str
):
    """
    Process failed payment.
    
    Args:
        merchant_request_id: Merchant request ID
        checkout_request_id: Checkout request ID
        result_code: Result code
        result_desc: Result description
    """
    try:
        db = get_db_session()
        
        logger.info(f"Payment failed - ResultCode: {result_code}, ResultDesc: {result_desc}")
        
        # Update payment status in database
        # In a real implementation, you'd find the specific order and update it
        payment_record = Payment(
            mpesa_transaction_id=checkout_request_id,
            amount=0.0,
            status=PaymentStatus.FAILED,
            failure_reason=result_desc
        )
        
        db.add(payment_record)
        db.commit()
        
        logger.info(f"Failed payment record created: {checkout_request_id}")
        
    except Exception as e:
        logger.error(f"Error processing failed payment: {e}")
    finally:
        db.close()


@mpesa_router.post("/webhook/mpesa/query-status")
async def query_payment_status(checkout_request_id: str):
    """Query M-Pesa payment status."""
    try:
        result = await mpesa_service.query_stk_push_status(checkout_request_id)
        return JSONResponse(content=result)
        
    except Exception as e:
        logger.error(f"Error querying payment status: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@mpesa_router.get("/webhook/mpesa/health")
async def mpesa_health_check():
    """Check M-Pesa service health."""
    try:
        is_healthy = await mpesa_service.health_check()
        return JSONResponse(content={
            "healthy": is_healthy,
            "service": "M-Pesa Daraja API",
            "timestamp": datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"M-Pesa health check failed: {e}")
        return JSONResponse(
            content={"healthy": False, "error": str(e)},
            status_code=500
        )
