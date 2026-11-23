"""Versioned API routes for organization registration and retrieval (auth-protected)."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request, status

from app.core.auth import require_admin_auth, require_admin_or_org_auth
from app.schemas.api_v1 import (
    OrganizationRegisterRequest,
    OrganizationWithCredentialResponse,
)
from app.schemas.domain import Organization
from app.services import organization_service

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.post(
    "",
    response_model=OrganizationWithCredentialResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register an organization as UNVERIFIED (admin credential required)",
)
def create_organization(
    payload: OrganizationRegisterRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> OrganizationWithCredentialResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return organization_service.register_organization(db_path, payload)


@router.get(
    "",
    response_model=List[Organization],
    summary="List registered organizations (admin or org credential required)",
)
def list_organizations(
    request: Request,
    _auth=Depends(require_admin_or_org_auth),
) -> List[Organization]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return organization_service.list_organizations(db_path)


@router.get(
    "/{org_id}",
    response_model=Organization,
    summary="Get organization metadata by ID (admin or org credential required)",
)
def get_organization(
    org_id: str,
    request: Request,
    _auth=Depends(require_admin_or_org_auth),
) -> Organization:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return organization_service.get_organization_by_id(db_path, org_id)
