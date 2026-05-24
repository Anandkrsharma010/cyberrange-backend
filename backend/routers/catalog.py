"""Read-only lab catalog (public GET).

Source of truth is `content_items` (filtered to type='lab', is_active=true,
visibility='public') joined with the `product_prices` row that may exist for
that content. A lab is considered purchasable iff it has an active
product_prices row.

Labs with visibility unlisted or private are omitted from this list; access
is handled elsewhere (direct links, entitlements, future grants).
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.pg import get_pg
from backend.schemas.catalog import CatalogLab, CatalogPrice, PublicContentPage

log = logging.getLogger("catalog")

router = APIRouter(prefix="/catalog", tags=["Catalog"])


def _feature_chips_from_metadata(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return []


@router.get("/labs", response_model=list[CatalogLab])
async def list_catalog_labs(
    pg: AsyncSession = Depends(get_pg),
):
    """
    List every active lab with its active price (if any).

    Public read — no JWT required so the marketing / Labs page can load for
    guests. Purchase and deploy still require authentication elsewhere.

    A row without an active product_prices entry is still returned with
    is_purchasable=false so the UI can render it as 'Coming soon' without
    inventing catalog data client-side.
    """
    result = await pg.execute(
        text("""
            SELECT
                ci.id,
                ci.title,
                ci.description,
                ci.difficulty,
                ci.duration_minutes,
                ci.metadata->>'slug'      AS slug,
                ci.metadata->>'lab_type'  AS lab_type,
                ci.metadata->'feature_chips' AS feature_chips,
                pp.amount_minor,
                pp.currency,
                (pp.is_active IS TRUE)    AS is_purchasable
            FROM content_items ci
            LEFT JOIN product_prices pp
                ON pp.content_id = ci.id AND pp.is_active = true
            WHERE ci.type = 'lab' AND ci.is_active = true
              AND ci.visibility = 'public'
            ORDER BY ci.created_at DESC
        """)
    )

    out: list[CatalogLab] = []
    for row in result.fetchall():
        price = None
        if row.amount_minor is not None and row.currency is not None:
            price = CatalogPrice(
                amount_minor=int(row.amount_minor),
                currency=row.currency,
            )
        out.append(
            CatalogLab(
                id=row.id,
                slug=row.slug,
                title=row.title,
                description=row.description,
                difficulty=row.difficulty,
                duration_minutes=row.duration_minutes,
                lab_type=row.lab_type,
                feature_chips=_feature_chips_from_metadata(row.feature_chips),
                is_purchasable=bool(row.is_purchasable),
                price=price,
            )
        )
    return out


@router.get("/pages/{slug}", response_model=PublicContentPage)
async def get_public_content_page(
    slug: str,
    pg: AsyncSession = Depends(get_pg),
):
    page_result = await pg.execute(
        text(
            """
            SELECT id, slug, title, description, seo_title, seo_description
            FROM website_pages
            WHERE slug = :slug
              AND status = 'published'
              AND archived_at IS NULL
            LIMIT 1
            """
        ),
        {"slug": slug.strip().lower()},
    )
    page = page_result.fetchone()
    if not page:
        raise HTTPException(status_code=404, detail="Published page not found")

    sections_result = await pg.execute(
        text(
            """
            SELECT section_key, section_type, position, payload
            FROM website_page_sections
            WHERE page_id = :page_id
              AND is_visible = true
            ORDER BY position ASC, created_at ASC
            """
        ),
        {"page_id": page.id},
    )
    sections = sections_result.fetchall()

    return PublicContentPage(
        slug=page.slug,
        title=page.title,
        description=page.description,
        seo_title=page.seo_title,
        seo_description=page.seo_description,
        sections=[
            {
                "section_key": s.section_key,
                "section_type": s.section_type,
                "position": s.position,
                "payload": s.payload or {},
            }
            for s in sections
        ],
    )
