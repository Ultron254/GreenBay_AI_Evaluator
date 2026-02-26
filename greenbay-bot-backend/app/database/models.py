"""SQLAlchemy models for GreenBay Market Chatbot."""

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, 
    ForeignKey, JSON, Enum as SQLEnum, LargeBinary
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime
from enum import Enum
import uuid

from app.database.db import Base


class OrderStatus(str, Enum):
    """Order status enumeration."""
    PENDING = "pending"
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentStatus(str, Enum):
    """Payment status enumeration."""
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class DeliveryStatus(str, Enum):
    """Delivery status enumeration."""
    PENDING = "pending"
    SCHEDULED = "scheduled"
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETURNED = "returned"


class User(Base):
    """User model for storing customer information."""
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    phone_number = Column(String(20), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=True)
    email = Column(String(100), nullable=True)
    default_address = Column(Text, nullable=True)
    delivery_address = Column(Text, nullable=True)  # Stored delivery address for checkout
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    is_active = Column(Boolean, default=True)
    
    # Relationships
    carts = relationship("Cart", back_populates="user")
    orders = relationship("Order", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")


class Cart(Base):
    """Shopping cart model."""
    __tablename__ = "carts"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    retailer_id = Column(String(100), nullable=True)  # For catalog products
    variant_id = Column(String(50), nullable=True)  # For product variants
    added_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    user = relationship("User", back_populates="carts")


class Order(Base):
    """Order model."""
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(50), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(SQLEnum(OrderStatus, native_enum=True), default=OrderStatus.PENDING)
    subtotal = Column(Float, nullable=False)
    delivery_cost = Column(Float, default=0.0)
    installation_cost = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order")
    delivery = relationship("Delivery", back_populates="order", uselist=False)
    payment = relationship("Payment", back_populates="order", uselist=False)


class OrderItem(Base):
    """Order items model."""
    __tablename__ = "order_items"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(String(50), nullable=False)
    product_name = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    retailer_id = Column(String(100), nullable=True)
    variant_id = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    order = relationship("Order", back_populates="items")


class Delivery(Base):
    """Delivery model."""
    __tablename__ = "deliveries"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    delivery_address = Column(Text, nullable=False)
    delivery_zone = Column(Integer, nullable=True)  # Jumia zone
    delivery_cost = Column(Float, nullable=False)
    installation_requested = Column(Boolean, default=False)
    installation_cost = Column(Float, default=0.0)
    delivery_date = Column(DateTime(timezone=True), nullable=True)
    status = Column(SQLEnum(DeliveryStatus), default=DeliveryStatus.PENDING)
    tracking_number = Column(String(100), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    order = relationship("Order", back_populates="delivery")


class Payment(Base):
    """Payment model."""
    __tablename__ = "payments"
    
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    checkout_request_id = Column(String(100), nullable=True, index=True)  # STK Push CheckoutRequestID for querying status
    mpesa_transaction_id = Column(String(100), nullable=True)
    mpesa_receipt_number = Column(String(100), nullable=True)
    amount = Column(Float, nullable=False)
    status = Column(SQLEnum(PaymentStatus), default=PaymentStatus.PENDING)
    phone_number = Column(String(20), nullable=False)
    payment_date = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    order = relationship("Order", back_populates="payment")


class Conversation(Base):
    """Conversation model for tracking chat sessions."""
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    thread_id = Column(String(100), unique=True, index=True, nullable=False)
    phone_number = Column(String(20), nullable=False)
    last_message_at = Column(DateTime(timezone=True), server_default=func.now())
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    user = relationship("User", back_populates="conversations")


class ConversationMessage(Base):
    """Conversation messages for storing user/assistant/tool content."""
    __tablename__ = "conversation_messages"
    
    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    role = Column(String(20), nullable=False)  # user | assistant | tool
    content = Column(Text, nullable=False)
    tool_name = Column(String(100), nullable=True)
    token_count = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    conversation = relationship("Conversation")


class TradeInSession(Base):
    """Trade-in session model for storing all trade-in data in one table."""
    __tablename__ = "trade_in_sessions"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(100), unique=True, index=True, nullable=False)
    user_phone = Column(String(20), nullable=False, index=True)
    transaction_type = Column(String(20), default="trade_in")  # trade_in or sell
    
    # Product information
    product_name = Column(String(200), nullable=True)
    product_model = Column(String(200), nullable=True)
    retail_price = Column(Float, nullable=True)
    currency = Column(String(10), default="KES")
    price_source = Column(String(500), nullable=True)
    
    # Image tracking
    images_uploaded = Column(Boolean, default=False)  # Whether images are uploaded
    image_urls = Column(JSON, nullable=True)  # List of image URLs
    s3_keys = Column(JSON, nullable=True)  # List of S3 keys
    
    # Assessment data
    condition_score = Column(Float, nullable=True)
    condition_grade = Column(String(50), nullable=True)  # Excellent, Good, Fair, Poor
    issues_found = Column(JSON, nullable=True)  # List of issues
    overall_assessment = Column(Text, nullable=True)
    
    # Category-specific assessment
    detected_category = Column(String(100), nullable=True)  # E.g., refrigerator, TV, stove
    category_questions = Column(Text, nullable=True)  # JSON string of questions
    condition_responses = Column(Text, nullable=True)  # JSON string of responses
    
    # Valuation data
    trade_in_value = Column(Float, nullable=True)
    final_offer = Column(Float, nullable=True)
    min_offer = Column(Float, nullable=True)  # Minimum offer (10% below final_offer)
    max_offer = Column(Float, nullable=True)  # Maximum offer (10% above final_offer)
    condition_factor = Column(Float, nullable=True)
    market_factor = Column(Float, nullable=True)
    valuation_reasoning = Column(Text, nullable=True)
    
    # Decision and redemption
    offer_decision = Column(String(50), nullable=True)  # accepted, rejected, pending
    redemption_method = Column(String(50), nullable=True)  # cash_pickup, store_visit, store_credit
    pickup_address = Column(String(500), nullable=True)
    contact_phone = Column(String(20), nullable=True)
    store_credit_balance = Column(Float, nullable=True)
    
    # Collection tracking
    product_collected = Column(Boolean, default=False)
    
    # Session lifecycle
    status = Column(String(50), default="active")  # active, completed, cancelled, pending
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    end_time = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships (keeping for backward compatibility during migration)
    images = relationship("TradeInImage", back_populates="session", cascade="all, delete-orphan")
    assessment = relationship("TradeInAssessment", back_populates="session", uselist=False)


class TradeInImage(Base):
    """Trade-in images model for storing uploaded images."""
    __tablename__ = "trade_in_images"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("trade_in_sessions.id"), nullable=False)
    image_url = Column(String(1000), nullable=False)
    s3_key = Column(String(500), nullable=False)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    session = relationship("TradeInSession", back_populates="images")


class TradeInAssessment(Base):
    """Trade-in assessment model for storing condition analysis."""
    __tablename__ = "trade_in_assessments"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("trade_in_sessions.id"), nullable=False)
    condition_score = Column(Float, nullable=False)
    condition_grade = Column(String(50), nullable=False)  # Excellent, Good, Fair, Poor
    issues_found = Column(JSON, nullable=True)  # List of issues
    overall_assessment = Column(Text, nullable=True)
    analysis_timestamp = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    session = relationship("TradeInSession", back_populates="assessment")


class TradeInValuation(Base):
    """Trade-in valuation model for storing calculated offers."""
    __tablename__ = "trade_in_valuations"
    
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("trade_in_sessions.id"), nullable=False)
    trade_in_value = Column(Float, nullable=False)
    final_offer = Column(Float, nullable=False)
    condition_factor = Column(Float, nullable=False)
    market_factor = Column(Float, nullable=False)
    valuation_reasoning = Column(Text, nullable=True)
    user_decision = Column(String(50), nullable=True)  # accepted, rejected, pending
    redemption_method = Column(String(50), nullable=True)  # cash_pickup, store_visit, store_credit
    pickup_address = Column(String(500), nullable=True)  # For cash pickup option
    contact_phone = Column(String(20), nullable=True)  # For pickup contact
    store_credit_balance = Column(Float, nullable=True)  # Remaining credit for store purchases
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    session = relationship("TradeInSession")
