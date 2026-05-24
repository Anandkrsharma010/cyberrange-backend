"""Request/response models for Razorpay billing."""

from uuid import UUID

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    content_id: UUID = Field(..., description="Lab or quiz content_items.id to purchase")


class CreateWorkshopOrderRequest(BaseModel):
    """One Razorpay order for seat_cap × unit price; notes carry workshop_id for webhook fulfillment."""

    workshop_id: UUID = Field(..., description="Target workshops.id (cohort package)")


class CreateOrderResponse(BaseModel):
    razorpay_order_id: str
    amount_minor: int
    currency: str
    razorpay_key_id: str
    internal_payment_id: UUID


class EntitlementRow(BaseModel):
    content_id: UUID
    status: str
    valid_from: str | None = None
    valid_until: str | None = None


class VerifyCaptureRequest(BaseModel):
    """Used after checkout — server fetches payment from Razorpay and fulfills DB if captured."""

    razorpay_payment_id: str = Field(..., min_length=3)
    razorpay_order_id: str = Field(..., min_length=3)


class VerifyCaptureResponse(BaseModel):
    status: str  # fulfilled | already_fulfilled | not_captured
    message: str | None = None
