"""Strict versioned schemas for Phase 5 A2A messaging, tasks, sharing, governance, and auditing."""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Literal

from pydantic import BaseModel, Field, field_validator

from app.agents.base import AgentRole
from app.schemas.domain import (
    ApprovalDecision,
    IdentityVerificationStatus,
    _validate_non_empty,
)


class A2AMessageType(str, Enum):
    TASK_HANDOFF = "TASK_HANDOFF"
    EVIDENCE_SHARE_NOTICE = "EVIDENCE_SHARE_NOTICE"
    RESULT_REPORT = "RESULT_REPORT"
    ACK = "ACK"
    ERROR_REPORT = "ERROR_REPORT"


class A2AMessageStatus(str, Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    BLOCKED_MISSING_EVIDENCE = "BLOCKED_MISSING_EVIDENCE"
    BLOCKED_MISSING_AUTH = "BLOCKED_MISSING_AUTH"


class SharingGrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class A2AMessageCreate(BaseModel):
    """Strict versioned A2A message creation schema (schema_version = 1.0)."""

    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str
    task_id: str | None = None
    recall_id: str
    sender_agent: AgentRole
    recipient_agent: AgentRole
    recipient_boundary_id: str | None = None
    message_type: A2AMessageType
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_references: List[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=5)
    expires_at: str | None = None

    @field_validator("conversation_id", "recall_id")
    @classmethod
    def check_non_empty(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class A2AMessage(BaseModel):
    """Persisted durable A2A message envelope."""

    id: str
    schema_version: str
    conversation_id: str
    task_id: str | None
    recall_id: str
    sender_agent: AgentRole
    recipient_agent: AgentRole
    sender_boundary_id: str | None
    recipient_boundary_id: str | None
    message_type: A2AMessageType
    payload: dict[str, Any]
    evidence_references: List[str]
    idempotency_key: str | None
    status: A2AMessageStatus
    attempt_count: int
    max_attempts: int
    claimed_by: str | None
    claimed_at: str | None
    delivered_at: str | None
    acknowledged_at: str | None
    processed_at: str | None
    expires_at: str | None
    error_message: str | None
    external_message_id: str | None = None
    external_reply_message_id: str | None = None
    external_receiver_agent_id: str | None = None
    transport_provider: str = "internal_sqlite"
    created_at: str


class AgentTaskCreate(BaseModel):
    """Request schema for creating a durable agent task with optional dependencies."""

    parent_task_id: str | None = None
    recall_id: str
    conversation_id: str
    assigned_agent: AgentRole
    requested_action: str
    input_evidence_references: List[str] = Field(default_factory=list)
    required_output_schema: str
    depends_on_task_ids: List[str] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0, le=5)
    deadline_at: str | None = None

    @field_validator("recall_id", "conversation_id", "requested_action", "required_output_schema")
    @classmethod
    def check_required(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class AgentTask(BaseModel):
    """Persisted durable agent task."""

    id: str
    parent_task_id: str | None
    recall_id: str
    conversation_id: str
    assigned_agent: AgentRole
    requested_action: str
    input_evidence_references: List[str]
    required_output_schema: str
    status: TaskStatus
    authorized_boundary_id: str | None
    depends_on_task_ids: List[str] = Field(default_factory=list)
    retry_count: int
    max_retries: int
    deadline_at: str | None
    result_reference: str | None
    failure_reason: str | None
    created_at: str
    completed_at: str | None


class HandoffDispatchRequest(BaseModel):
    """Request to create a durable task and deliver a TASK_HANDOFF message to a specialist."""

    recall_id: str
    conversation_id: str
    sender_agent: AgentRole
    recipient_agent: AgentRole
    requested_action: str
    required_output_schema: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_references: List[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    depends_on_task_ids: List[str] = Field(default_factory=list)
    idempotency_key: str | None = None
    expires_at: str | None = None


class HandoffDispatchResponse(BaseModel):
    task: AgentTask
    message: A2AMessage


class SharingGrantCreateRequest(BaseModel):
    """Explicit owner-authorized cross-organization information-sharing grant."""

    recall_id: str
    recipient_org_id: str
    allowed_fields: List[str] = Field(min_length=1)
    allowed_evidence_ids: List[str] = Field(default_factory=list)
    purpose: str
    authorized_by: str
    expires_at: str | None = None

    @field_validator("recall_id", "recipient_org_id", "purpose", "authorized_by")
    @classmethod
    def check_required(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class SharingGrant(BaseModel):
    """Persisted cross-organization sharing authorization record."""

    id: str
    recall_id: str
    owner_org_id: str
    owner_boundary_id: str
    recipient_org_id: str
    recipient_boundary_id: str
    allowed_fields: List[str]
    allowed_evidence_ids: List[str]
    purpose: str
    authorized_by: str
    status: SharingGrantStatus
    expires_at: str | None
    revoked_at: str | None
    created_at: str


class HumanApprovalDecisionRequest(BaseModel):
    """Authorized human decision on a pending approval proposal."""

    decision: Literal["APPROVED", "REJECTED"]
    reviewer_name: str
    rationale: str

    @field_validator("reviewer_name", "rationale")
    @classmethod
    def check_required(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class HumanApprovalRecord(BaseModel):
    """Persisted human approval record including proposing agent identity."""

    id: str
    recall_id: str
    action_type: str
    decision: ApprovalDecision
    proposing_agent_id: str | None
    reviewer_name: str | None
    rationale: str | None
    decided_at: str | None
    created_at: str


class OrgIdentityReviewRequest(BaseModel):
    """Human administrator review of an organization's legal identity."""

    target_status: IdentityVerificationStatus
    reviewer_name: str
    supporting_evidence_id: str | None = None
    notes: str | None = None

    @field_validator("reviewer_name")
    @classmethod
    def check_reviewer(cls, value: str) -> str:
        return _validate_non_empty(value, "reviewer_name")


class AuditEvent(BaseModel):
    """Tamper-evident hash-chained audit trail record."""

    seq: int
    id: str
    event_type: str
    actor_id: str
    boundary_id: str | None
    entity_type: str
    entity_id: str
    metadata: dict[str, Any]
    created_at: str
    prev_hash: str
    event_hash: str


class AuditChainVerification(BaseModel):
    """Result of verifying the SHA-256 hash chain across all audit_events."""

    valid: bool
    event_count: int
    broken_at_seq: int | None = None
