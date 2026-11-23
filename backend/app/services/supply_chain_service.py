"""Boundary-isolated shipment and inventory services.

Enforces server-side organization isolation: owner_boundary_id and sending/reporting
organization_id are derived exclusively from the authenticated OrganizationBoundaryContext.
"""

from __future__ import annotations

import sqlite3
from typing import List

from fastapi import HTTPException, status

from app.db.database import get_db_connection
from app.schemas.api_v1 import InventoryCreateRequest, ShipmentCreateRequest
from app.schemas.domain import (
    InventoryRecord,
    InventoryStatus,
    OrganizationBoundaryContext,
    ShipmentRecord,
    new_id,
)


def _row_to_shipment(row: sqlite3.Row) -> ShipmentRecord:
    return ShipmentRecord(
        id=row["id"],
        recall_id=row["recall_id"],
        batch_id=row["batch_id"],
        from_org_id=row["from_org_id"],
        to_org_id=row["to_org_id"],
        owner_boundary_id=row["owner_boundary_id"],
        quantity_shipped=row["quantity_shipped"],
        quantity_unit=row["quantity_unit"],
        shipped_at=row["shipped_at"],
        evidence_id=row["evidence_id"],
    )


def _row_to_inventory(row: sqlite3.Row) -> InventoryRecord:
    return InventoryRecord(
        id=row["id"],
        recall_id=row["recall_id"],
        batch_id=row["batch_id"],
        organization_id=row["organization_id"],
        owner_boundary_id=row["owner_boundary_id"],
        location_reference=row["location_reference"],
        quantity_on_hand=row["quantity_on_hand"],
        quantity_quarantined=row["quantity_quarantined"],
        quantity_unit=row["quantity_unit"],
        inventory_status=InventoryStatus(row["inventory_status"]),
        evidence_id=row["evidence_id"],
        reported_at=row["reported_at"],
    )


def _validate_recall_batch_and_evidence(
    conn: sqlite3.Connection, recall_id: str, batch_id: str, evidence_id: str
) -> None:
    batch = conn.execute(
        "SELECT id, recall_id FROM affected_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Affected batch '{batch_id}' was not found.",
        )
    if batch["recall_id"] != recall_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch '{batch_id}' does not belong to recall '{recall_id}'.",
        )
    ev = conn.execute(
        "SELECT id FROM evidence_records WHERE id = ?", (evidence_id,)
    ).fetchone()
    if ev is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Referenced evidence_id '{evidence_id}' does not exist.",
        )


def register_shipment(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    payload: ShipmentCreateRequest,
) -> ShipmentRecord:
    """Register a shipment strictly inside the authenticated organization's boundary."""
    if payload.to_org_id == caller.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="to_org_id must be a different organization from the sending organization.",
        )

    shipment_id = new_id("shp")
    with get_db_connection(db_path) as conn:
        _validate_recall_batch_and_evidence(
            conn, payload.recall_id, payload.batch_id, payload.evidence_id
        )
        to_org = conn.execute(
            "SELECT id FROM organizations WHERE id = ?", (payload.to_org_id,)
        ).fetchone()
        if to_org is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Receiving organization '{payload.to_org_id}' was not found.",
            )

        conn.execute(
            """
            INSERT INTO shipment_records (
                id, recall_id, batch_id, from_org_id, to_org_id,
                owner_boundary_id, quantity_shipped, quantity_unit,
                shipped_at, evidence_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                shipment_id,
                payload.recall_id,
                payload.batch_id,
                caller.organization_id,
                payload.to_org_id,
                caller.data_boundary_id,
                payload.quantity_shipped,
                payload.quantity_unit,
                payload.shipped_at,
                payload.evidence_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM shipment_records WHERE id = ?", (shipment_id,)
        ).fetchone()

    return _row_to_shipment(row)


def list_shipments_for_boundary(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    recall_id: str | None = None,
) -> List[ShipmentRecord]:
    """List only shipment records owned by the caller's server-resolved boundary."""
    with get_db_connection(db_path) as conn:
        if recall_id:
            rows = conn.execute(
                """
                SELECT * FROM shipment_records
                WHERE owner_boundary_id = ? AND recall_id = ?
                ORDER BY id ASC
                """,
                (caller.data_boundary_id, recall_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM shipment_records
                WHERE owner_boundary_id = ?
                ORDER BY id ASC
                """,
                (caller.data_boundary_id,),
            ).fetchall()
    return [_row_to_shipment(row) for row in rows]


def get_shipment_for_boundary(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    shipment_id: str,
) -> ShipmentRecord:
    """Return a shipment only if owned by the caller's boundary; fail closed with 403 otherwise."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM shipment_records WHERE id = ?", (shipment_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Shipment record '{shipment_id}' was not found.",
        )
    if row["owner_boundary_id"] != caller.data_boundary_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: shipment record belongs to another organization's data boundary.",
        )
    return _row_to_shipment(row)


def register_inventory(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    payload: InventoryCreateRequest,
) -> InventoryRecord:
    """Register an inventory record strictly inside the authenticated organization's boundary."""
    inventory_id = new_id("inv")
    with get_db_connection(db_path) as conn:
        _validate_recall_batch_and_evidence(
            conn, payload.recall_id, payload.batch_id, payload.evidence_id
        )
        conn.execute(
            """
            INSERT INTO inventory_records (
                id, recall_id, batch_id, organization_id, owner_boundary_id,
                location_reference, quantity_on_hand, quantity_quarantined,
                quantity_unit, inventory_status, evidence_id, reported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inventory_id,
                payload.recall_id,
                payload.batch_id,
                caller.organization_id,
                caller.data_boundary_id,
                payload.location_reference,
                payload.quantity_on_hand,
                payload.quantity_quarantined,
                payload.quantity_unit,
                payload.inventory_status.value,
                payload.evidence_id,
                payload.reported_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM inventory_records WHERE id = ?", (inventory_id,)
        ).fetchone()

    return _row_to_inventory(row)


def list_inventory_for_boundary(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    recall_id: str | None = None,
) -> List[InventoryRecord]:
    """List only inventory records owned by the caller's server-resolved boundary."""
    with get_db_connection(db_path) as conn:
        if recall_id:
            rows = conn.execute(
                """
                SELECT * FROM inventory_records
                WHERE owner_boundary_id = ? AND recall_id = ?
                ORDER BY id ASC
                """,
                (caller.data_boundary_id, recall_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM inventory_records
                WHERE owner_boundary_id = ?
                ORDER BY id ASC
                """,
                (caller.data_boundary_id,),
            ).fetchall()
    return [_row_to_inventory(row) for row in rows]


def get_inventory_for_boundary(
    db_path: str | None,
    caller: OrganizationBoundaryContext,
    inventory_id: str,
) -> InventoryRecord:
    """Return an inventory record only if owned by the caller's boundary; fail closed with 403 otherwise."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM inventory_records WHERE id = ?", (inventory_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Inventory record '{inventory_id}' was not found.",
        )
    if row["owner_boundary_id"] != caller.data_boundary_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: inventory record belongs to another organization's data boundary.",
        )
    return _row_to_inventory(row)
