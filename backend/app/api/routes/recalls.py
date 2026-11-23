"""Versioned API routes for recall notices, affected batches, public disclosure, traceability, and openFDA import."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, status

from app.core.auth import (
    optional_org_boundary,
    require_admin_auth,
)
from app.schemas.api_v1 import (
    BatchCreateRequest,
    RecallCreateRequest,
    TraceabilityResponse,
)
from app.schemas.domain import (
    AffectedBatch,
    OrganizationBoundaryContext,
    RecallNotice,
)
from app.services import fda_page_importer, openfda_importer, recall_service, traceability_service
from app.services.fda_page_importer import FdaPageImportRequest, FdaPageImportResponse
from app.services.openfda_importer import (
    OpenFdaIngestRequest,
    OpenFdaIngestResponse,
    OpenFdaSearchResponse,
)

router = APIRouter(prefix="/recalls", tags=["recalls"])


def _is_authenticated_caller(
    request: Request,
    x_admin_key: Optional[str],
    caller: OrganizationBoundaryContext | None,
) -> bool:
    if caller is not None:
        return True
    if x_admin_key:
        require_admin_auth(request, x_admin_key=x_admin_key)
        return True
    if request.cookies.get("rr_session"):
        require_admin_auth(request, x_admin_key=None)
        return True
    return False


@router.get(
    "/import/openfda/search",
    response_model=OpenFdaSearchResponse,
    summary="Search official openFDA Food Enforcement public recalls (admin required)",
)
def search_openfda_recalls(
    status_filter: Optional[str] = Query(default="Ongoing"),
    classification: Optional[str] = Query(default=None),
    query: Optional[str] = Query(default=None),
    limit: int = Query(default=10, ge=1, le=25),
    _actor: str = Depends(require_admin_auth),
) -> OpenFdaSearchResponse:
    return openfda_importer.search_openfda_food_recalls(
        status_filter=status_filter,
        classification=classification,
        query=query,
        limit=limit,
    )


@router.post(
    "/import/openfda/ingest",
    response_model=OpenFdaIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a selected official openFDA recall record into RecallRelay as SOURCE_MATERIAL (admin required)",
)
def ingest_openfda_recall(
    payload: OpenFdaIngestRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> OpenFdaIngestResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    registry = getattr(request.app.state, "integration_registry", None)
    lyzr_adapter = registry.get_adapter("lyzr") if registry is not None else None
    return openfda_importer.ingest_openfda_recall(
        db_path,
        payload,
        lyzr_adapter=lyzr_adapter,
    )




@router.post(
    "/import/fda-page",
    response_model=FdaPageImportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import an official fda.gov recall webpage as SOURCE_MATERIAL (admin required)",
)
def import_fda_page_route(
    payload: FdaPageImportRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> FdaPageImportResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    registry = getattr(request.app.state, "integration_registry", None)
    lyzr_adapter = registry.get_adapter("lyzr") if registry is not None else None
    return fda_page_importer.import_fda_webpage(
        db_path,
        payload,
        lyzr_adapter=lyzr_adapter,
    )


@router.post(
    "",
    response_model=RecallNotice,
    status_code=status.HTTP_201_CREATED,
    summary="Create a recall notice anchored to traceable source evidence (admin required)",
)
def create_recall(
    payload: RecallCreateRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> RecallNotice:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return recall_service.create_recall(db_path, payload)


@router.post(
    "/{recall_id}/public-disclosure",
    response_model=RecallNotice,
    summary="Approve or revoke public disclosure of a recall notice (admin required)",
)
def update_public_disclosure(
    recall_id: str,
    request: Request,
    approved: bool = Query(default=True),
    _actor: str = Depends(require_admin_auth),
) -> RecallNotice:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return recall_service.set_public_disclosure(
        db_path, recall_id, approved=approved, actor_id="admin"
    )


@router.get(
    "",
    response_model=List[RecallNotice],
    summary="List recall notices (unauthenticated callers see only publicly approved recalls)",
)
def list_recalls(
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    caller: OrganizationBoundaryContext | None = Depends(optional_org_boundary),
) -> List[RecallNotice]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    is_authed = _is_authenticated_caller(request, x_admin_key, caller)
    return recall_service.list_recalls(db_path, public_only=not is_authed)


@router.get(
    "/{recall_id}",
    response_model=RecallNotice,
    summary="Get a recall notice by ID (public disclosure enforced for unauthenticated callers)",
)
def get_recall(
    recall_id: str,
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    caller: OrganizationBoundaryContext | None = Depends(optional_org_boundary),
) -> RecallNotice:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    is_authed = _is_authenticated_caller(request, x_admin_key, caller)
    return recall_service.get_recall_by_id(
        db_path, recall_id, public_only=not is_authed
    )


@router.post(
    "/{recall_id}/batches",
    response_model=AffectedBatch,
    status_code=status.HTTP_201_CREATED,
    summary="Associate an affected batch with a recall notice (admin required)",
)
def create_batch(
    recall_id: str,
    payload: BatchCreateRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> AffectedBatch:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return recall_service.add_batch_to_recall(db_path, recall_id, payload)


@router.get(
    "/{recall_id}/batches",
    response_model=List[AffectedBatch],
    summary="List affected batches for a recall notice (public disclosure enforced)",
)
def list_batches(
    recall_id: str,
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    caller: OrganizationBoundaryContext | None = Depends(optional_org_boundary),
) -> List[AffectedBatch]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    is_authed = _is_authenticated_caller(request, x_admin_key, caller)
    return recall_service.list_batches_for_recall(
        db_path, recall_id, public_only=not is_authed
    )


@router.get(
    "/{recall_id}/traceability",
    response_model=TraceabilityResponse,
    summary="Get boundary-respecting traceability (public disclosure enforced for unauthenticated callers)",
)
def get_traceability(
    recall_id: str,
    request: Request,
    x_admin_key: Optional[str] = Header(default=None, alias="X-Admin-Key"),
    caller: OrganizationBoundaryContext | None = Depends(optional_org_boundary),
) -> TraceabilityResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    is_authed = _is_authenticated_caller(request, x_admin_key, caller)
    if not is_authed:
        # Enforce public disclosure gate before building traceability
        recall_service.get_recall_by_id(db_path, recall_id, public_only=True)
    return traceability_service.build_recall_traceability(db_path, recall_id, caller)
