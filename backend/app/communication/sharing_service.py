"""Cross-organization information-sharing authorization controls (default DENY)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import List

from fastapi import HTTPException, status

from app.communication.audit_trail import record_audit_event
from app.communication.schemas import (
    SharingGrant,
    SharingGrantCreateRequest,
    SharingGrantStatus,
)
from app.db.database import get_db_connection
from app.schemas.domain import OrganizationBoundaryContext, new_id, utc_now_iso


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except ValueError:
        return False


def _row_to_grant(row: sqlite3.Row) -> SharingGrant:
    return SharingGrant(
        id=row["id"],
        recall_id=row["recall_id"],
        owner_org_id=row["owner_org_id"],
        owner_boundary_id=row["owner_boundary_id"],
        recipient_org_id=row["recipient_org_id"],
        recipient_boundary_id=row["recipient_boundary_id"],
        allowed_fields=json.loads(row["allowed_fields_json"]),
        allowed_evidence_ids=json.loads(row["allowed_evidence_ids_json"]),
        purpose=row["purpose"],
        authorized_by=row["authorized_by"],
        status=SharingGrantStatus(row["status"]),
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        created_at=row["created_at"],
    )


def create_sharing_grant(
    db_path: str | None,
    owner: OrganizationBoundaryContext,
    payload: SharingGrantCreateRequest,
) -> SharingGrant:
    """Create an explicit, bounded cross-organization sharing grant owned by the caller."""
    if payload.recipient_org_id == owner.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="recipient_org_id must be a different organization from the owner.",
        )

    grant_id = new_id("shr")
    created_at = utc_now_iso()

    with get_db_connection(db_path) as conn:
        recall = conn.execute(
            "SELECT id FROM recall_notices WHERE id = ?", (payload.recall_id,)
        ).fetchone()
        if recall is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recall '{payload.recall_id}' was not found.",
            )
        recipient = conn.execute(
            "SELECT id, data_boundary_id FROM organizations WHERE id = ?",
            (payload.recipient_org_id,),
        ).fetchone()
        if recipient is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipient organization '{payload.recipient_org_id}' was not found.",
            )

        conn.execute(
            """
            INSERT INTO information_sharing_grants (
                id, recall_id, owner_org_id, owner_boundary_id,
                recipient_org_id, recipient_boundary_id,
                allowed_fields_json, allowed_evidence_ids_json,
                purpose, authorized_by, status, expires_at, revoked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, NULL, ?)
            """,
            (
                grant_id,
                payload.recall_id,
                owner.organization_id,
                owner.data_boundary_id,
                recipient["id"],
                recipient["data_boundary_id"],
                json.dumps(sorted(set(payload.allowed_fields))),
                json.dumps(sorted(set(payload.allowed_evidence_ids))),
                payload.purpose,
                payload.authorized_by,
                payload.expires_at,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM information_sharing_grants WHERE id = ?", (grant_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="SHARING_GRANT_CREATED",
        actor_id=payload.authorized_by,
        boundary_id=owner.data_boundary_id,
        entity_type="information_sharing_grant",
        entity_id=grant_id,
        metadata={
            "recall_id": payload.recall_id,
            "recipient_org_id": payload.recipient_org_id,
            "allowed_fields": sorted(set(payload.allowed_fields)),
        },
    )
    return _row_to_grant(row)


def revoke_sharing_grant(
    db_path: str | None,
    owner: OrganizationBoundaryContext,
    grant_id: str,
) -> SharingGrant:
    """Revoke an existing sharing grant (only the owning organization may revoke)."""
    now = utc_now_iso()
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM information_sharing_grants WHERE id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Sharing grant '{grant_id}' was not found.",
            )
        if row["owner_boundary_id"] != owner.data_boundary_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the owning organization may revoke this sharing grant.",
            )
        conn.execute(
            "UPDATE information_sharing_grants SET status = 'REVOKED', revoked_at = ? WHERE id = ?",
            (now, grant_id),
        )
        updated = conn.execute(
            "SELECT * FROM information_sharing_grants WHERE id = ?", (grant_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="SHARING_GRANT_REVOKED",
        actor_id=owner.organization_id,
        boundary_id=owner.data_boundary_id,
        entity_type="information_sharing_grant",
        entity_id=grant_id,
        metadata={"status": "REVOKED"},
    )
    return _row_to_grant(updated)


def list_sharing_grants(
    db_path: str | None, caller: OrganizationBoundaryContext
) -> List[SharingGrant]:
    """List grants where the caller is either the owner or authorized recipient."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM information_sharing_grants
            WHERE owner_boundary_id = ? OR recipient_boundary_id = ?
            ORDER BY created_at DESC, id ASC
            """,
            (caller.data_boundary_id, caller.data_boundary_id),
        ).fetchall()
    return [_row_to_grant(row) for row in rows]


def check_sharing_authorization(
    db_path: str | None,
    *,
    recall_id: str,
    owner_boundary_id: str,
    recipient_boundary_id: str,
    requested_fields: List[str] | None = None,
    requested_evidence_id: str | None = None,
) -> tuple[bool, SharingGrant | None]:
    """Evaluate default-DENY cross-organization sharing authorization."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM information_sharing_grants
            WHERE recall_id = ?
              AND owner_boundary_id = ?
              AND recipient_boundary_id = ?
              AND status = 'ACTIVE'
            ORDER BY created_at DESC
            """,
            (recall_id, owner_boundary_id, recipient_boundary_id),
        ).fetchall()

    for row in rows:
        grant = _row_to_grant(row)
        if _is_expired(grant.expires_at):
            with get_db_connection(db_path) as conn:
                conn.execute(
                    "UPDATE information_sharing_grants SET status = 'EXPIRED' WHERE id = ?",
                    (grant.id,),
                )
            continue
        if requested_fields and not set(requested_fields).issubset(
            set(grant.allowed_fields)
        ):
            continue
        if requested_evidence_id and requested_evidence_id not in set(
            grant.allowed_evidence_ids
        ):
            continue
        return True, grant

    return False, None
