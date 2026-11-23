"""Evidence registration and retrieval service."""

from __future__ import annotations

import hashlib
import sqlite3
from typing import List

from fastapi import HTTPException, status

from app.db.database import get_db_connection
from app.schemas.api_v1 import EvidenceRegisterRequest, EvidenceResponse, EvidenceStage
from app.schemas.domain import EvidenceSourceType, VerificationStatus, new_id


def _row_to_evidence(row: sqlite3.Row) -> EvidenceResponse:
    return EvidenceResponse(
        id=row["id"],
        source_type=EvidenceSourceType(row["source_type"]),
        evidence_stage=EvidenceStage(row["evidence_stage"]),
        source_url=row["source_url"],
        source_document=row["source_document"],
        source_organization=row["source_organization"],
        publication_date=row["publication_date"],
        retrieved_at=row["retrieved_at"],
        record_reference=row["record_reference"],
        verification_status=VerificationStatus(row["verification_status"]),
        sha256_hash=row["sha256_hash"],
        notes=row["notes"],
    )


def register_evidence(
    db_path: str | None, payload: EvidenceRegisterRequest
) -> EvidenceResponse:
    """Persist a traceable evidence record, computing SHA-256 when content is provided."""
    evidence_id = new_id("ev")
    computed_hash = payload.sha256_hash
    if payload.document_content is not None and not computed_hash:
        computed_hash = hashlib.sha256(
            payload.document_content.encode("utf-8")
        ).hexdigest()

    try:
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO evidence_records (
                    id, source_type, evidence_stage, source_url, source_document,
                    source_organization, publication_date, retrieved_at,
                    record_reference, verification_status, sha256_hash, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    payload.source_type.value,
                    payload.evidence_stage.value,
                    payload.source_url,
                    payload.source_document,
                    payload.source_organization,
                    payload.publication_date,
                    payload.retrieved_at,
                    payload.record_reference,
                    payload.verification_status.value,
                    computed_hash,
                    payload.notes,
                ),
            )
            row = conn.execute(
                "SELECT * FROM evidence_records WHERE id = ?", (evidence_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Evidence record violated database integrity constraints: {exc}",
        ) from exc

    return _row_to_evidence(row)


def list_evidence(db_path: str | None) -> List[EvidenceResponse]:
    """Return all registered evidence records ordered by retrieval timestamp."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM evidence_records ORDER BY retrieved_at DESC, id ASC"
        ).fetchall()
    return [_row_to_evidence(row) for row in rows]


def get_evidence_by_id(db_path: str | None, evidence_id: str) -> EvidenceResponse:
    """Retrieve a single evidence record or raise 404."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM evidence_records WHERE id = ?", (evidence_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Evidence record '{evidence_id}' was not found.",
        )
    return _row_to_evidence(row)
