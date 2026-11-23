"""Versioned API routes for sponsor integration status, live health probes, and sanitized logs."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Request

from app.core.auth import require_admin_auth, require_admin_or_org_auth
from app.integrations.base import (
    IntegrationOverviewResponse,
    IntegrationUsageLog,
    SponsorStatusReport,
    list_integration_usage_logs,
)
from app.integrations.registry import IntegrationRegistry

router = APIRouter(prefix="/integrations", tags=["integrations"])


def _get_registry(request: Request) -> IntegrationRegistry:
    registry = getattr(request.app.state, "integration_registry", None)
    if registry is None:
        db_path: str | None = getattr(request.app.state, "database_path", None)
        registry = IntegrationRegistry(db_path=db_path)
        request.app.state.integration_registry = registry
    return registry


@router.get(
    "/status",
    response_model=IntegrationOverviewResponse,
    summary="Get honest connection & implementation status for all sponsor integrations",
)
def get_integrations_status(
    request: Request,
    _auth=Depends(require_admin_or_org_auth),
) -> IntegrationOverviewResponse:
    return _get_registry(request).overview()


@router.post(
    "/{sponsor_name}/check",
    response_model=SponsorStatusReport,
    summary="Run a real connectivity check for a configured sponsor integration (admin required)",
)
def check_sponsor_integration(
    sponsor_name: str,
    request: Request,
    admin_actor: str = Depends(require_admin_auth),
) -> SponsorStatusReport:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return _get_registry(request).check_sponsor(
        sponsor_name, db_path=db_path, actor_id=admin_actor
    )


@router.get(
    "/logs",
    response_model=List[IntegrationUsageLog],
    summary="List sanitized external sponsor integration usage logs (admin required)",
)
def get_integration_logs(
    request: Request,
    _admin: str = Depends(require_admin_auth),
) -> List[IntegrationUsageLog]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return list_integration_usage_logs(db_path)
