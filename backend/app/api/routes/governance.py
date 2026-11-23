"""Versioned API routes for Human Approval decisions and Organization Identity Verification."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.communication.governance_service import (
    decide_human_approval,
    list_human_approvals,
    review_organization_identity,
)
from app.communication.schemas import (
    HumanApprovalDecisionRequest,
    HumanApprovalRecord,
    OrgIdentityReviewRequest,
)
from app.core.auth import require_admin_auth
from app.schemas.domain import Organization

router = APIRouter(tags=["governance"])


@router.get(
    "/approvals",
    response_model=List[HumanApprovalRecord],
    summary="List human approval proposals (admin required)",
)
def get_approvals(
    request: Request,
    recall_id: Optional[str] = Query(default=None),
    pending_only: bool = Query(default=False),
    _admin: str = Depends(require_admin_auth),
) -> List[HumanApprovalRecord]:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return list_human_approvals(
        db_path, recall_id=recall_id, pending_only=pending_only
    )


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=HumanApprovalRecord,
    summary="Approve or reject a pending human approval proposal (server-authenticated admin required)",
)
def submit_approval_decision(
    approval_id: str,
    payload: HumanApprovalDecisionRequest,
    request: Request,
    admin_actor: str = Depends(require_admin_auth),
) -> HumanApprovalRecord:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return decide_human_approval(
        db_path, approval_id, payload, authenticated_actor=admin_actor
    )


@router.post(
    "/organizations/{org_id}/identity-review",
    response_model=Organization,
    summary="Review an organization's identity verification status (server-authenticated admin required)",
)
def review_org_identity_route(
    org_id: str,
    payload: OrgIdentityReviewRequest,
    request: Request,
    admin_actor: str = Depends(require_admin_auth),
) -> Organization:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return review_organization_identity(
        db_path, org_id, payload, authenticated_actor=admin_actor
    )
