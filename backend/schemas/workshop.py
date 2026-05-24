"""Pydantic models for sys_admin workshop (cohort) APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


WorkshopMode = Literal["sponsored", "open_organizer"]
WorkshopPaymentStatus = Literal["pending", "paid", "waived", "refunded"]
WorkshopStatus = Literal["draft", "active", "archived"]
WorkshopAccessPolicy = Literal["requires_payment", "demo"]


class WorkshopAdminRow(BaseModel):
    user_id: UUID
    email: str
    name: Optional[str] = None
    is_lead: bool


class WorkshopOut(BaseModel):
    id: UUID
    internal_code: Optional[str] = None
    title: str
    description: Optional[str] = None
    content_id: UUID
    content_title: Optional[str] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    mode: WorkshopMode
    seat_cap: int
    used_seats: int = Field(
        description="Active cohort seats: COUNT(entitlements) WHERE workshop_id=this AND status=active (DB trigger).",
    )
    payment_status: WorkshopPaymentStatus
    payment_id: Optional[UUID] = None
    payer_ref: Optional[str] = None
    access_policy: WorkshopAccessPolicy = "requires_payment"
    status: WorkshopStatus
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime


class WorkshopDetailOut(WorkshopOut):
    admins: list[WorkshopAdminRow] = Field(default_factory=list)


class WorkshopCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=8000)
    content_id: UUID
    internal_code: Optional[str] = Field(None, max_length=120)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    mode: WorkshopMode = "sponsored"
    seat_cap: int = Field(..., ge=0)
    payment_status: WorkshopPaymentStatus = "pending"
    payment_id: Optional[UUID] = None
    payer_ref: Optional[str] = Field(None, max_length=500)
    access_policy: WorkshopAccessPolicy = "requires_payment"
    status: WorkshopStatus = "draft"


class WorkshopPatchRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=8000)
    internal_code: Optional[str] = Field(None, max_length=120)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    mode: Optional[WorkshopMode] = None
    seat_cap: Optional[int] = Field(None, ge=0)
    payment_status: Optional[WorkshopPaymentStatus] = None
    payment_id: Optional[UUID] = None
    payer_ref: Optional[str] = Field(None, max_length=500)
    access_policy: Optional[WorkshopAccessPolicy] = None
    status: Optional[WorkshopStatus] = None


class WorkshopAssignAdminRequest(BaseModel):
    is_lead: bool = False


class WorkshopGrantSeatRequest(BaseModel):
    """Grant one learner seat on this cohort (workshop-scoped entitlement)."""

    user_id: UUID = Field(..., description="Learner platform user id")
