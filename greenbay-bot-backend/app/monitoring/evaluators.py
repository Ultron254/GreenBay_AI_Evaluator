"""Custom evaluators for LangSmith monitoring."""

from typing import Dict, Any
from langsmith.evaluation import evaluate, LangChainStringEvaluator
from langsmith.schemas import Run, Example
from loguru import logger


def response_relevance(run: Run, example: Example) -> Dict[str, Any]:
    """Evaluate if the response is relevant to the user's query."""
    try:
        # Simple relevance check based on keyword matching
        user_input = str(run.inputs.get("message", "")).lower()
        agent_output = str(run.outputs.get("response", "")).lower()
        
        # Check if response contains relevant keywords
        relevant_keywords = [
            "product", "cart", "price", "delivery", "payment", "order",
            "help", "assistance", "support", "contact", "store", "location", "hours"
        ]
        
        relevance_score = 0
        for keyword in relevant_keywords:
            if keyword in user_input and keyword in agent_output:
                relevance_score += 1
        
        # Normalize score
        max_score = len(relevant_keywords)
        normalized_score = relevance_score / max_score if max_score > 0 else 0
        
        return {
            "score": normalized_score,
            "reason": f"Response relevance based on keyword matching: {relevance_score}/{max_score}",
            "details": {
                "matched_keywords": [kw for kw in relevant_keywords if kw in user_input and kw in agent_output],
                "user_input_length": len(user_input),
                "response_length": len(agent_output)
            }
        }
        
    except Exception as e:
        logger.error(f"Error in response_relevance evaluator: {e}")
        return {
            "score": 0,
            "reason": f"Evaluation error: {str(e)}"
        }


def tool_usage_efficiency(run: Run, example: Example) -> Dict[str, Any]:
    """Evaluate if the right tools were used efficiently."""
    try:
        # Check if appropriate tools were called
        run_name = run.name or ""
        run_status = run.status or "unknown"
        
        # Define expected tool usage patterns
        expected_tools = {
            "search_product": ["search", "find", "look", "product"],
            "add_to_cart": ["add", "cart", "buy", "purchase"],
            "process_final_checkout_with_mpesa": ["pay", "checkout", "payment", "mpesa"],
            "calculate_delivery_options": ["delivery", "shipping", "address"],
            "get_store_info": ["store", "location", "contact", "address"]
        }
        
        efficiency_score = 0
        tool_matched = False
        
        for tool_name, keywords in expected_tools.items():
            if tool_name in run_name:
                tool_matched = True
                # Check if user input matches expected keywords for this tool
                user_input = str(run.inputs.get("message", "")).lower()
                if any(keyword in user_input for keyword in keywords):
                    efficiency_score = 1.0
                else:
                    efficiency_score = 0.5  # Tool used but may not be optimal
        
        if not tool_matched:
            efficiency_score = 0.3  # No specific tool used
        
        return {
            "score": efficiency_score,
            "reason": f"Tool usage efficiency: {run_name} with status {run_status}",
            "details": {
                "tool_name": run_name,
                "run_status": run_status,
                "tool_matched": tool_matched
            }
        }
        
    except Exception as e:
        logger.error(f"Error in tool_usage_efficiency evaluator: {e}")
        return {
            "score": 0,
            "reason": f"Evaluation error: {str(e)}"
        }


def conversation_flow_quality(run: Run, example: Example) -> Dict[str, Any]:
    """Evaluate the quality of conversation flow."""
    try:
        # Check conversation flow indicators
        user_input = str(run.inputs.get("message", "")).lower()
        agent_output = str(run.outputs.get("response", "")).lower()
        
        flow_indicators = {
            "greeting": ["hello", "hi", "hey", "good morning", "good afternoon"],
            "product_inquiry": ["product", "item", "show", "find", "search"],
            "cart_management": ["cart", "add", "remove", "clear", "show"],
            "checkout": ["checkout", "buy", "purchase", "pay", "order"],
            "delivery": ["delivery", "shipping", "address", "location"],
            "payment": ["payment", "mpesa", "pay", "money", "cost"],
            "support": ["help", "support", "contact", "problem", "issue"]
        }
        
        # Determine conversation stage
        conversation_stage = "unknown"
        for stage, keywords in flow_indicators.items():
            if any(keyword in user_input for keyword in keywords):
                conversation_stage = stage
                break
        
        # Check if response is appropriate for the stage
        response_quality = 0.5  # Default score
        
        if conversation_stage == "greeting":
            if any(word in agent_output for word in ["hello", "hi", "welcome", "help"]):
                response_quality = 1.0
        elif conversation_stage == "product_inquiry":
            if any(word in agent_output for word in ["product", "item", "found", "search"]):
                response_quality = 1.0
        elif conversation_stage == "cart_management":
            if any(word in agent_output for word in ["cart", "added", "removed", "item"]):
                response_quality = 1.0
        elif conversation_stage == "checkout":
            if any(word in agent_output for word in ["checkout", "payment", "order", "total"]):
                response_quality = 1.0
        elif conversation_stage == "delivery":
            if any(word in agent_output for word in ["delivery", "address", "shipping", "cost"]):
                response_quality = 1.0
        elif conversation_stage == "payment":
            if any(word in agent_output for word in ["payment", "mpesa", "pay", "transaction"]):
                response_quality = 1.0
        elif conversation_stage == "support":
            if any(word in agent_output for word in ["help", "support", "assist", "contact"]):
                response_quality = 1.0
        
        return {
            "score": response_quality,
            "reason": f"Conversation flow quality for {conversation_stage} stage",
            "details": {
                "conversation_stage": conversation_stage,
                "user_input_length": len(user_input),
                "response_length": len(agent_output),
                "stage_appropriate": response_quality > 0.7
            }
        }
        
    except Exception as e:
        logger.error(f"Error in conversation_flow_quality evaluator: {e}")
        return {
            "score": 0,
            "reason": f"Evaluation error: {str(e)}"
        }


def response_length_appropriateness(run: Run, example: Example) -> Dict[str, Any]:
    """Evaluate if the response length is appropriate."""
    try:
        agent_output = str(run.outputs.get("response", ""))
        response_length = len(agent_output)
        
        # Define appropriate length ranges
        if response_length < 50:
            score = 0.3  # Too short
        elif response_length < 200:
            score = 1.0  # Good length
        elif response_length < 500:
            score = 0.8  # Slightly long but acceptable
        else:
            score = 0.5  # Too long
        
        return {
            "score": score,
            "reason": f"Response length appropriateness: {response_length} characters",
            "details": {
                "response_length": response_length,
                "length_category": "short" if response_length < 50 else "good" if response_length < 200 else "long"
            }
        }
        
    except Exception as e:
        logger.error(f"Error in response_length_appropriateness evaluator: {e}")
        return {
            "score": 0,
            "reason": f"Evaluation error: {str(e)}"
        }


# Export evaluators for use in LangSmith
CUSTOM_EVALUATORS = {
    "response_relevance": response_relevance,
    "tool_usage_efficiency": tool_usage_efficiency,
    "conversation_flow_quality": conversation_flow_quality,
    "response_length_appropriateness": response_length_appropriateness
}
