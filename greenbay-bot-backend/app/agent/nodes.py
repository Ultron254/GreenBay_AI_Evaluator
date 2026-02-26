"""LangGraph workflow nodes for GreenBay Market chatbot.

⚠️ DEPRECATED: This file is NOT used in the current implementation.

The system now uses LangGraph's `create_react_agent` which automatically handles:
- Intent classification (via tool selection)
- Routing (via reasoning)
- State transitions (via messages)

This file is kept for reference but is not imported or used anywhere.
The ReAct agent pattern replaces the need for custom nodes and routers.

See REACT_AGENT_IMPLEMENTATION.md for details on the current architecture.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime
from loguru import logger
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agent.state import ChatbotState, ConversationStage
from app.agent.tools import (
    get_user_info,
    search_product,
    select_product_from_last_search,
    add_to_cart,
    show_cart_items,
    find_upsell_product,
    get_or_set_delivery_address,
    calculate_checkout_total_with_delivery,
    process_final_checkout_with_mpesa,
    check_payment_status,
    generate_and_send_receipt,
    create_trade_in_session,
    set_current_user_phone,
    _get_current_user_phone
)
from app.config import get_settings

settings = get_settings()


# ============================================================================
# GREETING & USER INFO NODE
# ============================================================================

def greeting_node(state: ChatbotState) -> ChatbotState:
    """
    Greet the user and fetch their information.
    This is the entry point of the conversation.
    """
    logger.info(f"[GREETING NODE] Processing for user {state['user_phone']}")
    
    try:
        # Check if this is a continuation (already have AI messages)
        has_ai_messages = any(isinstance(msg, AIMessage) for msg in state.get("messages", []))
        
        if has_ai_messages:
            # This is a continuation - skip greeting, just update user phone
            set_current_user_phone(state["user_phone"])
            logger.info("[GREETING NODE] Continuation detected, skipping greeting")
            state["nodes_visited"].append("greeting")
            return state
        
        # New conversation - greet the user
        set_current_user_phone(state["user_phone"])
        
        # Get user info from database
        user_info_result = get_user_info.invoke({"user_phone": state["user_phone"]})
        
        if user_info_result.get("success"):
            state["user_info"] = user_info_result.get("user", {})
            user_name = state["user_info"].get("name", "")
            
            if user_name:
                greeting = f"Hi *{user_name}*! 😊 I'm Kay, your GreenBay Market assistant. How can I help you today?"
            else:
                greeting = "Hi! 😊 I'm Kay, your GreenBay Market assistant. How can I help you today?"
        else:
            greeting = "Hi! 😊 I'm Kay, your GreenBay Market assistant. How can I help you today?"
            state["user_info"] = {}
        
        state["messages"].append(AIMessage(content=greeting))
        state["conversation_stage"] = "browsing"
        state["needs_user_input"] = True
        state["nodes_visited"].append("greeting")
        
        # Check if user sent a simple greeting - if so, mark intent as help to avoid search
        last_user_msg = None
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                last_user_msg = msg.content.lower().strip()
                break
        
        if last_user_msg and last_user_msg in ["hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening", "hola", "sup", "yo"]:
            state["current_intent"] = "help"
            logger.info(f"[GREETING NODE] Detected simple greeting: '{last_user_msg}', setting intent to help")
        
        logger.info(f"[GREETING NODE] Completed. Stage: {state['conversation_stage']}")
        return state
        
    except Exception as e:
        logger.error(f"[GREETING NODE] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# INTENT CLASSIFICATION NODE
# ============================================================================

def classify_intent_node(state: ChatbotState) -> ChatbotState:
    """
    Classify user intent using LLM for dynamic context understanding.
    Falls back to rule-based for simple cases to save API calls.
    """
    logger.info("[INTENT CLASSIFIER] Analyzing user message")
    
    try:
        # Find the last user message
        last_user_message = None
        for message in reversed(state["messages"]):
            if isinstance(message, HumanMessage):
                last_user_message = message
                break
        
        if not last_user_message:
            logger.warning("[INTENT CLASSIFIER] No user message found, skipping")
            state["current_intent"] = "help"
            return state
        
        user_message = last_user_message.content.strip()
        user_message_lower = user_message.lower()
        
        # Get conversation context
        conversation_stage = state.get("conversation_stage", "browsing")
        has_upsell = state.get("upsell_product") is not None
        has_selected = state.get("selected_product") is not None
        has_cart = state.get("cart") and len(state["cart"]) > 0
        has_search_results = state.get("current_search_results") and len(state.get("current_search_results", [])) > 0
        
        # Get last 2 AI messages for context (what bot just said)
        recent_ai_messages = []
        for msg in reversed(state.get("messages", [])[-5:]):
            if isinstance(msg, AIMessage):
                recent_ai_messages.append(msg.content[:300])  # First 300 chars
                if len(recent_ai_messages) >= 2:
                    break
        
        # SIMPLE CASES: Use rule-based for obvious intents (saves API calls)
        # These are unambiguous and don't need LLM
        
        # 1. Simple greetings
        if user_message_lower in ["hi", "hello", "hey", "greetings"]:
            state["current_intent"] = "help"
            state["nodes_visited"].append("classify_intent")
            logger.info("[INTENT CLASSIFIER] Simple greeting -> help")
            return state
        
        # 2. Clear menu selections (numbers 1-5)
        if user_message_lower.isdigit() and 1 <= int(user_message_lower) <= 5:
            # Check if help menu was recently shown
            for msg in reversed(state.get("messages", [])[-3:]):
                if isinstance(msg, AIMessage) and "how i can help you" in msg.content.lower():
                    menu_num = int(user_message_lower)
                    intent_map = {1: "help", 2: "view_cart", 3: "checkout", 4: "track_order", 5: "trade_in"}
                    state["current_intent"] = intent_map.get(menu_num, "help")
                    state["nodes_visited"].append("classify_intent")
                    logger.info(f"[INTENT CLASSIFIER] Menu selection {menu_num} -> {state['current_intent']}")
                    return state
        
        # 3. Clear cart/checkout commands
        if any(word in user_message_lower for word in ["cart", "basket", "show cart"]):
            state["current_intent"] = "view_cart"
            state["nodes_visited"].append("classify_intent")
            logger.info("[INTENT CLASSIFIER] Cart command -> view_cart")
            return state
        
        if any(word in user_message_lower for word in ["checkout", "proceed to checkout"]) and has_cart:
            state["current_intent"] = "checkout"
            state["nodes_visited"].append("classify_intent")
            logger.info("[INTENT CLASSIFIER] Checkout command -> checkout")
            return state
        
        # COMPLEX CASES: Use LLM for context-aware classification
        # These need understanding of conversation flow and context
        
        # Build context for LLM
        context_info = {
            "conversation_stage": conversation_stage,
            "has_upsell_product": has_upsell,
            "has_selected_product": has_selected,
            "has_cart_items": has_cart,
            "has_search_results": has_search_results,
            "last_bot_message": recent_ai_messages[0] if recent_ai_messages else "None"
        }
        
        # Build classification prompt
        classification_prompt = f"""You are analyzing a user's message in an e-commerce WhatsApp chatbot conversation.

CONVERSATION CONTEXT:
- Current Stage: {conversation_stage}
- User just said: "{user_message}"
- Bot's last message: {context_info['last_bot_message'][:200]}

AVAILABLE STATE:
- Upsell product offered: {has_upsell}
- Product currently selected: {has_selected}
- Items in cart: {has_cart}
- Search results available: {has_search_results}

YOUR TASK:
Classify the user's intent based on what they're trying to do RIGHT NOW, considering the conversation context.

VALID INTENTS:
1. "product_advice" - User is asking a QUESTION about a product (why, how, is it worth, should I buy, what do you think, explain, tell me more, etc.)
2. "search_product" - User wants to search for NEW products (search query, looking for, find me, etc.)
3. "select_product" - User wants to ADD product to cart (yes, sure, add it, I'll take it - simple confirmation)
4. "view_product_details" - User wants to SEE product info/details (show details, more info, tell me about, etc.)
5. "view_cart" - User wants to see cart contents
6. "checkout" - User wants to proceed to checkout/payment
7. "help" - User needs help or wants menu
8. "upsell_accept" - User ACCEPTS upsell offer (yes, upgrade, take it, I'll take the better one - after seeing upsell)
9. "upsell_reject" - User REJECTS upsell (no, skip, keep original, decline - after seeing upsell)

CRITICAL RULES:
- If conversation_stage is "upsell_offer" and user asks "why is it better?" or "how is it better?" → "product_advice"
- If conversation_stage is "upsell_offer" and user says "yes" → "upsell_accept"
- If conversation_stage is "upsell_offer" and user says "no" → "upsell_reject"
- Questions (why, how, what, is it, should I, explain, tell me) → "product_advice"
- Simple confirmations without questions (yes, sure, ok) → depends on context
- If user says a number (1, 2, 3) and has_search_results → "view_product_details" or "select_product" (depends on stage)
- If user says a product name or description → "search_product"

Respond with ONLY the intent name (e.g., "product_advice"), nothing else."""

        # Use LLM for classification
        try:
            llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0,  # Deterministic
                api_key=settings.openai_api_key
            )
            
            classification_response = llm.invoke(classification_prompt).content.strip().lower()
            
            # Extract intent from response
            valid_intents = [
                "product_advice", "search_product", "select_product",
                "view_product_details", "view_cart", "checkout", "help",
                "upsell_accept", "upsell_reject"
            ]
            
            intent = None
            for valid_intent in valid_intents:
                if valid_intent in classification_response:
                    intent = valid_intent
                    break
            
            if not intent:
                # LLM didn't return a valid intent, use fallback
                logger.warning(f"[INTENT CLASSIFIER] LLM returned invalid intent: {classification_response}, using fallback")
                intent = "search_product"  # Safe default
            
            state["current_intent"] = intent
            state["nodes_visited"].append("classify_intent")
            logger.info(f"[INTENT CLASSIFIER] LLM classified intent: {intent} (response: {classification_response})")
            
        except Exception as llm_error:
            logger.error(f"[INTENT CLASSIFIER] LLM classification failed: {llm_error}, using fallback")
            # Fallback to rule-based
            if any(indicator in user_message_lower for indicator in ["why", "how", "what", "explain", "tell me", "is it", "should i"]):
                state["current_intent"] = "product_advice"
            elif len(user_message_lower) > 2:
                state["current_intent"] = "search_product"
            else:
                state["current_intent"] = "help"
            state["nodes_visited"].append("classify_intent")
            logger.info(f"[INTENT CLASSIFIER] Fallback classified intent: {state['current_intent']}")
        
        return state
        
    except Exception as e:
        logger.error(f"[INTENT CLASSIFIER] Error: {e}", exc_info=True)
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["current_intent"] = "help"  # Safe default
        return state


# ============================================================================
# PRODUCT SEARCH NODE
# ============================================================================

def product_search_node(state: ChatbotState) -> ChatbotState:
    """
    Search for products based on user query.
    """
    logger.info("[PRODUCT SEARCH] Searching for products")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Get the search query from last user message
        last_message = state["messages"][-1]
        if isinstance(last_message, HumanMessage):
            query = last_message.content
        else:
            # Fallback: look for earlier human message
            for msg in reversed(state["messages"]):
                if isinstance(msg, HumanMessage):
                    query = msg.content
                    break
            else:
                query = "phone"  # Default fallback
        
        # Call search tool
        search_result = search_product.invoke({"query": query, "limit": 5})
        
        if search_result.get("success") and search_result.get("products"):
            products = search_result["products"]
            state["current_search_results"] = products
            
            # Format products for display
            response = f"I found {len(products)} products for you:\n\n"
            
            for i, product in enumerate(products, 1):
                name = product.get("name", "Unknown")
                price = product.get("price", 0)
                stock = product.get("stock", 0)
                handle = product.get("handle", "")
                
                response += f"{i}. *{name}*\n"
                response += f"   • Price: KES {price:,.0f}\n"
                response += f"   • Stock: {stock} units"
                
                if stock < 3:
                    response += f" (Only {stock} left!)"
                
                response += f"\n   • Link: https://greenbay.market/products/{handle}\n\n"
            
            response += "Which one would you like? Just send me the number (e.g., '1' or '3')"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "product_selection"
            state["needs_user_input"] = True
            
        else:
            # No products found
            message = search_result.get("message", "No products found")
            state["messages"].append(AIMessage(
                content=f"I couldn't find any products matching '{query}'. Could you try describing what you're looking for differently?"
            ))
            state["conversation_stage"] = "browsing"
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("product_search")
        logger.info(f"[PRODUCT SEARCH] Completed. Found {len(state.get('current_search_results', []))} products")
        return state
        
    except Exception as e:
        logger.error(f"[PRODUCT SEARCH] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# PRODUCT DETAILS NODE
# ============================================================================

def product_details_node(state: ChatbotState) -> ChatbotState:
    """
    Show detailed information about a specific product WITHOUT adding to cart.
    User explicitly asked for details/info.
    """
    logger.info("[PRODUCT DETAILS] Showing product details")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Extract position from user message
        last_message = state["messages"][-1]
        user_text = last_message.content.lower() if isinstance(last_message, HumanMessage) else ""
        
        position = None
        product_id = None
        
        # Check if user is asking for "more details" about an already selected product
        # OR if user says "just want details" / "i want details" (maintaining context)
        if state.get("selected_product") and any(phrase in user_text for phrase in [
            "more details", "more info", "tell me more", "more information", 
            "additional", "what else", "elaborate", "just want details",
            "i want details", "i need details", "want details", "need details"
        ]):
            logger.info("[PRODUCT DETAILS] User wants MORE details about already selected product")
            # Keep the same product, just show richer details
            product_id = str(state["selected_product"]["id"])
        else:
            # Try to extract number
            if user_text.isdigit():
                position = int(user_text)
            else:
                # Try to find number in text like "3rd item", "item 3", "details about 3"
                import re
                number_match = re.search(r'\b(\d+)(?:st|nd|rd|th)?\b', user_text)
                if number_match:
                    position = int(number_match.group(1))
                    logger.info(f"[PRODUCT DETAILS] Extracted position {position} from: '{user_text}'")
                elif "first" in user_text or "1st" in user_text:
                    position = 1
                elif "second" in user_text or "2nd" in user_text:
                    position = 2
                elif "third" in user_text or "3rd" in user_text:
                    position = 3
                elif "fourth" in user_text or "4th" in user_text:
                    position = 4
                elif "fifth" in user_text or "5th" in user_text:
                    position = 5
                elif "last" in user_text:
                    position = len(state.get("current_search_results", []))
                # Check if user is referring to "it" or "that one" (context from selected_product)
                elif state.get("selected_product") and any(word in user_text for word in [
                    "it", "that", "this", "the one", "same"
                ]):
                    logger.info("[PRODUCT DETAILS] User referring to previously selected product")
                    product_id = str(state["selected_product"]["id"])
                # Check if user says just a number/position after asking for details
                # E.g., "i just want details" → "2nd one" (extract position from "2nd one")
                elif not position and not product_id:
                    # Try one more time to extract position from phrases like "2nd one", "the 2nd", etc.
                    import re
                    # Look for patterns like "2nd one", "the 2nd", "second one"
                    pos_match = re.search(r'\b(?:the\s+)?(\d+)(?:st|nd|rd|th)?\s*(?:one|item)?\b', user_text, re.IGNORECASE)
                    if pos_match:
                        position = int(pos_match.group(1))
                        logger.info(f"[PRODUCT DETAILS] Extracted position {position} from clarification: '{user_text}'")
            
            # Select product using the tool if we have a position
            if position is not None:
                selection_result = select_product_from_last_search.invoke({"position": position})
                
                if not selection_result.get("success"):
                    state["messages"].append(AIMessage(
                        content=selection_result.get("message", "Could not find that product. Please try again.")
                    ))
                    state["needs_user_input"] = True
                    return state
                
                product = selection_result["product"]
                state["selected_product"] = product
                product_id = str(product["id"])
            elif not product_id:
                # No position and no product_id means we couldn't determine what to show
                state["messages"].append(AIMessage(
                    content="Which product would you like to know more about? Please send me the number (1, 2, 3, etc.)"
                ))
                state["needs_user_input"] = True
                return state
        
        # Fetch FULL product details from Qdrant (includes description, images, tags, etc.)
        from app.agent.tools import get_product_details
        details_result = get_product_details.invoke({"product_id": product_id})
        
        if details_result.get("success"):
            product = details_result["product"]
            state["selected_product"] = product
            
            # Build RICH detailed product information response
            response = f"📦 *{product['name']}*\n\n"
            response += f"💰 *Price:* KES {product['price']:,.0f}\n"
            
            if product.get("vendor"):
                response += f"🏢 *Brand:* {product['vendor']}\n"
            
            if product.get("category"):
                response += f"📂 *Category:* {product['category']}\n"
            
            if product.get("stock"):
                stock = product["stock"]
                if stock <= 3:
                    response += f"📊 *Stock:* Only {stock} left! ⚠️\n"
                else:
                    response += f"📊 *Stock:* {stock} units available\n"
            
            # Show description (clean up HTML tags)
            if product.get("description"):
                import re
                # Remove HTML tags
                clean_desc = re.sub(r'<[^>]+>', '', product['description'])
                # Remove excessive whitespace
                clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()
                
                # Truncate if too long (WhatsApp message limit)
                if len(clean_desc) > 500:
                    clean_desc = clean_desc[:497] + "..."
                
                if clean_desc:
                    response += f"\n📝 *Description:*\n{clean_desc}\n"
            
            if product.get("tags"):
                response += f"\n🏷️ *Tags:* {product['tags']}\n"
            
            if product.get("handle"):
                product_url = f"https://greenbay.market/products/{product['handle']}"
                response += f"\n🔗 *View online:* {product_url}\n"
            
            response += f"\n\n💬 Would you like to add this to your cart?"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "product_selection"  # Ready for add to cart
            state["needs_user_input"] = True
        else:
            # Fetch failed
            state["messages"].append(AIMessage(
                content=details_result.get("message", "Could not load product details. Please try again.")
            ))
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("product_details")
        logger.info("[PRODUCT DETAILS] Completed")
        return state
        
    except Exception as e:
        logger.error(f"[PRODUCT DETAILS] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# PRODUCT ADVICE NODE
# ============================================================================

def product_advice_node(state: ChatbotState) -> ChatbotState:
    """
    Provide advice/opinion about a product when user asks questions.
    Handles both selected products and upsell products.
    """
    logger.info("[PRODUCT ADVICE] Providing product advice")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Determine which product to advise about
        product = None
        is_upsell_context = False
        original_product = None
        
        if state.get("upsell_product") and state.get("conversation_stage") == "upsell_offer":
            # User asking about the upsell product
            product = state["upsell_product"]
            is_upsell_context = True
        elif state.get("selected_product"):
            # User asking about the selected product
            product = state["selected_product"]
            original_product = product # Store for context
        
        if not product:
            state["messages"].append(AIMessage(
                content="Which product would you like advice about? Please tell me the number."
            ))
            state["needs_user_input"] = True
            return state

        logger.info("[PRODUCT ADVICE] Completed")
        return state
        
    except Exception as e:
        logger.error(f"[PRODUCT ADVICE] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        return state


# ============================================================================
# PRODUCT SELECTION NODE
# ============================================================================

def product_selection_node(state: ChatbotState) -> ChatbotState:
    """
    Handle product selection from search results.
    Can handle:
    1. User selecting by number (e.g., "3")
    2. User confirming add to cart after viewing details (e.g., "yes")
    """
    logger.info("[PRODUCT SELECTION] Processing selection")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Extract position from user message
        last_message = state["messages"][-1]
        user_text = last_message.content.lower() if isinstance(last_message, HumanMessage) else ""
        
        # Check if user is confirming add to cart for already selected product
        if state.get("selected_product") and any(word in user_text for word in [
            "yes", "sure", "ok", "add", "cart", "buy", "purchase"
        ]):
            logger.info("[PRODUCT SELECTION] User confirming add to cart for selected product")
            product = state["selected_product"]
            
            # Add to cart
            cart_result = add_to_cart.invoke({
                "product_id": str(product["id"]),
                "product_name": product["name"],
                "price": product["price"],
                "quantity": 1
            })
            
            if cart_result.get("success"):
                # Get updated cart from database to get accurate total and items
                from app.agent.tools import show_cart_items
                updated_cart = show_cart_items.invoke({})
                if updated_cart.get("success"):
                    state["cart"] = updated_cart.get("cart_items", [])
                    state["cart_total"] = updated_cart.get("total", 0.0)
                else:
                    # Fallback - calculate from just added item
                    state["cart"] = []
                    state["cart_total"] = product["price"] * 1
                
                response = f"✅ Added *{product['name']}* to your cart!\n\n"
                response += f"Cart Total: KES {state['cart_total']:,.0f}\n\n"
                response += "Would you like to:\n"
                response += "1. Continue shopping\n"
                response += "2. Proceed to checkout"
                
                state["messages"].append(AIMessage(content=response))
                state["conversation_stage"] = "cart_review"
                state["needs_user_input"] = True
                # DON'T skip upsell - user wants to see upsell offers
                # DON'T clear selected_product yet - upsell needs it
                # state["selected_product"] = None  # Keep for upsell
                state["skip_cart_display"] = True  # Flag to skip cart display (message already sent)
                state["nodes_visited"].append("product_selection")
                logger.info("[PRODUCT SELECTION] Completed - added selected product (upsell will show)")
                return state
            else:
                state["messages"].append(AIMessage(
                    content=f"Sorry, I couldn't add {product['name']} to your cart. Please try again."
                ))
                state["conversation_stage"] = "browsing"
                state["nodes_visited"].append("product_selection")
                return state
        
        # Otherwise, extract position from message
        position = None
        
        # Try to extract number
        if user_text.isdigit():
            position = int(user_text)
        elif "first" in user_text or "1st" in user_text:
            position = 1
        elif "second" in user_text or "2nd" in user_text:
            position = 2
        elif "third" in user_text or "3rd" in user_text:
            position = 3
        elif "fourth" in user_text or "4th" in user_text:
            position = 4
        elif "fifth" in user_text or "5th" in user_text:
            position = 5
        elif "last" in user_text:
            position = len(state.get("current_search_results", []))
        
        if position is None:
            # Could not extract position, ask again
            state["messages"].append(AIMessage(
                content="I didn't catch which product you want. Please send me the number (1, 2, 3, etc.)"
            ))
            state["needs_user_input"] = True
            return state
        
        # Select product using the tool
        selection_result = select_product_from_last_search.invoke({"position": position})
        
        if selection_result.get("success"):
            product = selection_result["product"]
            state["selected_product"] = product
            
            # Add to cart automatically
            cart_result = add_to_cart.invoke({
                "product_id": str(product["id"]),  # Convert to string
                "product_name": product["name"],
                "price": product["price"],  # Use 'price' not 'product_price'
                "quantity": 1
                # Note: user_phone is not a parameter, tool gets it from context
            })
            
            if cart_result.get("success"):
                # Get updated cart from database to get accurate total and items
                from app.agent.tools import show_cart_items
                updated_cart = show_cart_items.invoke({})
                if updated_cart.get("success"):
                    state["cart"] = updated_cart.get("cart_items", [])
                    state["cart_total"] = updated_cart.get("total", 0.0)
                else:
                    # Fallback - calculate from just added item
                    state["cart"] = []
                    state["cart_total"] = product["price"] * 1
                
                response = f"✅ Added *{product['name']}* to your cart!\n\n"
                response += f"Cart Total: KES {state['cart_total']:,.0f}\n\n"
                response += "Would you like to:\n"
                response += "1. Continue shopping\n"
                response += "2. Proceed to checkout"
                
                state["messages"].append(AIMessage(content=response))
                state["conversation_stage"] = "cart_review"
                state["needs_user_input"] = True
                # DON'T set upsell_offered - allow upsell to show
            else:
                state["messages"].append(AIMessage(
                    content=f"Sorry, I couldn't add {product['name']} to your cart. Please try again."
                ))
                state["conversation_stage"] = "browsing"
        else:
            # Selection failed
            state["messages"].append(AIMessage(
                content=selection_result.get("message", "Could not select that product. Please try again.")
            ))
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("product_selection")
        logger.info("[PRODUCT SELECTION] Completed")
        return state
        
    except Exception as e:
        logger.error(f"[PRODUCT SELECTION] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# UPSELL NODE
# ============================================================================

def upsell_node(state: ChatbotState) -> ChatbotState:
    """
    Offer upsell product after adding to cart.
    """
    logger.info("[UPSELL] Finding upsell opportunity")
    
    try:
        # Only offer upsell once per session
        if state.get("upsell_offered"):
            logger.info("[UPSELL] Already offered, skipping")
            return state
        
        set_current_user_phone(state["user_phone"])
        
        # Get the last added product
        selected_product = state.get("selected_product")
        if not selected_product:
            logger.info("[UPSELL] No selected product, skipping")
            return state
        
        # Find upsell
        upsell_result = find_upsell_product.invoke({
            "product_name": selected_product["name"],
            "product_price": selected_product["price"]
        })
        
        if upsell_result.get("success") and upsell_result.get("upsell_product"):
            upsell = upsell_result["upsell_product"]
            state["upsell_product"] = upsell
            state["upsell_offered"] = True
            
            # Check if "Added" message was already shown (skip_cart_display means it was)
            # If so, include it in the upsell response
            added_message_shown = state.get("skip_cart_display", False)
            
            if added_message_shown:
                # Find the "Added" message in recent messages
                added_msg = None
                for msg in reversed(state.get("messages", [])[-3:]):
                    if isinstance(msg, AIMessage) and "✅ added" in msg.content.lower():
                        added_msg = msg.content
                        break
                
                if added_msg:
                    response = added_msg + "\n\n" + "="*30 + "\n\n"
                else:
                    response = ""
            else:
                response = ""
            
            response += f"🌟 *Upgrade Suggestion!*\n\n"
            response += f"I noticed you're interested in *{selected_product['name']}*. "
            response += f"We have a better option:\n\n"
            response += f"*{upsell['name']}*\n"
            response += f"Price: KES {upsell['price']:,.0f}\n\n"
            response += f"It's {upsell_result.get('reasoning', 'a better choice')}.\n\n"
            response += "Would you like to upgrade? (yes/no)"
            
            state["messages"].append(AIMessage(content=response))
            state["needs_user_input"] = True
            state["conversation_stage"] = "upsell_offer"
            # Clear selected_product after showing upsell (no longer needed)
            state["selected_product"] = None
        else:
            # No upsell available, mark as offered
            state["upsell_offered"] = True
            logger.info("[UPSELL] No upsell found")
            # Clear selected_product if no upsell
            state["selected_product"] = None
        
        state["nodes_visited"].append("upsell")
        return state
        
    except Exception as e:
        logger.error(f"[UPSELL] Error: {e}")
        # Don't fail the whole flow for upsell errors
        state["upsell_offered"] = True
        return state


# ============================================================================
# CART REVIEW NODE
# ============================================================================

def cart_review_node(state: ChatbotState) -> ChatbotState:
    """
    Show cart contents and ask for next action.
    """
    logger.info("[CART REVIEW] Displaying cart")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Get cart details - tool gets user_phone from context, don't pass as parameter
        cart_result = show_cart_items.invoke({})
        
        if cart_result.get("success") and cart_result.get("cart_items"):
            items = cart_result["cart_items"]
            total = cart_result["total"]
            
            state["cart"] = items
            state["cart_total"] = total
            
            response = "🛒 *Your Cart:*\n\n"
            for i, item in enumerate(items, 1):
                product_name = item.get("product_name", "Unknown Product")
                quantity = item.get("quantity", 1)
                # Use 'total' field (item total) or calculate from price * quantity
                item_total = item.get("total", item.get("price", 0.0) * quantity)
                response += f"{i}. {product_name} x{quantity}\n"
                response += f"   KES {item_total:,.0f}\n\n"
            
            response += f"*Total: KES {total:,.0f}*\n\n"
            response += "Ready to checkout? (yes/no)"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "cart_review"
            state["needs_user_input"] = True
        else:
            # Use the message from the tool if available
            empty_message = cart_result.get("message", "Your cart is empty. What would you like to shop for today?")
            state["messages"].append(AIMessage(content=empty_message))
            state["conversation_stage"] = "browsing"
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("cart_review")
        return state
        
    except Exception as e:
        logger.error(f"[CART REVIEW] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        return state


# ============================================================================
# ADDRESS COLLECTION NODE
# ============================================================================

def address_collection_node(state: ChatbotState) -> ChatbotState:
    """
    Collect or verify delivery address.
    """
    logger.info("[ADDRESS COLLECTION] Processing address")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Check if we're receiving an address from user
        last_message = state["messages"][-1]
        
        if isinstance(last_message, HumanMessage) and state.get("conversation_stage") == "address_collection":
            # User is providing address
            address = last_message.content
            
            # Store the address
            address_result = get_or_set_delivery_address.invoke({
                "user_phone": state["user_phone"],
                "delivery_address": address
            })
            
            if address_result.get("success"):
                state["delivery_address"] = address_result.get("delivery_address")
                
                response = f"✅ Delivery address saved: {state['delivery_address']}\n\n"
                response += "Let me calculate your delivery cost..."
                
                state["messages"].append(AIMessage(content=response))
                state["conversation_stage"] = "delivery_setup"
                state["needs_user_input"] = False
            else:
                state["messages"].append(AIMessage(
                    content="I couldn't save that address. Could you provide it again?"
                ))
                state["needs_user_input"] = True
        else:
            # Check if user has a saved address
            address_result = get_or_set_delivery_address.invoke({"user_phone": state["user_phone"]})
            
            if address_result.get("needs_address"):
                # Need to ask for address
                state["messages"].append(AIMessage(
                    content="📍 What's your delivery address? (e.g., 'Westlands, Nairobi' or 'Mombasa')"
                ))
                state["conversation_stage"] = "address_collection"
                state["needs_user_input"] = True
            else:
                # Address available
                state["delivery_address"] = address_result.get("delivery_address")
                
                response = f"I'll deliver to: {state['delivery_address']}\n\n"
                response += "Is this correct? (yes/no)"
                
                state["messages"].append(AIMessage(content=response))
                state["needs_user_input"] = True
        
        state["nodes_visited"].append("address_collection")
        return state
        
    except Exception as e:
        logger.error(f"[ADDRESS COLLECTION] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# DELIVERY SETUP NODE
# ============================================================================

def delivery_setup_node(state: ChatbotState) -> ChatbotState:
    """
    Calculate delivery cost and show total.
    """
    logger.info("[DELIVERY SETUP] Calculating delivery")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        if not state.get("delivery_address"):
            logger.error("[DELIVERY SETUP] No delivery address")
            state["conversation_stage"] = "address_collection"
            return state
        
        # Calculate delivery cost
        delivery_result = calculate_checkout_total_with_delivery.invoke({
            "user_phone": state["user_phone"],
            "location": state["delivery_address"]
        })
        
        if delivery_result.get("success"):
            state["delivery_cost"] = delivery_result.get("delivery_cost", 0.0)
            state["delivery_zone"] = delivery_result.get("delivery_zone")
            total = delivery_result.get("total", 0.0)
            
            response = "📦 *Order Summary:*\n\n"
            response += f"Subtotal: KES {state['cart_total']:,.0f}\n"
            response += f"Delivery: KES {state['delivery_cost']:,.0f}\n"
            response += f"{'='*30}\n"
            response += f"*Total: KES {total:,.0f}*\n\n"
            response += "Ready to place your order? (yes/no)"
            
            state["messages"].append(AIMessage(content=response))
            state["payment_amount"] = total
            state["conversation_stage"] = "checkout_confirmation"
            state["needs_user_input"] = True
        else:
            state["messages"].append(AIMessage(
                content="Sorry, I couldn't calculate delivery for that location. Could you provide a different address?"
            ))
            state["conversation_stage"] = "address_collection"
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("delivery_setup")
        return state
        
    except Exception as e:
        logger.error(f"[DELIVERY SETUP] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        return state


# ============================================================================
# PAYMENT PROCESSING NODE
# ============================================================================

def payment_processing_node(state: ChatbotState) -> ChatbotState:
    """
    Initiate M-Pesa payment.
    """
    logger.info("[PAYMENT] Processing payment")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Initiate payment
        payment_result = process_final_checkout_with_mpesa.invoke({
            "user_phone": state["user_phone"]
        })
        
        if payment_result.get("success"):
            state["order_id"] = payment_result.get("order_id")
            state["payment_status"] = "initiated"
            
            response = "💳 *Payment Initiated!*\n\n"
            response += f"Order ID: {state['order_id']}\n"
            response += f"Amount: KES {state['payment_amount']:,.0f}\n\n"
            response += "📱 Please check your phone for the M-Pesa prompt and enter your PIN.\n\n"
            response += "I'll check the payment status in a moment..."
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "payment_confirmation"
            state["needs_user_input"] = False
        else:
            error_msg = payment_result.get("message", "Payment failed")
            state["messages"].append(AIMessage(
                content=f"❌ Payment could not be initiated: {error_msg}\n\nPlease try again."
            ))
            state["payment_status"] = "failed"
            state["conversation_stage"] = "error"
            state["needs_user_input"] = True
        
        state["nodes_visited"].append("payment_processing")
        return state
        
    except Exception as e:
        logger.error(f"[PAYMENT] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        state["payment_status"] = "failed"
        state["conversation_stage"] = "error"
        return state


# ============================================================================
# PAYMENT CONFIRMATION NODE
# ============================================================================

def payment_confirmation_node(state: ChatbotState) -> ChatbotState:
    """
    Check payment status and generate receipt.
    """
    logger.info("[PAYMENT CONFIRMATION] Checking status")
    
    try:
        set_current_user_phone(state["user_phone"])
        
        # Check payment status
        status_result = check_payment_status.invoke({
            "user_phone": state["user_phone"],
            "order_id": state.get("order_id"),
            "use_stk_query": True
        })
        
        payment_status = status_result.get("payment_status", "pending")
        state["payment_status"] = payment_status
        
        if payment_status == "completed":
            # Payment successful - generate receipt
            state["mpesa_transaction_id"] = status_result.get("mpesa_code")
            
            receipt_result = generate_and_send_receipt.invoke({
                "user_phone": state["user_phone"],
                "order_id": state["order_id"]
            })
            
            response = "✅ *Payment Successful!*\n\n"
            response += f"Order ID: {state['order_id']}\n"
            response += f"M-Pesa Code: {state['mpesa_transaction_id']}\n\n"
            
            if receipt_result.get("success"):
                response += "📧 Receipt has been generated!\n\n"
            
            response += "Thank you for shopping with GreenBay Market! 🎉\n\n"
            response += "Your order will be delivered soon. Is there anything else I can help you with?"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "completed"
            state["workflow_completed"] = True
            state["needs_user_input"] = True
            
        elif payment_status == "failed":
            response = "❌ Payment failed or was cancelled.\n\n"
            response += "Would you like to try again? (yes/no)"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "checkout_confirmation"
            state["needs_user_input"] = True
            
        else:
            # Still pending
            response = "⏳ Payment is still pending. Please complete the M-Pesa prompt on your phone.\n\n"
            response += "I'll check again in a moment..."
            
            state["messages"].append(AIMessage(content=response))
            state["needs_user_input"] = False
        
        state["nodes_visited"].append("payment_confirmation")
        return state
        
    except Exception as e:
        logger.error(f"[PAYMENT CONFIRMATION] Error: {e}")
        state["error_message"] = str(e)
        state["error_count"] += 1
        return state


# ============================================================================
# ERROR HANDLING NODE
# ============================================================================

def error_handling_node(state: ChatbotState) -> ChatbotState:
    """
    Handle errors gracefully.
    """
    logger.error(f"[ERROR HANDLER] Handling error: {state.get('error_message')}")
    
    try:
        if state["error_count"] >= state["max_retries"]:
            # Too many errors, escalate
            response = "I'm having trouble processing your request. 😔\n\n"
            response += "Please contact our support team at support@greenbay.market or call us.\n\n"
            response += "I apologize for the inconvenience!"
            
            state["messages"].append(AIMessage(content=response))
            state["interrupt_requested"] = True
            state["workflow_completed"] = True
        else:
            # Retry
            response = "Sorry, I encountered an issue. Let me try again...\n\n"
            response += "What were you trying to do?"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "browsing"
            state["error_message"] = None
        
        state["needs_user_input"] = True
        state["nodes_visited"].append("error_handling")
        return state
        
    except Exception as e:
        logger.critical(f"[ERROR HANDLER] Critical error in error handler: {e}")
        state["interrupt_requested"] = True
        return state


# ============================================================================
# HELP NODE
# ============================================================================

def help_node(state: ChatbotState) -> ChatbotState:
    """
    Provide help information or respond to simple greetings or menu selections.
    """
    logger.info("[HELP] Providing help")
    
    try:
        # Check if user wants to search (coming from "Continue shopping")
        if state.get("pending_action") == "search_product":
            logger.info("[HELP] User wants to search - asking for query")
            response = "Perfect! What products are you looking for? 🔍\n\n"
            response += "(e.g., 'iPhone', 'laptop', 'air fryer', 'TV')"
            
            state["messages"].append(AIMessage(content=response))
            state["conversation_stage"] = "browsing"
            state["needs_user_input"] = True
            state["pending_action"] = None  # Clear the flag
            state["nodes_visited"].append("help")
            return state
        
        # Check the last user message
        last_user_message = None
        for msg in reversed(state["messages"]):
            if isinstance(msg, HumanMessage):
                last_user_message = msg.content.lower().strip()
                break
        
        # Check if help node was recently visited (user is responding to menu)
        nodes_visited = state.get("nodes_visited", [])
        recently_showed_menu = nodes_visited and "help" in nodes_visited[-10:]  # Check last 10 nodes
        
        # If user sent a number and we recently showed the menu, check if it's option 1
        if recently_showed_menu and last_user_message:
            # Extract number from message
            import re
            menu_number = None
            if last_user_message.isdigit() and 1 <= int(last_user_message) <= 5:
                menu_number = int(last_user_message)
            else:
                number_match = re.search(r'\b([1-5])\b', last_user_message)
                if number_match:
                    menu_number = int(number_match.group(1))
            
            if menu_number == 1:
                logger.info("[HELP] User selected option 1 from menu - asking for search query")
                response = "Perfect! What products are you looking for? 🔍\n\n"
                response += "(e.g., 'iPhone', 'laptop', 'air fryer', 'TV')"
                
                state["messages"].append(AIMessage(content=response))
                state["conversation_stage"] = "browsing"
                state["needs_user_input"] = True
                state["nodes_visited"].append("help")
                return state
        
        # If it's a simple greeting, don't show help menu
        if last_user_message and last_user_message in ["hi", "hello", "hey", "greetings"]:
            logger.info("[HELP] Simple greeting detected, skipping help menu")
            state["conversation_stage"] = "browsing"
            state["needs_user_input"] = True
            state["nodes_visited"].append("help")
            return state
        
        # Default: show help menu
        response = "🤝 *How I Can Help You:*\n\n"
        response += "1. 🔍 *Search Products* - Just tell me what you're looking for\n"
        response += "2. 🛒 *Add to Cart* - Select products by number\n"
        response += "3. 💳 *Checkout* - Complete your purchase with M-Pesa\n"
        response += "4. 📦 *Track Orders* - Check your order status\n"
        response += "5. 🔄 *Trade-In* - Sell your old electronics\n\n"
        response += "What would you like to do?"
        
        state["messages"].append(AIMessage(content=response))
        state["conversation_stage"] = "browsing"
        state["needs_user_input"] = True
        state["nodes_visited"].append("help")
        
        return state
        
    except Exception as e:
        logger.error(f"[HELP] Error: {e}")
        return state

