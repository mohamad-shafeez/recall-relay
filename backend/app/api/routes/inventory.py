"""Versioned API routes for boundary-isolated inventory records."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.auth import require_org_boundary
from app.schemas.api_v1 import InventoryCreateRequest
from app.schemas.domain import InventoryRecord, OrganizationBoundaryContext
from app.services import supply_chain_service

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.post(
    "",
    response_model=InventoryRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Register an inventory record inside the authenticated organization's boundary",
)
def create_inventory(
    payload: InventoryCreateRequest,
    request: Request,
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> InventoryRecord:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.register_inventory(db_path, caller, payload)


@router.get(
    "",
    response_model=List[InventoryRecord],
    summary="List inventory records owned by the authenticated organization's boundary",
)
def list_inventory(
    request: Request,
    recall_id: Optional[str] = Query(default=None),
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> List[InventoryRecord]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.list_inventory_for_boundary(
        db_path, caller, recall_id=recall_id
    )


@router.get(
    "/{inventory_id}",
    response_model=InventoryRecord,
    summary="Get an inventory record owned by the authenticated organization's boundary",
)
def get_inventory(
    inventory_id: str,
    request: Request,
    caller: OrganizationBoundaryContext = Depends(require_org_boundary),
) -> InventoryRecord:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return supply_chain_service.get_inventory_for_boundary(db_path, caller, inventory_id)
