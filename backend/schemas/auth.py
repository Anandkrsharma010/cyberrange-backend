"""
backend/schemas/auth.py (updated)

Changes vs previous:
- CurrentUser.role validator now checks against ALL_ROLES constant.
- Role strings 'student' and 'admin' are no longer valid.
"""

from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from uuid import UUID
from typing import Literal, Optional
from pydantic import EmailStr

from backend.config import ALL_ROLES


class SSOCallbackRequest(BaseModel):
    provider: str = Field(..., min_length=2, max_length=50)
    id_token: str = Field(..., min_length=10)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class CurrentUser(BaseModel):
    id: UUID
    email: EmailStr
    role: str
    is_active: bool
    created_at: Optional[datetime] = None

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in ALL_ROLES:
            raise ValueError(
                f"Invalid role '{v}'. Must be one of: {', '.join(sorted(ALL_ROLES))}"
            )
        return v