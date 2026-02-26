"""Tools for managing interactive WhatsApp reply buttons."""

from typing import List, Dict, Any
from langchain_core.tools import tool
from loguru import logger


@tool
def suggest_reply_buttons(buttons: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Suggest interactive reply buttons to display to the user.
    
    Use this tool BEFORE sending your response to provide quick-action buttons.
    The buttons will be displayed alongside your message in WhatsApp.
    
    **When to use buttons:**
    - Offering menu choices (e.g., "Product Discovery", "Trade-in", "Track Order")
    - Yes/No questions (e.g., "Yes", "No")
    - Confirmation requests (e.g., "Confirm", "Cancel")
    - Multiple choice options (e.g., product categories, delivery options)
    - Action prompts (e.g., "View Cart", "Checkout Now", "Continue Shopping")
    
    **Button guidelines:**
    - Provide 1-3 buttons (max 3 for WhatsApp)
    - Keep button text short (max 20 characters recommended)
    - Use clear, action-oriented language
    - Match buttons to the conversation context
    
    Args:
        buttons: List of button dictionaries, each with:
            - id: Unique identifier for the button (e.g., "product_discovery", "yes", "no")
            - title: Display text shown on the button (max 20 chars recommended)
            
    Examples:
        # Main menu
        suggest_reply_buttons([
            {"id": "product_discovery", "title": "🔍 Browse Products"},
            {"id": "trade_in", "title": "📱 Trade-in"},
            {"id": "track_order", "title": "📦 Track Order"}
        ])
        
        # Yes/No question
        suggest_reply_buttons([
            {"id": "yes", "title": "Yes"},
            {"id": "no", "title": "No"}
        ])
        
        # Product categories
        suggest_reply_buttons([
            {"id": "phones", "title": "📱 Phones"},
            {"id": "laptops", "title": "💻 Laptops"},
            {"id": "accessories", "title": "🎧 Accessories"}
        ])
    
    Returns:
        Dict with success status and button information
    """
    try:
        # Validate buttons
        if not buttons or not isinstance(buttons, list):
            return {
                "success": False,
                "error": "Buttons must be a non-empty list"
            }
        
        if len(buttons) > 3:
            logger.warning(f"Too many buttons provided ({len(buttons)}), WhatsApp supports max 3. Using first 3.")
            buttons = buttons[:3]
        
        # Validate each button
        validated_buttons = []
        for idx, button in enumerate(buttons):
            if not isinstance(button, dict):
                logger.warning(f"Button {idx} is not a dict, skipping")
                continue
                
            if "id" not in button or "title" not in button:
                logger.warning(f"Button {idx} missing 'id' or 'title', skipping")
                continue
                
            # Clean and validate
            btn_id = str(button["id"]).strip()
            btn_title = str(button["title"]).strip()
            
            if not btn_id or not btn_title:
                logger.warning(f"Button {idx} has empty id or title, skipping")
                continue
                
            if len(btn_title) > 20:
                logger.warning(f"Button title too long ({len(btn_title)} chars): '{btn_title}'. Truncating to 20 chars.")
                btn_title = btn_title[:20]
            
            validated_buttons.append({
                "id": btn_id,
                "title": btn_title
            })
        
        if not validated_buttons:
            return {
                "success": False,
                "error": "No valid buttons after validation"
            }
        
        logger.info(f"✓ Suggested {len(validated_buttons)} reply buttons: {[b['title'] for b in validated_buttons]}")
        
        return {
            "success": True,
            "buttons": validated_buttons,
            "count": len(validated_buttons),
            "message": f"Buttons will be displayed with your response"
        }
        
    except Exception as e:
        logger.error(f"Error in suggest_reply_buttons: {e}")
        return {
            "success": False,
            "error": str(e)
        }
