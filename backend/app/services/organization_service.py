"""Organization registration and retrieval service with server-assigned boundaries."""

from __future__ import annotations

import sqlite3
from typing import List

from fastapi import HTTPException, status

from app.core.auth import issue_org_credential
from app.db.database import get_db_connection
from app.schemas.api_v1 import (
    OrganizationRegisterRequest,
    OrganizationWithCredentialResponse,
)
from app.schemas.domain import (
    ConnectionStatus,
    IdentityVerificationStatus,
    Organization,
    OrganizationRole,
    new_id,
    utc_now_iso,
)


def _row_to_org(row: sqlite3.Row) -> Organization:
    return Organization(
        id=row["id"],
        name=row["name"],
        role=OrganizationRole(row["role"]),
        connection_status=ConnectionStatus(row["connection_status"]),
        identity_verification_status=IdentityVerificationStatus(
            row["identity_verification_status"]
        ),
        data_boundary_id=row["data_boundary_id"],
        source_url=row["source_url"],
        source_document=row["source_document"],
        source_organization=row["source_organization"],
        retrieved_at=row["retrieved_at"],
        created_at=row["created_at"],
    )


def register_organization(
    db_path: str | None, payload: OrganizationRegisterRequest
) -> OrganizationWithCredentialResponse:
    """Register an organization as UNVERIFIED with a server-assigned boundary and bearer token."""
    org_id = new_id("org")
    boundary_id = new_id("bnd")
    created_at = utc_now_iso()

    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO organizations (
                    id, name, role, connection_status, identity_verification_status,
                    data_boundary_id, source_url, source_document, source_organization,
                    retrieved_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    org_id,
                    payload.name,
                    payload.role.value,
                    payload.connection_status.value,
                    IdentityVerificationStatus.UNVERIFIED.value,
                    boundary_id,
                    payload.source_url,
                    payload.source_document,
                    payload.source_organization,
                    payload.retrieved_at,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM organizations WHERE id = ?", (org_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Organization record violated database constraints: {exc}",
        ) from exc

    access_token = issue_org_credential(db_path, org_id)
    return OrganizationWithCredentialResponse(
        organization=_row_to_org(row),
        access_token=access_token,
    )


def list_organizations(db_path: str | None) -> List[Organization]:
    """List registered organizations (directory metadata only; no private records)."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM organizations ORDER BY created_at DESC, id ASC"
        ).fetchall()
    return [_row_to_org(row) for row in rows]


def get_organization_by_id(db_path: str | None, org_id: str) -> Organization:
    """Retrieve a registered organization or raise 404."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM organizations WHERE id = ?", (org_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organization '{org_id}' was not found.",
        )
    return _row_to_org(row)
