"""Response models for the read-only lab catalog.

The catalog is sourced from content_items + product_prices with no
additional tables or duplicated data. Fields map 1:1 onto existing
DB columns (plus two optional metadata keys: 'slug', 'lab_type').
"""

from uuid import UUID

from pydantic import BaseModel


class CatalogPrice(BaseModel):
    amount_minor: int
    currency: str


class CatalogLab(BaseModel):
    id: UUID
    slug: str | None = None
    title: str
    description: str | None = None
    difficulty: str | None = None
    duration_minutes: int | None = None
    lab_type: str | None = None
    feature_chips: list[str] = []
    is_purchasable: bool
    price: CatalogPrice | None = None


class PublicPageSection(BaseModel):
    section_key: str
    section_type: str
    position: int
    payload: dict


class PublicContentPage(BaseModel):
    slug: str
    title: str
    description: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    sections: list[PublicPageSection]
