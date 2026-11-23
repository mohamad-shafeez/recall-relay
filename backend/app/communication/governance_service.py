"""Governance services: Human Approval workflow and Organization Identity Verification."""

from __future__ import annotations

import sqlite3
from typing import List

from fastapi import HTTPException, status

from app.communication.audit_trail import record_audit_event
from app.communication.schemas import (
    HumanApprovalDecisionRequest,
    HumanApprovalRecord,
    OrgIdentityReviewRequest,
)
from app.db.database import get_db_connection
from app.schemas.domain import (
    ApprovalDecision,
    IdentityVerificationStatus,
    Organization,
    utc_now_iso,
)
from app.services.organization_service import _row_to_org


def _row_to_approval(row: sqlite3.Row) -> HumanApprovalRecord:
    proposing = (
        row["proposing_agent_id"] if "proposing_agent_id" in row.keys() else None
    )
    return HumanApprovalRecord(
        id=row["id"],
        recall_id=row["recall_id"],
        action_type=row["action_type"],
        decision=ApprovalDecision(row["decision"]),
        proposing_agent_id=proposing,
        reviewer_name=row["reviewer_name"],
        rationale=row["rationale"],
        decided_at=row["decided_at"],
        created_at=row["created_at"],
    )


def list_human_approvals(
    db_path: str | None, *, recall_id: str | None = None, pending_only: bool = False
) -> List[HumanApprovalRecord]:
    clauses: List[str] = []
    params: List[str] = []
    if recall_id:
        clauses.append("recall_id = ?")
        params.append(recall_id)
    if pending_only:
        clauses.append("decision = 'PENDING'")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM human_approvals {where_sql} ORDER BY created_at DESC, id ASC",
            tuple(params),
        ).fetchall()
    return [_row_to_approval(row) for row in rows]


def decide_human_approval(
    db_path: str | None,
    approval_id: str,
    payload: HumanApprovalDecisionRequest,
    *,
    authenticated_actor: str = "admin",
) -> HumanApprovalRecord:
    """Record an authorized human administrator's decision bound to server-authenticated identity."""
    decided_at = utc_now_iso()
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM human_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Approval request '{approval_id}' was not found.",
            )
        if row["decision"] != "PENDING":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Approval request '{approval_id}' was already decided ({row['decision']}).",
            )
        proposing_agent_id = (
            row["proposing_agent_id"] if "proposing_agent_id" in row.keys() else None
        )
        if (
            proposing_agent_id
            and payload.reviewer_name.strip().lower() == proposing_agent_id.strip().lower()
        ) or payload.reviewer_name.strip().lower().startswith("agent_"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agents cannot approve their own proposals; an authorized human reviewer is required.",
            )

        bound_reviewer = f"{payload.reviewer_name.strip()} [server-auth:{authenticated_actor}]"
        preserved_rationale = (
            f"Proposal: {row['rationale'] or 'N/A'} | Decision ({payload.decision}): {payload.rationale}"
        )
        conn.execute(
            """
            UPDATE human_approvals
            SET decision = ?, reviewer_name = ?, rationale = ?, decided_at = ?
            WHERE id = ?
            """,
            (
                payload.decision,
                bound_reviewer,
                preserved_rationale,
                decided_at,
                approval_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM human_approvals WHERE id = ?", (approval_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="HUMAN_APPROVAL_DECIDED",
        actor_id=f"{authenticated_actor}:{payload.reviewer_name.strip()}",
        entity_type="human_approval",
        entity_id=approval_id,
        metadata={
            "decision": payload.decision,
            "authenticated_actor": authenticated_actor,
            "declared_reviewer": payload.reviewer_name.strip(),
            "action_type": updated["action_type"],
            "recall_id": updated["recall_id"],
        },
    )
    return _row_to_approval(updated)


def review_organization_identity(
    db_path: str | None,
    org_id: str,
    payload: OrgIdentityReviewRequest,
    *,
    authenticated_actor: str = "admin",
) -> Organization:
    """Transition an organization's identity verification status with server-authenticated admin oversight."""
    if (
        payload.target_status == IdentityVerificationStatus.VERIFIED
        and not payload.supporting_evidence_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Marking an organization VERIFIED requires an authoritative supporting_evidence_id.",
        )

    reviewed_at = utc_now_iso()
    with get_db_connection(db_path) as conn:
        org_row = conn.execute(
            "SELECT * FROM organizations WHERE id = ?", (org_id,)
        ).fetchone()
        if org_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organization '{org_id}' was not found.",
            )

        if payload.supporting_evidence_id:
            ev = conn.execute(
                "SELECT id FROM evidence_records WHERE id = ?",
                (payload.supporting_evidence_id,),
            ).fetchone()
            if ev is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Supporting evidence '{payload.supporting_evidence_id}' was not found.",
                )

        bound_reviewer = f"{payload.reviewer_name.strip()} [server-auth:{authenticated_actor}]"
        conn.execute(
            """
            UPDATE organizations
            SET identity_verification_status = ?,
                identity_verified_by = ?,
                identity_verified_at = ?,
                identity_verification_evidence_id = COALESCE(?, identity_verification_evidence_id)
            WHERE id = ?
            """,
            (
                payload.target_status.value,
                bound_reviewer,
                reviewed_at,
                payload.supporting_evidence_id,
                org_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM organizations WHERE id = ?", (org_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="ORG_IDENTITY_REVIEWED",
        actor_id=f"{authenticated_actor}:{payload.reviewer_name.strip()}",
        boundary_id=updated["data_boundary_id"],
        entity_type="organization",
        entity_id=org_id,
        metadata={
            "target_status": payload.target_status.value,
            "authenticated_actor": authenticated_actor,
            "supporting_evidence_id": payload.supporting_evidence_id,
        },
    )
    return _row_to_org(updated)
