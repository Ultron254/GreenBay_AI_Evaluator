"""WhatsApp Business API webhook handlers."""

from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
import json
import requests
import asyncio
from loguru import logger

from app.config import get_settings
from app.agent.graph import greenbay_agent
from app.services.s3_service import upload_whatsapp_image_to_s3
from app.services.conversation_log import log_message
from app.database.db import get_db
from app.database.models import User, Conversation
from datetime import datetime
import random
import time

settings = get_settings()

# Create router
whatsapp_router = APIRouter()


class WhatsAppService:
    """Service for handling WhatsApp Business API operations."""
    
    def __init__(self):
        """Initialize WhatsApp service."""
        self.api_token = settings.whatsapp_api_token
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.verify_token = settings.whatsapp_verify_token
        # Use the latest stable version from your WhatsApp app settings
        self.base_url = f"https://graph.facebook.com/v24.0/{self.phone_number_id}"
        logger.info(f"WhatsApp Service initialized with Phone Number ID: {self.phone_number_id}")
    
    async def send_message(
        self,
        to: str,
        message: str,
        message_type: str = "text"
    ) -> Dict[str, Any]:
        """
        Send a message via WhatsApp Business API.
        
        Args:
            to: Recipient phone number
            message: Message content
            message_type: Type of message (text, image, document)
            
        Returns:
            Dictionary with send result
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json"
            }
            
            if message_type == "text":
                payload = {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": message}
                }
            elif message_type == "image":
                payload = {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "image",
                    "image": {"link": message}
                }
            elif message_type == "document":
                payload = {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "document",
                    "document": {"link": message, "filename": "receipt.pdf"}
                }
            else:
                raise ValueError(f"Unsupported message type: {message_type}")
            
            response = requests.post(
                f"{self.base_url}/messages",
                headers=headers,
                json=payload
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"Message sent successfully to {to}")
                return {
                    "success": True,
                    "message_id": data.get("messages", [{}])[0].get("id"),
                    "status": "sent"
                }
            else:
                logger.error(f"Failed to send message: {response.text}")
                return {
                    "success": False,
                    "error": response.text,
                    "status": "failed"
                }
                
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")
            return {
                "success": False,
                "error": str(e),
                "status": "error"
            }
    
    async def send_receipt(
        self,
        to: str,
        receipt_url: str
    ) -> Dict[str, Any]:
        """
        Send a receipt document via WhatsApp.
        
        Args:
            to: Recipient phone number
            receipt_url: URL of the receipt PDF
            
        Returns:
            Dictionary with send result
        """
        return await self.send_message(to, receipt_url, "document")
    
    async def mark_message_as_read_with_typing(self, message_id: str) -> Dict[str, Any]:
        """
        Mark a message as read and send typing indicator in a single request.
        
        According to WhatsApp Business API, this is the correct way to:
        1. Mark message as read
        2. Show typing indicator that user is preparing a response
        
        Args:
            message_id: WhatsApp message ID
            
        Returns:
            Dictionary with operation result
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json"
            }
            
            # WhatsApp Business API: mark as read + typing indicator in one call
            payload = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {
                    "type": "text"
                }
            }
            
            response = requests.post(
                f"{self.base_url}/messages",
                headers=headers,
                json=payload
            )
            
            if response.status_code == 200:
                logger.debug(f"Message {message_id} marked as read with typing indicator")
                return {
                    "success": True,
                    "status": "read_and_typing"
                }
            else:
                logger.debug(f"Failed to mark as read with typing: {response.text}")
                return {
                    "success": False,
                    "error": response.text,
                    "status": "failed"
                }
                
        except Exception as e:
            logger.debug(f"Error marking message as read with typing (non-critical): {e}")
            return {
                "success": False,
                "error": str(e),
                "status": "error"
            }
    
    async def send_typing_indicator(self, to: str, message_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Send typing indicator to show that a response is being prepared.
        
        Note: WhatsApp Business API doesn't support standalone typing indicators.
        This method uses a workaround by sending a very short invisible message
        or by marking the last message as read with typing indicator if message_id is provided.
        
        Args:
            to: Recipient phone number
            message_id: Optional message ID to mark as read with typing indicator
            
        Returns:
            Dictionary with operation result
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json"
            }
            
            # If we have a message_id, use the mark_as_read_with_typing approach
            if message_id:
                return await self.mark_message_as_read_with_typing(message_id)
            
            # WhatsApp Business API doesn't support standalone typing indicators
            # As a workaround, we can send a very short message that acts as a typing indicator
            # However, this is not ideal. Instead, we'll just return success and rely on
            # the typing indicator from mark_message_as_read_with_typing
            
            # Alternative: Send a minimal text message that acts as a "typing" signal
            # But this would be visible to the user, so we skip it
            
            logger.debug(f"Typing indicator requested for {to} (using mark_as_read_with_typing if message_id available)")
            return {
                "success": True,
                "status": "typing",
                "note": "WhatsApp API doesn't support standalone typing indicators"
            }
                
        except Exception as e:
            logger.debug(f"Error sending typing indicator (non-critical): {e}")
            return {
                "success": False,
                "error": str(e),
                "status": "error"
            }
    
    def verify_webhook(self, mode: str, token: str, challenge: str) -> Optional[str]:
        """
        Verify WhatsApp webhook.
        
        Args:
            mode: Verification mode
            token: Verification token
            challenge: Challenge string
            
        Returns:
            Challenge string if verification successful, None otherwise
        """
        if mode == "subscribe" and token == self.verify_token:
            logger.info("WhatsApp webhook verified successfully")
            return challenge
        else:
            logger.warning("WhatsApp webhook verification failed")
            return None
    
    def parse_webhook_message(self, webhook_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Parse incoming webhook message.
        
        Args:
            webhook_data: Raw webhook data
            
        Returns:
            List of parsed messages
        """
        messages = []
        
        try:
            if "entry" in webhook_data:
                for entry in webhook_data["entry"]:
                    if "changes" in entry:
                        for change in entry["changes"]:
                            if change.get("field") == "messages":
                                value = change.get("value", {})
                                
                                # Extract contact info if available
                                contact_name = None
                                if "contacts" in value and len(value["contacts"]) > 0:
                                    contact = value["contacts"][0]
                                    contact_name = contact.get("profile", {}).get("name")
                                
                                if "messages" in value:
                                    for message in value["messages"]:
                                        parsed_message = {
                                            "id": message.get("id"),
                                            "from": message.get("from"),
                                            "timestamp": message.get("timestamp"),
                                            "type": message.get("type"),
                                            "text": None,
                                            "image": None,
                                            "document": None,
                                            "contact_name": contact_name  # Add contact name
                                        }
                                        
                                        # Parse message content based on type
                                        if message.get("type") == "text":
                                            parsed_message["text"] = message.get("text", {}).get("body")
                                        elif message.get("type") == "image":
                                            parsed_message["image"] = message.get("image", {})
                                        elif message.get("type") == "document":
                                            parsed_message["document"] = message.get("document", {})
                                        
                                        messages.append(parsed_message)
            
            return messages
            
        except Exception as e:
            logger.error(f"Error parsing webhook message: {e}")
            return []


# Global WhatsApp service instance
whatsapp_service = WhatsAppService()


@whatsapp_router.get("/webhook")
async def verify_webhook(request: Request):
    """Verify WhatsApp webhook."""
    try:
        # Get query parameters from request
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        
        if not mode or not token or not challenge:
            logger.error(f"Missing parameters - mode: {mode}, token: {token}, challenge: {challenge}")
            raise HTTPException(status_code=400, detail="Missing required parameters")
        
        result = whatsapp_service.verify_webhook(mode, token, challenge)
        
        if result:
            logger.info(f"Webhook verified successfully. Returning challenge: {challenge}")
            # Return plain text response (not JSON) - WhatsApp expects raw challenge string
            return PlainTextResponse(content=challenge, status_code=200)
        else:
            logger.error(f"Verification failed - token mismatch. Expected: {settings.whatsapp_verify_token}, Got: {token}")
            raise HTTPException(status_code=403, detail="Verification failed")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Webhook verification error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@whatsapp_router.post("/webhook")
async def handle_webhook(request: Request):
    """Handle incoming WhatsApp webhook messages."""
    try:
        # Parse webhook data
        webhook_data = await request.json()
        logger.info(f"Received WhatsApp webhook: {json.dumps(webhook_data, indent=2)}")
        
        # Parse messages
        messages = whatsapp_service.parse_webhook_message(webhook_data)
        
        if not messages:
            logger.info("No messages found in webhook")
            return JSONResponse(content={"status": "ok"})
        
        # Process each message
        for message in messages:
            await process_whatsapp_message(message)
        
        return JSONResponse(content={"status": "ok"})
        
    except Exception as e:
        logger.error(f"Error handling WhatsApp webhook: {e}")
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500
        )


def update_user_and_conversation(user_phone: str, contact_name: Optional[str] = None, thread_id: str = None):
    """
    Update or create user with name, and update/create conversation record.
    
    Args:
        user_phone: User's phone number
        contact_name: Contact name from WhatsApp (optional)
        thread_id: Conversation thread ID (optional, will be generated if not provided)
    """
    try:
        db = next(get_db())
        
        # Get or create user
        user = db.query(User).filter(User.phone_number == user_phone).first()
        
        if not user:
            # Create new user
            user = User(phone_number=user_phone, name=contact_name)
            db.add(user)
            db.commit()
            db.refresh(user)
            logger.info(f"Created new user: {user_phone} with name: {contact_name}")
        elif contact_name and user.name != contact_name:
            # Update user name if provided and different
            user.name = contact_name
            user.updated_at = datetime.now()
            db.commit()
            logger.info(f"Updated user name for {user_phone}: {contact_name}")
        
        # Generate thread_id if not provided
        if not thread_id:
            thread_id = f"whatsapp_{user_phone}"
        
        # Get or create conversation
        conversation = db.query(Conversation).filter(
            Conversation.thread_id == thread_id
        ).first()
        
        if not conversation:
            # Create new conversation
            conversation = Conversation(
                user_id=user.id,
                thread_id=thread_id,
                phone_number=user_phone,
                is_active=True
            )
            db.add(conversation)
            db.commit()
            logger.info(f"Created new conversation: {thread_id} for {user_phone}")
        else:
            # Update conversation timestamp
            conversation.last_message_at = datetime.now()
            conversation.is_active = True
            if conversation.user_id != user.id:
                conversation.user_id = user.id
            db.commit()
            logger.debug(f"Updated conversation: {thread_id}")
        
        # Extract user info before closing session (user should exist at this point)
        user_name = user.name if user and user.name else contact_name
        user_id = user.id if user else None
        
        db.close()
        
        # Return user info as dict instead of ORM object
        return {
            "phone_number": user_phone,
            "name": user_name,
            "id": user_id
        }
        
    except Exception as e:
        logger.error(f"Error updating user/conversation: {e}")
        if 'db' in locals():
            db.close()
        return {
            "phone_number": user_phone,
            "name": contact_name,
            "id": None
        }


async def process_whatsapp_message(message: Dict[str, Any]):
    """
    Process a single WhatsApp message.
    
    Args:
        message: Parsed message data
    """
    try:
        user_phone = message.get("from")
        message_type = message.get("type")
        message_id = message.get("id")
        contact_name = message.get("contact_name")  # Extract contact name from message
        message_timestamp = message.get("timestamp")  # Unix timestamp in seconds
        
        if not user_phone:
            logger.warning("No sender phone number found")
            return
        
        # Filter old messages - only process messages from last 4 minutes (240 seconds)
        if message_timestamp:
            import time
            current_time = int(time.time())
            message_time = int(message_timestamp)
            message_age_seconds = current_time - message_time
            
            # Only process messages that are less than 4 minutes old
            if message_age_seconds > 240:
                logger.info(f"Skipping old message from {user_phone} (age: {message_age_seconds}s, {message_age_seconds//60} minutes)")
                return
            else:
                logger.debug(f"Processing recent message from {user_phone} (age: {message_age_seconds}s)")
        
        # Update user and conversation in database
        user_info = update_user_and_conversation(user_phone, contact_name)
        user_name = user_info.get("name") if user_info else contact_name
        
        # Check for duplicate message processing
        if message_id:
            import redis
            from app.config import get_settings
            
            settings = get_settings()
            redis_url = f"redis://"
            if settings.redis_password:
                redis_url += f":{settings.redis_password}@"
            redis_url += f"{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
            
            redis_client = redis.from_url(redis_url)
            processed_key = f"processed_message:{message_id}"
            
            # Check if message was already processed
            if redis_client.get(processed_key):
                logger.info(f"Message {message_id} already processed, skipping")
                return
            
            # Mark message as being processed (expires in 1 hour)
            redis_client.setex(processed_key, 3600, "processed")
        
        # Extract message content based on type
        if message_type == "text":
            content = message.get("text")
            if not content:
                logger.warning("No text content found")
                return
            # Log incoming user message
            try:
                log_message(user_phone=user_phone, role="user", content=content)
            except Exception:
                pass
            
            # Note: "done" messages should always reach the agent
            # The agent and tools will handle duplicate/ongoing analysis gracefully
        elif message_type == "image":
            # Handle image uploads - download and upload to S3, then cache
            image_data = message.get("image", {})
            image_id = image_data.get("id")
            
            if image_id:
                try:
                    # Download image from WhatsApp and upload to S3
                    from app.services.s3_service import upload_whatsapp_image_to_s3
                    from app.config import get_settings
                    
                    settings = get_settings()
                    s3_result = await upload_whatsapp_image_to_s3(
                        image_id=image_id,
                        user_phone=user_phone,
                        access_token=settings.whatsapp_api_token
                    )
                    
                    # Extract S3 key from URL
                    image_url = s3_result["url"]
                    s3_key = s3_result["key"]  # Changed from "s3_key" to "key"
                    
                    # Check if there's an ACTIVE trade-in session (not cancelled, not completed)
                    # CRITICAL: Only process images for sessions that are:
                    # 1. status = "active" (not "cancelled" or "completed")
                    # 2. offer_decision is None or "pending" (not "accepted" or "rejected")
                    # 3. Has product info (product_name is set)
                    from app.database.db import get_db
                    from app.database.models import TradeInSession
                    
                    db = next(get_db())
                    active_trade_in_session = db.query(TradeInSession).filter(
                        TradeInSession.user_phone == user_phone,
                        TradeInSession.status == "active",  # Must be active
                        TradeInSession.product_name.isnot(None),  # Has product info
                        TradeInSession.offer_decision.is_(None)  # Offer not yet decided (None means still in assessment)
                    ).order_by(TradeInSession.created_at.desc()).first()
                    db.close()
                    
                    # Only auto-process images if there's an active trade-in session
                    if active_trade_in_session:
                        # Store image in trade-in session
                        from app.agent.tools import add_images_to_trade_in_session, set_current_user_phone
                        
                        # Set user phone in context for tool to access
                        set_current_user_phone(user_phone)
                        logger.debug(f"Set user phone {user_phone} in context for image upload")
                        
                        # Add to trade-in session
                        # replace_existing=False means new correct images append to existing correct ones
                        # If validation fails, ALL images (old + new) are cleared regardless of this flag
                        db_result = add_images_to_trade_in_session.invoke({
                            "image_urls": [image_url],
                            "s3_keys": [s3_key],
                            "replace_existing": False  # Append correct images, but wrong images clear everything
                        })
                        
                        # Check if validation failed (images don't match product)
                        if not db_result["success"]:
                            # Validation failed - send error message to user
                            error_message = db_result.get("message", "The images you uploaded don't match the product. Please upload correct images.")
                            send_result = await whatsapp_service.send_message(
                                to=user_phone,
                                message=error_message,
                                message_type="text"
                            )
                            if send_result["success"]:
                                logger.info(f"Image validation error sent to {user_phone}: {error_message}")
                                try:
                                    log_message(user_phone=user_phone, role="assistant", content=error_message)
                                except Exception:
                                    pass
                            return  # Don't process further
                    
                        # Images validated successfully
                        if db_result["success"]:
                            # Mark image message as read (if message_id available)
                            if message_id:
                                try:
                                    await whatsapp_service.mark_message_as_read_with_typing(message_id)
                                except Exception as e:
                                    logger.warning(f"Failed to mark image message as read: {e}")
                            
                            # For image uploads, send a simple acknowledgment and don't trigger agent
                            # The agent will handle "done" message separately to trigger assessment
                            # Use dynamic messages to avoid repetition
                            image_acknowledgments = [
                                "Got your images! 📸 Let me know when you're ready for me to analyze them by saying \"done\" or \"analyze.\"",
                                "Images received! 👍 Just say \"done\" or \"analyze\" when you're ready for me to review them.",
                                "Perfect! I've got your photos. 😊 When you're ready, just say \"done\" or \"analyze\" and I'll take a look.",
                                "Thanks for the images! 📷 Say \"done\" or \"analyze\" whenever you're ready for me to assess them.",
                                "Images saved! ✅ Let me know when you want me to analyze them - just say \"done\" or \"analyze.\""
                            ]
                            response_text = random.choice(image_acknowledgments)
                            send_result = await whatsapp_service.send_message(
                                to=user_phone,
                                message=response_text,
                                message_type="text"
                            )
                            if send_result["success"]:
                                logger.info(f"Image upload acknowledgment sent to {user_phone}")
                                try:
                                    log_message(user_phone=user_phone, role="assistant", content=response_text)
                                except Exception:
                                    pass
                            return  # Don't process with agent - just acknowledge and wait for "done"
                        else:
                            content = f"[IMAGE_UPLOADED] Image uploaded but failed to store in session: {db_result['message']}"
                            logger.warning(f"Image uploaded but failed to store in database for {user_phone}: {db_result['message']}")
                    else:
                        # No active trade-in session - let agent handle the image contextually
                        logger.info(f"No active trade-in session for {user_phone}, passing image to agent for context-aware handling")
                        content = f"[User uploaded an image: {image_url}]"
                        # Continue to agent processing below
                        
                except Exception as e:
                    logger.error(f"Error processing image upload for {user_phone}: {e}")
                    content = f"[IMAGE_UPLOADED] Error: {str(e)}"
            else:
                logger.warning("No image ID found")
                return
        elif message_type == "document":
            # Handle document uploads
            doc_data = message.get("document", {})
            doc_id = doc_data.get("id")
            
            if doc_id:
                content = f"[DOCUMENT_UPLOADED:{doc_id}]"
                logger.info(f"Document uploaded by {user_phone}: {doc_id}")
            else:
                logger.warning("No document ID found")
                return
        else:
            logger.info(f"Unsupported message type: {message_type}")
            return
        
        # Mark message as read and show typing indicator in one call
        # WhatsApp automatically dismisses typing indicator when we respond or after 25 seconds
        if message_id:
            try:
                await whatsapp_service.mark_message_as_read_with_typing(message_id)
            except Exception as e:
                logger.warning(f"Failed to mark message as read with typing: {e}")
        
        # Generate thread ID for conversation continuity (same as in update_user_and_conversation)
        thread_id = f"whatsapp_{user_phone}"
        
        # Record start time for processing duration
        start_time = time.time()
        
        # Keep typing indicator active during agent processing
        # WhatsApp typing indicators auto-dismiss after 25 seconds, so we refresh it periodically
        # Since WhatsApp doesn't support standalone typing indicators, we'll use mark_as_read_with_typing
        # with the original message_id to refresh the typing indicator
        async def keep_typing_active():
            """Keep typing indicator active by refreshing it every 20 seconds."""
            while True:
                await asyncio.sleep(20)  # Refresh every 20 seconds (before 25s timeout)
                try:
                    # Refresh typing indicator by marking the original message as read again
                    if message_id:
                        await whatsapp_service.mark_message_as_read_with_typing(message_id)
                        logger.debug(f"Refreshed typing indicator for {user_phone} during processing")
                    else:
                        break  # No message_id to refresh with
                except Exception as e:
                    logger.debug(f"Failed to refresh typing indicator (non-critical): {e}")
                    break  # Stop trying if it fails
        
        # Start background task to keep typing indicator active
        typing_task = None
        if message_type == "text" and content:
            typing_task = asyncio.create_task(keep_typing_active())
        
        # Process message with agent
        # Note: Typing indicator will automatically dismiss when we send response or after 25 seconds
        try:
            agent_response = await greenbay_agent.process_message(
                user_phone=user_phone,
                message=content,
                thread_id=thread_id
            )
        except Exception as e:
            logger.error(f"Error processing message for {user_phone}: {e}")
            agent_response = {
                "success": False,
                "response": "Sorry, I encountered an issue. Could you try again?"
            }
        finally:
            # Cancel typing indicator refresh task when processing is done
            if typing_task:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
        
        # Calculate processing time
        processing_time = time.time() - start_time
        
        # Ensure typing indicator is active right before sending response
        # This handles cases where processing was fast but typing indicator expired
        if processing_time > 0.5 and message_id:  # Only if processing took some time and we have message_id
            try:
                await whatsapp_service.mark_message_as_read_with_typing(message_id)
                logger.debug(f"Refreshed typing indicator before response to {user_phone}")
            except Exception as e:
                logger.debug(f"Failed to refresh typing indicator before response (non-critical): {e}")
        
        # Log processing time
        if processing_time < 2.0:
            logger.debug(f"Fast response for {user_phone}: {processing_time:.2f}s")
        else:
            logger.info(f"Processing took {processing_time:.2f}s for {user_phone}")
        
        if agent_response["success"]:
            # Send response back to user
            response_text = agent_response["response"]
            
            # Send final response
            send_result = await whatsapp_service.send_message(
                to=user_phone,
                message=response_text,
                message_type="text"
            )
            
            if send_result["success"]:
                logger.info(f"Response sent successfully to {user_phone}")
                # Log assistant response
                try:
                    log_message(user_phone=user_phone, role="assistant", content=response_text)
                except Exception:
                    pass
            else:
                logger.error(f"Failed to send response to {user_phone}: {send_result['error']}")
        else:
            # Send error message
            error_message = "I'm sorry, I encountered an error. Please try again or contact support."
            await whatsapp_service.send_message(
                to=user_phone,
                message=error_message,
                message_type="text"
            )
            logger.error(f"Agent processing failed for {user_phone}: {agent_response.get('error')}")
        
    except Exception as e:
        logger.error(f"Error processing WhatsApp message: {e}")
        
        # Send error message to user
        try:
            user_phone = message.get("from")
            if user_phone:
                await whatsapp_service.send_message(
                    to=user_phone,
                    message="I'm sorry, I encountered an error. Please try again.",
                    message_type="text"
                )
        except Exception as send_error:
            logger.error(f"Failed to send error message: {send_error}")


@whatsapp_router.post("/webhook/send")
async def send_message_endpoint(
    to: str,
    message: str,
    message_type: str = "text"
):
    """Send a message via WhatsApp (for testing or manual use)."""
    try:
        result = await whatsapp_service.send_message(to, message, message_type)
        return JSONResponse(content=result)
        
    except Exception as e:
        logger.error(f"Error sending message: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )


@whatsapp_router.post("/webhook/send-receipt")
async def send_receipt_endpoint(
    to: str,
    receipt_url: str
):
    """Send a receipt document via WhatsApp."""
    try:
        result = await whatsapp_service.send_receipt(to, receipt_url)
        return JSONResponse(content=result)
        
    except Exception as e:
        logger.error(f"Error sending receipt: {e}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )
