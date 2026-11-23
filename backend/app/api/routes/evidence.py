"""Versioned API routes for evidence registration and retrieval (boundary-enforced)."""

from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.auth import require_admin_auth, require_admin_or_org_auth
from app.db.database import get_db_connection
from app.schemas.api_v1 import EvidenceRegisterRequest, EvidenceResponse
from app.schemas.domain import EvidenceSourceType
from app.services import evidence_service

router = APIRouter(prefix="/evidence", tags=["evidence"])

_PUBLIC_EVIDENCE_TYPES = {
    EvidenceSourceType.OFFICIAL_PUBLIC_NOTICE,
    EvidenceSourceType.PUBLIC_DATASET,
}


def _org_can_view_evidence(
    db_path: str | None, caller_boundary_id: str, ev: EvidenceResponse
) -> bool:
    if ev.source_type in _PUBLIC_EVIDENCE_TYPES:
        return True
    # Private org uploads require an active sharing grant covering this evidence_id
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT allowed_evidence_ids_json FROM information_sharing_grants
            WHERE recipient_boundary_id = ? AND status = 'ACTIVE'
            """,
            (caller_boundary_id,),
        ).fetchall()
    for row in rows:
        allowed_ids = json.loads(row["allowed_evidence_ids_json"])
        if ev.id in allowed_ids:
            return True
    return False


@router.post(
    "",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register traceable source evidence (admin credential required)",
)
def create_evidence(
    payload: EvidenceRegisterRequest,
    request: Request,
    _actor: str = Depends(require_admin_auth),
) -> EvidenceResponse:
    db_path: str | None = getattr(request.app.state, "database_path", None)
    return evidence_service.register_evidence(db_path, payload)


@router.get(
    "",
    response_model=List[EvidenceResponse],
    summary="List registered evidence records (private org uploads filtered by sharing grant)",
)
def list_evidence(
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> List[EvidenceResponse]:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    all_ev = evidence_service.list_evidence(db_path)
    if is_admin:
        return all_ev
    caller_bnd = boundary.data_boundary_id if boundary else ""
    return [ev for ev in all_ev if _org_can_view_evidence(db_path, caller_bnd, ev)]


@router.get(
    "/{evidence_id}",
    response_model=EvidenceResponse,
    summary="Get an evidence record by ID (private org uploads require sharing grant or admin)",
)
def get_evidence(
    evidence_id: str,
    request: Request,
    auth_tuple=Depends(require_admin_or_org_auth),
) -> EvidenceResponse:
    is_admin, boundary = auth_tuple
    db_path: str | None = getattr(request.app.state, "database_path", None)
    ev = evidence_service.get_evidence_by_id(db_path, evidence_id)
    if not is_admin:
        caller_bnd = boundary.data_boundary_id if boundary else ""
        if not _org_can_view_evidence(db_path, caller_bnd, ev):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: private organization evidence requires an active sharing grant.",
            )
    return ev
