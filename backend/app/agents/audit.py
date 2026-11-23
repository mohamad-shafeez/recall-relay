"""Execution audit logging for RecallRelay agents with scoped idempotency.

Persists sanitized execution metadata (record IDs and evidence IDs only).
Never stores API keys, bearer tokens, or private document bodies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, List

from pydantic import BaseModel

from app.db.database import get_db_connection
from app.schemas.domain import new_id, utc_now_iso


class AgentExecutionAudit(BaseModel):
    """Persisted audit record for a single agent execution."""

    id: str
    agent_id: str
    agent_role: str
    authorized_boundary_id: str | None
    idempotency_key: str | None
    idempotency_scope_hash: str | None = None
    input_references: List[str]
    output_references: List[str]
    evidence_references: List[str]
    execution_status: str
    started_at: str
    completed_at: str | None
    error_message: str | None


def compute_idempotency_scope_hash(
    *,
    agent_id: str,
    authorized_boundary_id: str | None,
    canonical_input: dict[str, Any],
    idempotency_key: str,
) -> str:
    """Scope idempotency strictly to (agent_id, boundary_id, canonical_input, key)."""
    payload = json.dumps(
        {
            "agent_id": agent_id,
            "boundary_id": authorized_boundary_id or "__ADMIN__",
            "input": canonical_input,
            "key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_to_audit(row: sqlite3.Row) -> AgentExecutionAudit:
    scope_hash = (
        row["idempotency_scope_hash"]
        if "idempotency_scope_hash" in row.keys()
        else None
    )
    return AgentExecutionAudit(
        id=row["id"],
        agent_id=row["agent_id"],
        agent_role=row["agent_role"],
        authorized_boundary_id=row["authorized_boundary_id"],
        idempotency_key=row["idempotency_key"],
        idempotency_scope_hash=scope_hash,
        input_references=json.loads(row["input_references_json"]),
        output_references=json.loads(row["output_references_json"]),
        evidence_references=json.loads(row["evidence_references_json"]),
        execution_status=row["execution_status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_message=row["error_message"],
    )


def find_execution_by_scope_hash(
    db_path: str | None, scope_hash: str
) -> AgentExecutionAudit | None:
    """Look up an existing execution strictly by its scoped idempotency hash."""
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM agent_executions WHERE idempotency_scope_hash = ?",
            (scope_hash,),
        ).fetchone()
    return _row_to_audit(row) if row is not None else None


def start_execution_audit(
    db_path: str | None,
    *,
    agent_id: str,
    agent_role: str,
    authorized_boundary_id: str | None,
    input_references: List[str],
    idempotency_key: str | None = None,
    idempotency_scope_hash: str | None = None,
) -> AgentExecutionAudit:
    """Create a RUNNING audit row before an agent executes."""
    execution_id = new_id("exec")
    started_at = utc_now_iso()
    # Store scoped key string when present so two different orgs can safely use the same client key
    stored_key = f"{idempotency_scope_hash[:16]}:{idempotency_key}" if (idempotency_key and idempotency_scope_hash) else idempotency_key
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_executions (
                id, agent_id, agent_role, authorized_boundary_id, idempotency_key,
                idempotency_scope_hash, input_references_json, output_references_json,
                evidence_references_json, execution_status, started_at, completed_at, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, NULL, NULL)
            """,
            (
                execution_id,
                agent_id,
                agent_role,
                authorized_boundary_id,
                stored_key,
                idempotency_scope_hash,
                json.dumps(sorted(set(input_references))),
                "[]",
                "[]",
                started_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM agent_executions WHERE id = ?", (execution_id,)
        ).fetchone()
    return _row_to_audit(row)


def finish_execution_audit(
    db_path: str | None,
    *,
    execution_id: str,
    execution_status: str,
    output_references: List[str],
    evidence_references: List[str],
    error_message: str | None = None,
) -> AgentExecutionAudit:
    """Update an audit row upon completion, failure, or timeout."""
    completed_at = utc_now_iso()
    safe_error = error_message[:500] if error_message else None
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE agent_executions
            SET execution_status = ?,
                output_references_json = ?,
                evidence_references_json = ?,
                completed_at = ?,
                error_message = ?
            WHERE id = ?
            """,
            (
                execution_status,
                json.dumps(sorted(set(output_references))),
                json.dumps(sorted(set(evidence_references))),
                completed_at,
                safe_error,
                execution_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM agent_executions WHERE id = ?", (execution_id,)
        ).fetchone()
    return _row_to_audit(row)


def list_execution_audits(
    db_path: str | None,
    *,
    agent_role: str | None = None,
    authorized_boundary_id: str | None = None,
) -> List[AgentExecutionAudit]:
    """List agent execution audit records, optionally filtered by role or boundary."""
    clauses: List[str] = []
    params: List[str] = []
    if agent_role:
        clauses.append("agent_role = ?")
        params.append(agent_role)
    if authorized_boundary_id:
        clauses.append("authorized_boundary_id = ?")
        params.append(authorized_boundary_id)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM agent_executions {where_sql} ORDER BY started_at DESC, id ASC",
            tuple(params),
        ).fetchall()
    return [_row_to_audit(row) for row in rows]
