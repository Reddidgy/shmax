from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from typing import Optional
from app.schemas.auth import UserResponse


class ContactAdd(BaseModel):
    contact_user_id: UUID


class ContactResponse(BaseModel):
    id: UUID
    user: UserResponse
    created_at: datetime

    class Config:
        from_attributes = True


class UserSearchResult(BaseModel):
    id: UUID
    username: str
    display_name: str
    avatar_url: Optional[str] = None

    class Config:
        from_attributes = True
