"""LangGraph agent state definitions."""

from typing import TypedDict, List, Dict, Any, Optional
from langchain_core.messages import BaseMessage


class ChatbotState(TypedDict):
    """State structure for the GreenBay Market chatbot agent."""
    
    # Core conversation state
    messages: List[BaseMessage]
    thread_id: str
    user_phone: str
    
    # User information
    user_info: Dict[str, Any]  # name, email, default_address
    
    # Cart management
    cart: List[Dict[str, Any]]  # cart items with product details
    cart_total: float
    
    # Product search and selection
    current_search_results: List[Dict[str, Any]]  # Latest search results
    selected_product: Optional[Dict[str, Any]]  # Currently selected product
    
    # Order management
    current_order: Optional[Dict[str, Any]]  # Current order being processed
    order_id: Optional[str]
    
    # Delivery information
    delivery_address: Optional[str]
    delivery_cost: float
    delivery_zone: Optional[int]
    installation_requested: bool
    installation_cost: float
    
    # Payment information
    payment_status: str  # pending, completed, failed
    payment_amount: float
    mpesa_transaction_id: Optional[str]
    
    # Cross-selling
    cross_sell_offered: bool  # Whether cross-sell has been offered
    cross_sell_accepted: bool  # Whether cross-sell was accepted
    
    # Session management
    session_start_time: str
    last_activity: str
    conversation_stage: str  # greeting, browsing, cart, checkout, payment, etc.
    
    # Error handling
    error_message: Optional[str]
    retry_count: int
    
    # Flags for workflow control
    awaiting_user_response: bool  # Whether agent is waiting for user input
    workflow_completed: bool  # Whether current workflow is completed
    
    # Additional metadata
    metadata: Dict[str, Any]  # Additional state data


