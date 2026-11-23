"""Versioned API v1 request and response schemas for Phases 3 and 4."""

from __future__ import annotations

from enum import Enum
from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.domain import (
    AffectedBatch,
    ConnectionStatus,
    EvidenceRecord,
    EvidenceSourceType,
    InventoryRecord,
    InventoryStatus,
    Organization,
    OrganizationRole,
    RecallNotice,
    RecallSeverity,
    RecallStatus,
    ShipmentRecord,
    VerificationStatus,
    _validate_http_url,
    _validate_non_empty,
    utc_now_iso,
)


class EvidenceStage(str, Enum):
    """Explicitly separates raw source material, AI extraction, and independent verification."""

    SOURCE_MATERIAL = "SOURCE_MATERIAL"
    AI_EXTRACTED = "AI_EXTRACTED"
    INDEPENDENTLY_VERIFIED = "INDEPENDENTLY_VERIFIED"


class EvidenceRegisterRequest(BaseModel):
    """Request payload for registering traceable source evidence.

    Ordinary API clients cannot self-declare INDEPENDENTLY_VERIFIED or VERIFIED status.
    """

    source_type: EvidenceSourceType
    evidence_stage: EvidenceStage = EvidenceStage.SOURCE_MATERIAL
    source_url: str | None = None
    source_document: str | None = None
    document_content: str | None = None
    source_organization: str
    publication_date: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    record_reference: str
    verification_status: VerificationStatus = VerificationStatus.AWAITING_EVIDENCE
    sha256_hash: str | None = None
    notes: str | None = None

    @field_validator("source_url")
    @classmethod
    def check_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value)

    @field_validator("source_organization", "record_reference")
    @classmethod
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)

    @model_validator(mode="after")
    def enforce_provenance_and_stage_integrity(self) -> "EvidenceRegisterRequest":
        doc = self.source_document.strip() if self.source_document else None
        self.source_document = doc or None
        if not self.source_url and not self.source_document:
            raise ValueError(
                "Evidence must include at least one traceable source_url or source_document."
            )
        if self.evidence_stage == EvidenceStage.INDEPENDENTLY_VERIFIED:
            raise ValueError(
                "Clients cannot self-declare INDEPENDENTLY_VERIFIED; only the authorized verification procedure may verify evidence."
            )
        if self.verification_status == VerificationStatus.VERIFIED:
            raise ValueError(
                "Registering evidence cannot self-declare VERIFIED status; use AWAITING_EVIDENCE until independently verified."
            )
        return self


class EvidenceResponse(EvidenceRecord):
    """Persisted evidence response including evidence_stage."""

    evidence_stage: EvidenceStage = EvidenceStage.SOURCE_MATERIAL


class OrganizationRegisterRequest(BaseModel):
    """Request payload for registering an independent organization.

    Newly registered organizations always start with identity_verification_status=UNVERIFIED.
    """

    name: str
    role: OrganizationRole
    connection_status: ConnectionStatus = ConnectionStatus.NOT_CONNECTED
    source_url: str | None = None
    source_document: str | None = None
    source_organization: str
    retrieved_at: str = Field(default_factory=utc_now_iso)

    @field_validator("name", "source_organization")
    @classmethod
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)

    @field_validator("source_url")
    @classmethod
    def check_url(cls, value: str | None) -> str | None:
        return _validate_http_url(value)

    @model_validator(mode="after")
    def require_provenance(self) -> "OrganizationRegisterRequest":
        doc = self.source_document.strip() if self.source_document else None
        self.source_document = doc or None
        if not self.source_url and not self.source_document:
            raise ValueError(
                "Organization registration requires a traceable source_url or source_document."
            )
        return self


class OrganizationWithCredentialResponse(BaseModel):
    """Returned once upon organization registration with its server-issued bearer token."""

    organization: Organization
    access_token: str


class RecallCreateRequest(BaseModel):
    """Request payload for creating a recall notice anchored to traceable evidence."""

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
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)


class BatchCreateRequest(BaseModel):
    """Request payload for associating an affected batch with a recall notice."""

    batch_code: str
    product_name: str
    expiration_or_best_by: str | None = None
    reported_quantity: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    evidence_id: str

    @field_validator("batch_code", "product_name", "evidence_id")
    @classmethod
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)


class ShipmentCreateRequest(BaseModel):
    """Request payload for registering a shipment within the authenticated org's boundary."""

    recall_id: str
    batch_id: str
    to_org_id: str
    quantity_shipped: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    shipped_at: str | None = None
    evidence_id: str

    @field_validator("recall_id", "batch_id", "to_org_id", "evidence_id")
    @classmethod
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)


class InventoryCreateRequest(BaseModel):
    """Request payload for registering inventory within the authenticated org's boundary."""

    recall_id: str
    batch_id: str
    location_reference: str | None = None
    quantity_on_hand: int | None = Field(default=None, ge=0)
    quantity_quarantined: int | None = Field(default=None, ge=0)
    quantity_unit: str | None = None
    inventory_status: InventoryStatus = InventoryStatus.AWAITING_EVIDENCE
    evidence_id: str
    reported_at: str | None = None

    @field_validator("recall_id", "batch_id", "evidence_id")
    @classmethod
    def check_required(cls, value: str, info) -> str:
        return _validate_non_empty(value, info.field_name)


class TraceabilityGap(BaseModel):
    """Explicitly surfaced missing information or unresolved relationship."""

    gap_type: str
    status: VerificationStatus
    message: str
    related_id: str | None = None


class TraceabilityResponse(BaseModel):
    """End-to-end recall traceability view honoring organization data boundaries."""

    recall: RecallNotice
    originating_evidence: EvidenceResponse
    batches: List[AffectedBatch]
    evidence_references: List[EvidenceResponse]
    caller_boundary_id: str | None
    visible_shipments: List[ShipmentRecord]
    visible_inventory: List[InventoryRecord]
    total_recorded_shipments_in_system: int
    total_recorded_inventory_in_system: int
    missing_information: List[TraceabilityGap]
    unresolved_relationships: List[TraceabilityGap]
    traceability_status: VerificationStatus
