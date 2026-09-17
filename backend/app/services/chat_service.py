from typing import List, Optional, Tuple
from uuid import UUID
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_
from sqlalchemy.orm import selectinload
from app.models.conversation import Conversation
from app.models.conversation_participant import ConversationParticipant
from app.models.message import Message, MessageState
from app.models.user import User
from app.schemas.chat import ConversationResponse, MessageResponse
from app.schemas.auth import UserResponse


async def create_conversation(
    db: AsyncSession,
    creator_id: UUID,
    participant_ids: List[UUID],
    title: Optional[str] = None
) -> Conversation:
    """Create a new conversation"""
    # Check for existing 1:1 conversation
    is_group = len(participant_ids) > 1

    if not is_group:
        # Check if 1:1 conversation already exists
        other_user_id = participant_ids[0]
        result = await db.execute(
            select(Conversation)
            .join(ConversationParticipant, Conversation.id == ConversationParticipant.conversation_id)
            .where(
                and_(
                    Conversation.is_group == False,
                    ConversationParticipant.user_id.in_([creator_id, other_user_id])
                )
            )
            .group_by(Conversation.id)
            .having(func.count(ConversationParticipant.user_id) == 2)
        )
        existing = result.scalar_one_or_none()
        if existing:
            return existing

    # Create new conversation
    conversation = Conversation(
        created_by=creator_id,
        is_group=is_group,
        title=title if is_group else None
    )
    db.add(conversation)
    await db.flush()

    # Add creator as participant
    creator_participant = ConversationParticipant(
        conversation_id=conversation.id,
        user_id=creator_id
    )
    db.add(creator_participant)

    # Add other participants
    for participant_id in participant_ids:
        if participant_id != creator_id:
            participant = ConversationParticipant(
                conversation_id=conversation.id,
                user_id=participant_id
            )
            db.add(participant)

    await db.commit()
    await db.refresh(conversation)
    return conversation


async def send_message(
    db: AsyncSession,
    conversation_id: UUID,
    sender_id: UUID,
    content: Optional[str],
    message_type: str,
    media_url: Optional[str] = None,
    thumbnail_url: Optional[str] = None
) -> Message:
    """Create and persist a new message"""
    # Verify sender is participant
    result = await db.execute(
        select(ConversationParticipant)
        .where(
            and_(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == sender_id
            )
        )
    )
    participant = result.scalar_one_or_none()
    if not participant:
        raise ValueError("Sender is not a participant in this conversation")

    # Create message
    message = Message(
        conversation_id=conversation_id,
        sender_id=sender_id,
        content=content,
        message_type=message_type,
        media_url=media_url,
        thumbnail_url=thumbnail_url,
        state=MessageState.sent
    )
    db.add(message)

    # Update conversation updated_at
    await db.execute(
        select(Conversation)
        .where(Conversation.id == conversation_id)
    )
    conversation = (await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )).scalar_one()
    conversation.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(message)
    return message


async def get_conversations(
    db: AsyncSession,
    user_id: UUID,
    limit: int = 50,
    cursor: Optional[datetime] = None
) -> List[Tuple[Conversation, Optional[Message], int]]:
    """Get user's conversations with last message and unread count"""
    query = (
        select(Conversation)
        .join(ConversationParticipant)
        .where(ConversationParticipant.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
    )

    if cursor:
        query = query.where(Conversation.updated_at < cursor)

    result = await db.execute(query)
    conversations = result.scalars().all()

    conversations_with_data = []
    for conv in conversations:
        # Get last message
        last_msg_result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        last_message = last_msg_result.scalar_one_or_none()

        # Get unread count
        participant_result = await db.execute(
            select(ConversationParticipant)
            .where(
                and_(
                    ConversationParticipant.conversation_id == conv.id,
                    ConversationParticipant.user_id == user_id
                )
            )
        )
        participant = participant_result.scalar_one()

        unread_query = select(func.count(Message.id)).where(
            and_(
                Message.conversation_id == conv.id,
                Message.sender_id != user_id
            )
        )
        if participant.last_read_at:
            unread_query = unread_query.where(Message.created_at > participant.last_read_at)

        unread_count = await db.scalar(unread_query)

        conversations_with_data.append((conv, last_message, unread_count or 0))

    return conversations_with_data


async def get_messages(
    db: AsyncSession,
    conversation_id: UUID,
    user_id: UUID,
    limit: int = 30,
    cursor: Optional[datetime] = None
) -> List[Message]:
    """Get messages from a conversation with pagination"""
    # Verify user is participant
    result = await db.execute(
        select(ConversationParticipant)
        .where(
            and_(
                ConversationParticipant.conversation_id == conversation_id,
                ConversationParticipant.user_id == user_id
            )
        )
    )
    if not result.scalar_one_or_none():
        raise ValueError("User is not a participant in this conversation")

    query = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(limit)
    )

    if cursor:
        query = query.where(Message.created_at < cursor)

    result = await db.execute(query)
    messages = result.scalars().all()
    return list(reversed(messages))  # Return in ascending order


async def update_message_state(
    db: AsyncSession,
    message_id: UUID,
    new_state: MessageState
) -> Message:
    """Update message delivery state"""
    result = await db.execute(
        select(Message).where(Message.id == message_id)
    )
    message = result.scalar_one_or_none()
    if not message:
        raise ValueError("Message not found")

    message.state = new_state
    await db.commit()
    await db.refresh(message)
    return message
