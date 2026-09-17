import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, ForeignKey, DateTime, Enum, Index
from sqlalchemy.dialects.postgresql import UUID
import enum
from app.database import Base


class MessageType(str, enum.Enum):
    text = "text"
    video_circle = "video_circle"
    image = "image"


class MessageState(str, enum.Enum):
    sent = "sent"
    delivered = "delivered"
    read = "read"


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    content = Column(Text, nullable=True)
    message_type = Column(Enum(MessageType), nullable=False, default=MessageType.text)
    media_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)
    state = Column(Enum(MessageState), nullable=False, default=MessageState.sent)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        Index("idx_messages_conversation_created", "conversation_id", "created_at"),
    )
