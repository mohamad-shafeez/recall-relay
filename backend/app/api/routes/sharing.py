"""Versioned API routes for explicit cross-organization information-sharing grants."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request, status

from app.communication.schemas import SharingGrant, SharingGrantCreateRequest
from app.communication.sharing_service import (
    create_sharing_grant,
    list_sharing_grants,
    revoke_sharing_grant,
)
from app.core.auth import require_org_boundary
from app.schemas.domain import OrganizationBoundaryContext

router = APIRouter(prefix="/sharing", tags=["sharing"])


@router.post(
    "/grants",
    response_model=SharingGrant,
    status_code=status.HTTP_201_CREATED,
    summary="Authorize a bounded cross-organization information-sharing grant (owner org bearer required)",
)
def create_grant(
    payload: SharingGrantCreateRequest,
    request: Request,
    owner: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> SharingGrant:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return create_sharing_grant(db_path, owner, payload)


@router.get(
    "/grants",
    response_model=List[SharingGrant],
    summary="List sharing grants owned by or granted to the caller's organization",
)
def list_grants(
    request: Request,
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> List[SharingGrant]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return list_sharing_grants(db_path, caller)


@router.post(
    "/grants/{grant_id}/revoke",
    response_model=SharingGrant,
    summary="Revoke a sharing grant (owning organization only)",
)
def revoke_grant(
    grant_id: str,
    request: Request,
    owner: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> SharingGrant:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return revoke_sharing_grant(db_path, owner, grant_id)
