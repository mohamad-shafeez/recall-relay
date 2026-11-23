"""Recall notice and affected batch management service with public/private disclosure gating."""

from __future__ import annotations

import sqlite3
from typing import List

from fastapi import HTTPException, status

from app.communication.audit_trail import record_audit_event
from app.db.database import get_db_connection
from app.schemas.api_v1 import BatchCreateRequest, RecallCreateRequest
from app.schemas.domain import (
    AffectedBatch,
    RecallNotice,
    RecallSeverity,
    RecallStatus,
    new_id,
    utc_now_iso,
)


def _row_to_recall(row: sqlite3.Row) -> RecallNotice:
    return RecallNotice(
        id=row["id"],
        external_recall_number=row["external_recall_number"],
        title=row["title"],
        issuing_authority=row["issuing_authority"],
        product_description=row["product_description"],
        reason=row["reason"],
        severity=RecallSeverity(row["severity"]),
        status=RecallStatus(row["status"]),
        manufacturer_org_id=row["manufacturer_org_id"],
        evidence_id=row["evidence_id"],
        published_at=row["published_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_batch(row: sqlite3.Row) -> AffectedBatch:
    return AffectedBatch(
        id=row["id"],
        recall_id=row["recall_id"],
        batch_code=row["batch_code"],
        product_name=row["product_name"],
        expiration_or_best_by=row["expiration_or_best_by"],
        reported_quantity=row["reported_quantity"],
        quantity_unit=row["quantity_unit"],
        evidence_id=row["evidence_id"],
    )


def create_recall(db_path: str | None, payload: RecallCreateRequest) -> RecallNotice:
    """Create a recall notice anchored to existing traceable evidence (private until approved)."""
    recall_id = new_id("rec")
    now = utc_now_iso()

    with get_db_connection(db_path) as conn:
        ev = conn.execute(
            "SELECT id FROM evidence_records WHERE id = ?", (payload.evidence_id,)
        ).fetchone()
        if ev is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Referenced evidence_id '{payload.evidence_id}' does not exist.",
            )

        if payload.manufacturer_org_id:
            org = conn.execute(
                "SELECT id FROM organizations WHERE id = ?",
                (payload.manufacturer_org_id,),
            ).fetchone()
            if org is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Referenced manufacturer_org_id '{payload.manufacturer_org_id}' does not exist.",
                )

        try:
            conn.execute(
                """
                INSERT INTO recall_notices (
                    id, external_recall_number, title, issuing_authority,
                    product_description, reason, severity, status,
                    manufacturer_org_id, evidence_id, published_at,
                    created_at, updated_at, public_disclosure_approved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    recall_id,
                    payload.external_recall_number,
                    payload.title,
                    payload.issuing_authority,
                    payload.product_description,
                    payload.reason,
                    payload.severity.value,
                    payload.status.value,
                    payload.manufacturer_org_id,
                    payload.evidence_id,
                    payload.published_at,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Recall notice could not be created (duplicate or constraint error): {exc}",
            ) from exc

        row = conn.execute(
            "SELECT * FROM recall_notices WHERE id = ?", (recall_id,)
        ).fetchone()

    return _row_to_recall(row)


def set_public_disclosure(
    db_path: str | None, recall_id: str, *, approved: bool, actor_id: str
) -> RecallNotice:
    """Explicitly approve or revoke public disclosure for a recall notice."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM recall_notices WHERE id = ?", (recall_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recall notice '{recall_id}' was not found.",
            )
        conn.execute(
            "UPDATE recall_notices SET public_disclosure_approved = ?, updated_at = ? WHERE id = ?",
            (1 if approved else 0, utc_now_iso(), recall_id),
        )
        updated = conn.execute(
            "SELECT * FROM recall_notices WHERE id = ?", (recall_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="RECALL_PUBLIC_DISCLOSURE_UPDATED",
        actor_id=actor_id,
        entity_type="recall_notice",
        entity_id=recall_id,
        metadata={"public_disclosure_approved": approved},
    )
    return _row_to_recall(updated)


def list_recalls(
    db_path: str | None, *, public_only: bool = False
) -> List[RecallNotice]:
    """List recall notices; public callers only see recalls approved for public disclosure."""
    with get_db_connection(db_path) as conn:
        if public_only:
            rows = conn.execute(
                """
                SELECT * FROM recall_notices
                WHERE public_disclosure_approved = 1
                ORDER BY created_at DESC, id ASC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM recall_notices ORDER BY created_at DESC, id ASC"
            ).fetchall()
    return [_row_to_recall(row) for row in rows]


def get_recall_by_id(
    db_path: str | None, recall_id: str, *, public_only: bool = False
) -> RecallNotice:
    """Retrieve a single recall notice; enforce public disclosure gate for unauthenticated callers."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM recall_notices WHERE id = ?", (recall_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recall notice '{recall_id}' was not found.",
        )
    if public_only and not row["public_disclosure_approved"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Public recall notice '{recall_id}' was not found.",
        )
    return _row_to_recall(row)


def add_batch_to_recall(
    db_path: str | None, recall_id: str, payload: BatchCreateRequest
) -> AffectedBatch:
    """Associate an affected batch with an existing recall notice and evidence record."""
    batch_id = new_id("bat")

    with get_db_connection(db_path) as conn:
        recall = conn.execute(
            "SELECT id FROM recall_notices WHERE id = ?", (recall_id,)
        ).fetchone()
        if recall is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recall notice '{recall_id}' was not found.",
            )

        ev = conn.execute(
            "SELECT id FROM evidence_records WHERE id = ?", (payload.evidence_id,)
        ).fetchone()
        if ev is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Referenced evidence_id '{payload.evidence_id}' does not exist.",
            )

        try:
            conn.execute(
                """
                INSERT INTO affected_batches (
                    id, recall_id, batch_code, product_name,
                    expiration_or_best_by, reported_quantity, quantity_unit, evidence_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    recall_id,
                    payload.batch_code,
                    payload.product_name,
                    payload.expiration_or_best_by,
                    payload.reported_quantity,
                    payload.quantity_unit,
                    payload.evidence_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Batch '{payload.batch_code}' could not be added to recall '{recall_id}': {exc}",
            ) from exc

        row = conn.execute(
            "SELECT * FROM affected_batches WHERE id = ?", (batch_id,)
        ).fetchone()

    return _row_to_batch(row)


def list_batches_for_recall(
    db_path: str | None, recall_id: str, *, public_only: bool = False
) -> List[AffectedBatch]:
    """List all affected batches associated with a recall notice."""
    # Enforces existence and public_only gate
    get_recall_by_id(db_path, recall_id, public_only=public_only)
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM affected_batches WHERE recall_id = ? ORDER BY batch_code ASC",
            (recall_id,),
        ).fetchall()
    return [_row_to_batch(row) for row in rows]
