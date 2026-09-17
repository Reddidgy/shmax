from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from typing import List
from datetime import datetime, timedelta
from uuid import UUID

from app.database import get_db
from app.auth.security import get_current_user
from app.models.user import User
from app.models.contact import Contact
from app.models.friend_request import FriendRequest, FriendRequestStatus, InviteLink
from app.schemas.friend_request import InviteLinkCreate, InviteLinkResponse

router = APIRouter(prefix="/invites", tags=["invites"])


@router.post("", response_model=InviteLinkResponse, status_code=status.HTTP_201_CREATED)
async def create_invite_link(
    request: InviteLinkCreate = InviteLinkCreate(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    invite = InviteLink(
        user_id=current_user.id,
        expires_at=datetime.utcnow() + timedelta(days=request.expires_in_days),
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)

    response = InviteLinkResponse.model_validate(invite)
    response.invite_url = f"/invites/{invite.code}"
    return response


@router.get("/{code}", response_model=InviteLinkResponse)
async def resolve_invite_link(code: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(InviteLink).where(
            and_(
                InviteLink.code == code,
                InviteLink.is_active == True
            )
        )
    )
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=404, detail="Invite link not found or expired")
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Invite link has expired")

    return invite


@router.post("/{code}/accept", response_model=InviteLinkResponse)
async def accept_invite_link(
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(InviteLink).where(
            and_(
                InviteLink.code == code,
                InviteLink.is_active == True
            )
        )
    )
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=404, detail="Invite link not found or expired")
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        raise HTTPException(status_code=410, detail="Invite link has expired")
    if invite.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot accept your own invite")

    # Check if already friends
    existing_contact_result = await db.execute(
        select(Contact).where(
            and_(
                Contact.user_id == current_user.id,
                Contact.contact_user_id == invite.user_id
            )
        )
    )
    existing_contact = existing_contact_result.scalar_one_or_none()
    if existing_contact:
        raise HTTPException(status_code=400, detail="Already friends")

    # Create friend request with accepted status and bidirectional contact relationship
    friend_request = FriendRequest(
        from_user_id=current_user.id,
        to_user_id=invite.user_id,
        status=FriendRequestStatus.accepted,
    )
    contact1 = Contact(user_id=current_user.id, contact_user_id=invite.user_id)
    contact2 = Contact(user_id=invite.user_id, contact_user_id=current_user.id)
    db.add_all([friend_request, contact1, contact2])

    await db.commit()
    await db.refresh(invite)
    return invite


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_invite_link(
    invite_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(InviteLink).where(InviteLink.id == invite_id)
    )
    invite = result.scalar_one_or_none()

    if not invite:
        raise HTTPException(status_code=404, detail="Invite link not found")
    if invite.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    invite.is_active = False
    await db.commit()
