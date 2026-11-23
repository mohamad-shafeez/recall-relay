"""Abstract MessageTransport interface (future BAND seam) and InternalSQLiteTransport.

Phase 7 security hardening:
- Boundary-scoped message idempotency (prevents cross-org key collisions).
- Cross-organization sharing grant enforcement on cross-boundary messages.
- Strict org-boundary role enforcement before claiming any inbox message.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List

from fastapi import HTTPException, status

from app.agents.base import AgentAuthContext, AgentRole
from app.agents.specialists import build_agent
from app.agents.tools import ToolRegistry
from app.communication.audit_trail import record_audit_event
from app.communication.schemas import (
    A2AMessage,
    A2AMessageCreate,
    A2AMessageStatus,
    A2AMessageType,
)
from app.communication.sharing_service import check_sharing_authorization
from app.db.database import get_db_connection
from app.schemas.domain import OrganizationRole, new_id, utc_now_iso

_VALID_TRANSITIONS = {
    A2AMessageStatus.PENDING: {
        A2AMessageStatus.DELIVERED,
        A2AMessageStatus.EXPIRED,
        A2AMessageStatus.FAILED,
    },
    A2AMessageStatus.DELIVERED: {
        A2AMessageStatus.PROCESSING,
        A2AMessageStatus.EXPIRED,
        A2AMessageStatus.FAILED,
    },
    A2AMessageStatus.PROCESSING: {
        A2AMessageStatus.PROCESSED,
        A2AMessageStatus.FAILED,
        A2AMessageStatus.EXPIRED,
    },
    A2AMessageStatus.FAILED: {
        A2AMessageStatus.PENDING,
    },
    A2AMessageStatus.PROCESSED: set(),
    A2AMessageStatus.EXPIRED: set(),
}

_AGENT_REQUIRED_ORG_ROLE = {
    AgentRole.MANUFACTURER_OPERATIONS_AGENT: OrganizationRole.MANUFACTURER,
    AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT: OrganizationRole.DISTRIBUTOR,
    AgentRole.RETAILER_INVENTORY_AGENT: OrganizationRole.RETAILER,
}


def compute_message_idempotency_scope(
    *,
    sender_agent: AgentRole,
    recipient_agent: AgentRole,
    sender_boundary_id: str | None,
    idempotency_key: str,
) -> str:
    """Scope A2A message idempotency strictly to (sender_agent, recipient_agent, sender_boundary, key)."""
    canonical = json.dumps(
        {
            "sender_agent": sender_agent.value,
            "recipient_agent": recipient_agent.value,
            "sender_boundary_id": sender_boundary_id or "__ADMIN__",
            "key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _row_to_message(row: sqlite3.Row) -> A2AMessage:
    return A2AMessage(
        id=row["id"],
        schema_version=row["schema_version"],
        conversation_id=row["conversation_id"],
        task_id=row["task_id"],
        recall_id=row["recall_id"],
        sender_agent=AgentRole(row["sender_agent"]),
        recipient_agent=AgentRole(row["recipient_agent"]),
        sender_boundary_id=row["sender_boundary_id"],
        recipient_boundary_id=row["recipient_boundary_id"],
        message_type=A2AMessageType(row["message_type"]),
        payload=json.loads(row["payload_json"]),
        evidence_references=json.loads(row["evidence_references_json"]),
        idempotency_key=row["idempotency_key"],
        status=A2AMessageStatus(row["status"]),
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        delivered_at=row["delivered_at"],
        acknowledged_at=row["acknowledged_at"],
        processed_at=row["processed_at"],
        expires_at=row["expires_at"],
        error_message=row["error_message"],
        external_message_id=row["external_message_id"] if "external_message_id" in row.keys() else None,
        external_reply_message_id=row["external_reply_message_id"] if "external_reply_message_id" in row.keys() else None,
        external_receiver_agent_id=row["external_receiver_agent_id"] if "external_receiver_agent_id" in row.keys() else None,
        transport_provider=row["transport_provider"] if "transport_provider" in row.keys() else "internal_sqlite",
        created_at=row["created_at"],
    )


class MessageTransport(ABC):
    """Clean transport interface separating internal SQLite transport from future BAND integration."""

    transport_name: str

    @abstractmethod
    def send_message(
        self,
        payload: A2AMessageCreate,
        *,
        sender_boundary_id: str | None,
        actor_id: str,
    ) -> A2AMessage:
        """Persist and enqueue a new versioned A2A message."""

    @abstractmethod
    def deliver_message(self, message_id: str, *, actor_id: str) -> A2AMessage:
        """Transition a PENDING message to DELIVERED (or EXPIRED if past expires_at)."""

    @abstractmethod
    def acknowledge_message(
        self, message_id: str, *, actor_id: str, caller_boundary_id: str | None
    ) -> A2AMessage:
        """Record recipient acknowledgement of a delivered message."""

    @abstractmethod
    def claim_next_message(
        self,
        recipient_agent: AgentRole,
        *,
        worker_id: str,
        caller_boundary_id: str | None = None,
    ) -> A2AMessage | None:
        """Atomically claim one DELIVERED message for PROCESSING without double-claiming."""


class InternalSQLiteTransport(MessageTransport):
    """Durable internal SQLite A2A message transport (not BAND)."""

    transport_name = "internal_sqlite_transport"

    def __init__(self, db_path: str | None) -> None:
        self.db_path = db_path

    def send_message(
        self,
        payload: A2AMessageCreate,
        *,
        sender_boundary_id: str | None,
        actor_id: str,
    ) -> A2AMessage:
        # Enforce default-DENY cross-organization sharing grant on cross-boundary messages
        if (
            sender_boundary_id is not None
            and payload.recipient_boundary_id is not None
            and sender_boundary_id != payload.recipient_boundary_id
        ):
            requested_fields = sorted(payload.payload.keys())
            if payload.evidence_references:
                for ev_id in payload.evidence_references:
                    allowed, _ = check_sharing_authorization(
                        self.db_path,
                        recall_id=payload.recall_id,
                        owner_boundary_id=sender_boundary_id,
                        recipient_boundary_id=payload.recipient_boundary_id,
                        requested_fields=requested_fields or None,
                        requested_evidence_id=ev_id,
                    )
                    if not allowed:
                        raise HTTPException(
                            status_code=status.HTTP_403_FORBIDDEN,
                            detail=(
                                "Cross-organization message denied: no active information-sharing "
                                f"grant authorizes sharing fields and evidence '{ev_id}'."
                            ),
                        )
            else:
                allowed, _ = check_sharing_authorization(
                    self.db_path,
                    recall_id=payload.recall_id,
                    owner_boundary_id=sender_boundary_id,
                    recipient_boundary_id=payload.recipient_boundary_id,
                    requested_fields=requested_fields or None,
                )
                if not allowed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "Cross-organization message denied: no active information-sharing "
                            "grant covers the requested payload fields."
                        ),
                    )

        # Boundary-scoped idempotent duplicate delivery check
        scope_hash: str | None = None
        if payload.idempotency_key:
            scope_hash = compute_message_idempotency_scope(
                sender_agent=payload.sender_agent,
                recipient_agent=payload.recipient_agent,
                sender_boundary_id=sender_boundary_id,
                idempotency_key=payload.idempotency_key,
            )
            with get_db_connection(self.db_path) as conn:
                existing = conn.execute(
                    "SELECT * FROM a2a_messages WHERE idempotency_scope_hash = ?",
                    (scope_hash,),
                ).fetchone()
                if existing is not None:
                    return _row_to_message(existing)

        message_id = new_id("msg")
        created_at = utc_now_iso()
        initial_status = (
            A2AMessageStatus.EXPIRED.value
            if _is_expired(payload.expires_at)
            else A2AMessageStatus.PENDING.value
        )

        with get_db_connection(self.db_path) as conn:
            recall = conn.execute(
                "SELECT id FROM recall_notices WHERE id = ?", (payload.recall_id,)
            ).fetchone()
            if recall is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Recall '{payload.recall_id}' was not found.",
                )

            conn.execute(
                """
                INSERT INTO a2a_messages (
                    id, schema_version, conversation_id, task_id, recall_id,
                    sender_agent, recipient_agent, sender_boundary_id, recipient_boundary_id,
                    message_type, payload_json, evidence_references_json, idempotency_key,
                    idempotency_scope_hash, status, attempt_count, max_attempts, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    message_id,
                    payload.schema_version,
                    payload.conversation_id,
                    payload.task_id,
                    payload.recall_id,
                    payload.sender_agent.value,
                    payload.recipient_agent.value,
                    sender_boundary_id,
                    payload.recipient_boundary_id,
                    payload.message_type.value,
                    json.dumps(payload.payload, sort_keys=True),
                    json.dumps(sorted(set(payload.evidence_references))),
                    payload.idempotency_key,
                    scope_hash,
                    initial_status,
                    payload.max_attempts,
                    payload.expires_at,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()

        record_audit_event(
            self.db_path,
            event_type="MESSAGE_CREATED",
            actor_id=actor_id,
            boundary_id=sender_boundary_id,
            entity_type="a2a_message",
            entity_id=message_id,
            metadata={
                "sender_agent": payload.sender_agent.value,
                "recipient_agent": payload.recipient_agent.value,
                "message_type": payload.message_type.value,
                "status": initial_status,
                "transport": self.transport_name,
            },
        )
        return _row_to_message(row)

    def get_message(
        self, message_id: str, *, is_admin: bool, caller_boundary_id: str | None
    ) -> A2AMessage:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Message '{message_id}' was not found.",
            )
        msg = _row_to_message(row)
        if not is_admin:
            if caller_boundary_id not in {
                msg.sender_boundary_id,
                msg.recipient_boundary_id,
            }:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied: message belongs to another organization boundary.",
                )
        return msg

    def list_messages(
        self,
        *,
        is_admin: bool,
        caller_boundary_id: str | None,
        recipient_agent: AgentRole | None = None,
        recall_id: str | None = None,
    ) -> List[A2AMessage]:
        clauses: List[str] = []
        params: List[str] = []
        if recipient_agent:
            clauses.append("recipient_agent = ?")
            params.append(recipient_agent.value)
        if recall_id:
            clauses.append("recall_id = ?")
            params.append(recall_id)
        if not is_admin:
            clauses.append("(sender_boundary_id = ? OR recipient_boundary_id = ?)")
            params.extend([caller_boundary_id or "", caller_boundary_id or ""])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM a2a_messages {where_sql} ORDER BY created_at DESC, id ASC",
                tuple(params),
            ).fetchall()
        return [_row_to_message(row) for row in rows]

    def deliver_message(self, message_id: str, *, actor_id: str) -> A2AMessage:
        now = utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Message '{message_id}' was not found.",
                )
            current = A2AMessageStatus(row["status"])
            if _is_expired(row["expires_at"]) and current not in {
                A2AMessageStatus.PROCESSED,
                A2AMessageStatus.EXPIRED,
            }:
                conn.execute(
                    "UPDATE a2a_messages SET status = 'EXPIRED' WHERE id = ?",
                    (message_id,),
                )
                updated = conn.execute(
                    "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
                ).fetchone()
                msg = _row_to_message(updated)
            else:
                if A2AMessageStatus.DELIVERED not in _VALID_TRANSITIONS[current]:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot transition message from '{current.value}' to 'DELIVERED'.",
                    )
                conn.execute(
                    "UPDATE a2a_messages SET status = 'DELIVERED', delivered_at = ? WHERE id = ?",
                    (now, message_id),
                )
                updated = conn.execute(
                    "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
                ).fetchone()
                msg = _row_to_message(updated)

        record_audit_event(
            self.db_path,
            event_type="MESSAGE_DELIVERED" if msg.status == A2AMessageStatus.DELIVERED else "MESSAGE_EXPIRED",
            actor_id=actor_id,
            boundary_id=msg.recipient_boundary_id,
            entity_type="a2a_message",
            entity_id=message_id,
            metadata={"status": msg.status.value},
        )
        return msg

    def acknowledge_message(
        self, message_id: str, *, actor_id: str, caller_boundary_id: str | None
    ) -> A2AMessage:
        now = utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Message '{message_id}' was not found.",
                )
            if (
                row["recipient_boundary_id"]
                and caller_boundary_id
                and row["recipient_boundary_id"] != caller_boundary_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Only the recipient boundary may acknowledge this message.",
                )
            if row["status"] not in ("DELIVERED", "PROCESSING", "PROCESSED"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Cannot acknowledge message in status '{row['status']}'.",
                )
            conn.execute(
                "UPDATE a2a_messages SET acknowledged_at = COALESCE(acknowledged_at, ?) WHERE id = ?",
                (now, message_id),
            )
            updated = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            msg = _row_to_message(updated)

        record_audit_event(
            self.db_path,
            event_type="MESSAGE_ACKNOWLEDGED",
            actor_id=actor_id,
            boundary_id=caller_boundary_id,
            entity_type="a2a_message",
            entity_id=message_id,
            metadata={"status": msg.status.value},
        )
        return msg

    def claim_next_message(
        self,
        recipient_agent: AgentRole,
        *,
        worker_id: str,
        caller_boundary_id: str | None = None,
    ) -> A2AMessage | None:
        """Atomically claim the oldest unexpired DELIVERED message using BEGIN IMMEDIATE."""
        now = utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if caller_boundary_id is not None:
                # Strict boundary match for org-scoped agents (never claim NULL-boundary messages)
                candidates = conn.execute(
                    """
                    SELECT * FROM a2a_messages
                    WHERE recipient_agent = ? AND status = 'DELIVERED'
                      AND recipient_boundary_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (recipient_agent.value, caller_boundary_id),
                ).fetchall()
            else:
                candidates = conn.execute(
                    """
                    SELECT * FROM a2a_messages
                    WHERE recipient_agent = ? AND status = 'DELIVERED'
                      AND recipient_boundary_id IS NULL
                    ORDER BY created_at ASC, id ASC
                    """,
                    (recipient_agent.value,),
                ).fetchall()

            claimed_row = None
            for row in candidates:
                if _is_expired(row["expires_at"]):
                    conn.execute(
                        "UPDATE a2a_messages SET status = 'EXPIRED' WHERE id = ?",
                        (row["id"],),
                    )
                    continue
                cursor = conn.execute(
                    """
                    UPDATE a2a_messages
                    SET status = 'PROCESSING',
                        claimed_by = ?,
                        claimed_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE id = ? AND status = 'DELIVERED'
                    """,
                    (worker_id, now, row["id"]),
                )
                if cursor.rowcount == 1:
                    claimed_row = conn.execute(
                        "SELECT * FROM a2a_messages WHERE id = ?", (row["id"],)
                    ).fetchone()
                    break

        if claimed_row is None:
            return None
        msg = _row_to_message(claimed_row)
        record_audit_event(
            self.db_path,
            event_type="MESSAGE_CLAIMED",
            actor_id=worker_id,
            boundary_id=caller_boundary_id,
            entity_type="a2a_message",
            entity_id=msg.id,
            metadata={"attempt_count": msg.attempt_count},
        )
        return msg

    def mark_processed(self, message_id: str, *, actor_id: str) -> A2AMessage:
        now = utc_now_iso()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Message not found.")
            if row["status"] != "PROCESSING":
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot mark message PROCESSED from status '{row['status']}'.",
                )
            conn.execute(
                "UPDATE a2a_messages SET status = 'PROCESSED', processed_at = ? WHERE id = ?",
                (now, message_id),
            )
            updated = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            msg = _row_to_message(updated)

        record_audit_event(
            self.db_path,
            event_type="MESSAGE_PROCESSED",
            actor_id=actor_id,
            boundary_id=msg.recipient_boundary_id,
            entity_type="a2a_message",
            entity_id=message_id,
            metadata={"status": "PROCESSED"},
        )
        return msg

    def mark_failed(
        self, message_id: str, *, actor_id: str, error_message: str, requeue_if_allowed: bool = True
    ) -> A2AMessage:
        safe_err = error_message[:500]
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Message not found.")
            can_retry = requeue_if_allowed and (row["attempt_count"] < row["max_attempts"])
            next_status = "PENDING" if can_retry else "FAILED"
            conn.execute(
                """
                UPDATE a2a_messages
                SET status = ?, error_message = ?, claimed_by = NULL, claimed_at = NULL
                WHERE id = ?
                """,
                (next_status, safe_err, message_id),
            )
            updated = conn.execute(
                "SELECT * FROM a2a_messages WHERE id = ?", (message_id,)
            ).fetchone()
            msg = _row_to_message(updated)

        record_audit_event(
            self.db_path,
            event_type="MESSAGE_FAILED" if next_status == "FAILED" else "MESSAGE_RETRY_SCHEDULED",
            actor_id=actor_id,
            boundary_id=msg.recipient_boundary_id,
            entity_type="a2a_message",
            entity_id=message_id,
            metadata={"status": next_status, "attempt_count": msg.attempt_count},
        )
        return msg

    def process_recipient_inbox(
        self,
        recipient_agent: AgentRole,
        *,
        auth: AgentAuthContext,
        worker_id: str,
        timeout_seconds: float = 5.0,
    ) -> dict | None:
        """Verify caller role authorization BEFORE claiming, then execute specialist & publish result."""
        required_org_role = _AGENT_REQUIRED_ORG_ROLE.get(recipient_agent)
        if required_org_role is not None:
            if auth.boundary is None or auth.boundary.role != required_org_role:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Inbox access denied: processing '{recipient_agent.value}' requires "
                        f"an authenticated '{required_org_role.value}' organization boundary."
                    ),
                )

        boundary_id = auth.boundary.data_boundary_id if auth.boundary else None
        msg = self.claim_next_message(
            recipient_agent, worker_id=worker_id, caller_boundary_id=boundary_id
        )
        if msg is None:
            return None

        agent = build_agent(
            recipient_agent,
            db_path=self.db_path,
            tool_registry=ToolRegistry(),
            timeout_seconds=timeout_seconds,
        )
        envelope = agent.run(
            msg.payload,
            auth,
            idempotency_key=f"a2a_msg_{msg.id}",
        )

        if envelope.execution_status == "COMPLETED":
            processed_msg = self.mark_processed(msg.id, actor_id=worker_id)
            if msg.task_id:
                with get_db_connection(self.db_path) as conn:
                    conn.execute(
                        """
                        UPDATE agent_tasks
                        SET status = 'COMPLETED', result_reference = ?, completed_at = ?
                        WHERE id = ?
                        """,
                        (envelope.execution_id, utc_now_iso(), msg.task_id),
                    )
            reply = self.send_message(
                A2AMessageCreate(
                    conversation_id=msg.conversation_id,
                    task_id=msg.task_id,
                    recall_id=msg.recall_id,
                    sender_agent=recipient_agent,
                    recipient_agent=msg.sender_agent,
                    recipient_boundary_id=msg.sender_boundary_id,
                    message_type=A2AMessageType.RESULT_REPORT,
                    payload={
                        "source_message_id": msg.id,
                        "execution_id": envelope.execution_id,
                        "result": envelope.output or {},
                    },
                    evidence_references=envelope.evidence_references,
                    idempotency_key=f"reply_{msg.id}",
                ),
                sender_boundary_id=boundary_id,
                actor_id=worker_id,
            )
            self.deliver_message(reply.id, actor_id=worker_id)
            return {
                "processed_message": processed_msg.model_dump(),
                "execution": envelope.model_dump(),
                "result_message": reply.model_dump(),
            }

        failed_msg = self.mark_failed(
            msg.id,
            actor_id=worker_id,
            error_message=envelope.error_message or "Agent execution failed.",
            requeue_if_allowed=True,
        )
        if msg.task_id and failed_msg.status == A2AMessageStatus.FAILED:
            with get_db_connection(self.db_path) as conn:
                conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status = 'FAILED', failure_reason = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (failed_msg.error_message, utc_now_iso(), msg.task_id),
                )
        return {
            "processed_message": failed_msg.model_dump(),
            "execution": envelope.model_dump(),
            "result_message": None,
        }
