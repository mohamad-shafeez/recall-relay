"""Versioned API routes for asynchronous one-click coordinated recall response orchestration."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, status

from app.core.auth import require_admin_auth
from app.services import orchestration_service
from app.services.orchestration_service import (
    OrchestrationRunRequest,
    OrchestrationRunResponse,
)

router = APIRouter(prefix="/orchestrations", tags=["orchestrations"])


@router.post(
    "/run",
    response_model=OrchestrationRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start asynchronous coordinated response workflow for an existing recall (admin required, HTTP 202)",
)
def run_orchestration(
    payload: OrchestrationRunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    actor: str = Depends(require_admin_auth),
) -> OrchestrationRunResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    registry = getattr(request.app.state, "integration_registry", None)
    run, should_execute = orchestration_service.start_coordinated_response(
        db_path,
        payload,
        registry=registry,
        actor_id=actor or "admin",
        requester_scope=actor or "admin",
    )
    if should_execute:
        background_tasks.add_task(
            orchestration_service.execute_orchestration_in_background,
            db_path,
            run.id,
            registry=registry,
            actor_id=actor or "admin",
        )
    return run


@router.get(
    "",
    response_model=List[OrchestrationRunResponse],
    summary="List persisted coordinated response orchestration runs (admin required)",
)
def list_orchestrations(
    request: Request,
    recall_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    _actor: str = Depends(require_admin_auth),
) -> List[OrchestrationRunResponse]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return orchestration_service.list_orchestrations(
        db_path, recall_id=recall_id, limit=limit
    )


@router.get(
    "/{orchestration_id}",
    response_model=OrchestrationRunResponse,
    summary="Poll persisted status and step timeline for a coordinated response run (admin required)",
)
def get_orchestration(
    orchestration_id: str,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> OrchestrationRunResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return orchestration_service.get_orchestration(db_path, orchestration_id)
