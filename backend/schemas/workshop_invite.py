from pydantic import BaseModel, EmailStr, Field


class WorkshopInviteCreateBody(BaseModel):
    email: EmailStr


class WorkshopInviteRedeemBody(BaseModel):
    token: str = Field(..., min_length=20, max_length=500)
