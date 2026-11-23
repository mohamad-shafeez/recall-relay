"""Tamper-evident hash-chained audit trail for Phase 5 coordination events.

Never stores passwords, bearer tokens, API keys, or raw private document content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, List

from app.communication.schemas import AuditChainVerification, AuditEvent
from app.db.database import get_db_connection
from app.schemas.domain import new_id, utc_now_iso

_GENESIS_HASH = "0" * 64
_SENSITIVE_KEY_FRAGMENTS = ("token", "secret", "password", "api_key", "authorization", "document_content")


def _sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in metadata.items():
        if any(frag in key.lower() for frag in _SENSITIVE_KEY_FRAGMENTS):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = value
    return sanitized


def _compute_event_hash(
    *,
    event_id: str,
    event_type: str,
    actor_id: str,
    boundary_id: str | None,
    entity_type: str,
    entity_id: str,
    metadata_json: str,
    created_at: str,
    prev_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "id": event_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "boundary_id": boundary_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata_json": metadata_json,
            "created_at": created_at,
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        seq=row["seq"],
        id=row["id"],
        event_type=row["event_type"],
        actor_id=row["actor_id"],
        boundary_id=row["boundary_id"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        metadata=json.loads(row["metadata_json"]),
        created_at=row["created_at"],
        prev_hash=row["prev_hash"],
        event_hash=row["event_hash"],
    )


def record_audit_event(
    db_path: str | None,
    *,
    event_type: str,
    actor_id: str,
    entity_type: str,
    entity_id: str,
    boundary_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append a hash-chained audit event transactionally."""
    event_id = new_id("aud")
    created_at = utc_now_iso()
    clean_meta = _sanitize_metadata(metadata or {})
    metadata_json = json.dumps(clean_meta, sort_keys=True, separators=(",", ":"))

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        prev_row = conn.execute(
            "SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev_row["event_hash"] if prev_row is not None else _GENESIS_HASH
        event_hash = _compute_event_hash(
            event_id=event_id,
            event_type=event_type,
            actor_id=actor_id,
            boundary_id=boundary_id,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_json=metadata_json,
            created_at=created_at,
            prev_hash=prev_hash,
        )
        conn.execute(
            """
            INSERT INTO audit_events (
                id, event_type, actor_id, boundary_id, entity_type,
                entity_id, metadata_json, created_at, prev_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                actor_id,
                boundary_id,
                entity_type,
                entity_id,
                metadata_json,
                created_at,
                prev_hash,
                event_hash,
            ),
        )
        row = conn.execute(
            "SELECT * FROM audit_events WHERE id = ?", (event_id,)
        ).fetchone()
    return _row_to_event(row)


def list_audit_events(
    db_path: str | None, *, event_type: str | None = None, limit: int = 100
) -> List[AuditEvent]:
    with get_db_connection(db_path) as conn:
        if event_type:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE event_type = ? ORDER BY seq DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_row_to_event(row) for row in rows]


def verify_audit_chain(db_path: str | None) -> AuditChainVerification:
    """Recompute and verify every SHA-256 hash link in audit_events."""
    with get_db_connection(db_path) as conn:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()

    expected_prev = _GENESIS_HASH
    for row in rows:
        if row["prev_hash"] != expected_prev:
            return AuditChainVerification(
                valid=False, event_count=len(rows), broken_at_seq=row["seq"]
            )
        recomputed = _compute_event_hash(
            event_id=row["id"],
            event_type=row["event_type"],
            actor_id=row["actor_id"],
            boundary_id=row["boundary_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            metadata_json=row["metadata_json"],
            created_at=row["created_at"],
            prev_hash=row["prev_hash"],
        )
        if recomputed != row["event_hash"]:
            return AuditChainVerification(
                valid=False, event_count=len(rows), broken_at_seq=row["seq"]
            )
        expected_prev = row["event_hash"]

    return AuditChainVerification(valid=True, event_count=len(rows), broken_at_seq=None)
