"""Public (unauthenticated) invite preview — no secrets beyond masked email + title."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.pg import get_pg
from backend.services.workshop_invites import preview_invite

log = logging.getLogger("public_invites")

router = APIRouter(prefix="/public/workshop-invites", tags=["Public"])


@router.get("/preview")
async def preview_workshop_invite(
    token: str = Query(..., min_length=20, max_length=500),
    pg: AsyncSession = Depends(get_pg),
):
    return await preview_invite(pg, raw_token=token)
