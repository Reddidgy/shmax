from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List
from datetime import datetime
from uuid import UUID

from app.database import get_db
from app.auth.security import get_current_user
from app.models.user import User
from app.models.contact import Contact
from app.models.friend_request import FriendRequest, FriendRequestStatus
from app.schemas.friend_request import FriendRequestCreate, FriendRequestResponse

router = APIRouter(prefix="/friend-requests", tags=["friend-requests"])


@router.post("", response_model=FriendRequestResponse, status_code=status.HTTP_201_CREATED)
async def send_friend_request(
    request: FriendRequestCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if request.to_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot send friend request to yourself")

    # Check if target user exists
    target_user_result = await db.execute(
        select(User).where(User.id == request.to_user_id)
    )
    target_user = target_user_result.scalar_one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if already friends
    existing_contact_result = await db.execute(
        select(Contact).where(
            and_(
                Contact.user_id == current_user.id,
                Contact.contact_user_id == request.to_user_id
            )
        )
    )
    existing_contact = existing_contact_result.scalar_one_or_none()
    if existing_contact:
        raise HTTPException(status_code=400, detail="Already friends")

    # Check if request already sent
    existing_request_result = await db.execute(
        select(FriendRequest).where(
            and_(
                FriendRequest.from_user_id == current_user.id,
                FriendRequest.to_user_id == request.to_user_id,
                FriendRequest.status == FriendRequestStatus.pending
            )
        )
    )
    existing_request = existing_request_result.scalar_one_or_none()
    if existing_request:
        raise HTTPException(status_code=400, detail="Friend request already sent")

    # Check if there's a pending request from the other user (mutual request scenario)
    reverse_request_result = await db.execute(
        select(FriendRequest).where(
            and_(
                FriendRequest.from_user_id == request.to_user_id,
                FriendRequest.to_user_id == current_user.id,
                FriendRequest.status == FriendRequestStatus.pending
            )
        )
    )
    reverse_request = reverse_request_result.scalar_one_or_none()
    if reverse_request:
        # Mutual request - automatically accept and create contact relationship
        reverse_request.status = FriendRequestStatus.accepted
        reverse_request.updated_at = datetime.utcnow()

        contact1 = Contact(user_id=current_user.id, contact_user_id=request.to_user_id)
        contact2 = Contact(user_id=request.to_user_id, contact_user_id=current_user.id)
        db.add_all([contact1, contact2])

        await db.commit()
        await db.refresh(reverse_request)
        return reverse_request

    # Create new friend request
    friend_request = FriendRequest(
        from_user_id=current_user.id,
        to_user_id=request.to_user_id,
    )
    db.add(friend_request)
    await db.commit()
    await db.refresh(friend_request)
    return friend_request


@router.post("/{request_id}/accept", response_model=FriendRequestResponse)
async def accept_friend_request(
    request_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    friend_request_result = await db.execute(
        select(FriendRequest).where(FriendRequest.id == request_id)
    )
    friend_request = friend_request_result.scalar_one_or_none()

    if not friend_request:
        raise HTTPException(status_code=404, detail="Friend request not found")
    if friend_request.to_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if friend_request.status != FriendRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Request already processed")

    # Update request status
    friend_request.status = FriendRequestStatus.accepted
    friend_request.updated_at = datetime.utcnow()

    # Create bidirectional contact relationship
    contact1 = Contact(user_id=current_user.id, contact_user_id=friend_request.from_user_id)
    contact2 = Contact(user_id=friend_request.from_user_id, contact_user_id=current_user.id)
    db.add_all([contact1, contact2])

    await db.commit()
    await db.refresh(friend_request)
    return friend_request


@router.post("/{request_id}/decline", response_model=FriendRequestResponse)
async def decline_friend_request(
    request_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    friend_request_result = await db.execute(
        select(FriendRequest).where(FriendRequest.id == request_id)
    )
    friend_request = friend_request_result.scalar_one_or_none()

    if not friend_request:
        raise HTTPException(status_code=404, detail="Friend request not found")
    if friend_request.to_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    if friend_request.status != FriendRequestStatus.pending:
        raise HTTPException(status_code=400, detail="Request already processed")

    friend_request.status = FriendRequestStatus.declined
    friend_request.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(friend_request)
    return friend_request


@router.get("/incoming", response_model=List[FriendRequestResponse])
async def get_incoming_requests(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FriendRequest)
        .where(
            and_(
                FriendRequest.to_user_id == current_user.id,
                FriendRequest.status == FriendRequestStatus.pending
            )
        )
        .order_by(FriendRequest.created_at.desc())
    )
    return result.scalars().all()


@router.get("/outgoing", response_model=List[FriendRequestResponse])
async def get_outgoing_requests(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(FriendRequest)
        .where(
            and_(
                FriendRequest.from_user_id == current_user.id,
                FriendRequest.status == FriendRequestStatus.pending
            )
        )
        .order_by(FriendRequest.created_at.desc())
    )
    return result.scalars().all()
