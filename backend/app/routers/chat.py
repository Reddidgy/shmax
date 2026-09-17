from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from uuid import UUID
from datetime import datetime
from app.database import get_db
from app.models.user import User
from app.models.conversation import Conversation
from app.models.conversation_participant import ConversationParticipant
from app.models.message import Message
from app.schemas.chat import (
    ConversationCreate,
    ConversationResponse,
    MessageCreate,
    MessageResponse
)
from app.schemas.auth import UserResponse
from app.auth.security import get_current_user
from app.services.chat_service import (
    create_conversation,
    send_message,
    get_conversations,
    get_messages
)
from app.routers.websocket import manager

router = APIRouter(prefix="/conversations", tags=["chat"])


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_new_conversation(
    conversation_data: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create a new conversation"""
    # Validate participant IDs
    for participant_id in conversation_data.participant_ids:
        result = await db.execute(select(User).where(User.id == participant_id))
        if not result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User {participant_id} not found"
            )

    conversation = await create_conversation(
        db=db,
        creator_id=current_user.id,
        participant_ids=conversation_data.participant_ids,
        title=conversation_data.title
    )

    # Fetch participants
    participants_result = await db.execute(
        select(User)
        .join(ConversationParticipant)
        .where(ConversationParticipant.conversation_id == conversation.id)
    )
    participants = participants_result.scalars().all()

    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        is_group=conversation.is_group,
        participants=[UserResponse.model_validate(p) for p in participants],
        last_message=None,
        unread_count=0,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at
    )


@router.get("", response_model=List[ConversationResponse])
async def list_conversations(
    limit: int = Query(50, le=50),
    cursor: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get user's conversations"""
    cursor_dt = datetime.fromisoformat(cursor) if cursor else None
    conversations_data = await get_conversations(db, current_user.id, limit, cursor_dt)

    responses = []
    for conv, last_msg, unread_count in conversations_data:
        # Fetch participants
        participants_result = await db.execute(
            select(User)
            .join(ConversationParticipant)
            .where(ConversationParticipant.conversation_id == conv.id)
        )
        participants = participants_result.scalars().all()

        # Build last message response if exists
        last_message_response = None
        if last_msg:
            sender_result = await db.execute(select(User).where(User.id == last_msg.sender_id))
            sender = sender_result.scalar_one()
            last_message_response = MessageResponse(
                id=last_msg.id,
                conversation_id=last_msg.conversation_id,
                sender=UserResponse.model_validate(sender),
                content=last_msg.content,
                message_type=last_msg.message_type,
                media_url=last_msg.media_url,
                thumbnail_url=last_msg.thumbnail_url,
                state=last_msg.state,
                created_at=last_msg.created_at
            )

        responses.append(
            ConversationResponse(
                id=conv.id,
                title=conv.title,
                is_group=conv.is_group,
                participants=[UserResponse.model_validate(p) for p in participants],
                last_message=last_message_response,
                unread_count=unread_count,
                created_at=conv.created_at,
                updated_at=conv.updated_at
            )
        )

    return responses


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get conversation details"""
    # Verify user is participant
    result = await db.execute(
        select(ConversationParticipant)
        .where(
            ConversationParticipant.conversation_id == conversation_id,
            ConversationParticipant.user_id == current_user.id
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found"
        )

    # Get conversation
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = conv_result.scalar_one()

    # Get participants
    participants_result = await db.execute(
        select(User)
        .join(ConversationParticipant)
        .where(ConversationParticipant.conversation_id == conversation_id)
    )
    participants = participants_result.scalars().all()

    return ConversationResponse(
        id=conversation.id,
        title=conversation.title,
        is_group=conversation.is_group,
        participants=[UserResponse.model_validate(p) for p in participants],
        last_message=None,
        unread_count=0,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at
    )


@router.get("/{conversation_id}/messages", response_model=List[MessageResponse])
async def list_messages(
    conversation_id: UUID,
    limit: int = Query(30, le=100),
    cursor: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get messages from a conversation"""
    cursor_dt = datetime.fromisoformat(cursor) if cursor else None

    try:
        messages = await get_messages(db, conversation_id, current_user.id, limit, cursor_dt)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )

    # Build message responses
    responses = []
    for msg in messages:
        sender_result = await db.execute(select(User).where(User.id == msg.sender_id))
        sender = sender_result.scalar_one()

        responses.append(
            MessageResponse(
                id=msg.id,
                conversation_id=msg.conversation_id,
                sender=UserResponse.model_validate(sender),
                content=msg.content,
                message_type=msg.message_type,
                media_url=msg.media_url,
                thumbnail_url=msg.thumbnail_url,
                state=msg.state,
                created_at=msg.created_at
            )
        )

    return responses


@router.post("/{conversation_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def create_message(
    conversation_id: UUID,
    message_data: MessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Send a message in a conversation"""
    try:
        message = await send_message(
            db=db,
            conversation_id=conversation_id,
            sender_id=current_user.id,
            content=message_data.content,
            message_type=message_data.message_type,
            media_url=message_data.media_url,
            thumbnail_url=message_data.thumbnail_url
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e)
        )

    # Broadcast new message to other participants via WebSocket
    try:
        # Query all participant user IDs for this conversation
        participants_result = await db.execute(
            select(ConversationParticipant.user_id)
            .where(ConversationParticipant.conversation_id == conversation_id)
        )
        participant_ids = [str(row[0]) for row in participants_result.all()]

        # Broadcast event to all participants except the sender
        event_payload = {
            "type": "new_message",
            "conversation_id": str(conversation_id),
            "message": {
                "id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "sender": {
                    "id": str(current_user.id),
                    "username": current_user.username,
                    "display_name": current_user.display_name,
                    "avatar_url": current_user.avatar_url,
                },
                "content": message.content,
                "message_type": message.message_type,
                "media_url": message.media_url,
                "thumbnail_url": message.thumbnail_url,
                "state": message.state.value if hasattr(message.state, 'value') else str(message.state),
                "created_at": message.created_at.isoformat(),
            }
        }
        await manager.broadcast_to_conversation(
            event_payload,
            participant_ids,
            exclude_user=str(current_user.id)
        )
    except Exception as e:
        # Log error but don't fail the HTTP response
        print(f"WebSocket broadcast error: {e}")

    return MessageResponse(
        id=message.id,
        conversation_id=message.conversation_id,
        sender=UserResponse.model_validate(current_user),
        content=message.content,
        message_type=message.message_type,
        media_url=message.media_url,
        thumbnail_url=message.thumbnail_url,
        state=message.state,
        created_at=message.created_at
    )
