"""Durable task coordination service with cycle detection, depth limits, and A2A handoffs."""

from __future__ import annotations

import json
import sqlite3
from typing import List, Set

from fastapi import HTTPException, status

from app.agents.base import AgentRole
from app.communication.audit_trail import record_audit_event
from app.communication.schemas import (
    A2AMessageCreate,
    A2AMessageType,
    AgentTask,
    AgentTaskCreate,
    HandoffDispatchRequest,
    HandoffDispatchResponse,
    TaskStatus,
)
from app.communication.transport import InternalSQLiteTransport, MessageTransport
from app.core.config import get_settings
from app.db.database import get_db_connection
from app.schemas.domain import new_id, utc_now_iso


def _row_to_task(row: sqlite3.Row, dep_ids: List[str]) -> AgentTask:
    return AgentTask(
        id=row["id"],
        parent_task_id=row["parent_task_id"],
        recall_id=row["recall_id"],
        conversation_id=row["conversation_id"],
        assigned_agent=AgentRole(row["assigned_agent"]),
        requested_action=row["requested_action"],
        input_evidence_references=json.loads(row["input_evidence_references_json"]),
        required_output_schema=row["required_output_schema"],
        status=TaskStatus(row["status"]),
        authorized_boundary_id=row["authorized_boundary_id"],
        depends_on_task_ids=dep_ids,
        retry_count=row["retry_count"],
        max_retries=row["max_retries"],
        deadline_at=row["deadline_at"],
        result_reference=row["result_reference"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _get_parent_depth(conn: sqlite3.Connection, parent_task_id: str | None) -> int:
    depth = 0
    current = parent_task_id
    visited: Set[str] = set()
    while current is not None:
        if current in visited:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Circular parent task delegation detected.",
            )
        visited.add(current)
        row = conn.execute(
            "SELECT parent_task_id FROM agent_tasks WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Parent task '{current}' was not found.",
            )
        depth += 1
        current = row["parent_task_id"]
    return depth


def _has_dependency_path(
    conn: sqlite3.Connection, start_id: str, target_id: str
) -> bool:
    """DFS to detect if adding start_id -> target_id creates a circular dependency."""
    stack = [start_id]
    visited: Set[str] = set()
    while stack:
        node = stack.pop()
        if node == target_id:
            return True
        if node in visited:
            continue
        visited.add(node)
        rows = conn.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (node,),
        ).fetchall()
        for r in rows:
            stack.append(r["depends_on_task_id"])
    return False


def create_task(
    db_path: str | None,
    payload: AgentTaskCreate,
    *,
    authorized_boundary_id: str | None,
    actor_id: str,
    initial_status: TaskStatus = TaskStatus.PENDING,
) -> AgentTask:
    """Create a durable agent task enforcing max delegation depth and acyclic dependencies."""
    task_id = new_id("tsk")
    created_at = utc_now_iso()
    max_depth = get_settings().a2a_max_delegation_depth

    with get_db_connection(db_path) as conn:
        recall = conn.execute(
            "SELECT id FROM recall_notices WHERE id = ?", (payload.recall_id,)
        ).fetchone()
        if recall is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recall '{payload.recall_id}' was not found.",
            )

        depth = _get_parent_depth(conn, payload.parent_task_id)
        if depth >= max_depth:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Task delegation depth limit ({max_depth}) exceeded.",
            )

        dep_ids = sorted(set(payload.depends_on_task_ids))
        for dep_id in dep_ids:
            dep_row = conn.execute(
                "SELECT id FROM agent_tasks WHERE id = ?", (dep_id,)
            ).fetchone()
            if dep_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Dependency task '{dep_id}' was not found.",
                )

        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, parent_task_id, recall_id, conversation_id, assigned_agent,
                requested_action, input_evidence_references_json, required_output_schema,
                status, authorized_boundary_id, retry_count, max_retries,
                deadline_at, result_reference, failure_reason, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, NULL, ?, NULL)
            """,
            (
                task_id,
                payload.parent_task_id,
                payload.recall_id,
                payload.conversation_id,
                payload.assigned_agent.value,
                payload.requested_action,
                json.dumps(sorted(set(payload.input_evidence_references))),
                payload.required_output_schema,
                initial_status.value,
                authorized_boundary_id,
                payload.max_retries,
                payload.deadline_at,
                created_at,
            ),
        )
        for dep_id in dep_ids:
            conn.execute(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
                (task_id, dep_id),
            )
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()

    record_audit_event(
        db_path,
        event_type="TASK_ASSIGNED",
        actor_id=actor_id,
        boundary_id=authorized_boundary_id,
        entity_type="agent_task",
        entity_id=task_id,
        metadata={
            "assigned_agent": payload.assigned_agent.value,
            "status": initial_status.value,
            "dependencies": dep_ids,
        },
    )
    return _row_to_task(row, dep_ids)


def add_task_dependency(
    db_path: str | None, task_id: str, depends_on_task_id: str
) -> None:
    """Attach a dependency to an existing task while strictly preventing cycles."""
    if task_id == depends_on_task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A task cannot depend on itself.",
        )
    with get_db_connection(db_path) as conn:
        if _has_dependency_path(conn, depends_on_task_id, task_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Circular task dependency detected and blocked.",
            )
        conn.execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
            (task_id, depends_on_task_id),
        )


def get_task(db_path: str | None, task_id: str) -> AgentTask:
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task '{task_id}' was not found.",
            )
        dep_rows = conn.execute(
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_task_id ASC",
            (task_id,),
        ).fetchall()
    return _row_to_task(row, [r["depends_on_task_id"] for r in dep_rows])


def list_tasks(
    db_path: str | None, *, recall_id: str | None = None
) -> List[AgentTask]:
    with get_db_connection(db_path) as conn:
        if recall_id:
            rows = conn.execute(
                "SELECT * FROM agent_tasks WHERE recall_id = ? ORDER BY created_at DESC, id ASC",
                (recall_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_tasks ORDER BY created_at DESC, id ASC"
            ).fetchall()
        tasks: List[AgentTask] = []
        for row in rows:
            dep_rows = conn.execute(
                "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ? ORDER BY depends_on_task_id ASC",
                (row["id"],),
            ).fetchall()
            tasks.append(_row_to_task(row, [r["depends_on_task_id"] for r in dep_rows]))
    return tasks


def dispatch_agent_handoff(
    db_path: str | None,
    payload: HandoffDispatchRequest,
    *,
    sender_boundary_id: str | None,
    recipient_boundary_id: str | None,
    actor_id: str,
    transport: MessageTransport | None = None,
) -> HandoffDispatchResponse:
    """Create a durable task and deliver a TASK_HANDOFF message via InternalSQLiteTransport."""
    initial_status = TaskStatus.PENDING
    if (
        payload.recipient_agent
        in {
            AgentRole.MANUFACTURER_OPERATIONS_AGENT,
            AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT,
            AgentRole.RETAILER_INVENTORY_AGENT,
        }
        and recipient_boundary_id is None
    ):
        initial_status = TaskStatus.BLOCKED_MISSING_AUTH
    elif (
        payload.recipient_agent == AgentRole.RECALL_SOURCE_ANALYST
        and not payload.evidence_references
        and not payload.payload.get("evidence_id")
    ):
        initial_status = TaskStatus.BLOCKED_MISSING_EVIDENCE

    task = create_task(
        db_path,
        AgentTaskCreate(
            parent_task_id=payload.parent_task_id,
            recall_id=payload.recall_id,
            conversation_id=payload.conversation_id,
            assigned_agent=payload.recipient_agent,
            requested_action=payload.requested_action,
            input_evidence_references=payload.evidence_references,
            required_output_schema=payload.required_output_schema,
            depends_on_task_ids=payload.depends_on_task_ids,
        ),
        authorized_boundary_id=recipient_boundary_id,
        actor_id=actor_id,
        initial_status=initial_status,
    )

    transport = transport or InternalSQLiteTransport(db_path)
    msg = transport.send_message(
        A2AMessageCreate(
            conversation_id=payload.conversation_id,
            task_id=task.id,
            recall_id=payload.recall_id,
            sender_agent=payload.sender_agent,
            recipient_agent=payload.recipient_agent,
            recipient_boundary_id=recipient_boundary_id,
            message_type=A2AMessageType.TASK_HANDOFF,
            payload=payload.payload,
            evidence_references=payload.evidence_references,
            idempotency_key=payload.idempotency_key,
            expires_at=payload.expires_at,
        ),
        sender_boundary_id=sender_boundary_id,
        actor_id=actor_id,
    )
    if msg.status.value == "PENDING":
        msg = transport.deliver_message(msg.id, actor_id=actor_id)
    return HandoffDispatchResponse(task=task, message=msg)
