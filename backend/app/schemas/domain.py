"""Domain enums and Pydantic v2 models for RecallRelay.

Enforces real-data provenance rules, explicit unknown/awaiting states,
organization identity verification separation, and per-organization data boundaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """Generate a prefixed unique identifier."""
    return f"{prefix}_{uuid4().hex[:12]}"


def _validate_http_url(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source_url must be a valid http:// or https:// URL.")
    return cleaned


def _validate_non_empty(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be empty.")
    return cleaned


class OrganizationRole(str, Enum):
    MANUFACTURER = "MANUFACTURER"
    DISTRIBUTOR = "DISTRIBUTOR"
    RETAILER = "RETAILER"
    REGULATOR = "REGULATOR"
    COORDINATOR = "COORDINATOR"


class ConnectionStatus(str, Enum):
    CONNECTED = "CONNECTED"
    NOT_CONNECTED = "NOT_CONNECTED"
    AWAITING_AUTHORIZATION = "AWAITING_AUTHORIZATION"


class IdentityVerificationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class EvidenceSourceType(str, Enum):
    OFFICIAL_PUBLIC_NOTICE = "OFFICIAL_PUBLIC_NOTICE"
    PUBLIC_DATASET = "PUBLIC_DATASET"
    AUTHORIZED_ORG_UPLOAD = "AUTHORIZED_ORG_UPLOAD"
    AGENT_ATTESTATION = "AGENT_ATTESTATION"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    UNKNOWN = "UNKNOWN"
    DISCREPANCY = "DISCREPANCY"
    NOT_CONNECTED = "NOT_CONNECTED"


class RecallSeverity(str, Enum):
    CLASS_I = "CLASS_I"
    CLASS_II = "CLASS_II"
    CLASS_III = "CLASS_III"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class RecallStatus(str, Enum):
    INITIATED = "INITIATED"
    IN_PROGRESS = "IN_PROGRESS"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class InventoryStatus(str, Enum):
    QUARANTINE_REQUIRED = "QUARANTINE_REQUIRED"
    QUARANTINED = "QUARANTINED"
    NO_AFFECTED_STOCK = "NO_AFFECTED_STOCK"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    UNKNOWN = "UNKNOWN"
    NOT_CONNECTED = "NOT_CONNECTED"


class AcknowledgementStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    NOT_CONNECTED = "NOT_CONNECTED"
    DECLINED = "DECLINED"
    UNKNOWN = "UNKNOWN"


class FindingType(str, Enum):
    QUANTITY_RECONCILIATION = "QUANTITY_RECONCILIATION"
    CHAIN_COVERAGE = "CHAIN_COVERAGE"
    ACKNOWLEDGEMENT_GAP = "ACKNOWLEDGEMENT_GAP"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    DISCREPANCY = "DISCREPANCY"


class AgentMessageType(str, Enum):
    RECALL_ALERT = "RECALL_ALERT"
    TRACE_REQUEST = "TRACE_REQUEST"
    TRACE_RESPONSE = "TRACE_RESPONSE"
    INVENTORY_REQUEST = "INVENTORY_REQUEST"
    INVENTORY_RESPONSE = "INVENTORY_RESPONSE"
    VERIFICATION_REPORT = "VERIFICATION_REPORT"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    ACK = "ACK"


class DeliveryStatus(str, Enum):
    QUEUED = "QUEUED"
    DELIVERED = "DELIVERED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"
    NOT_CONNECTED = "NOT_CONNECTED"


class ApprovalDecision(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class OrganizationBoundaryContext(BaseModel):
    """Access-boundary descriptor enforcing per-organization data isolation."""

    model_config = ConfigDict(frozen=True)

    organization_id: str
    role: OrganizationRole
    data_boundary_id: str


class EvidenceRecordCreate(BaseModel):
    """Input schema for registering traceable provenance evidence."""

    source_type: EvidenceSourceType
    source_url: str | None = None
    source_document: str | None = None
    source_organization: str
    publication_date: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    record_reference: str
    verification_status: VerificationStatus = VerificationStatus.AWAITING_EVIDENCE
    sha256_hash: str | None = None
    notes: str | None = None

    @field_validator("source_url")
    @classmethod
    def check_source_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value)

    @field_validator("source_organization")
    @classmethod
    def check_source_org(cls, value: str) -> str:
        return _validate_non_empty(value, "source_organization")

    @field_validator("record_reference")
    @classmethod
    def check_record_ref(cls, value: str) -> str:
        return _validate_non_empty(value, "record_reference")

    @model_validator(mode="after")
    def require_url_or_document(self) -> "EvidenceRecordCreate":
        doc = self.source_document.strip() if self.source_document else None
        self.source_document = doc or None
        if not self.source_url and not self.source_document:
            raise ValueError(
                "Evidence must include at least one traceable source_url or source_document."
            )
        return self


class EvidenceRecord(EvidenceRecordCreate):
    """Persisted evidence record."""

    id: str = Field(default_factory=lambda: new_id("ev"))


class OrganizationCreate(BaseModel):
    """Input schema for registering an independent organization."""

    name: str
    role: OrganizationRole
    connection_status: ConnectionStatus = ConnectionStatus.NOT_CONNECTED
    identity_verification_status: IdentityVerificationStatus = (
        IdentityVerificationStatus.UNVERIFIED
    )
    data_boundary_id: str
    source_url: str | None = None
    source_document: str | None = None
    source_organization: str
    retrieved_at: str = Field(default_factory=utc_now_iso)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _validate_non_empty(value, "name")

    @field_validator("data_boundary_id")
    @classmethod
    def check_boundary(cls, value: str) -> str:
        return _validate_non_empty(value, "data_boundary_id")

    @field_validator("source_organization")
    @classmethod
    def check_source_org(cls, value: str) -> str:
        return _validate_non_empty(value, "source_organization")

    @field_validator("source_url")
    @classmethod
    def check_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value)

    @model_validator(mode="after")
    def require_provenance(self) -> "OrganizationCreate":
        doc = self.source_document.strip() if self.source_document else None
        self.source_document = doc or None
        if not self.source_url and not self.source_document:
            raise ValueError(
                "Organization record requires a traceable source_url or source_document."
            )
        return self


class Organization(OrganizationCreate):
    """Persisted organization entity."""

    id: str = Field(default_factory=lambda: new_id("org"))
    created_at: str = Field(default_factory=utc_now_iso)


class RecallNoticeCreate(BaseModel):
    """Input schema for ingesting an official recall notice."""

    external_recall_number: str
    title: str
    issuing_authority: str
    product_description: str
    reason: str
    severity: RecallSeverity = RecallSeverity.UNKNOWN
    status: RecallStatus = RecallStatus.AWAITING_EVIDENCE
    manufacturer_org_id: str | None = None
    evidence_id: str
    published_at: str | None = None

    @field_validator(
        "external_recall_number",
        "title",
        "issuing_authority",
        "product_description",
        "reason",
        "evidence_id",
    )
    @classmethod
    def check_required_strings(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class RecallNotice(RecallNoticeCreate):
    """Persisted recall notice."""

    id: str = Field(default_factory=lambda: new_id("rec"))
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class AffectedBatchCreate(BaseModel):
    """Batch or lot record linked to an official recall notice."""

    recall_id: str
    batch_code: str
    product_name: str
    expiration_or_best_by: str | None = None
    reported_quantity: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    evidence_id: str

    @field_validator("recall_id", "batch_code", "product_name", "evidence_id")
    @classmethod
    def check_required(cls, value: str, info: Any) -> str:
        return _validate_non_empty(value, info.field_name)


class AffectedBatch(AffectedBatchCreate):
    """Persisted affected batch record."""

    id: str = Field(default_factory=lambda: new_id("bat"))


class ShipmentRecordCreate(BaseModel):
    """Shipment record owned by an organization's private data boundary."""

    recall_id: str
    batch_id: str
    from_org_id: str
    to_org_id: str
    owner_boundary_id: str
    quantity_shipped: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    shipped_at: str | None = None
    evidence_id: str

    @model_validator(mode="after")
    def distinct_orgs(self) -> "ShipmentRecordCreate":
        if self.from_org_id == self.to_org_id:
            raise ValueError("from_org_id and to_org_id must be distinct organizations.")
        return self


class ShipmentRecord(ShipmentRecordCreate):
    """Persisted shipment record."""

    id: str = Field(default_factory=lambda: new_id("shp"))


class InventoryRecordCreate(BaseModel):
    """Inventory record owned by a participating organization."""

    recall_id: str
    batch_id: str
    organization_id: str
    owner_boundary_id: str
    location_reference: str | None = None
    quantity_on_hand: int | None = Field(default=None, ge=0)
    quantity_quarantined: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    inventory_status: InventoryStatus = InventoryStatus.AWAITING_EVIDENCE
    evidence_id: str
    reported_at: str | None = None


class InventoryRecord(InventoryRecordCreate):
    """Persisted inventory record."""

    id: str = Field(default_factory=lambda: new_id("inv"))


class AcknowledgementCreate(BaseModel):
    """Organization recall acknowledgement record."""

    recall_id: str
    organization_id: str
    status: AcknowledgementStatus = AcknowledgementStatus.AWAITING_EVIDENCE
    action_taken: str | None = None
    acknowledged_at: str | None = None
    evidence_id: str | None = None

    @model_validator(mode="after")
    def require_evidence_when_acknowledged(self) -> "AcknowledgementCreate":
        if self.status == AcknowledgementStatus.ACKNOWLEDGED and not self.evidence_id:
            raise ValueError(
                "An ACKNOWLEDGED status requires a traceable evidence_id."
            )
        return self


class Acknowledgement(AcknowledgementCreate):
    """Persisted acknowledgement record."""

    id: str = Field(default_factory=lambda: new_id("ack"))


class VerificationFindingCreate(BaseModel):
    """Independent verification or discrepancy finding."""

    recall_id: str
    finding_type: FindingType
    status: VerificationStatus = VerificationStatus.AWAITING_EVIDENCE
    summary: str
    expected_quantity: int | None = Field(default=None, ge=0)
    reported_quantity: int | None = Field(default=None, ge=0)
    evidence_id: str | None = None

    @field_validator("summary")
    @classmethod
    def check_summary(cls, value: str) -> str:
        return _validate_non_empty(value, "summary")


class VerificationFinding(VerificationFindingCreate):
    """Persisted verification finding."""

    id: str = Field(default_factory=lambda: new_id("ver"))
    created_at: str = Field(default_factory=utc_now_iso)


class AgentMessageCreate(BaseModel):
    """Auditable agent-to-agent message record."""

    recall_id: str
    correlation_id: str
    sender_agent: str
    recipient_agent: str
    sender_boundary_id: str
    recipient_boundary_id: str
    message_type: AgentMessageType
    delivery_status: DeliveryStatus = DeliveryStatus.QUEUED
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence_id: str | None = None


class AgentMessage(AgentMessageCreate):
    """Persisted agent-to-agent message."""

    id: str = Field(default_factory=lambda: new_id("msg"))
    created_at: str = Field(default_factory=utc_now_iso)


class HumanApprovalCreate(BaseModel):
    """Human-in-the-loop approval record for consequential actions."""

    recall_id: str
    action_type: str
    decision: ApprovalDecision = ApprovalDecision.PENDING
    reviewer_name: str | None = None
    rationale: str | None = None
    decided_at: str | None = None

    @field_validator("action_type")
    @classmethod
    def check_action_type(cls, value: str) -> str:
        return _validate_non_empty(value, "action_type")


class HumanApproval(HumanApprovalCreate):
    """Persisted human approval record."""

    id: str = Field(default_factory=lambda: new_id("apr"))
    created_at: str = Field(default_factory=utc_now_iso)
