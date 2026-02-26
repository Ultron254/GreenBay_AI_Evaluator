"""Agent tools for Image Tools."""

from typing import Dict, Any, List, Optional
from loguru import logger
from langchain.tools import tool
from langsmith import traceable

from app.agent.tools.common import *


@tool
@traceable(name="analyze_standalone_image")
def analyze_standalone_image(image_url: str, analysis_prompt: str = "") -> Dict[str, Any]:
    """
    Analyze a single standalone image using Vision LLM.
    
    WHEN TO CALL THIS:
    - User uploads an image WITHOUT an active trade-in/sell session
    - User asks "what's in the image?" or "tell me about this image"
    - User wants to identify a product from an image
    - Any general image analysis request
    
    DO NOT CALL THIS:
    - During active trade-in sessions (use complete_trade_in_assessment instead)
    - For product search (use search_product instead)
    
    Args:
        image_url: The URL of the image to analyze (provided by webhook as [User uploaded an image: {url}])
        analysis_prompt: Optional custom prompt. If not provided, uses default general analysis.
    
    Returns:
        Dictionary with analysis results:
        {
            "success": bool,
            "analysis": str,  # Detailed description of what's in the image
            "message": str
        }
        Use a single asterisk (*John Doe*) on each side whenever you make text bold; don't use double asterisks.
    """
    try:
        from image_analyzer import ImageAnalyzer
        from app.config import get_settings
        
        settings = get_settings()
        
        if not settings.openai_api_key:
            return {
                "success": False,
                "analysis": "",
                "message": "Image analysis is not available at the moment."
            }
        
        # Initialize analyzer
        analyzer = ImageAnalyzer(api_key=settings.openai_api_key)
        
        # Default prompt if none provided
        if not analysis_prompt:
            analysis_prompt = """
            Analyze this image and provide a small description (in paragraph style).
            
            Include:
            1. What is the main subject/product in the image?
            2. What is the condition (if applicable)?
            3. Any brand names or model numbers visible?
            
            At the end, if the product is an electronics or home appliance, ask the user if they want to trade-in or sell it.
            """
        
        # Analyze the image
        result = analyzer.analyze_image(image_url, analysis_prompt)
        
        if result.get("success"):
            analysis_text = result.get("analysis", "")
            
            logger.info(f"Successfully analyzed standalone image: {image_url[:100]}")
            
            return {
                "success": True,
                "analysis": analysis_text,
                "message": "Image analyzed successfully."
            }
        else:
            error_msg = result.get("error", "Unknown error")
            logger.error(f"Failed to analyze standalone image: {error_msg}")
            return {
                "success": False,
                "analysis": "",
                "message": f"Failed to analyze image: {error_msg}"
            }
            
    except Exception as e:
        logger.error(f"Error in analyze_standalone_image: {e}", exc_info=True)
        return {
            "success": False,
            "analysis": "",
            "message": f"Error analyzing image: {str(e)}"
        }



