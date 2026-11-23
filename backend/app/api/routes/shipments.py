"""Versioned API routes for boundary-isolated shipment records."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.auth import require_org_boundary
from app.schemas.api_v1 import ShipmentCreateRequest
from app.schemas.domain import OrganizationBoundaryContext, ShipmentRecord
from app.services import supply_chain_service

router = APIRouter(prefix="/shipments", tags=["shipments"])


@router.post(
    "",
    response_model=ShipmentRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Register a shipment record inside the authenticated organization's boundary",
)
def create_shipment(
    payload: ShipmentCreateRequest,
    request: Request,
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> ShipmentRecord:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.register_shipment(db_path, caller, payload)


@router.get(
    "",
    response_model=List[ShipmentRecord],
    summary="List shipment records owned by the authenticated organization's boundary",
)
def list_shipments(
    request: Request,
    recall_id: Optional[str] = Query(default=None),
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> List[ShipmentRecord]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.list_shipments_for_boundary(
        db_path, caller, recall_id=recall_id
    )


@router.get(
    "/{shipment_id}",
    response_model=ShipmentRecord,
    summary="Get a shipment record owned by the authenticated organization's boundary",
)
def get_shipment(
    shipment_id: str,
    request: Request,
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> ShipmentRecord:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.get_shipment_for_boundary(db_path, caller, shipment_id)
