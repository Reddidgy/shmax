from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload
from typing import List, Optional
from uuid import UUID
from app.database import get_db
from app.models.user import User
from app.models.contact import Contact
from app.schemas.contact import ContactAdd, ContactResponse, UserSearchResult
from app.schemas.auth import UserResponse
from app.auth.security import get_current_user

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.get("", response_model=List[ContactResponse])
async def get_contacts(
    limit: int = Query(50, le=50),
    cursor: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get user's contact list"""
    query = (
        select(Contact)
        .where(Contact.user_id == current_user.id)
        .order_by(Contact.created_at.desc())
        .limit(limit)
    )

    if cursor:
        # Cursor is the contact ID
        query = query.where(Contact.id < cursor)

    result = await db.execute(query)
    contacts = result.scalars().all()

    # Fetch contact user details
    contact_responses = []
    for contact in contacts:
        user_result = await db.execute(
            select(User).where(User.id == contact.contact_user_id)
        )
        contact_user = user_result.scalar_one()

        contact_responses.append(
            ContactResponse(
                id=contact.id,
                user=UserResponse.model_validate(contact_user),
                created_at=contact.created_at
            )
        )

    return contact_responses


@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
async def add_contact(
    contact_data: ContactAdd,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Add a new contact"""
    # Check if contact user exists
    result = await db.execute(
        select(User).where(User.id == contact_data.contact_user_id)
    )
    contact_user = result.scalar_one_or_none()
    if not contact_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    # Can't add yourself
    if contact_data.contact_user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot add yourself as a contact"
        )

    # Check if contact already exists
    result = await db.execute(
        select(Contact).where(
            and_(
                Contact.user_id == current_user.id,
                Contact.contact_user_id == contact_data.contact_user_id
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Contact already exists"
        )

    # Create contact
    contact = Contact(
        user_id=current_user.id,
        contact_user_id=contact_data.contact_user_id
    )
    db.add(contact)
    await db.commit()
    await db.refresh(contact)

    return ContactResponse(
        id=contact.id,
        user=UserResponse.model_validate(contact_user),
        created_at=contact.created_at
    )


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_contact(
    contact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Remove a contact"""
    result = await db.execute(
        select(Contact).where(
            and_(
                Contact.id == contact_id,
                Contact.user_id == current_user.id
            )
        )
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Contact not found"
        )

    await db.delete(contact)
    await db.commit()
    return None


@router.get("/search", response_model=List[UserSearchResult])
async def search_users(
    q: str = Query(..., min_length=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Search for users by username or display name"""
    # Get list of existing contacts
    existing_contacts_result = await db.execute(
        select(Contact.contact_user_id).where(Contact.user_id == current_user.id)
    )
    existing_contact_ids = [row[0] for row in existing_contacts_result.all()]

    # Search users (exclude self and existing contacts)
    query = (
        select(User)
        .where(
            and_(
                User.id != current_user.id,
                User.id.notin_(existing_contact_ids) if existing_contact_ids else True,
                User.is_active == True,
                or_(
                    User.username.ilike(f"%{q}%"),
                    User.display_name.ilike(f"%{q}%")
                )
            )
        )
        .limit(20)
    )

    result = await db.execute(query)
    users = result.scalars().all()

    return [
        UserSearchResult(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            avatar_url=user.avatar_url
        )
        for user in users
    ]
