# backend/schemas/admin.py
import json
from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator
from backend.config import ALL_ROLES, GUARDRAIL_DEFAULT_MAX_CONCURRENT, GUARDRAIL_DEFAULT_MAX_DURATION_HOURS

LabVisibility = Literal["public", "unlisted", "private"]


class CourseCreateRequest(BaseModel):
    title:            str            = Field(..., min_length=3, max_length=200)
    description:      Optional[str]  = None
    difficulty:       Optional[str]  = None
    duration_minutes: Optional[int]  = None
    lab_type:         str            = Field(
        ...,
        description="Must match a Terraform lab directory — e.g. 'windows' or 'lab-2'",
    )
    slug:             Optional[str]  = Field(
        default=None,
        description="Optional URL slug for /labs and checkout links",
    )
    feature_chips:    list[str]      = Field(
        default_factory=list,
        description="Short labels shown on the public labs catalog (e.g. VM names)",
    )
    visibility:       LabVisibility  = Field(
        default="public",
        description="public = listed in GET /catalog/labs; unlisted/private = hidden from public catalog",
    )

    @property
    def metadata_json(self) -> str:
        """Serialize catalog metadata into the metadata JSONB column."""
        meta: dict = {"lab_type": self.lab_type}
        if self.slug and self.slug.strip():
            meta["slug"] = self.slug.strip().lower()
        chips = [c.strip() for c in (self.feature_chips or []) if c and c.strip()]
        if chips:
            meta["feature_chips"] = chips
        return json.dumps(meta)


class CourseVisibilityPatchRequest(BaseModel):
    visibility: LabVisibility


class CourseContentPatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=3, max_length=200)
    description: Optional[str] = None
    difficulty: Optional[str] = None
    duration_minutes: Optional[int] = Field(default=None, ge=0)
    lab_type: Optional[str] = None
    slug: Optional[str] = None
    feature_chips: Optional[list[str]] = None


class CoursePriceUpsertRequest(BaseModel):
    amount_minor: int = Field(..., ge=1, le=10_000_000_00)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    is_active: bool = True

    @field_validator("currency")
    @classmethod
    def currency_uppercase(cls, v: str) -> str:
        return v.upper()


class GuardrailSetRequest(BaseModel):
    max_concurrent_deployments: int = Field(
        default=GUARDRAIL_DEFAULT_MAX_CONCURRENT,
        ge=1,
        le=50,
    )
    max_duration_hours: int = Field(
        default=GUARDRAIL_DEFAULT_MAX_DURATION_HOURS,
        ge=1,
        le=72,
    )


class RoleSetRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in ALL_ROLES:
            raise ValueError(
                f"Invalid role '{v}'. Must be one of: {', '.join(sorted(ALL_ROLES))}"
            )
        return v


PageStatus = Literal["draft", "published", "archived"]
SectionType = Literal["hero", "rich_text", "cta", "links", "faq", "media", "custom"]
ResourceType = Literal["text", "link", "pdf", "file", "manual"]


class WebsitePageCreateRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=120)
    title: str = Field(..., min_length=2, max_length=200)
    description: Optional[str] = None
    seo_title: Optional[str] = Field(default=None, max_length=200)
    seo_description: Optional[str] = Field(default=None, max_length=500)


class WebsitePagePatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=200)
    description: Optional[str] = None
    seo_title: Optional[str] = Field(default=None, max_length=200)
    seo_description: Optional[str] = Field(default=None, max_length=500)


class WebsitePageStatusPatchRequest(BaseModel):
    status: PageStatus


class WebsitePageSectionCreateRequest(BaseModel):
    section_key: str = Field(..., min_length=1, max_length=120)
    section_type: SectionType
    position: int = Field(default=0, ge=0, le=10000)
    is_visible: bool = True
    payload: dict = Field(default_factory=dict)

    @property
    def payload_json(self) -> str:
        return json.dumps(self.payload)


class WebsitePageSectionPatchRequest(BaseModel):
    section_key: Optional[str] = Field(default=None, min_length=1, max_length=120)
    section_type: Optional[SectionType] = None
    position: Optional[int] = Field(default=None, ge=0, le=10000)
    is_visible: Optional[bool] = None
    payload: Optional[dict] = None

    @property
    def payload_json(self) -> Optional[str]:
        if self.payload is None:
            return None
        return json.dumps(self.payload)


class CourseResourceCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    resource_type: ResourceType
    url: Optional[str] = Field(default=None, max_length=2000)
    file_key: Optional[str] = Field(default=None, max_length=500)
    mime_type: Optional[str] = Field(default=None, max_length=120)
    position: int = Field(default=0, ge=0, le=10000)
    is_visible: bool = True
    metadata: dict = Field(default_factory=dict)

    @property
    def metadata_json(self) -> str:
        return json.dumps(self.metadata)


class CourseResourcePatchRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    resource_type: Optional[ResourceType] = None
    url: Optional[str] = Field(default=None, max_length=2000)
    file_key: Optional[str] = Field(default=None, max_length=500)
    mime_type: Optional[str] = Field(default=None, max_length=120)
    position: Optional[int] = Field(default=None, ge=0, le=10000)
    is_visible: Optional[bool] = None
    metadata: Optional[dict] = None

    @property
    def metadata_json(self) -> Optional[str]:
        if self.metadata is None:
            return None
        return json.dumps(self.metadata)


class OpsFeedWorkflowPatchRequest(BaseModel):
    """Phase 3 — optional assignee + escalation on a feed row (sys_admin)."""

    assigned_to_user_id: Optional[str] = None
    escalation: Optional[Literal["none", "watch", "urgent"]] = None