from pydantic import BaseModel, Field
from uuid import UUID
from datetime import datetime


class LabDeployRequest(BaseModel):
    content_id: UUID
    expires_at: datetime = Field(
        ...,
        description="When the lab should expire and be automatically destroyed. "
                    "Admin must set this explicitly since entitlement is not checked.",
    )


class AdminDeployForUserRequest(BaseModel):
    target_user_id: UUID
    content_id: UUID
    expires_at: datetime = Field(
        ...,
        description="When the lab should expire and be automatically destroyed.",
    )


class DeploymentMemberResponse(BaseModel):
    deployment_id: UUID
    user_id: UUID
    email: str
    added_by: UUID
    added_at: datetime