"""LangGraph ReAct Agent implementation for GreenBay Market chatbot.

This module implements a proper ReAct (Reasoning and Acting) tool-calling agent
using LangGraph's create_react_agent. The ReAct pattern allows the agent to:
1. Reason about user queries
2. Decide which tools to call
3. Execute tools and observe results
4. Iterate until a final answer is reached

The agent uses a messages-only state structure as required by LangGraph's ReAct agent,
with custom state (cart, user info, etc.) managed separately through context variables.
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import time
from loguru import logger

from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langsmith import traceable, Client

from app.agent.tools import ALL_TOOLS, set_current_user_phone
from app.agent.compressed_prompt import get_compressed_system_prompt
from app.config import get_settings

settings = get_settings()


class GreenBayChatbotAgent:
    """
    GreenBay Market WhatsApp E-commerce Chatbot Agent using LangGraph ReAct pattern.
    
    This agent implements the ReAct (Reasoning and Acting) framework:
    - Reasoning: The LLM reasons about what actions to take
    - Acting: The agent calls tools to perform actions
    - Observing: The agent observes tool results and continues reasoning
    
    The ReAct agent automatically handles:
    - Tool selection based on user queries
    - Tool execution and result processing
    - Iterative reasoning until a final answer
    - Conversation state management through messages
    """
    
    def __init__(self):
        """Initialize the ReAct chatbot agent."""
        self.settings = settings
        
        # Initialize LLM with tool-calling support
        self.llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            api_key=settings.openai_api_key
        )
        
        # Create memory saver for conversation persistence
        # This stores conversation history across sessions
        self.memory = MemorySaver()
        
        # Create ReAct Agent with all tools
        # The ReAct agent automatically handles:
        # - Tool selection and calling
        # - Result processing
        # - Iterative reasoning loops
        # - State management (messages only)
        self.agent = create_react_agent(
            model=self.llm,
            tools=ALL_TOOLS,
            checkpointer=self.memory,
            # Optional: customize the system message
            # system_message=self._get_system_prompt()
        )
        
        # Initialize LangSmith client for custom monitoring
        if self.settings.langsmith_api_key:
            self.langsmith_client = Client(
                api_url=self.settings.langsmith_endpoint,
                api_key=self.settings.langsmith_api_key
            )
        else:
            self.langsmith_client = None
        
        logger.info("✓ GreenBay ReAct Chatbot Agent initialized successfully")
        logger.info(f"  - Model: {self.llm.model_name}")
        logger.info(f"  - Tools available: {len(ALL_TOOLS)}")
        logger.info(f"  - Memory: Enabled (MemorySaver)")
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for the agent."""
        return get_compressed_system_prompt()
    
    @traceable(name="process_whatsapp_message")
    async def process_message(
        self,
        user_phone: str,
        message: str,
        thread_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Process a user message using the ReAct agent pattern.
        
        The ReAct agent will:
        1. Receive the user message
        2. Reason about what tools to call
        3. Execute tools and observe results
        4. Continue reasoning until a final answer
        
        Args:
            user_phone: User's phone number (used for context and state management)
            message: User's message
            thread_id: Optional thread ID for conversation continuity
            
        Returns:
            Dictionary with agent response and metadata
        """
        start_time = time.time()
        try:
            # Generate thread ID if not provided
            # Thread ID is used for conversation persistence
            if not thread_id:
                thread_id = f"whatsapp_{user_phone}_{datetime.now().strftime('%Y%m%d')}"
            
            # Set user phone in context variable BEFORE agent invocation
            # This allows all tools to access the user phone via contextvars
            set_current_user_phone(user_phone)
            logger.info(f"✓ Set user phone {user_phone} in context before agent invocation")
            
            # ReAct agent state structure: Only "messages" is required
            # The ReAct agent manages conversation state through messages
            # Custom state (cart, user info, etc.) is managed separately via:
            # - Context variables (for user_phone)
            # - Database (for persistent data like cart, orders)
            # - Tool return values (for temporary state)
            
            # Get existing conversation history from checkpointer
            config = {"configurable": {"thread_id": thread_id}}
            
            # Check for existing state and get messages
            existing_messages = []
            try:
                current_state = await self.agent.aget_state(config)
                if current_state and "messages" in current_state.values:
                    existing_messages = current_state.values["messages"]
                    logger.debug(f"Found {len(existing_messages)} existing messages in thread {thread_id}")
            except Exception as state_error:
                logger.debug(f"No existing state found for thread {thread_id}: {state_error}")
                existing_messages = []
            
            # Build messages list:
            # 1. System message (only if this is a new conversation)
            # 2. Existing conversation history
            # 3. New user message
            messages = []
            
            # Check if this is mid-conversation
            is_mid_conversation = len(existing_messages) > 0
            
            # Add system message only if no existing messages
            if not existing_messages:
                system_message = SystemMessage(content=self._get_system_prompt())
                messages.append(system_message)
                logger.debug("Added system prompt to new conversation")
            else:
                # For mid-conversation, add a context note if user is greeting
                greeting_words = ['hi', 'hello', 'hey', 'greetings', 'good morning', 'good afternoon', 'good evening']
                user_message_lower = message.lower().strip()
                is_greeting = any(user_message_lower.startswith(word) for word in greeting_words) or user_message_lower in greeting_words
                
                if is_greeting:
                    # Add subtle context hint for the agent
                    logger.debug(f"Mid-conversation greeting detected: '{message}'")
            
            # Add existing conversation history (excluding system message if already present)
            messages.extend(existing_messages)
            
            # Add new user message
            human_message = HumanMessage(content=message)
            messages.append(human_message)
            
            # ReAct agent state: Only messages are passed
            # The agent will automatically:
            # - Parse the messages
            # - Decide which tools to call
            # - Execute tools
            # - Process results
            # - Generate final response
            initial_state = {"messages": messages}
            
            # Run the ReAct agent with increased recursion limit
            # recursion_limit controls max iterations of reasoning + acting
            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 50  # Allow up to 50 reasoning iterations
            }
            
            logger.debug(f"Invoking ReAct agent with {len(messages)} messages, recursion_limit=50")
            
            # Invoke the ReAct agent
            # This will trigger the ReAct loop:
            # 1. Agent reasons about the query
            # 2. Agent decides to call a tool (or respond)
            # 3. Tool is executed
            # 4. Agent observes the result
            # 5. Agent continues reasoning
            # 6. Repeat until final answer
            result = await self.agent.ainvoke(initial_state, config=config)
            
            # Extract response from ReAct agent result
            # The last message should be an AIMessage with the final response
            response_text = None
            tool_calls_made = 0
            
            if result and "messages" in result:
                messages = result["messages"]
                
                # Count ALL tool calls across all AIMessages in this turn
                for msg in messages:
                    if isinstance(msg, AIMessage):
                        if hasattr(msg, 'tool_calls') and msg.tool_calls:
                            tool_calls_made += len(msg.tool_calls)
                
                # Find the last AIMessage (final response) for content
                for msg in reversed(messages):
                    if isinstance(msg, AIMessage) and msg.content:
                        response_text = msg.content
                        break
                
                # If no content in last AIMessage, check if there were tool calls
                if not response_text:
                    # Check if agent is waiting for tool results or made tool calls
                    last_ai_msg = None
                    for msg in reversed(messages):
                        if isinstance(msg, AIMessage):
                            last_ai_msg = msg
                            break
                    
                    if last_ai_msg and hasattr(last_ai_msg, 'tool_calls') and last_ai_msg.tool_calls:
                        # Agent made tool calls but hasn't responded yet
                        # This shouldn't happen with ReAct agent, but handle gracefully
                        logger.warning("Agent made tool calls but no response content found")
                        response_text = "I'm processing your request. Please wait a moment."
                    else:
                        response_text = "I'm sorry, I couldn't process your request. Please try again."
            
            if not response_text:
                response_text = "I'm sorry, I couldn't process your request. Please try again."
            
            processing_time = time.time() - start_time
            logger.info(f"✓ ReAct agent completed in {processing_time:.2f}s (tools called: {tool_calls_made})")
            
            # Log custom metrics to LangSmith
            if self.langsmith_client:
                try:
                    self.langsmith_client.create_run(
                        name="react_agent_whatsapp_message",
                        run_type="chain",
                        inputs={
                            "user_phone": user_phone,
                            "message": message,
                            "thread_id": thread_id
                        },
                        outputs={
                            "response": response_text,
                            "success": True,
                            "tool_calls": tool_calls_made
                        },
                        extra={
                            "metadata": {
                                "processing_time": processing_time,
                                "user_phone": user_phone,
                                "message_length": len(message),
                                "thread_id": thread_id,
                                "response_length": len(response_text),
                                "tool_calls_made": tool_calls_made,
                                "agent_type": "react"
                            }
                        }
                    )
                except Exception as trace_error:
                    logger.warning(f"Failed to log to LangSmith: {trace_error}")
            
            return {
                "success": True,
                "response": response_text,
                "thread_id": thread_id,
                "user_phone": user_phone,
                "tool_calls_made": tool_calls_made,
                "processing_time": processing_time
            }
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error processing message: {e}")
            
            # Handle incomplete tool calls error specifically
            # This can happen if the agent state gets corrupted
            if "tool_calls that do not have a corresponding ToolMessage" in error_msg or "INVALID_CHAT_HISTORY" in error_msg:
                logger.warning("Detected incomplete tool calls, attempting to recover")
                try:
                    # Retry with a fresh state (new thread)
                    new_thread_id = f"{thread_id}_recovery_{int(time.time())}"
                    logger.info(f"Retrying with fresh thread: {new_thread_id}")
                    
                    config = {
                        "configurable": {"thread_id": new_thread_id},
                        "recursion_limit": 50
                    }
                    
                    # Fresh state with system prompt and user message
                    fresh_state = {
                        "messages": [
                            SystemMessage(content=self._get_system_prompt()),
                            HumanMessage(content=message)
                        ]
                    }
                    
                    result = await self.agent.ainvoke(fresh_state, config=config)
                    
                    if result and "messages" in result:
                        for msg in reversed(result["messages"]):
                            if isinstance(msg, AIMessage) and msg.content:
                                response_text = msg.content
                                break
                        
                        if response_text:
                            return {
                                "success": True,
                                "response": response_text,
                                "thread_id": new_thread_id,
                                "user_phone": user_phone,
                                "recovered": True
                            }
                except Exception as retry_error:
                    logger.error(f"Recovery attempt failed: {retry_error}")
            
            # Log errors to LangSmith
            if self.langsmith_client:
                try:
                    self.langsmith_client.create_run(
                        name="whatsapp_message_error",
                        run_type="chain",
                        inputs={"user_phone": user_phone, "message": message},
                        outputs={"error": str(e)},
                        extra={
                            "metadata": {
                                "error_type": type(e).__name__,
                                "processing_time": time.time() - start_time,
                                "user_phone": user_phone,
                                "message_length": len(message)
                            }
                        }
                    )
                except Exception as trace_error:
                    logger.warning(f"Failed to log error to LangSmith: {trace_error}")
            
            return {
                "success": False,
                "response": "I'm sorry, I encountered an error. Please try again or contact support.",
                "thread_id": thread_id,
                "user_phone": user_phone,
                "error": str(e)
            }
    
    async def get_conversation_history(
        self,
        thread_id: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get conversation history for a thread.
        
        Args:
            thread_id: Thread identifier
            limit: Maximum number of messages to return
            
        Returns:
            List of conversation messages
        """
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await self.agent.aget_state(config)
            
            if state and "messages" in state.values:
                messages = state.values["messages"]
                # Return last N messages
                recent_messages = messages[-limit:] if len(messages) > limit else messages
                
                formatted_messages = []
                for msg in recent_messages:
                    formatted_messages.append({
                        "type": "human" if isinstance(msg, HumanMessage) else "ai",
                        "content": msg.content,
                        "timestamp": datetime.now().isoformat()
                    })
                
                return formatted_messages
            
            return []
            
        except Exception as e:
            logger.error(f"Error getting conversation history: {e}")
            return []
    
    async def health_check(self) -> bool:
        """
        Check if the agent is healthy.
        
        Returns:
            True if agent is healthy, False otherwise
        """
        try:
            # Test with a simple message
            test_result = await self.process_message(
                user_phone="254700000000",
                message="Hello",
                thread_id="health_check"
            )
            return test_result["success"]
            
        except Exception as e:
            logger.error(f"Agent health check failed: {e}")
            return False


# Global agent instance
greenbay_agent = GreenBayChatbotAgent()
