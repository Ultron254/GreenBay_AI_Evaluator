"""Service for writing conversation messages to the database."""

from typing import Optional
from loguru import logger
from datetime import datetime

from app.database.db import get_db
from app.database.models import Conversation, User, ConversationMessage


def log_message(user_phone: str, role: str, content: str, tool_name: Optional[str] = None, token_count: Optional[int] = None, thread_id: Optional[str] = None) -> None:
    """
    Persist a conversation message (user/assistant/tool) linked to the user's conversation.
    - Creates the user and/or conversation if needed.
    - Links the message to the conversation.
    """
    db = next(get_db())
    try:
        # Get or create user
        user = db.query(User).filter(User.phone_number == user_phone).first()
        if not user:
            user = User(phone_number=user_phone)
            db.add(user)
            db.commit()
            db.refresh(user)

        # Determine thread id
        if not thread_id:
            thread_id = f"whatsapp_{user_phone}"

        # Get or create conversation
        convo = db.query(Conversation).filter(Conversation.thread_id == thread_id).first()
        if not convo:
            convo = Conversation(user_id=user.id, thread_id=thread_id, phone_number=user_phone, is_active=True)
            db.add(convo)
            db.commit()
            db.refresh(convo)
        else:
            convo.last_message_at = datetime.now()
            if convo.user_id != user.id:
                convo.user_id = user.id
            db.commit()

        # Write message
        msg = ConversationMessage(
            conversation_id=convo.id,
            role=role,
            content=content or "",
            tool_name=tool_name,
            token_count=token_count
        )
        db.add(msg)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"Failed to log conversation message: {e}")
    finally:
        db.close()


