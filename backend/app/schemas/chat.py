from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime
from typing import Optional, List
from app.schemas.auth import UserResponse
from app.models.message import MessageType, MessageState


class ConversationCreate(BaseModel):
    participant_ids: List[UUID]
    title: Optional[str] = None


class MessageResponse(BaseModel):
    id: UUID
    conversation_id: UUID
    sender: UserResponse
    content: Optional[str] = None
    message_type: MessageType
    media_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    state: MessageState
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationResponse(BaseModel):
    id: UUID
    title: Optional[str] = None
    is_group: bool
    participants: List[UserResponse]
    last_message: Optional[MessageResponse] = None
    unread_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MessageCreate(BaseModel):
    content: Optional[str] = None
    message_type: MessageType = MessageType.text
    media_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    class Config:
        use_enum_values = True


class MessageStateUpdate(BaseModel):
    message_id: UUID
    state: MessageState

    class Config:
        use_enum_values = True


class TypingIndicator(BaseModel):
    conversation_id: UUID
    user_id: UUID
    is_typing: bool
