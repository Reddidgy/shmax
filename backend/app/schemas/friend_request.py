from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from uuid import UUID
from app.schemas.auth import UserResponse


class FriendRequestCreate(BaseModel):
    to_user_id: UUID


class FriendRequestResponse(BaseModel):
    id: UUID
    from_user_id: UUID
    to_user_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime
    from_user: Optional[UserResponse] = None
    to_user: Optional[UserResponse] = None

    class Config:
        from_attributes = True


class InviteLinkCreate(BaseModel):
    expires_in_days: Optional[int] = 7


class InviteLinkResponse(BaseModel):
    id: UUID
    user_id: UUID
    code: str
    is_active: bool
    created_at: datetime
    expires_at: datetime
    invite_url: Optional[str] = None

    class Config:
        from_attributes = True
