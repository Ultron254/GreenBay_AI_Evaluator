"""Agent tools for Trade In Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable
import uuid
from datetime import datetime
from app.database.db import get_db
from app.database.models import TradeInSession
from tavily import TavilyClient
from app.config import get_settings
import re
import json
from openai import OpenAI
from app.agent.tools.common import *


# ============================================
# Helper functions for trade-in assessment
# ============================================

def _calculate_offer_range(amount: float) -> Dict[str, float]:
    """Calculate 10% offer range around base amount."""
    if amount is None:
        return {"low": 0.0, "high": 0.0}
    variation = round(amount * 0.10, 2)
    low = round(max(0.0, amount - variation), 2)
    high = round(amount + variation, 2)
    return {"low": low, "high": high}


def _format_valuation_reasoning(reasoning_text: str) -> str:
    """Format valuation reasoning to 2-3 sentences without scores/metrics."""
    if not reasoning_text:
        return "Based on the product's condition and market value."
    
    # Split into sentences
    sentence_chunks = re.split(r'(?<=[.!?])\s+', reasoning_text)
    banned_keywords = [
        "score", "grade", "metric", "rating",
        "condition score", "condition grade",
        "final offer", "final value", "exact offer"
    ]
    
    filtered_sentences = []
    for sentence in sentence_chunks:
        clean_sentence = sentence.strip()
        if not clean_sentence:
            continue
        if any(keyword in clean_sentence.lower() for keyword in banned_keywords):
            continue
        filtered_sentences.append(clean_sentence)
        if len(filtered_sentences) == 3:
            break
    
    if not filtered_sentences:
        return "Based on the product's condition and market value."
    
    formatted = " ".join(filtered_sentences)
    # Remove explicit score patterns
    formatted = re.sub(r'\d+\s*/\s*\d+', '', formatted)
    formatted = re.sub(r'\d+\s*%', '', formatted)
    formatted = re.sub(r'(score|grade|rating)\s*(of)?\s*\d+', '', formatted, flags=re.IGNORECASE)
    formatted = re.sub(r'\s+', ' ', formatted).strip()
    
    if formatted and not formatted.endswith('.'):
        formatted += '.'
    
    return formatted or "Based on the product's condition and market value."


def _extract_category_from_name(name: str) -> str:
    """Extract broad category from product name.
    
    Returns category keys that match the pricing_policy DB table:
    refrigerator, washing_machine, tv_monitor, cooker_oven,
    microwave, small_kitchen, smartphone, other
    """
    name_lower = name.lower()
    categories = {
        "refrigerator": ["refrigerator", "fridge", "freezer"],
        "washing_machine": ["washing machine", "washer", "dryer", "laundry"],
        "tv_monitor": ["tv", "television", "monitor", "screen", "display"],
        "cooker_oven": ["stove", "cooker", "oven", "cooking", "range", "hob"],
        "microwave": ["microwave"],
        "small_kitchen": ["blender", "mixer", "toaster", "kettle", "iron",
                          "juicer", "coffee maker", "air fryer", "food processor",
                          "vacuum", "fan", "dispenser", "speaker", "soundbar",
                          "keyboard", "mouse", "mice", "headphone", "earphone",
                          "tablet", "ipad"],
        "smartphone": ["phone", "smartphone", "iphone", "galaxy", "pixel",
                       "redmi", "tecno", "infinix", "oppo", "vivo", "realme"],
    }
    
    for category, keywords in categories.items():
        if any(keyword in name_lower for keyword in keywords):
            return category
    
    # Check for laptop/computer (map to other for now)
    laptop_kw = ["laptop", "notebook", "macbook", "computer", "desktop", "pc"]
    if any(kw in name_lower for kw in laptop_kw):
        return "other"
    
    return "other"


def _validate_product_images(
    user_phone: str,
    session_data: Dict[str, Any],
    image_urls: List[str]
) -> Optional[Dict[str, Any]]:
    """
    Validate images match expected product category.
    Returns error dict if validation fails, None if validation passes.
    """
    try:
        from image_analyzer import ImageAnalyzer
        
        analyzer = ImageAnalyzer()
        product_name = session_data.get("product_name", "")
        product_model = session_data.get("product_model", "")
        
        # Get expected category
        if session_data.get('detected_category'):
            expected_category = session_data['detected_category'].lower()
        else:
            expected_category = _extract_category_from_name(product_name)
        
        validation_prompt = f"""
        Validate product images before trade-in assessment.
        
        Expected BROAD category: {expected_category}
        Expected product: {product_name} {product_model}
        
        Analyze images and return JSON:
        {{
            "category_match": true/false,
            "detected_category": "mouse|keyboard|laptop|phone|refrigerator|stove|washing machine|tv|speaker|tablet|other",
            "what_you_see": "concise description",
            "reason": "why matches or not"
        }}
        
        RULES: Be VERY LENIENT. Only reject if categories are COMPLETELY DIFFERENT (e.g., mouse vs laptop).
        If same broad category, set category_match to TRUE.
        """
        
        validation_result = analyzer.analyze_multiple_images_single_call(image_urls, validation_prompt)
        
        if not validation_result.get("success"):
            return None  # Validation failed, but don't block
        
        validation_text = validation_result.get("analysis", "")
        json_match = re.search(r'\{.*\}', validation_text, re.DOTALL)
        if not json_match:
            return None  # Can't parse, don't block
        
        try:
            validation_data = json.loads(json_match.group())
            images_match = validation_data.get("category_match", True)
            
            if images_match is False:
                detected_cat = validation_data.get("detected_category", "").lower()
                expected_cat_lower = expected_category.lower()
                
                # Double-check: only reject if truly different
                if detected_cat and expected_cat_lower and detected_cat != expected_cat_lower:
                    # Check if related
                    if expected_cat_lower in detected_cat or detected_cat in expected_cat_lower:
                        logger.info(f"Related categories ({expected_cat_lower} vs {detected_cat}), accepting")
                        return None
                    
                    # Truly different - reject and clear images
                    logger.warning(f"Category mismatch: expected {expected_cat_lower}, detected {detected_cat}")
                    
                    db = next(get_db())
                    try:
                        session = db.query(TradeInSession).filter(
                            TradeInSession.user_phone == user_phone,
                            TradeInSession.session_id == session_data["session_id"]
                        ).first()
                        if session:
                            session.image_urls = []
                            session.s3_keys = []
                            session.images_uploaded = False
                            db.commit()
                    finally:
                        db.close()
                    
                    what_you_see = validation_data.get("what_you_see", "different items")
                    return {
                        "success": False,
                        "message": f"The images don't match your {product_name}. I see {what_you_see}. Please upload clear photos of your {product_name} {product_model}. I've removed the incorrect images."
                    }
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse validation JSON: {e}")
            return None  # Don't block on parsing errors
    
    except Exception as e:
        logger.warning(f"Error validating images: {e}")
        return None  # Don't block on validation errors
    
    return None  # Validation passed


def _analyze_product_condition(
    image_urls: List[str],
    product_name: str,
    product_model: str
) -> Dict[str, Any]:
    """
    Analyze product images for condition assessment.
    Returns dict with condition_score, condition_grade, issues_found, assessment_text.
    """
    from image_analyzer import ImageAnalyzer
    
    analyzer = ImageAnalyzer()
    
    assessment_prompt = f"""
    Analyze the condition of this {product_name} {product_model} for trade-in valuation.
    
    Assess for:
    - Scratches, dents, cracks, or damage
    - Wear and tear (buttons, ports, surfaces)
    - Cosmetic condition (stains, discoloration)
    - Overall functionality indicators
    
    Return JSON format:
    {{
        "condition_grade": "Excellent" or "Good" or "Fair" or "Poor",
        "condition_score": <number 0-100>,
        "issues_found": ["list", "of", "issues"],
        "detailed_assessment": "detailed description"
    }}
    
    IMPORTANT: Return ONLY valid JSON. Be accurate and realistic.
    """
    
    analysis_result = analyzer.analyze_multiple_images_single_call(image_urls, assessment_prompt)
    
    if not analysis_result["success"]:
        raise Exception(f"Image analysis failed: {analysis_result.get('error')}")
    
    assessment_text = analysis_result.get("analysis", "")
    if not assessment_text:
        raise Exception("No assessment results from image analysis")
    
    # Parse JSON response
    condition_grade = "Good"
    condition_score = 75.0
    issues_found = []
    
    try:
        json_match = re.search(r'\{[\s\S]*\}', assessment_text)
        if json_match:
            assessment_json = json.loads(json_match.group())
            condition_grade = assessment_json.get("condition_grade", "Good")
            condition_score = float(assessment_json.get("condition_score", 75.0))
            issues_found = assessment_json.get("issues_found", [])
            logger.info(f"Parsed condition: {condition_grade}, score: {condition_score}")
        else:
            # Fallback to keyword matching
            assessment_lower = assessment_text.lower()
            if "excellent" in assessment_lower:
                condition_grade = "Excellent"
                condition_score = 90.0
            elif "poor" in assessment_lower:
                condition_grade = "Poor"
                condition_score = 40.0
            elif "fair" in assessment_lower:
                condition_grade = "Fair"
                condition_score = 60.0
            
            # Extract issues
            if "scratch" in assessment_lower:
                issues_found.append("Minor scratches")
            if "dent" in assessment_lower:
                issues_found.append("Dents")
            if "crack" in assessment_lower:
                issues_found.append("Cracks")
            if "wear" in assessment_lower:
                issues_found.append("Wear and tear")
            
            logger.warning("Failed to parse JSON, using keyword fallback")
    
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Error parsing assessment JSON: {e}")
        # Use defaults with basic keyword detection
        if "excellent" in assessment_text.lower():
            condition_grade = "Excellent"
            condition_score = 90.0
        elif "poor" in assessment_text.lower():
            condition_grade = "Poor"
            condition_score = 40.0
        elif "fair" in assessment_text.lower():
            condition_grade = "Fair"
            condition_score = 60.0
    
    # Ensure score is valid
    condition_score = max(0.0, min(100.0, condition_score))
    
    return {
        "condition_score": condition_score,
        "condition_grade": condition_grade,
        "issues_found": issues_found,
        "assessment_text": assessment_text
    }


def _validate_product_category(user_message: str) -> Dict[str, Any]:
    """
    Validate if the product user wants to sell/trade-in is electronics or home appliances.
    
    Args:
        user_message: The user's message expressing intent
    
    Returns:
        Dictionary with:
        - is_valid: bool (True if electronics/appliances, False otherwise)
        - category: str (detected category)
        - reason: str (explanation if not valid)
    """
    try:
        from openai import OpenAI
        from app.config import get_settings
        import json
        
        settings = get_settings()
        
        if not settings.openai_api_key:
            # If no API key, allow through (fail open)
            logger.warning("No OpenAI API key available for category validation")
            return {"is_valid": True, "category": "unknown", "reason": ""}
        
        # CRITICAL: If message is vague/contextual (like "interested to sell it", "yes", "want to sell it"),
        # allow through - user is responding to previous context (e.g., image analysis)
        contextual_patterns = [
            r'\b(interested|want|looking)\s+(to\s+)?(sell|trade)',
            r'^(yes|yeah|yep|sure|ok|okay)\b',
            r'\bit\b',  # References "it" (previous context)
            r'^sell\s*it\s*$',
            r'^trade\s*it\s*$',
        ]
        
        user_msg_lower = user_message.lower().strip()
        for pattern in contextual_patterns:
            if re.search(pattern, user_msg_lower, re.IGNORECASE):
                logger.info(f"Contextual response detected ('{user_message}'), allowing through - assumes previous context")
                return {"is_valid": True, "category": "contextual", "reason": ""}
        
        client = OpenAI(api_key=settings.openai_api_key)
        
        validation_prompt = f"""
You are a product category validator for a trade-in/buy-back service.

ACCEPTED CATEGORIES (WE ACCEPT THESE):
- Electronics: phones, tablets, laptops, computers, cameras, gaming consoles, smartwatches, earphones, speakers, etc.
- Home Appliances: refrigerators, washing machines, microwaves, ovens, air conditioners, fans, irons, blenders, TVs, etc.

REJECTED CATEGORIES (WE DO NOT ACCEPT):
- Clothing, shoes, accessories
- Furniture (sofas, beds, tables, chairs)
- Vehicles (cars, motorcycles, bicycles)
- Books, toys, games (non-electronic)
- Jewelry, watches (non-smart)
- Sports equipment
- Food, groceries
- Any other non-electronics/non-appliances

USER MESSAGE:
"{user_message}"

Analyze the user's message and determine:
1. What product category they're trying to sell/trade-in
2. Whether we accept it (electronics/appliances) or not

Return ONLY valid JSON in this exact format:
{{
    "is_valid": true/false,
    "category": "detected category name",
    "reason": "brief explanation if not valid, empty string if valid"
}}

Examples:
- "I want to sell my iPhone" â†’ {{"is_valid": true, "category": "smartphone", "reason": ""}}
- "Looking to trade-in my washing machine" â†’ {{"is_valid": true, "category": "home appliance", "reason": ""}}
- "Want to sell my car" â†’ {{"is_valid": false, "category": "vehicle", "reason": "We only accept electronics and home appliances."}}
- "Sell my sofa" â†’ {{"is_valid": false, "category": "furniture", "reason": "We only accept electronics and home appliances."}}
"""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": validation_prompt}],
            temperature=0.1,
            max_tokens=200
        )
        
        content = response.choices[0].message.content.strip()
        
        # Extract JSON from response (re is imported at module level)
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            
            logger.info(f"Category validation result: {result}")
            
            return {
                "is_valid": bool(result.get("is_valid", False)),
                "category": result.get("category", "unknown"),
                "reason": result.get("reason", "")
            }
        else:
            logger.warning(f"Failed to parse LLM validation response: {content}")
            # Fail open - allow through if we can't parse
            return {"is_valid": True, "category": "unknown", "reason": ""}
            
    except Exception as e:
        logger.error(f"Error in category validation: {e}", exc_info=True)
        # Fail open - allow through on error
        return {"is_valid": True, "category": "unknown", "reason": ""}


# ============================================
# Main Tools
# ============================================

@tool
@traceable(name="create_trade_in_session")
def create_trade_in_session(intent_type: str = "trade_in", user_message: str = "") -> Dict[str, Any]:
    """
    This tool creates a new trade-in/sell session - START OF TRADE-IN WORKFLOW
    
    WHEN TO CALL THIS:
    - Call this immediately when user expresses intent to trade-in or sell a product/item.
    - User phrases: "sell", "trade-in", "trade in", "looking to sell", "want to sell", etc.
    
    CRITICAL VALIDATION:
    - This tool ONLY accepts electronics and home appliances
    - If user wants to sell something else (clothes, furniture, vehicles, etc.), politely decline
    
    AFTER CALLING THIS TOOL:
    - If success=False and message contains "not accept", inform user we only accept electronics/appliances
    - If success=True, generate your own friendly response acknowledging the user's request
    - Ask the user to provide the product name and model (this is needed for price search)
    - Use the transaction_type from the result to tailor your language:
      * If transaction_type is "sell" â†’ use "sell" terminology
      * If transaction_type is "trade_in" â†’ use "trade-in" terminology
    
    Args:
        intent_type: "trade_in" (default) or "sell". Use "sell" when customer wants direct cash sale (no store credit option).
        user_message: The user's message expressing intent to sell/trade-in (for validation)
    
    Returns:
        Dictionary with:
        - success: bool
        - session_id: str (unique session identifier, empty if validation fails)
        - transaction_type: str ("sell" or "trade_in")
        - message: str (error message if validation fails)
        
        After receiving this result, generate your own response asking for product name and model.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # Get user phone from context
        user_phone = _get_current_user_phone()
        logger.info(f"Creating trade-in session for user {user_phone}")
        
        # Validate product category using LLM
        if user_message.strip():
            validation_result = _validate_product_category(user_message)
            if not validation_result["is_valid"]:
                logger.warning(f"Trade-in rejected for user {user_phone}: {validation_result['reason']}")
                return {
                    "success": False,
                    "session_id": "",
                    "transaction_type": "",
                    "message": f"I'm sorry, but we currently only accept electronics and home appliances for trade-in or sale. {validation_result['reason']}",
                    "category_rejected": True
                }
        
        # Check if there's an active or cancelled session with store credit balance
        db = next(get_db())
        existing_session_with_credit = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status.in_(["active", "cancelled"]),
            TradeInSession.store_credit_balance > 0,
            TradeInSession.offer_decision == "accepted",
            TradeInSession.redemption_method == "store_credit"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        # Check if there's an active incomplete session (without offer accepted)
        active_incomplete_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active",
            TradeInSession.offer_decision != "accepted"
        ).first()
        
        # Generate unique session ID
        session_id = f"trade_in_{user_phone}_{int(datetime.now().timestamp())}"
        
        # If there's an active incomplete session, cancel it first
        if active_incomplete_session:
            active_incomplete_session.status = "cancelled"
            active_incomplete_session.end_time = datetime.now()
            logger.info(f"Cancelling incomplete session {active_incomplete_session.session_id}")
            # Commit the cancellation immediately to ensure it's saved
            db.commit()
        
        # Normalize intent type
        normalized_intent = (intent_type or "trade_in").strip().lower()
        if normalized_intent in {"sell", "sale"}:
            transaction_type = "sell"
        else:
            transaction_type = "trade_in"
        
        # Create new trade-in session
        trade_in_session = TradeInSession(
            session_id=session_id,
            user_phone=user_phone,
            status="active"
        )
        trade_in_session.transaction_type = transaction_type
        
        db.add(trade_in_session)
        db.commit()
        db.refresh(trade_in_session)
        
        logger.info(f"Created trade-in session {session_id} for {user_phone}")
        
        # Return session data - agent will generate its own response
        return {
            "success": True,
            "session_id": session_id,
            "transaction_type": transaction_type
        }
        
    except Exception as e:
        logger.error(f"Error creating trade-in session: {e}")
        return {
            "success": False,
            "session_id": "",
            "error": str(e)
        }
    finally:
        if 'db' in locals():
            db.close()


@tool
@traceable(name="search_product_price_kenya")
def search_product_price_kenya(product_name: str, product_model: str = "") -> Dict[str, Any]:
    """
    Search for product retail price in Kenya using Tavily search.
    
    When the user provides the product name and model that they want to trade-in or sell, call this tool to search for the product price in Kenya. 
    
    CRITICAL: This tool is for INTERNAL valuation only. NEVER reveal retail prices to users.
    When price search completes, just proceed to the next step(update_session_with_product_info) without mentioning the price amount.
    
    Args:
        product_name: Product name (e.g., "Samsung 4K TV")
        product_model: Product model (optional, e.g., "UN55AU8000")
        
    Returns:
        Dictionary with price information:
        {
            "success": bool,
            "retail_price": float,
            "currency": str,
            "price_source": str,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        settings = get_settings()
        
        if not settings.tavily_api_key:
            logger.warning("Tavily API key not configured, using fallback price estimation")
            return {
                "success": False,
                "retail_price": 0.0,
                "currency": "KES",
                "price_source": "fallback",
                "message": "Price search service not configured"
            }
        
        client = TavilyClient(api_key=settings.tavily_api_key)
        
        # Build search query
        search_query = f"{product_name} {product_model}".strip()
        search_query += " price Kenya KES"
        
        logger.info(f"Searching for price: {search_query}")
        
        # Search for product price
        response = client.search(
            query=search_query,
            search_depth="advanced",
            max_results=5
        )
        
        results = response.get("results", [])
        
        if not results:
            logger.warning(f"No price results found for {product_name}")
            return {
                "success": False,
                "retail_price": 0.0,
                "currency": "KES",
                "price_source": "not_found",
                "message": "Price not found in search results"
            }
        
        # Extract price from search results
        prices_found = []
        for result in results:
            content = result.get("content", "")
            title = result.get("title", "")
            text = f"{title} {content}"
            
            # Look for KES prices (format: KES 50,000 or KES 50000)
            price_patterns = [
                r'KES\s*([\d,]+)',
                r'KSh\s*([\d,]+)',
                r'Kenya\s*Shilling[s]?\s*([\d,]+)',
            ]
            
            for pattern in price_patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                for match in matches:
                    try:
                        # Remove commas and convert to float
                        price = float(match.replace(",", ""))
                        if 1000 <= price <= 10000000:  # Reasonable price range for Kenya
                            prices_found.append(price)
                    except ValueError:
                        continue
        
        if not prices_found:
            logger.warning(f"No valid prices extracted for {product_name}")
            return {
                "success": False,
                "retail_price": 0.0,
                "currency": "KES",
                "price_source": "not_found",
                "message": "Could not extract price from search results"
            }
        
        # Use median price (more reliable than average)
        prices_found.sort()
        median_price = prices_found[len(prices_found) // 2]
        
        logger.info(f"Found price for {product_name}: {median_price} KES")
        
        return {
            "success": True,
            "retail_price": median_price,
            "currency": "KES",
            "price_source": "tavily_search",
            "message": f"Price found: {median_price} KES"
        }
        
    except ImportError:
        logger.warning("Tavily client not installed, using fallback")
        return {
            "success": False,
            "retail_price": 0.0,
            "currency": "KES",
            "price_source": "fallback",
            "message": "Price search service not available"
        }
    except Exception as e:
        logger.error(f"Error searching product price: {e}")
        return {
            "success": False,
            "retail_price": 0.0,
            "currency": "KES",
            "price_source": "error",
            "message": f"Failed to search price: {str(e)}"
        }


@tool
@traceable(name="update_session_with_product_info")
def update_session_with_product_info(product_name: str, product_model: str = "", retail_price: float = 0.0, currency: str = "KES", price_source: str = "") -> Dict[str, Any]:
    """
    Update active session with product information after price search.
    
    WORKFLOW:
    1. Find active trade-in session
    2. Validate that the user provided a clear product model (LLM-powered). If not, return `needs_model=True`
       so the agent can ask: "Could you share the exact model (e.g., LG GL-C652HLCM)?"
    3. Save product name/model/price once validation passes
    
    Args:
        product_name: Name of the product (e.g., "LG TV", "Haier oven")
        product_model: Specific model/variant (e.g., "GL-C652HLCM"). REQUIRED before proceeding.
        retail_price: Retail price found
        currency: Currency (default: KES)
        price_source: Source of the price
        
    Returns:
        Dictionary with update status:
        {
            "success": bool,
            "message": str,
            "needs_model": bool,  # present when model details are insufficient
            "ask_prompt": str     # prompt to send to user when model is missing
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find the most recent active trade-in session (ORDER BY created_at DESC)
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No active trade-in session found. Please start a new trade-in session first."
            }
        
        def _has_specific_model(name: str, model: str) -> Dict[str, Any]:
            """
            Determine if the provided details include a concrete model.
            Returns {"has_model": bool, "ask_prompt": str}
            """
            model_text = (model or "").strip()
            
            # First, try to extract model from product_name if model_text is empty
            # Check if product_name contains model-like patterns (e.g., "Rapoo MT760 mouse")
            if not model_text and name:
                import re
                # Look for alphanumeric codes in product name (e.g., MT760, G502, iPhone 13)
                model_patterns = [
                    r'\b([A-Z]{1,3}\d{2,5})\b',  # MT760, G502, S21
                    r'\b([A-Z]+\d+[A-Z]*)\b',    # GL-C652HLCM, MS3032JAS
                    r'\b(\d+[A-Z]+\d*)\b',       # 65A80K
                ]
                for pattern in model_patterns:
                    match = re.search(pattern, name, re.IGNORECASE)
                    if match:
                        model_text = match.group(1)
                        logger.info(f"Extracted model '{model_text}' from product name '{name}'")
                        break
            
            if not model_text:
                return {
                    "has_model": False,
                    "ask_prompt": "Could you share the exact model name/number (e.g., LG GL-C652HLCM or Haier HO-2318E)?"
                }
            
            def _heuristic(text: str) -> bool:
                """Check if text contains a model identifier."""
                if not text:
                    return False
                tokens = text.replace("-", " ").split()
                
                # Check for alphanumeric model codes (e.g., MT760, G502, GL-C652HLCM)
                # More lenient: at least 3 chars with both letters and digits
                long_alnum = any(
                    len(token) >= 3 and any(ch.isdigit() for ch in token) and any(ch.isalpha() for ch in token)
                    for token in tokens
                )
                if long_alnum:
                    return True
                
                # Check for slash codes (e.g., "16/512")
                slash_codes = any("/" in token and sum(ch.isdigit() for ch in token) >= 2 for token in tokens)
                if slash_codes:
                    return True
                
                # Check for numeric suffixes with units (e.g., "438L", "65inch")
                numeric_suffixes = ("gb", "tb", "l", "\"", "inch", "kg", "w", "v")
                for token in tokens:
                    lower = token.lower()
                    for suffix in numeric_suffixes:
                        if lower.endswith(suffix) and len(lower) > len(suffix) and lower[:-len(suffix)].replace(".", "").isdigit():
                            return True
                
                # Check for pure numeric model codes (e.g., "760", "13", "S21") - at least 2 digits
                pure_numeric = any(len(token) >= 2 and token.isdigit() for token in tokens)
                if pure_numeric:
                    return True
                
                return False
            
            if _heuristic(model_text):
                return {"has_model": True}
            
            if not settings.openai_api_key:
                return {
                    "has_model": False,
                    "ask_prompt": "Could you share the exact model name/number (e.g., LG GL-C652HLCM or Haier HO-2318E)?"
                }
            
            try:
                from openai import OpenAI
                client = OpenAI(api_key=settings.openai_api_key)
                prompt = f"""
You are validating whether a customer provided an valid and specific product model for a trade-in/sell request.

product_name: "{name}"
product_model: "{model}"

Return JSON only:
{{
  "has_specific_model": true/false,
  "explanation": "short reason",
  "ask_prompt": "A friendly sentence asking for the exact model if missing"
}}

Specific models include identifiers like "GL-C652HLCM", "MS3032JAS", "XR-65A80K", capacities (e.g., "438L"), or storage/variant info (e.g., "16/512 GB").
If the user only gave a brand or generic label ("LG TV", "Haier oven"), mark has_specific_model=false.
"""
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=250
                )
                content = response.choices[0].message.content.strip()
                match = re.search(r'\{.*\}', content, re.DOTALL)
                if match:
                    data = json.loads(match.group())
                    return {
                        "has_model": bool(data.get("has_specific_model")),
                        "ask_prompt": data.get("ask_prompt") or data.get("explanation") or "Could you share the exact model name/number?"
                    }
            except Exception as e:
                logger.warning(f"LLM model validation failed: {e}")
            
            return {
                "has_model": False,
                "ask_prompt": "Could you share the exact model name/number (for example, 'VON VAFE40D 400L' or 'Samsung WW11CGC04DAB')?"
            }
        
        validation = _has_specific_model(product_name, product_model)
        if not validation.get("has_model"):
            ask_prompt = validation.get("ask_prompt") or "Please provide the exact model so I can proceed."
            return {
                "success": False,
                "needs_model": True,
                "message": ask_prompt,
                "ask_prompt": ask_prompt
            }
        
        # Update session with product information
        trade_in_session.product_name = product_name
        trade_in_session.product_model = product_model
        trade_in_session.retail_price = retail_price
        trade_in_session.currency = currency
        trade_in_session.price_source = price_source
        
        db.commit()
        
        logger.info(f"Updated session {trade_in_session.session_id} with product info: {product_name} {product_model}")
        
        return {
            "success": True,
        }
        
    except Exception as e:
        logger.error(f"Error updating session with product info: {e}")
        return {
            "success": False,
            "message": f"Error updating session: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()


@tool
@traceable(name="generate_category_specific_questions")
def generate_category_specific_questions(product_name: str, product_model: str = "", product_category: str = "") -> Dict[str, Any]:
    """
    This tool will generate assessment questions for trade-in/sell product
    
    This tool returns 3-5 questions. CRITICAL: After calling this tool:
    
    **ASK QUESTIONS ONE BY ONE - NOT ALL AT ONCE**
    
    CORRECT WORKFLOW:
    1. Tool returns questions array (e.g., 5 questions: q_0, q_1, q_2, q_3, q_4)
    2. Ask ONLY the FIRST question (q_0): "1. Does the Macbook Pro power on and boot up?"
    3. Wait for user's answer
    4. Call store_condition_response(question_key="q_0", answer=user's_answer)
    5. Tool returns responses_stored=1, total_questions=5
    6. Ask ONLY the SECOND question (q_1): "2. Are there any visible scratches?"
    7. Wait for user's answer
    8. Call store_condition_response(question_key="q_1", answer=user's_answer)
    9. Continue until responses_stored == total_questions
    10. When all questions answered â†’ proceed to ask for images
    
    WRONG WORKFLOW (DO NOT DO THIS):
    âŒ Presenting all questions in one message: "Here are 5 questions: 1... 2... 3... 4... 5..."
    
    WHY ONE-BY-ONE:
    - Each answer is stored immediately in database
    - User isn't overwhelmed by many questions
    - Provides better conversation flow
    - Agent can clarify unclear answers immediately
    
    Args:
        product_name: Product name (e.g., "Macbook Pro")
        product_model: Model (e.g., "M4 16/512 GB")
        product_category: Category (optional)
        
    Returns:
        Array of questions. ASK THEM ONE BY ONE using the workflow above.
        CRUCIAL--Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    db = None
    try:
        settings = get_settings()
        
        # Validate API key exists
        if not settings.openai_api_key:
            logger.error("OpenAI API key not configured")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Question generation service not configured"
            }
        
        # Validate user context
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("No user phone context available")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "User context not available"
            }
        
        # Get database session first to check for active trade-in session
        db = next(get_db())
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            logger.error(f"No active trade-in session found for {user_phone}")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "No active trade-in session found. Please start a new trade-in session first."
            }
        
        # Determine product category with better fallback logic
        if product_category and product_category.strip():
            category_hint = product_category.lower().strip()
        elif product_model and product_model.strip():
            # Try to infer category from model if available
            category_hint = f"{product_name} {product_model}".lower().strip()
        else:
            category_hint = product_name.lower().strip()
        
        client = OpenAI(api_key=settings.openai_api_key)
        
        prompt = f"""Based on the product "{product_name}" (model: {product_model}, category: {category_hint}), generate 3-5 (Maximum 5) specific condition assessment questions that would impact its trade-in value.

The questions should be:
1. Specific to this product type (refrigerator, stove/cooker, TV, washing machine, laptop, phone, etc.)
2. Focus on functional components that affect value
3. Easy for customers to answer (Yes/No or simple choices)
4. Cover both internal and external conditions

Return ONLY a JSON object with this exact structure:
{{
    "questions": ["Question 1?", "Question 2?", "Question 3?"],
    "category": "detected_category"
}}

IMPORTANT: Return valid JSON only. The "category" field must be a general product category (e.g., "laptop", "refrigerator", "tv", "phone")."""
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=500
        )
        
        # Safe JSON parsing with fallback
        try:
            result = json.loads(response.choices[0].message.content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM JSON response: {e}. Content: {response.choices[0].message.content}")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Failed to generate questions due to invalid response format"
            }
        
        questions = result.get("questions", [])
        detected_category = result.get("category", "")
        
        # Fallback for missing category
        if not detected_category or not isinstance(detected_category, str):
            detected_category = category_hint
            logger.warning(f"LLM did not return valid category, using fallback: {detected_category}")
        
        # Validate LLM response structure
        if not isinstance(questions, list):
            logger.error(f"Invalid questions type from LLM (expected list): {type(questions)}")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Failed to generate valid questions (invalid format)"
            }
        
        if len(questions) == 0:
            logger.error("LLM returned empty questions list")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Failed to generate questions (empty result)"
            }
        
        # Ensure all questions are non-empty strings and limit to 5
        questions = [str(q).strip() for q in questions if q and str(q).strip()][:5]
        
        if len(questions) == 0:
            logger.error("No valid questions after filtering empty strings")
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Failed to generate valid questions (all empty)"
            }
        
        # Ensure usage age question is included (insert as 4th if list is full, otherwise append)
        age_question = "How long have you used this product? (e.g., 6 months, 2 years)"
        has_age_question = any("how long" in q.lower() and ("use" in q.lower() or "own" in q.lower()) for q in questions)
        
        if not has_age_question:
            if len(questions) >= 5:
                # Insert as 4th question (index 3) to preserve first 3 important questions
                questions.insert(3, age_question)
                questions = questions[:5]  # Keep only 5
            else:
                questions.append(age_question)
        
        # Store questions in session with rollback on failure
        try:
            trade_in_session.category_questions = json.dumps(questions)
            trade_in_session.detected_category = detected_category
            db.commit()
            logger.info(f"Generated and stored {len(questions)} questions for {product_name} (category: {detected_category})")
        except Exception as db_error:
            logger.error(f"Failed to store questions in database: {db_error}")
            db.rollback()
            return {
                "success": False,
                "questions": [],
                "category": "",
                "message": "Failed to save questions to database"
            }
        
        return {
            "success": True,
            "questions": questions,
            "category": detected_category,
            "total_questions": len(questions),
            "message": f"Generated {len(questions)} assessment questions for {product_name}",
            "next_action": f"NOW: Ask ONLY question 0 (the first question). After user answers, IMMEDIATELY call store_condition_response('q_0', user_answer) before asking question 1."
        }
        
    except Exception as e:
        logger.error(f"Error generating category questions: {e}", exc_info=True)
        if db:
            try:
                db.rollback()
            except:
                pass
        return {
            "success": False,
            "questions": [],
            "category": "",
            "message": f"Error generating questions: {str(e)}"
        }
    finally:
        if db:
            try:
                db.close()
            except:
                pass


@tool
@traceable(name="store_condition_response")
def store_condition_response(question_key: str, answer: str) -> Dict[str, Any]:
    """
    This tool will store each answer to assessment questions
    
    CRITICAL: ONLY call this if you are CURRENTLY asking a trade-in assessment question.
    - If there's no active trade-in/sell session with unanswered questions, DO NOT call this tool
    
    Call this IMMEDIATELY after user answers each question.
    
    AFTER CALLING THIS:
    1. Tool returns: responses_stored=X, total_questions=Y
    2. Check if responses_stored < total_questions:
       - YES â†’ Ask the NEXT question only (question number = responses_stored)
       - NO (all answered) â†’ Proceed to ask for images (STEP 6)
    
    EXAMPLE FLOW:
    - User answers Q1 â†’ Call store_condition_response("q_0", answer)
    - Returns: responses_stored=1, total_questions=5
    - Since 1 < 5 â†’ Ask ONLY Q2 (question index 1)
    - User answers Q2 â†’ Call store_condition_response("q_1", answer)  
    - Returns: responses_stored=2, total_questions=5
    - Since 2 < 5 â†’ Ask ONLY Q3 (question index 2)
    - ... continue until responses_stored=5
    - When responses_stored=5 â†’ Ask for images, don't ask more questions
    
    QUESTION KEYS:
    - First question: "q_0"
    - Second question: "q_1"
    - Third question: "q_2"
    - Fourth question: "q_3"
    - Fifth question: "q_4"
    
    Args:
        question_key: Question identifier (q_0, q_1, q_2, etc.)
        answer: User's full answer with context
        
    Returns:
        responses_stored (how many answered) and total_questions (total count).
        If responses_stored < total_questions: Ask next question.
        If responses_stored == total_questions: Proceed to images.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    db = None
    try:
        # Validate user context
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("No user phone context available")
            return {
                "success": False,
                "message": "User context not available"
            }
        
        # Validate and normalize question_key early
        if not question_key or not isinstance(question_key, str):
            logger.error(f"Invalid question_key: {question_key}")
            return {
                "success": False,
                "message": "Invalid question key provided"
            }
        
        # Normalize question_key - only accept q_X format or numeric
        if question_key.startswith("q_"):
            # Validate format: q_0, q_1, etc.
            try:
                idx = int(question_key.replace("q_", ""))
                if idx < 0 or idx > 10:  # Sanity check
                    raise ValueError("Index out of range")
                normalized_key = question_key
            except ValueError:
                logger.error(f"Invalid question_key format: {question_key}")
                return {
                    "success": False,
                    "message": f"Invalid question key format: {question_key}. Expected format: q_0, q_1, etc."
                }
        else:
            # Try to parse as numeric index
            try:
                idx = int(question_key)
                if idx < 0 or idx > 10:  # Sanity check
                    raise ValueError("Index out of range")
                normalized_key = f"q_{idx}"
            except ValueError:
                logger.error(f"Invalid question_key (not q_X or numeric): {question_key}")
                return {
                    "success": False,
                    "message": f"Invalid question key: {question_key}. If user said a number after seeing product search results, they're selecting a product - use select_product_from_last_search() instead."
                }
        
        # Validate answer
        if not answer or not isinstance(answer, str) or not answer.strip():
            logger.error(f"Invalid answer provided: {answer}")
            return {
                "success": False,
                "message": "Invalid or empty answer provided"
            }
        
        db = next(get_db())
        
        # Use SELECT FOR UPDATE to prevent race conditions
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).with_for_update().first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No active trade-in session found. If user said a number after seeing product search results, they're selecting a product - use select_product_from_last_search() instead."
            }
        
        # Parse questions with error handling
        try:
            questions = json.loads(trade_in_session.category_questions) if trade_in_session.category_questions else []
            if not isinstance(questions, list):
                raise ValueError("Questions is not a list")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"Failed to parse category_questions JSON: {e}")
            db.rollback()
            return {
                "success": False,
                "message": "Invalid question data in session. Please restart the trade-in process."
            }
        
        if not questions or len(questions) == 0:
            return {
                "success": False,
                "message": "No assessment questions found in trade-in session. If user said a number after seeing product search results, they're selecting a product - use select_product_from_last_search() instead."
            }
        
        # Parse existing responses with error handling
        try:
            responses = json.loads(trade_in_session.condition_responses) if trade_in_session.condition_responses else {}
            if not isinstance(responses, dict):
                logger.warning(f"Responses is not a dict, resetting: {type(responses)}")
                responses = {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"Failed to parse condition_responses JSON: {e}. Resetting responses.")
            responses = {}
        
        # Count only q_ prefixed keys for accurate count
        responses_count = len([k for k in responses.keys() if k.startswith("q_")])
        
        # Check if all questions are already answered
        if responses_count >= len(questions):
            return {
                "success": False,
                "message": f"All {len(questions)} questions have already been answered. If user said a number after seeing product search results, they're selecting a product - use select_product_from_last_search() instead.",
                "responses_stored": responses_count,
                "total_questions": len(questions)
            }
        
        # Check if this specific question was already answered
        if normalized_key in responses:
            logger.warning(f"Question {normalized_key} already answered, overwriting")
        
        # Store the answer with normalized key only
        responses[normalized_key] = answer.strip()
        
        # Update session with rollback on failure
        try:
            trade_in_session.condition_responses = json.dumps(responses)
            db.commit()
        except Exception as db_error:
            logger.error(f"Failed to commit response to database: {db_error}")
            db.rollback()
            return {
                "success": False,
                "message": "Failed to save response to database"
            }
        
        # Recalculate count after storage
        responses_count = len([k for k in responses.keys() if k.startswith("q_")])
        
        # Determine next action based on workflow state
        next_action = ""
        if responses_count < len(questions):
            next_question_index = responses_count
            next_action = f"NOW: Ask ONLY question {next_question_index} (question #{next_question_index + 1}). After user answers, call store_condition_response('q_{next_question_index}', user_answer)."
        else:
            # All questions answered - check workflow state
            if not trade_in_session.images_uploaded:
                next_action = "All questions answered! NOW: Ask user to upload clear photos from different angles."
            elif not trade_in_session.final_offer:
                next_action = "Images already uploaded. NOW: Call `complete_trade_in_assessment()` to generate the offer range."
            elif trade_in_session.offer_decision == "accepted":
                next_action = "Offer already accepted. NOW: Ask user to choose a redemption method and call `set_redemption_method()`."
            else:
                next_action = "Offer range already shared. If user accepts, IMMEDIATELY call `accept_trade_in_offer()` and move to redemption options."
        
        logger.info(f"Stored condition response for {user_phone}: question {normalized_key}, progress {responses_count}/{len(questions)}")
        
        return {
            "success": True,
            "message": "Response stored successfully",
            "question_key": normalized_key,
            "responses_stored": responses_count,
            "total_questions": len(questions),
            "all_questions_answered": responses_count >= len(questions),
            "next_action": next_action
        }
        
    except Exception as e:
        logger.error(f"Error storing condition response: {e}", exc_info=True)
        if db:
            try:
                db.rollback()
            except:
                pass
        return {
            "success": False,
            "message": f"Error storing response: {str(e)}"
        }
    finally:
        if db:
            try:
                db.close()
            except:
                pass


@tool
@traceable(name="complete_trade_in_assessment")
def complete_trade_in_assessment() -> Dict[str, Any]:
    """
    
    WHEN TO CALL THIS (CRITICAL):
    - IMMEDIATELY after the user says "done", "analyze", or confirms they've finished uploading photos
    - When user says "done" or "analyze" after uploading images, call THIS tool, NOT add_images_to_trade_in_session
    - Do NOT wait for additional confirmation once images are providedâ€”just invoke this tool right away
    - If add_images_to_trade_in_session returns an error saying "images already exist", call THIS tool immediately
    
    WORKFLOW:
    1. Get trade-in/sell session data from database
    2. Analyze images for condition assessment
    3. Calculate trade-in/sell valuation
    4. Store assessment and valuation in database
    5. Return complete offer
    
    CRITICAL TERMINOLOGY RULES:
    - If transaction_type is "sell" â†’ use "sell" or "sale" terminology, NEVER mention "trade-in"
    - If transaction_type is "trade_in" â†’ use "trade-in" terminology, NEVER mention "sell" or "sale"
    - Check transaction_type in session to determine which terminology to use
    
    CRITICAL: MESSAGE FORMATTING RULES:
    - NEVER mention condition score (e.g., "condition score of 60") in the message
    - NEVER mention final offer amount (e.g., "Final Offer: KES 12,600") in the message
    - ONLY show the estimated offer RANGE (e.g., "KES 11,340 - KES 13,860")
    - The message already includes the formatted valuation reasoning and range - use it as-is
    - Do NOT add condition score, condition grade, or final offer to the message
    
    OFFER NEGOTIATION:
    - NEVER negotiate offers upon user request
    - If user asks to negotiate, politely explain: "I understand you'd like to discuss the offer. However, the final offer can only be determined after we receive and inspect your product. The range I've provided is an estimate based on the information you've shared."
    
    Returns:
        Dictionary with complete assessment summary (no raw metrics exposed to the agent):
        {
            "success": bool,
            "product_name": str,
            "product_model": str,
            "retail_price": float,
            "offer_range_low": float,
            "offer_range_high": float,
            "message": str
        }
        Internal values (condition score, condition grade, exact offer) are stored in the database only.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    db = None
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        user_phone = _get_current_user_phone()
        
        # Get session data
        session_result = get_trade_in_session_data.invoke({})
        if not session_result["success"]:
            return {"success": False, "message": session_result["message"]}
        
        session_data = session_result["session_data"]
        
        # Check if assessment already complete
        db = next(get_db())
        active_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if active_session and active_session.final_offer and active_session.final_offer > 0:
            logger.info(f"Assessment already complete for {user_phone}")
            offer_range = (
                {"low": active_session.min_offer, "high": active_session.max_offer}
                if active_session.min_offer and active_session.max_offer
                else _calculate_offer_range(active_session.final_offer)
            )
            
            transaction_type = active_session.transaction_type or "trade_in"
            transaction_term = "trade-in" if transaction_type == "trade_in" else "sale"
            valuation_reasoning = _format_valuation_reasoning(active_session.valuation_reasoning or "")
            
            return {
                "success": True,
                "product_name": session_data["product_name"],
                "product_model": session_data["product_model"],
                "retail_price": session_data["retail_price"],
                "offer_range_low": offer_range["low"],
                "offer_range_high": offer_range["high"],
                "message": (
                    f"The assessment is complete! {valuation_reasoning} "
                    f"I'm offering you an estimated range of *KES {offer_range['low']:,.0f} - KES {offer_range['high']:,.0f}* for your {transaction_term}.\n\n"
                    f"*Estimated Offer Range:* KES {offer_range['low']:,.0f} - KES {offer_range['high']:,.0f}\n\n"
                    f"The exact payout will be finalized after we receive and inspect the product. Would you like to accept this offer?"
                ),
            }
        
        db.close()
        db = None
        
        # Check prerequisites
        images = session_data.get("images") or []
        if not images:
            return {
                "success": False,
                "message": f"No images found for {session_data.get('product_name', 'product')}. Please upload images first, then say 'done'."
            }
        
        product_name = session_data.get("product_name") or ""
        product_model = session_data.get("product_model") or ""
        if not product_name or product_name == "None":
            return {"success": False, "message": "Product information is missing."}
        
        logger.info(f"Starting assessment: {len(images)} images for {product_name}")
        
        # Validate images match product
        validation_error = _validate_product_images(user_phone, session_data, images)
        if validation_error:
            return validation_error
        
        # Search retail price if missing
        if not session_data.get("retail_price") or session_data.get("retail_price", 0) == 0:
            logger.warning(f"No retail price for {product_name}. Searching...")
            search_result = search_product_price_kenya.invoke({
                "product_name": product_name,
                "product_model": product_model
            })
            
            if search_result.get("success") and search_result.get("retail_price", 0) > 0:
                update_session_with_product_info.invoke({
                    "product_name": product_name,  # Use existing product_name
                    "product_model": product_model,  # Use existing product_model
                    "retail_price": search_result["retail_price"],
                    "currency": search_result.get("currency", "KES"),
                    "price_source": search_result.get("price_source", "Tavily Search")
                })
                logger.info(f"Updated with retail price: KES {search_result['retail_price']:,}")
                session_result = get_trade_in_session_data.invoke({})
                session_data = session_result["session_data"]
            else:
                return {"success": False, "message": "Unable to find retail price for this product."}
        
        # Analyze condition
        condition_data = _analyze_product_condition(images, product_name, product_model)
        
        # Get category-specific responses
        db = next(get_db())
        current_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        condition_responses = {}
        category_questions = []
        product_category = ""
        if current_session:
            if current_session.condition_responses:
                try:
                    condition_responses = json.loads(current_session.condition_responses)
                except (json.JSONDecodeError, TypeError):
                    pass
            if current_session.category_questions:
                try:
                    category_questions = json.loads(current_session.category_questions)
                except (json.JSONDecodeError, TypeError):
                    pass
            product_category = current_session.detected_category or ""
        
        db.close()
        db = None
        
        # Calculate valuation
        valuation_result = calculate_trade_in_valuation.invoke({
            "product_name": product_name,
            "product_model": product_model,
            "retail_price": session_data["retail_price"],
            "condition_score": condition_data["condition_score"],
            "condition_grade": condition_data["condition_grade"],
            "issues_found": condition_data["issues_found"],
            "condition_responses": condition_responses,
            "category_questions": category_questions,
            "product_category": product_category
        })
        
        if not valuation_result["success"]:
            return {"success": False, "message": f"Valuation failed: {valuation_result['message']}"}
        
        # Store results
        db = next(get_db())
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        offer_range = _calculate_offer_range(valuation_result["final_offer"])
        
        trade_in_session.condition_score = condition_data["condition_score"]
        trade_in_session.condition_grade = condition_data["condition_grade"]
        trade_in_session.issues_found = condition_data["issues_found"]
        trade_in_session.overall_assessment = condition_data["assessment_text"]
        trade_in_session.trade_in_value = valuation_result["trade_in_value"]
        trade_in_session.final_offer = valuation_result["final_offer"]
        trade_in_session.min_offer = offer_range["low"]
        trade_in_session.max_offer = offer_range["high"]
        trade_in_session.condition_factor = valuation_result["condition_factor"]
        trade_in_session.market_factor = valuation_result["market_factor"]
        trade_in_session.valuation_reasoning = valuation_result["valuation_reasoning"]
        trade_in_session.offer_decision = "pending"
        
        db.commit()
        
        transaction_type = trade_in_session.transaction_type or "trade_in"
        transaction_term = "trade-in" if transaction_type == "trade_in" else "sale"
        valuation_reasoning = _format_valuation_reasoning(valuation_result["valuation_reasoning"])
        
        logger.info(f"Completed {transaction_type} assessment: {condition_data['condition_grade']}, KES {offer_range['low']:,.0f}-{offer_range['high']:,.0f}")
        
        return {
            "success": True,
            "product_name": product_name,
            "product_model": product_model,
            "retail_price": session_data["retail_price"],
            "offer_range_low": offer_range["low"],
            "offer_range_high": offer_range["high"],
            "message": (
                f"The assessment is complete! {valuation_reasoning} "
                f"I'm offering you an estimated range of *KES {offer_range['low']:,.0f} - KES {offer_range['high']:,.0f}* for your {transaction_term}.\n\n"
                f"*Estimated Offer Range:* KES {offer_range['low']:,.0f} - KES {offer_range['high']:,.0f}\n\n"
                f"The exact payout will be finalized after we receive and inspect the product. Would you like to accept this offer?"
            ),
        }
        
    except Exception as e:
        logger.error(f"Error in assessment: {e}", exc_info=True)
        return {"success": False, "message": f"Error in assessment: {str(e)}"}
    finally:
        if db:
            try:
                db.close()
            except:
                pass


@tool
@traceable(name="calculate_trade_in_valuation")
def calculate_trade_in_valuation(product_name: str, product_model: str, retail_price: float, condition_score: float, condition_grade: str, issues_found: list, condition_responses: Dict[str, Any] = None, category_questions: list = None, product_category: str = "") -> Dict[str, Any]:
    """
    Calculate trade-in valuation using the DETERMINISTIC offer engine.
    
    This function replaces the previous GPT-4o-based valuation with a pure-
    computation pipeline that produces identical results for identical inputs.
    
    WORKFLOW:
    1. Load pricing policy for the product category
    2. Gather comparables from historical data
    3. Score image quality and risk
    4. Run deterministic offer engine
    5. Persist ValuationSession and DecisionLedger
    6. Return valuation with reasoning
    
    Args:
        product_name: Name of the product
        product_model: Specific model
        retail_price: Retail price in KES
        condition_score: Condition score (0-100)
        condition_grade: Condition grade (Excellent/Good/Fair/Poor or A/B/C/D)
        issues_found: List of issues found
        condition_responses: Dict of customer responses to category-specific questions
        category_questions: List of category-specific questions asked
        product_category: Detected product category (e.g., refrigerator, stove, TV)
        
    Returns:
        Dictionary with valuation:
        {
            "success": bool,
            "trade_in_value": float,
            "currency": str,
            "valuation_reasoning": str,
            "condition_factor": float,
            "market_factor": float,
            "final_offer": float,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    db = None
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        from greenbay_ai_evaluator.config import CONDITION_GRADE_MAP
        from greenbay_ai_evaluator.engine.offer_engine import (
            PricingPolicyData,
            compute_valuation,
        )
        from greenbay_ai_evaluator.models.evaluator_models import (
            DecisionLedger,
            PricingPolicy,
            ValuationSession,
            NegotiationRound,
        )
        from greenbay_ai_evaluator.services.comparables_service import get_comparables
        from greenbay_ai_evaluator.services.image_quality_service import score_images
        from greenbay_ai_evaluator.services.risk_service import assess_risk

        user_phone = _get_current_user_phone()
        db = next(get_db())

        # Determine category
        category = (product_category or "").lower().strip()
        if not category:
            category = _extract_category_from_name(product_name)

        # Extract brand from product name (first word heuristic)
        brand = ""
        if product_name:
            parts = product_name.strip().split()
            if parts:
                brand = parts[0]

        # Map condition grade to letter
        grade = CONDITION_GRADE_MAP.get(
            (condition_grade or "").lower().strip(), "C"
        )

        # Load pricing policy
        policy_row = (
            db.query(PricingPolicy)
            .filter(
                PricingPolicy.category == category,
                PricingPolicy.is_active.is_(True),
            )
            .first()
        )

        if not policy_row:
            # Fallback: try partial match
            policy_row = (
                db.query(PricingPolicy)
                .filter(PricingPolicy.is_active.is_(True))
                .filter(PricingPolicy.category.ilike(f"%{category}%"))
                .first()
            )

        if not policy_row:
            # Use a sensible default if no policy found
            logger.warning(f"No pricing policy for '{category}', using fallback")
            policy_data = PricingPolicyData(
                category=category,
                margin_pct=0.30,
                max_offer_pct=0.65,
                walkaway_pct=0.30,
                brand_premium_json={},
                condition_multiplier_json={"A": 1.0, "B": 0.85, "C": 0.65, "D": 0.40},
            )
        else:
            policy_data = PricingPolicyData(
                category=policy_row.category,
                margin_pct=policy_row.margin_pct,
                max_offer_pct=policy_row.max_offer_pct,
                walkaway_pct=policy_row.walkaway_pct,
                depreciation_year1=policy_row.depreciation_year1 or 0.20,
                depreciation_year2_3=policy_row.depreciation_year2_3 or 0.12,
                depreciation_year4_5=policy_row.depreciation_year4_5 or 0.10,
                depreciation_year6_plus=policy_row.depreciation_year6_plus or 0.08,
                round_step_pct=policy_row.round_step_pct or 0.05,
                max_negotiation_rounds=policy_row.max_negotiation_rounds or 3,
                brand_premium_json=policy_row.brand_premium_json or {},
                condition_multiplier_json=policy_row.condition_multiplier_json or {},
            )

        # Build defects list from issues_found + condition_responses
        defects = []
        if issues_found:
            for issue in issues_found:
                if isinstance(issue, dict):
                    defects.append(issue)
                else:
                    defects.append({"type": str(issue).lower().replace(" ", "_"), "description": str(issue)})

        # Add functional defects from condition responses
        if condition_responses and category_questions:
            for i, question in enumerate(category_questions):
                question_key = f"q_{i}"
                answer = condition_responses.get(question_key, condition_responses.get(str(i), ""))
                answer_lower = str(answer).lower()
                question_lower = str(question).lower()
                # Flag negative answers as defects
                if any(neg in answer_lower for neg in ["no", "broken", "missing", "not work", "damaged", "leak", "noise", "mold"]):
                    defect_type = question_lower.replace(" ", "_")[:50]
                    defects.append({"type": defect_type, "description": f"{question}: {answer}", "severity": "medium"})

        # Gather comparables
        comp_result = get_comparables(
            category=category,
            brand=brand,
            model=product_model,
            db_session=db,
        )

        # Score images & risk
        iq_result = score_images()
        risk_result = assess_risk(phone_number=user_phone or "")

        # Estimate age (default to 2 years if unknown)
        age_years = 2.0

        # Run deterministic valuation
        result = compute_valuation(
            category=category,
            brand=brand,
            model=product_model or "",
            age_years=age_years,
            condition_grade=grade,
            condition_score=condition_score,
            defects=defects,
            seller_asking_price=None,  # Not collected at this stage
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            comparables=comp_result.comparables,
            pricing_policy=policy_data,
            retail_price=retail_price,
        )

        # Find the active trade-in session to link
        trade_in_session = None
        if user_phone:
            trade_in_session = (
                db.query(TradeInSession)
                .filter(
                    TradeInSession.user_phone == user_phone,
                    TradeInSession.status == "active",
                )
                .order_by(TradeInSession.created_at.desc())
                .first()
            )

        # Persist ValuationSession
        vs = ValuationSession(
            trade_in_session_id=trade_in_session.id if trade_in_session else None,
            category=category,
            brand=brand,
            model=product_model,
            age_years=age_years,
            condition_grade=grade,
            condition_score=condition_score,
            defects=defects,
            seller_asking_price=None,
            image_quality_score=iq_result.score,
            risk_score=risk_result.score,
            retail_price=retail_price,
            estimated_resale_value=result.estimated_resale_value,
            confidence_score=result.confidence_score,
            acquisition_ceiling=result.acquisition_ceiling,
            opening_offer=result.opening_offer,
            walkaway_limit=result.walkaway_limit,
            decision=result.decision,
            decision_reason=result.decision_reason,
            pricing_policy_snapshot={
                "category": policy_data.category,
                "margin_pct": policy_data.margin_pct,
                "max_offer_pct": policy_data.max_offer_pct,
                "walkaway_pct": policy_data.walkaway_pct,
            },
            comparable_data={
                "count": comp_result.count,
                "weighted_average": comp_result.weighted_average,
            },
        )
        db.add(vs)

        # Decision ledger
        db.add(DecisionLedger(
            valuation_session_id=vs.id,
            event_type="evaluation_created",
            actor="system",
            data={
                "decision": result.decision,
                "opening_offer": result.opening_offer,
                "ceiling": result.acquisition_ceiling,
                "resale_value": result.estimated_resale_value,
                "condition_grade": grade,
            },
        ))

        # Round 1 (system opening offer)
        db.add(NegotiationRound(
            valuation_session_id=vs.id,
            round_number=1,
            actor="system",
            offer_amount=result.opening_offer,
            ceiling_at_time=result.acquisition_ceiling,
            decision=result.decision,
            reason=result.decision_reason,
        ))

        db.commit()

        # Build reasoning text
        reasoning = (
            f"Based on the current retail price of KES {retail_price:,.0f}, "
            f"condition grade {grade}, and market analysis, "
            f"the estimated resale value is KES {result.estimated_resale_value:,.0f}."
        )
        if result.defect_deduction_total > 0:
            reasoning += f" Deductions of KES {result.defect_deduction_total:,.0f} applied for identified issues."

        # Use opening_offer as the final_offer for backward compatibility
        final_offer = result.opening_offer
        condition_factor = policy_data.condition_multiplier_json.get(grade, 0.65)
        market_factor = policy_data.max_offer_pct

        logger.info(
            f"Deterministic valuation for {product_name} {product_model}: "
            f"KES {final_offer:,.0f} (resale={result.estimated_resale_value:,.0f}, "
            f"ceiling={result.acquisition_ceiling:,.0f})"
        )

        return {
            "success": True,
            "trade_in_value": result.estimated_resale_value,
            "currency": "KES",
            "valuation_reasoning": reasoning,
            "condition_factor": condition_factor,
            "market_factor": market_factor,
            "final_offer": final_offer,
            "message": f"Trade-in value calculated: {final_offer} KES",
            # New fields for the evaluator
            "valuation_session_id": vs.id,
            "acquisition_ceiling": result.acquisition_ceiling,
            "walkaway_limit": result.walkaway_limit,
            "confidence_score": result.confidence_score,
            "decision": result.decision,
        }
        
    except Exception as e:
        logger.error(f"Error calculating trade-in valuation: {e}", exc_info=True)
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        return {
            "success": False,
            "trade_in_value": 0,
            "currency": "KES",
            "valuation_reasoning": "",
            "condition_factor": 0.0,
            "market_factor": 0.0,
            "final_offer": 0,
            "message": f"Failed to calculate valuation: {str(e)}"
        }
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@tool
@traceable(name="negotiate_trade_in_counter")
def negotiate_trade_in_counter(seller_counter_price: float) -> Dict[str, Any]:
    """
    Process a seller's counter-offer using the deterministic negotiation engine.
    
    Call this tool when the seller proposes a different price after receiving
    the initial trade-in valuation offer.
    
    Args:
        seller_counter_price: The price (in KES) the seller wants
        
    Returns:
        Dictionary with negotiation result:
        {
            "success": bool,
            "decision": str,       # "accept" | "counter" | "final" | "decline"
            "our_offer": float,    # Our new offer amount
            "message": str,        # Human-readable message for the seller
            "round_number": int,
            "is_terminal": bool
        }
    """
    db = None
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        from greenbay_ai_evaluator.engine.negotiation_engine import (
            NegotiationState,
            process_counter,
        )
        from greenbay_ai_evaluator.models.evaluator_models import (
            DecisionLedger,
            NegotiationRound,
            PricingPolicy,
            ValuationSession,
        )

        user_phone = _get_current_user_phone()
        db = next(get_db())

        # Find the active trade-in session
        trade_in_session = None
        if user_phone:
            trade_in_session = (
                db.query(TradeInSession)
                .filter(
                    TradeInSession.user_phone == user_phone,
                    TradeInSession.status == "active",
                )
                .order_by(TradeInSession.created_at.desc())
                .first()
            )

        if not trade_in_session:
            return {
                "success": False,
                "decision": "error",
                "our_offer": 0,
                "message": "No active trade-in session found.",
                "round_number": 0,
                "is_terminal": True,
            }

        # Find the most recent valuation session
        vs = (
            db.query(ValuationSession)
            .filter(ValuationSession.trade_in_session_id == trade_in_session.id)
            .order_by(ValuationSession.created_at.desc())
            .first()
        )

        if not vs:
            return {
                "success": False,
                "decision": "error",
                "our_offer": 0,
                "message": "No valuation found. Please run the valuation first.",
                "round_number": 0,
                "is_terminal": True,
            }

        if vs.final_decision is not None:
            return {
                "success": False,
                "decision": "error",
                "our_offer": 0,
                "message": "This valuation has already concluded. Start a new trade-in session.",
                "round_number": 0,
                "is_terminal": True,
            }

        # Get current round number
        latest_round = (
            db.query(NegotiationRound)
            .filter(NegotiationRound.valuation_session_id == vs.id)
            .order_by(NegotiationRound.round_number.desc())
            .first()
        )
        current_round = (latest_round.round_number if latest_round else 0) + 1

        # Load max rounds from pricing policy
        max_rounds = 3
        if vs.category:
            policy = (
                db.query(PricingPolicy)
                .filter(
                    PricingPolicy.category == vs.category,
                    PricingPolicy.is_active.is_(True),
                )
                .first()
            )
            if policy and policy.max_negotiation_rounds:
                max_rounds = policy.max_negotiation_rounds
            if policy and policy.round_step_pct:
                step_pct = policy.round_step_pct
            else:
                step_pct = 0.05
        else:
            step_pct = 0.05

        # Determine our previous offer
        our_last_offer = latest_round.offer_amount if latest_round else vs.opening_offer

        # Build negotiation state
        neg_state = NegotiationState(
            current_round=current_round,
            previous_system_offer=our_last_offer,
            acquisition_ceiling=vs.acquisition_ceiling,
            walkaway_limit=vs.walkaway_limit,
        )

        # Run negotiation engine
        neg_result = process_counter(
            seller_counter=seller_counter_price,
            state=neg_state,
            max_negotiation_rounds=max_rounds,
            round_step_pct=step_pct,
        )

        # Determine if this round is terminal
        is_terminal = neg_result.decision in ("accept", "decline")

        # Persist seller's counter as a round
        db.add(NegotiationRound(
            valuation_session_id=vs.id,
            round_number=current_round,
            actor="seller",
            offer_amount=seller_counter_price,
            ceiling_at_time=vs.acquisition_ceiling,
            decision="counter",
            reason=f"Seller counter-offer: KES {seller_counter_price:,.0f}",
        ))

        # Persist our response
        response_round = current_round + 1
        db.add(NegotiationRound(
            valuation_session_id=vs.id,
            round_number=response_round,
            actor="system",
            offer_amount=neg_result.system_offer,
            ceiling_at_time=vs.acquisition_ceiling,
            decision=neg_result.decision,
            reason=neg_result.reason,
        ))

        # Decision ledger entry
        db.add(DecisionLedger(
            valuation_session_id=vs.id,
            event_type="negotiation_round",
            actor="system",
            data={
                "round": current_round,
                "seller_counter": seller_counter_price,
                "our_response": neg_result.system_offer,
                "decision": neg_result.decision,
                "reason": neg_result.reason,
            },
        ))

        # If terminal, update session
        if is_terminal:
            vs.final_decision = neg_result.decision
            vs.final_offer = neg_result.system_offer

        db.commit()

        # Build human-readable message
        if neg_result.decision == "accept":
            message = f"We accept your price of KES {seller_counter_price:,.0f}. Let's proceed!"
        elif neg_result.decision == "counter":
            message = f"We can offer KES {neg_result.system_offer:,.0f}. Would you accept this?"
        else:  # decline
            message = f"Unfortunately, KES {seller_counter_price:,.0f} is above what we can offer. Our maximum was KES {vs.acquisition_ceiling:,.0f}."

        logger.info(
            f"Negotiation round {current_round}: seller={seller_counter_price:,.0f}, "
            f"decision={neg_result.decision}, our_offer={neg_result.system_offer:,.0f}"
        )

        return {
            "success": True,
            "decision": neg_result.decision,
            "our_offer": neg_result.system_offer,
            "message": message,
            "round_number": response_round,
            "is_terminal": is_terminal,
        }

    except Exception as e:
        logger.error(f"Error in negotiation: {e}", exc_info=True)
        if db:
            try:
                db.rollback()
            except Exception:
                pass
        return {
            "success": False,
            "decision": "error",
            "our_offer": 0,
            "message": f"Negotiation error: {str(e)}",
            "round_number": 0,
            "is_terminal": True,
        }
    finally:
        if db:
            try:
                db.close()
            except Exception:
                pass


@tool
@traceable(name="cancel_active_session")
def cancel_active_session() -> Dict[str, Any]:
    """
    Cancel the current active trade-in session.
    
    WORKFLOW:
    1. Find active trade-in session
    2. Update status to 'cancelled'
    3. Set end_time
    4. Return confirmation
    
    Returns:
        Dictionary with cancellation status:
        {
            "success": bool,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from datetime import datetime
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find active trade-in session OR most recent session with credit (even if cancelled)
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).first()
        
        # If no active session, check for cancelled session with credit
        if not trade_in_session:
            trade_in_session = db.query(TradeInSession).filter(
                TradeInSession.user_phone == user_phone,
                TradeInSession.store_credit_balance > 0,
                TradeInSession.offer_decision == "accepted",
                TradeInSession.redemption_method == "store_credit"
            ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": True,  # Not an error - just nothing to cancel
                "message": "No session to cancel."
            }
        
        # Check if offer was accepted AND user chose cash_pickup or store_visit (these are locked)
        if trade_in_session.offer_decision == "accepted":
            if trade_in_session.redemption_method in ["cash_pickup", "store_visit"]:
                return {
                    "success": False,
                    "message": f"Cannot cancel session with {trade_in_session.redemption_method}. You've chosen {trade_in_session.redemption_method.replace('_', ' ')} - contact support if you need assistance."
                }
            # For store_credit, allow cancellation (user can start new trade-in)
            # Just keep the credit balance for future use
        
        # Store status before cancellation to check if it was already cancelled
        was_already_cancelled = (trade_in_session.status == "cancelled")
        
        # Cancel the session (only if not already cancelled)
        if not was_already_cancelled:
            trade_in_session.status = "cancelled"
            trade_in_session.end_time = datetime.now()
            db.commit()
        
        logger.info(f"Cancelled trade-in session {trade_in_session.session_id}")
        
        # Determine message based on offer status and credit availability
        # CRITICAL: Credit is ONLY available after:
        # 1. User accepts offer
        # 2. Product is inspected/received
        # 3. Trade-in is marked as "completed"
        # If user declines, no credit is available regardless of previous choices
        
        # CRITICAL: Only mention credit if:
        # 1. Session status is "completed" (product inspected/received)
        # 2. Store credit balance > 0
        # 3. Offer was accepted
        # Otherwise, NEVER mention credit - it's not available yet
        
        has_available_credit = (
            trade_in_session.status == "completed" and
            trade_in_session.store_credit_balance and
            trade_in_session.store_credit_balance > 0 and
            trade_in_session.offer_decision == "accepted"
        )
        
        if was_already_cancelled:
            # Session was already cancelled
            if has_available_credit:
                message = "Your trade-in session was already completed. Your credit remains available."
            else:
                message = f"Your trade-in session for {trade_in_session.product_name or 'product'} has been cancelled."
        else:
            # Just cancelled now - NEVER mention credit (credit only available after inspection and completion)
            message = f"Your trade-in session for {trade_in_session.product_name or 'product'} has been cancelled."
        
        return {
            "success": True,
            "message": message
        }
        
    except Exception as e:
        logger.error(f"Error cancelling trade-in session: {e}")
        return {
            "success": False,
            "message": f"Error cancelling session: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()


@tool
@traceable(name="add_images_to_trade_in_session")
def add_images_to_trade_in_session(image_urls: list, s3_keys: list, replace_existing: bool = True) -> Dict[str, Any]:
    """
    Add images to an existing trade-in session. Validates that images match the product.
    
    CRITICAL: This tool is ONLY for adding images when they are uploaded.
    - DO NOT call this tool when user says "done" or "analyze"
    - When user says "done" or "analyze", call `complete_trade_in_assessment()` instead
    - This tool will REJECT empty image lists to prevent accidentally clearing images
    
    WORKFLOW:
    1. Find active trade-in session for user
    2. Validate images match the product using Vision LLM
    3. Replace existing images (if replace_existing=True) or append
    4. Store image URLs and S3 keys
    5. Return success status
    
    Args:
        image_urls: List of image URLs
        s3_keys: List of S3 keys
        replace_existing: If True, replace old images; if False, append
        
    Returns:
        Dictionary with operation status:
        {
            "success": bool,
            "images_added": int,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession, TradeInImage
        from image_analyzer import ImageAnalyzer
        import json
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find active trade-in session with product info (prioritize sessions with product_name)
        # ORDER BY created_at DESC to get the most recent session
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active",
            TradeInSession.product_name.isnot(None)
        ).order_by(TradeInSession.created_at.desc()).with_for_update().first()
        
        # If no session with product info, get any active session
        if not trade_in_session:
            trade_in_session = db.query(TradeInSession).filter(
                TradeInSession.user_phone == user_phone,
                TradeInSession.status == "active"
            ).order_by(TradeInSession.created_at.desc()).with_for_update().first()
        
        if not trade_in_session:
            return {
                "success": False,
                "images_added": 0,
                "message": "No active trade-in session found. Please start a new trade-in session."
            }
        
        # CRITICAL: Prevent clearing images by rejecting empty lists
        if not image_urls or not s3_keys or len(image_urls) == 0 or len(s3_keys) == 0:
            logger.warning(f"add_images_to_trade_in_session called with empty image lists - this would clear images! Rejecting.")
            
            # Check if images already exist in session
            existing_images = trade_in_session.image_urls or []
            if len(existing_images) > 0:
                return {
                    "success": False,
                    "images_added": 0,
                    "message": "ERROR: This tool was called incorrectly. Images already exist in the session. When user says 'done' or 'analyze', you MUST call `complete_trade_in_assessment()` instead of this tool. DO NOT call add_images_to_trade_in_session with empty lists.",
                    "action_required": "Call complete_trade_in_assessment() immediately"
                }
            else:
                return {
                    "success": False,
                    "images_added": 0,
                    "message": "No images provided. Please upload images first. If you've already uploaded images and want to analyze them, call `complete_trade_in_assessment()` instead."
                }
        
        # Validate images match the product if product name is available
        product_name = trade_in_session.product_name or ""
        product_model = trade_in_session.product_model or ""
        
        if product_name and product_name != "None":
            try:
                # Extract broad category from product name if detected_category is not set
                def extract_category_from_name(name: str) -> str:
                    """Extract broad category from product name."""
                    name_lower = name.lower()
                    # Common category keywords
                    categories = {
                        "mouse": ["mouse", "mice"],
                        "keyboard": ["keyboard"],
                        "laptop": ["laptop", "notebook", "macbook"],
                        "phone": ["phone", "smartphone", "iphone", "samsung"],
                        "refrigerator": ["refrigerator", "fridge", "freezer"],
                        "stove": ["stove", "cooker", "oven", "cooking"],
                        "washing machine": ["washing machine", "washer"],
                        "tv": ["tv", "television", "monitor"],
                        "speaker": ["speaker", "soundbar"],
                        "tablet": ["tablet", "ipad"]
                    }
                    
                    for category, keywords in categories.items():
                        if any(keyword in name_lower for keyword in keywords):
                            return category
                    
                    return "other"
                
                # Use Vision LLM to validate images match the product
                analyzer = ImageAnalyzer()
                # Get detected category or extract from product name
                if trade_in_session.detected_category:
                    expected_category = trade_in_session.detected_category.lower()
                else:
                    expected_category = extract_category_from_name(product_name)
                
                validation_prompt = f"""
                You are validating product images for a trade-in assessment.
                
                Expected BROAD category: {expected_category}
                Product mentioned: {product_name} {product_model}
                
                Analyze these images and determine:
                1. Do the images show an item in the same BROAD CATEGORY as expected?
                   - Category examples: mouse, keyboard, laptop, phone, refrigerator, stove, washing machine, tv, speaker, tablet
                   - You should ONLY check if it's the same BROAD category (e.g., both are "mouse" or both are "laptop")
                   - DO NOT require exact brand/model match
                   - DO NOT require exact appearance match
                   - If images show ANY item in the same category, set category_match to TRUE
                
                2. What product category and item do you actually see in the images?
                
                Return your response in JSON format:
                {{
                    "category_match": true/false,
                    "detected_category": "mouse | keyboard | laptop | phone | refrigerator | stove | washing machine | tv | speaker | tablet | other",
                    "what_you_see": "concise description",
                    "reason": "why category matches or not"
                }}
                
                CRITICAL VALIDATION RULES:
                - If expected category is "mouse" and images show ANY mouse (wireless, wired, gaming, office), set category_match to TRUE
                - If expected category is "laptop" and images show ANY laptop (any brand, any size), set category_match to TRUE
                - If expected category is "phone" and images show ANY phone (any brand, any model), set category_match to TRUE
                - Only set category_match to FALSE if images show a COMPLETELY DIFFERENT category (e.g., mouse vs keyboard, phone vs laptop)
                - Be VERY LENIENT - when in doubt, set category_match to TRUE
                - Only reject if you are CERTAIN the category is different
                """
                
                validation_result = analyzer.analyze_multiple_images_single_call(image_urls, validation_prompt)
                
                if validation_result.get("success"):
                    validation_text = validation_result.get("analysis", "")
                    
                    # Parse JSON response
                    import re
                    json_match = re.search(r'\{.*\}', validation_text, re.DOTALL)
                    if json_match:
                        try:
                            validation_data = json.loads(json_match.group())
                            images_match = validation_data.get("category_match", True)
                            what_you_see = validation_data.get("what_you_see", "")
                            
                            # Only reject if category_match is explicitly False AND we're confident
                            # If validation is unclear or category_match is True/None, proceed
                            if images_match is False:
                                detected_cat = validation_data.get("detected_category", "").lower()
                                expected_cat_lower = expected_category.lower()
                                
                                # Double-check: only reject if categories are clearly different
                                # If detected category is similar or same, accept it
                                if detected_cat and expected_cat_lower and detected_cat != expected_cat_lower:
                                    # Check if they're related categories (e.g., "mouse" vs "computer mouse")
                                    if expected_cat_lower in detected_cat or detected_cat in expected_cat_lower:
                                        # Related categories - accept
                                        logger.info(f"Categories are related ({expected_cat_lower} vs {detected_cat}), accepting images")
                                        images_match = True
                                    else:
                                        # Truly different categories - reject
                                        logger.warning(f"Image category mismatch: expected {expected_cat_lower}, detected {detected_cat}")
                                        # Always clear ALL existing images when wrong images detected (including old incorrect ones)
                                        trade_in_session.image_urls = []
                                        trade_in_session.s3_keys = []
                                        trade_in_session.images_uploaded = False
                                        
                                        db.commit()
                                        
                                        return {
                                            "success": False,
                                            "images_added": 0,
                                            "message": f"The images you uploaded don't match the expected category for your {product_name}. I can see: {what_you_see}. Please upload clear photos of your {product_name} {product_model} from different angles. I've removed all previously uploaded images, so please upload the correct ones."
                                        }
                                else:
                                    # Unclear validation - accept to be safe
                                    logger.info(f"Validation unclear, accepting images to avoid false rejection")
                                    images_match = True
                        except (json.JSONDecodeError, KeyError) as e:
                            # If JSON parsing fails, log but proceed (don't block user)
                            logger.warning(f"Failed to parse validation JSON: {e}, proceeding with image upload")
                else:
                    # If validation fails, log but proceed (don't block user)
                    logger.warning(f"Failed to validate images: {validation_result.get('error', 'Unknown error')}, proceeding anyway")
            except Exception as e:
                # If validation fails, log but proceed (don't block user)
                logger.warning(f"Error validating images: {e}, proceeding anyway")
        
        # Replace or append images
        if replace_existing:
            # Replace existing images (clear old ones)
            trade_in_session.image_urls = image_urls
            trade_in_session.s3_keys = s3_keys
        else:
            # Append new images to existing ones
            current_image_urls = list(trade_in_session.image_urls) if trade_in_session.image_urls else []
            current_s3_keys = list(trade_in_session.s3_keys) if trade_in_session.s3_keys else []
            current_image_urls.extend(image_urls)
            current_s3_keys.extend(s3_keys)
            trade_in_session.image_urls = current_image_urls
            trade_in_session.s3_keys = current_s3_keys
        
        trade_in_session.images_uploaded = True
        
        images_added = len(image_urls)
        
        db.commit()
        
        logger.info(f"Added {images_added} images to trade-in session {trade_in_session.session_id} (replace_existing={replace_existing})")
        
        # Use dynamic messages to avoid repetition
        import random
        image_messages = [
            f"Got your {images_added} image(s)! ðŸ“¸ Say 'done' or 'analyze' when you're ready for me to review them.",
            f"Images received! ðŸ‘ I've saved {images_added} photo(s). Just say 'done' or 'analyze' when you want me to assess them.",
            f"Perfect! I've got your {images_added} photo(s). ðŸ˜Š When you're ready, say 'done' or 'analyze' and I'll take a look.",
            f"Thanks! I've stored your {images_added} image(s). ðŸ“· Say 'done' or 'analyze' whenever you're ready for assessment.",
            f"Images saved! âœ… I have {images_added} photo(s) ready. Let me know when to analyze them - just say 'done' or 'analyze.'"
        ]
        
        return {
            "success": True,
            "images_added": images_added,
            "message": random.choice(image_messages)
        }
        
    except Exception as e:
        logger.error(f"Error adding images to trade-in session: {e}")
        return {
            "success": False,
            "images_added": 0,
            "message": f"Error adding images: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()


@tool
@traceable(name="get_trade_in_session_data")
def get_trade_in_session_data() -> Dict[str, Any]:
    """
    Get complete trade-in session data for assessment and valuation.
    
    WORKFLOW:
    1. Find active trade-in session
    2. Get all related data (images, assessment, valuation)
    3. Return complete session data
    
    Returns:
        Dictionary with complete session data:
        {
            "success": bool,
            "session_data": dict,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession, TradeInImage, TradeInAssessment, TradeInValuation
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find active trade-in session with product info (prioritize sessions with product_name)
        # ORDER BY created_at DESC to get the most recent session
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active",
            TradeInSession.product_name.isnot(None)
        ).order_by(TradeInSession.created_at.desc()).first()
        
        # If no session with product info, get any active session
        if not trade_in_session:
            trade_in_session = db.query(TradeInSession).filter(
                TradeInSession.user_phone == user_phone,
                TradeInSession.status == "active"
            ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "session_data": {},
                "message": "No active trade-in session found. Please start a new trade-in session."
            }
        
        # Prepare session data from unified session table
        session_data = {
            "session_id": trade_in_session.session_id,
            "product_name": trade_in_session.product_name,
            "product_model": trade_in_session.product_model,
            "retail_price": trade_in_session.retail_price,
            "currency": trade_in_session.currency,
            "price_source": trade_in_session.price_source,
            "images": trade_in_session.image_urls or [],
            "s3_keys": trade_in_session.s3_keys or [],
            "images_uploaded": trade_in_session.images_uploaded,
            "assessment": {
                "condition_score": trade_in_session.condition_score,
                "condition_grade": trade_in_session.condition_grade,
                "issues_found": trade_in_session.issues_found,
                "overall_assessment": trade_in_session.overall_assessment
            } if trade_in_session.condition_score else None,
            "valuation": {
                "trade_in_value": trade_in_session.trade_in_value,
                "final_offer": trade_in_session.final_offer,
                "condition_factor": trade_in_session.condition_factor,
                "market_factor": trade_in_session.market_factor,
                "valuation_reasoning": trade_in_session.valuation_reasoning,
                "offer_decision": trade_in_session.offer_decision
            } if trade_in_session.final_offer else None
        }
        
        num_images = len(trade_in_session.image_urls or [])
        logger.info(f"Retrieved trade-in session data for {user_phone}: {num_images} images")
        
        return {
            "success": True,
            "session_data": session_data,
            "message": f"Retrieved session data with {num_images} images"
        }
        
    except Exception as e:
        logger.error(f"Error getting trade-in session data: {e}")
        return {
            "success": False,
            "session_data": {},
            "message": f"Error retrieving session data: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()




@tool
@traceable(name="get_pending_trade_in_sessions")
def get_pending_trade_in_sessions(user_phone: str = "") -> Dict[str, Any]:
    """
    Get all pending or accepted trade-in sessions for a user.
    
    WORKFLOW:
    1. Query database for sessions with pending or accepted offer decisions
    2. Return session details
    3. Show user their trade-in status
    
    Args:
        user_phone: User's phone number (optional, will be extracted from context)
        
    Returns:
        Dictionary with session information:
        {
            "success": bool,
            "sessions": list,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        # If no user_phone provided, get from context
        if not user_phone:
            user_phone = _get_current_user_phone()
        
        db = next(get_db())
        
        # Find ALL active/pending sessions (regardless of redemption method or offer decision)
        # This includes:
        # 1. Active sessions with pending offers (not yet accepted/rejected)
        # 2. Active sessions with accepted offers (any redemption method)
        # 3. Cancelled sessions with store credit balance > 0 (for store credit redemptions)
        sessions = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status.in_(["active", "cancelled", "pending"]),
            (
                # Pending decisions (not yet accepted/rejected)
                (TradeInSession.offer_decision.is_(None)) |
                (TradeInSession.offer_decision == "pending") |
                # Accepted offers (any status)
                (TradeInSession.offer_decision == "accepted")
            )
        ).order_by(TradeInSession.created_at.desc()).all()
        
        if not sessions:
            return {
                "success": True,
                "sessions": [],
                "message": "You don't have any pending trade-in sessions."
            }
        
        # Format session data
        session_list = []
        for session in sessions:
            session_list.append({
                "session_id": session.session_id,
                "product_name": f"{session.product_name} {session.product_model}".strip(),
                "retail_price": session.retail_price,
                "final_offer": session.final_offer,
                "offer_decision": session.offer_decision,
                "redemption_method": session.redemption_method,
                "status": session.status,
                "pickup_address": session.pickup_address,
                "contact_phone": session.contact_phone,
                "store_credit_balance": session.store_credit_balance
            })
        
        db.close()
        
        logger.info(f"Found {len(session_list)} pending trade-in sessions for {user_phone}")
        
        return {
            "success": True,
            "sessions": session_list,
            "message": f"Found {len(session_list)} pending trade-in session(s)."
        }
        
    except Exception as e:
        logger.error(f"Error getting pending trade-in sessions: {e}")
        return {
            "success": False,
            "sessions": [],
            "message": f"Error retrieving trade-in sessions: {str(e)}"
        }



@tool
@traceable(name="reject_trade_in_offer")
def reject_trade_in_offer() -> Dict[str, Any]:
    """
    Reject trade-in/sell offer when user declines.
    
    WHEN TO CALL THIS:
    After presenting offer, if user responds with ANY of these:
    - "no" / "nope" / "no thanks" / "not interested"
    - "decline" / "reject" / "I decline" / "I'll pass"
    - "too low" / "not enough" / "that's low"
    - ANY negative response indicating rejection
    
    IMMEDIATELY call this tool to:
    1. Mark offer_decision as "rejected"
    2. Cancel the session
    3. Clean up the workflow
    
    AFTER CALLING THIS:
    - Session is cancelled and cleaned up
    - User can start a new trade-in/sell session if they want
    - Generate a friendly response acknowledging their decision
    
    Returns:
        Dictionary with rejection status.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    db = None
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        user_phone = _get_current_user_phone()
        if not user_phone:
            logger.error("No user phone context available")
            return {
                "success": False,
                "message": "User context not available"
            }
        
        db = next(get_db())
        
        # Find the most recent active trade-in session
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No active trade-in session found."
            }
        
        # Mark as rejected and cancel
        trade_in_session.offer_decision = "rejected"
        trade_in_session.status = "cancelled"
        trade_in_session.end_time = datetime.now()
        
        # Log to decision ledger
        from greenbay_ai_evaluator.models.evaluator_models import DecisionLedger, ValuationSession as EvalSession
        vs = db.query(EvalSession).filter(
            EvalSession.trade_in_session_id == trade_in_session.id
        ).order_by(EvalSession.created_at.desc()).first()
        if vs:
            db.add(DecisionLedger(
                valuation_session_id=vs.id,
                event_type="declined",
                actor="seller",
                data={"session_id": trade_in_session.session_id,
                      "final_offer": trade_in_session.final_offer},
            ))
        
        db.commit()
        
        logger.info(f"Rejected and cancelled trade-in session {trade_in_session.session_id}")
        
        return {
            "success": True,
            "session_id": trade_in_session.session_id,
            "message": "Trade-in offer rejected and session cancelled successfully."
        }
        
    except Exception as e:
        logger.error(f"Error rejecting trade-in offer: {e}", exc_info=True)
        if db:
            try:
                db.rollback()
            except:
                pass
        return {
            "success": False,
            "message": f"Error rejecting offer: {str(e)}"
        }
    finally:
        if db:
            try:
                db.close()
            except:
                pass


@tool
@traceable(name="accept_trade_in_offer")
def accept_trade_in_offer() -> Dict[str, Any]:
    """
    STEP 10: Accept trade-in offer when user agrees
    
    WHEN TO CALL THIS:
    After presenting offer (STEP 8), if user responds with ANY of these:
    - "yes" / "yeah" / "yep" / "sure" / "okay" / "ok" 
    - "accept" / "I accept" / "I'll take it" / "sounds good" / "deal" / "agreed"
    - ANY affirmative response indicating acceptance
    
    IMMEDIATELY call this tool - DO NOT:
    - Ask for images again
    - Go back to previous steps
    - Request confirmations
    
    AFTER CALLING THIS:
    1. Tool returns success with redemption options
    2. Present redemption options (STEP 11) based on transaction type:
       - For trade-in: Show options 1, 2, and 3 (Cash Pickup, Store Visit, Store Credit)
       - For sell/sale: Show only options 1 and 2 (Cash Pickup, Store Visit)
    3. WAIT for user to choose redemption method (1, 2, or 3)
    4. DO NOT auto-select store credit - user must choose
    5. After user chooses â†’ call set_redemption_method(user_input="user's exact response")
    
    CRITICAL RULES:
    - This workflow is LINEAR - after acceptance, move FORWARD to redemption
    - NEVER loop back to ask for images or repeat assessment
    - NEVER negotiate the offer - if user asks to negotiate, politely explain that the final offer can only be determined after product inspection
    - Use correct terminology: "trade-in" for trade_in transactions, "sale" for sell transactions
    
    Returns:
        Dictionary with acceptance status. After success, proceed to STEP 11 (redemption options).
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession, TradeInValuation
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find the most recent active trade-in session (ORDER BY created_at DESC)
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "active"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No active trade-in session found. Please start a new trade-in session."
            }
        
        # Check if valuation exists in session
        if not trade_in_session.final_offer:
            return {
                "success": False,
                "message": "No valuation found. Please complete the assessment first."
            }
        
        # Get offer range (use stored min/max if available, otherwise calculate)
        if trade_in_session.min_offer and trade_in_session.max_offer:
            range_low = trade_in_session.min_offer
            range_high = trade_in_session.max_offer
        else:
            base_offer = trade_in_session.final_offer
            range_low = round(max(0.0, base_offer * 0.9), 2)
            range_high = round(base_offer * 1.1, 2)
        
        # Update session with acceptance (but NOT redemption method - user must choose)
        trade_in_session.offer_decision = "accepted"
        trade_in_session.redemption_method = None  # Clear any previous selection
        trade_in_session.pickup_address = None
        trade_in_session.contact_phone = None
        trade_in_session.store_credit_balance = None
        # DO NOT set redemption_method here - user must choose
        # DO NOT set store_credit_balance here - only set if user chooses store_credit
        
        # Log to decision ledger
        from greenbay_ai_evaluator.models.evaluator_models import DecisionLedger, ValuationSession as EvalSession
        vs = db.query(EvalSession).filter(
            EvalSession.trade_in_session_id == trade_in_session.id
        ).order_by(EvalSession.created_at.desc()).first()
        if vs:
            db.add(DecisionLedger(
                valuation_session_id=vs.id,
                event_type="accepted",
                actor="seller",
                data={"session_id": trade_in_session.session_id,
                      "final_offer": trade_in_session.final_offer,
                      "offer_range_low": range_low,
                      "offer_range_high": range_high},
            ))
        
        db.commit()
        
        # Determine available redemption options based on transaction type
        is_trade_in = trade_in_session.transaction_type == "trade_in"
        
        if is_trade_in:
            redemption_options = (
                f"How would you like to redeem your offer (KES {range_low:,.0f} - KES {range_high:,.0f})?\n\n"
                f"*1. Cash at Pickup* - We collect your product and pay cash after inspection\n"
                f"*2. Store Visit* - Visit our store for instant cash after inspection\n"
                f"*3. Store Credit* - Use credit to shop online after inspection\n\n"
                f"Please reply with 1, 2, or 3 (or the option name)."
            )
        else:
            redemption_options = (
                f"How would you like to redeem your offer (KES {range_low:,.0f} - KES {range_high:,.0f})?\n\n"
                f"*1. Cash at Pickup* - We collect your product and pay cash after inspection\n"
                f"*2. Store Visit* - Visit our store for instant cash after inspection\n\n"
                f"Please reply with 1 or 2 (or the option name)."
            )

        logger.info(f"Trade-in offer accepted for {user_phone}: Range KES {range_low:,.0f} - KES {range_high:,.0f}")
        
        return {
            "success": True,
            "message": (
                f"Your acceptance of the offer is confirmed! ðŸŽ‰\n\n"
                f"{redemption_options}"
            ),
            "offer_range_low": range_low,
            "offer_range_high": range_high,
            "product_name": f"{trade_in_session.product_name} {trade_in_session.product_model}",
            "transaction_type": trade_in_session.transaction_type,
            "is_trade_in": is_trade_in
        }
        
    except Exception as e:
        logger.error(f"Error accepting trade-in offer: {e}")
        return {
            "success": False,
            "message": f"Error accepting offer: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()




@tool
@traceable(name="set_redemption_method")
def set_redemption_method(user_input: str = "", redemption_method: str = "", pickup_address: str = "", contact_phone: str = "") -> Dict[str, Any]:
    """
    Set the redemption method for an accepted trade-in/sell offer. Can parse user input or accept direct method.
    
    WORKFLOW:
    1. If user_input provided, use LLM to parse the redemption method from text
    2. Find accepted trade-in/sell session (allows changes even if completed)
    3. Validate that store_credit is only for trade-in transactions (not sell/sale)
    4. Update session with redemption method
    5. Return appropriate response based on method
    6. If you call this BEFORE the user picks an option, the tool will return success=False with a `prompt` telling you to present the options again â€“ send that prompt to the user and wait for their reply before retrying
    
    REDEMPTION OPTIONS:
    - After user accepts offer â†’ call `accept_trade_in_offer()` which will ask for redemption method
    - WAIT for user to choose redemption method (1, 2, or 3)
    - DO NOT auto-select store credit - user must choose
    - Only show store credit option (3) for trade-in transactions, not for sell/sale
    - After user chooses â†’ call this tool with user_input="user's response"
    
    Args:
        user_input: Raw user input (e.g., "3", "store credit", "third option") - parsed using LLM
        redemption_method: Direct method (cash_pickup, store_visit, store_credit) - used if provided
        pickup_address: Address for cash pickup (required for cash_pickup)
        contact_phone: Contact phone for pickup (optional)
        
    Returns:
        Dictionary with redemption setup status:
        {
            "success": bool,
            "message": str,
            "redemption_method": str,
            "next_steps": str,
            "prompt": str  # Only when user has not selected a method yet
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
        """
    try:
        from datetime import datetime
        from app.database.db import get_db
        from app.database.models import TradeInSession, TradeInValuation
        from langchain_openai import ChatOpenAI
        from app.config import get_settings
        import json
        import re
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        STORE_ADDRESS = "Head Office, Amani Gardens, 71 Church Road, Nairobi, Kenya"
        STORE_CONTACT = "+2540205002173"
        STORE_EMAIL = "info@greenbay.market"
        STORE_HOURS = "9:00 am - 5:00 pm Between Monday to Friday"
        
        # Parse redemption method from user input if provided
        if user_input and not redemption_method:
            settings = get_settings()
            llm = ChatOpenAI(
                model="gpt-4o-mini",
                temperature=0.1,
                api_key=settings.openai_api_key
            )
            
            parse_prompt = f"""
            Parse the user's input to determine which redemption method they want for their trade-in.
            
            Available options:
            1. cash_pickup - "1", "one", "first", "first option", "cash pickup", "pickup"
            2. store_visit - "2", "two", "second", "second option", "store visit", "visit"
            3. store_credit - "3", "three", "third", "third option", "store credit", "credit"
            
            User input: "{user_input}"
            
            Return ONLY the method name (cash_pickup, store_visit, or store_credit) in JSON format:
            {{"redemption_method": "store_credit"}}
            """
            
            try:
                response = llm.invoke(parse_prompt)
                parse_text = response.content
                json_match = re.search(r'\{.*\}', parse_text, re.DOTALL)
                if json_match:
                    parse_data = json.loads(json_match.group())
                    redemption_method = parse_data.get("redemption_method", "")
                else:
                    # Fallback: simple keyword matching
                    user_lower = user_input.lower()
                    if any(x in user_lower for x in ["3", "three", "third", "store credit", "credit"]):
                        redemption_method = "store_credit"
                    elif any(x in user_lower for x in ["2", "two", "second", "store visit", "visit"]):
                        redemption_method = "store_visit"
                    elif any(x in user_lower for x in ["1", "one", "first", "cash pickup", "pickup"]):
                        redemption_method = "cash_pickup"
            except Exception as e:
                logger.warning(f"Failed to parse redemption method from user input: {e}")
                # Fallback: simple keyword matching
                user_lower = user_input.lower()
                if any(x in user_lower for x in ["3", "three", "third", "store credit", "credit"]):
                    redemption_method = "store_credit"
                elif any(x in user_lower for x in ["2", "two", "second", "store visit", "visit"]):
                    redemption_method = "store_visit"
                elif any(x in user_lower for x in ["1", "one", "first", "cash pickup", "pickup"]):
                    redemption_method = "cash_pickup"
        
        # Create database session
        db = next(get_db())
        
        # Find accepted trade-in session (allow changes even if completed for flexibility)
        trade_in_session = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.offer_decision == "accepted"
        ).order_by(TradeInSession.created_at.desc()).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No accepted trade-in session found. Please accept a trade-in offer first."
            }
        
        # Allow changing redemption method even if completed (for flexibility)
        # Only block if explicitly cancelled
        if trade_in_session.status == "cancelled":
            return {
                "success": False,
                "message": f"Your trade-in session for {trade_in_session.product_name or 'product'} has been cancelled. Please start a new trade-in session."
            }
        
        range_low = trade_in_session.min_offer or round(max(0.0, (trade_in_session.final_offer or 0) * 0.9), 2)
        range_high = trade_in_session.max_offer or round((trade_in_session.final_offer or 0) * 1.1, 2)
        is_trade_in = trade_in_session.transaction_type == "trade_in"
        prompt_lines = [
            f"How would you like to redeem your offer (KES {range_low:,.0f} - KES {range_high:,.0f})?",
            "",
            "*1. Cash at Pickup* - We collect your product and pay cash after inspection",
            "*2. Store Visit* - Visit our store for instant cash after inspection",
        ]
        if is_trade_in:
            prompt_lines.append("*3. Store Credit* - Use credit to shop online after inspection")
        prompt_lines.append("")
        prompt_lines.append(
            "Please reply with 1, 2, or 3 (or the option name)."
            if is_trade_in else "Please reply with 1 or 2 (or the option name)."
        )
        redemption_prompt = "\n".join(prompt_lines)
        
        selection_pending = not trade_in_session.redemption_method
        if selection_pending and not user_input:
            return {
                "success": False,
                "message": (
                    "Redemption method not selected yet. Present these options to the user and wait for their choice before calling this tool again:\n\n"
                    f"{redemption_prompt}"
                ),
                "prompt": redemption_prompt
            }
        
        if not redemption_method:
            return {
                "success": False,
                "message": (
                    "Could not determine redemption method from the user's response. Share the options again and wait for their reply:\n\n"
                    f"{redemption_prompt}"
                ),
                "prompt": redemption_prompt
            }
        
        # If changing from completed to store_credit, reactivate session
        previous_status = trade_in_session.status
        previous_method = trade_in_session.redemption_method
        
        # CRITICAL: Store credit is only for "trade_in" transaction type
        if redemption_method == "store_credit" and trade_in_session.transaction_type != "trade_in":
            return {
                "success": False,
                "message": "Store credit is only available for trade-in transactions, not for selling. Please choose cash pickup or store visit."
            }
        
        # Update session with redemption method
        trade_in_session.redemption_method = redemption_method
        
        # Reactivate session if changing to store_credit from completed status
        if previous_status == "completed" and redemption_method == "store_credit":
            trade_in_session.status = "active"
            trade_in_session.end_time = None  # Clear end_time since it's active again
            logger.info(f"Reactivated session for {user_phone} to change to store_credit")
        
        # Mark as pending if changing from store_credit to cash_pickup or store_visit (awaiting product collection)
        if previous_method == "store_credit" and redemption_method in ["cash_pickup", "store_visit"]:
            trade_in_session.status = "pending"
        
        if redemption_method == "cash_pickup":
            if not pickup_address:
                return {
                    "success": False,
                    "message": "Pickup address is required for cash pickup option."
                }
            trade_in_session.pickup_address = pickup_address
            trade_in_session.contact_phone = contact_phone or user_phone
            # Mark session as pending until product is collected
            if trade_in_session.status != "pending":
                trade_in_session.status = "pending"
            next_steps = (
                f"Our agent will come to {pickup_address} to collect and inspect the product. "
                f"Once our agent receives and inspects the product, they will provide you with the cash."
            )
            
        elif redemption_method == "store_visit":
            # Mark session as pending until product is delivered to store
            if trade_in_session.status != "pending":
                trade_in_session.status = "pending"
            next_steps = (
                f"Please visit our store at: *{STORE_ADDRESS}*. "
                f"Our store keeper will provide you with the cash after receiving and inspecting the product. "
                f"You can contact us at {STORE_CONTACT} or email {STORE_EMAIL}. "
                f"Business hours: {STORE_HOURS}."
            )
            
        elif redemption_method == "store_credit":
            # Set store credit balance only when user chooses this option
            trade_in_session.store_credit_balance = trade_in_session.final_offer
            # Store credit keeps session active until all credit is used
            if trade_in_session.status == "completed":
                trade_in_session.status = "active"
                trade_in_session.end_time = None
            next_steps = (
                f"Once our agent receives and inspects your product, the offer credit will be added to your account to trade online."
            )
            
        else:
            return {
                "success": False,
                "message": "Invalid redemption method. Choose: cash_pickup, store_visit, or store_credit."
            }
        
        db.commit()
        
        logger.info(f"Redemption method set for {user_phone}: {redemption_method}")
        
        return {
            "success": True,
            "message": f"Redemption method set to {redemption_method.replace('_', ' ')}.",
            "redemption_method": redemption_method,
            "next_steps": next_steps
        }
        
    except Exception as e:
        logger.error(f"Error setting redemption method: {e}")
        return {
            "success": False,
            "message": f"Error setting redemption method: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()


@tool
@traceable(name="get_pending_trade_ins")
def get_pending_trade_ins() -> Dict[str, Any]:
    """
    Get all pending trade-in sessions waiting for product collection.
    
    A trade-in is "pending" when:
    - User has accepted the offer
    - Redemption method is set (cash_pickup or store_visit)
    - Product has NOT been collected by GreenBay Market yet
    
    Returns:
        Dictionary with pending trade-ins:
        {
            "success": bool,
            "pending_count": int,
            "pending_sessions": [
                {
                    "session_id": str,
                    "product_name": str,
                    "offer_range_low": float,
                    "offer_range_high": float,
                    "redemption_method": str,
                    "created_at": str,
                    "pickup_address": str
                }
            ],
            "message": str
        }
        
        CRITICAL: Always show offer RANGE (offer_range_low to offer_range_high), NEVER show exact final_offer.
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Get database session
        db = next(get_db())
        
        # Find all pending trade-in sessions
        pending_sessions = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status == "pending",
            TradeInSession.offer_decision == "accepted"
        ).order_by(TradeInSession.created_at.desc()).all()
        
        if not pending_sessions:
            return {
                "success": True,
                "pending_count": 0,
                "pending_sessions": [],
                "message": "You don't have any pending trade-in sessions at the moment."
            }
        
        # Format pending sessions
        formatted_sessions = []
        for session in pending_sessions:
            # Calculate offer range (use stored min/max if available, otherwise calculate)
            if session.min_offer and session.max_offer:
                offer_range_low = session.min_offer
                offer_range_high = session.max_offer
            else:
                offer_range = _calculate_offer_range(session.final_offer)
                offer_range_low = offer_range["low"]
                offer_range_high = offer_range["high"]
            
            formatted_sessions.append({
                "session_id": session.session_id,
                "product_name": f"{session.product_name} {session.product_model}".strip(),
                "offer_range_low": offer_range_low,
                "offer_range_high": offer_range_high,
                "redemption_method": session.redemption_method,
                "created_at": session.created_at.strftime("%Y-%m-%d %H:%M"),
                "pickup_address": session.pickup_address if session.redemption_method == "cash_pickup" else "Store visit required"
            })
        
        return {
            "success": True,
            "pending_count": len(pending_sessions),
            "pending_sessions": formatted_sessions,
            "message": f"You have {len(pending_sessions)} pending trade-in session(s)."
        }
        
    except Exception as e:
        logger.error(f"Error getting pending trade-ins: {e}")
        return {
            "success": False,
            "pending_count": 0,
            "pending_sessions": [],
            "message": f"Error retrieving pending trade-ins: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()




@tool
@traceable(name="mark_trade_in_as_completed")
def mark_trade_in_as_completed(session_id: str = "") -> Dict[str, Any]:
    """
    Mark a pending trade-in as completed when product is received by GreenBay Market.
    
    THIS TOOL IS FOR ADMIN/STAFF USE ONLY - Called when:
    - Product has been physically received at the store
    - Product has been collected by delivery agent
    - Product condition has been verified
    
    Args:
        session_id: The trade-in session ID to mark as completed
        
    Returns:
        Dictionary with completion status:
        {
            "success": bool,
            "message": str,
            "product_name": str,
            "final_offer": float
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from datetime import datetime
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Get database session
        db = next(get_db())
        
        # If no session_id provided, get the most recent pending session
        if not session_id:
            trade_in_session = db.query(TradeInSession).filter(
                TradeInSession.user_phone == user_phone,
                TradeInSession.status == "pending"
            ).order_by(TradeInSession.created_at.desc()).first()
        else:
            trade_in_session = db.query(TradeInSession).filter(
                TradeInSession.session_id == session_id,
                TradeInSession.user_phone == user_phone,
                TradeInSession.status == "pending"
            ).first()
        
        if not trade_in_session:
            return {
                "success": False,
                "message": "No pending trade-in session found to mark as completed."
            }
        
        # Mark session as completed
        trade_in_session.status = "completed"
        trade_in_session.product_collected = True
        trade_in_session.end_time = datetime.now()
        
        db.commit()
        
        logger.info(f"Trade-in session {trade_in_session.session_id} marked as completed for {user_phone}")
        
        return {
            "success": True,
            "message": f"Trade-in for {trade_in_session.product_name} {trade_in_session.product_model} has been marked as completed. Payment of KES {trade_in_session.final_offer:,} will be processed shortly.",
            "product_name": f"{trade_in_session.product_name} {trade_in_session.product_model}".strip(),
            "final_offer": trade_in_session.final_offer
        }
        
    except Exception as e:
        logger.error(f"Error marking trade-in as completed: {e}")
        return {
            "success": False,
            "message": f"Error marking trade-in as completed: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()




@tool
@traceable(name="get_store_credit_balance")
def get_store_credit_balance() -> Dict[str, Any]:
    """
    Get total store credit balance across all active sessions.
    
    Returns:
        Dictionary with balance information:
        {
            "success": bool,
            "total_balance": float,
            "sessions": list,
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        db = next(get_db())
        
        # Get all active sessions with store credit (include cancelled sessions that still have credit)
        sessions = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status.in_(["active", "cancelled"]),
            TradeInSession.offer_decision == "accepted",
            TradeInSession.redemption_method == "store_credit",
            TradeInSession.store_credit_balance > 0
        ).all()
        
        total_balance = sum(s.store_credit_balance for s in sessions if s.store_credit_balance)
        
        session_list = []
        for session in sessions:
            session_list.append({
                "product": f"{session.product_name} {session.product_model}".strip(),
                "balance": session.store_credit_balance,
                "session_id": session.session_id
            })
        
        db.close()
        
        return {
            "success": True,
            "total_balance": total_balance,
            "sessions": session_list,
            "message": f"You have KES {total_balance:,} in store credit available."
        }
        
    except Exception as e:
        logger.error(f"Error getting store credit balance: {e}")
        return {
            "success": False,
            "total_balance": 0.0,
            "sessions": [],
            "message": f"Error retrieving store credit balance: {str(e)}"
        }




@tool
@traceable(name="use_store_credit")
def use_store_credit(product_name: str, product_price: float, product_id: str = "", quantity: int = 1) -> Dict[str, Any]:
    """
    Use store credit for a purchase.
    
    WORKFLOW:
    1. Find user's store credit balance
    2. Calculate remaining balance after purchase
    3. Update credit balance
    4. Create order and payment records
    5. Return purchase summary
    
    Args:
        product_name: Name of the product being purchased
        product_price: Price of the product
        product_id: Product ID (optional)
        quantity: Quantity (default 1)
        
    Returns:
        Dictionary with purchase status:
        {
            "success": bool,
            "message": str,
            "remaining_credit": float,
            "payment_required": float,
            "order_id": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from app.database.db import get_db
        from app.database.models import TradeInSession, TradeInValuation, User, Order, OrderItem, Payment, OrderStatus, PaymentStatus
        from datetime import datetime
        import uuid
        
        # Get user phone from context
        user_phone = _get_current_user_phone()
        
        # Create database session
        db = next(get_db())
        
        # Find ALL user's sessions with accepted offer and store credit balance
        sessions = db.query(TradeInSession).filter(
            TradeInSession.user_phone == user_phone,
            TradeInSession.status.in_(["active", "cancelled"]),
            TradeInSession.offer_decision == "accepted",
            TradeInSession.redemption_method == "store_credit",
            TradeInSession.store_credit_balance > 0
        ).all()
        
        if not sessions:
            return {
                "success": False,
                "message": "No store credit available. Please accept a trade-in offer with store credit redemption first."
            }
        
        # Calculate total credit balance across all sessions
        total_credit = sum(s.store_credit_balance for s in sessions if s.store_credit_balance)
        
        if total_credit <= 0:
            return {
                "success": False,
                "message": "No store credit balance available."
            }
        
        current_credit = total_credit
        
        # Get or create user
        user = db.query(User).filter(User.phone_number == user_phone).first()
        if not user:
            user = User(phone_number=user_phone, name="")
            db.add(user)
            db.flush()
        
        # Calculate payment requirement
        payment_required = 0.0
        if product_price > current_credit:
            payment_required = product_price - current_credit
        
        # Create order
        order_id = f"GB{datetime.now().strftime('%Y%m%d%H%M%S')}"
        order = Order(
            order_id=order_id,
            user_id=user.id,
            status=OrderStatus.CONFIRMED if payment_required == 0 else OrderStatus.PENDING,
            subtotal=product_price,
            total_amount=product_price,
            notes=f"Purchased with store credit (KES {min(current_credit, product_price):,} credit applied)"
        )
        db.add(order)
        db.flush()
        
        # Create order item
        order_item = OrderItem(
            order_id=order.id,
            product_id=product_id or "unknown",
            product_name=product_name,
            price=product_price,
            quantity=quantity
        )
        db.add(order_item)
        
        # Create payment record
        payment = Payment(
            order_id=order.id,
            mpesa_transaction_id=f"STORE_CREDIT_{order_id}",
            amount=product_price,
            status=PaymentStatus.COMPLETED if payment_required == 0 else PaymentStatus.PENDING,
            phone_number=user_phone
        )
        db.add(payment)
        
        # Calculate remaining credit after purchase
        remaining_credit = current_credit - product_price if product_price <= current_credit else 0.0
        
        # Distribute the deduction across all sessions proportionally
        if remaining_credit > 0:
            # Calculate proportional deduction from each session
            deduction_ratio = (current_credit - remaining_credit) / current_credit
            
            for session in sessions:
                if session.store_credit_balance > 0:
                    # Deduct proportionally from each session
                    deduction_amount = session.store_credit_balance * deduction_ratio
                    session.store_credit_balance -= deduction_amount
                    
                    # Mark session as completed if balance is now 0
                    if session.store_credit_balance <= 0:
                        session.store_credit_balance = 0
                        session.status = "completed"
                        session.end_time = datetime.now()
        else:
            # All credit is used, zero out all sessions
            for session in sessions:
                session.store_credit_balance = 0
                session.status = "completed"
                session.end_time = datetime.now()
        
        # Build message
        if product_price <= current_credit:
            if remaining_credit == 0:
                message = f"Purchase successful! You bought {product_name} for KES {product_price:,}. Your store credit is now fully used."
            else:
                message = f"Purchase successful! You bought {product_name} for KES {product_price:,}. Remaining credit: KES {remaining_credit:,}."
        else:
            message = f"Purchase successful! You used KES {current_credit:,} credit. Additional payment needed: KES {payment_required:,} via M-Pesa."
        
        db.commit()
        
        logger.info(f"Store credit purchase: {product_name} for {user_phone}, order: {order_id}")
        
        return {
            "success": True,
            "message": message,
            "remaining_credit": remaining_credit,
            "payment_required": payment_required,
            "order_id": order_id
        }
        
    except Exception as e:
        logger.error(f"Error using store credit: {e}")
        return {
            "success": False,
            "message": f"Error processing store credit purchase: {str(e)}"
        }
    finally:
        if 'db' in locals():
            db.close()


# Context-aware storage for user context (works across async boundaries)
from contextvars import ContextVar

# Create a context variable for user phone (async-safe)
_current_user_phone: ContextVar[str] = ContextVar('current_user_phone', default=None)




