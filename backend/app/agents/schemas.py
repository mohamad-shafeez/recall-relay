"""Explicit input and output Pydantic schemas for all seven RecallRelay agents."""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field

from app.schemas.domain import (
    AffectedBatch,
    HumanApproval,
    InventoryRecord,
    ShipmentRecord,
    VerificationFinding,
    VerificationStatus,
)


# 1. Recall Source Analyst
class SourceAnalystInput(BaseModel):
    evidence_id: str
    source_text: str | None = None
    allow_external_ai_on_private_evidence: bool = False


class ExtractedClaim(BaseModel):
    field_name: str
    value: str
    source_evidence_id: str
    source_reference: str
    evidence_stage: str = "SOURCE_MATERIAL"
    verbatim_span: str | None = None
    verification_status: VerificationStatus = VerificationStatus.AWAITING_EVIDENCE


class SourceAnalystOutput(BaseModel):
    evidence_id: str
    source_reference: str
    extracted_claims: List[ExtractedClaim]
    missing_or_ambiguous_fields: List[str]
    evidence_verification_status: VerificationStatus
    execution_mode_used: str = "DETERMINISTIC"
    lyzr_status: str = "CONFIGURATION_MISSING"
    lyzr_error: str | None = None
    lyzr_analysis: dict[str, Any] | None = None
    deterministic_cross_check_preserved: bool = True
    notes: str


# 2. Recall Scope Agent
class RecallScopeInput(BaseModel):
    recall_id: str


class RecallScopeOutput(BaseModel):
    recall_id: str
    external_recall_number: str
    originating_evidence_id: str
    originating_evidence_status: VerificationStatus
    batches: List[AffectedBatch]
    unknown_quantity_batch_ids: List[str]
    unresolved_scope_gaps: List[str]
    scope_status: VerificationStatus


# 3. Manufacturer Operations Agent
class ManufacturerOpsInput(BaseModel):
    recall_id: str


class ManufacturerOpsOutput(BaseModel):
    recall_id: str
    manufacturer_org_id: str
    boundary_id: str
    affected_batch_ids: List[str]
    dispatches: List[ShipmentRecord]
    receiving_org_ids: List[str]
    missing_acknowledgement_org_ids: List[str]
    unknown_quantity_dispatch_ids: List[str]
    unresolved_gaps: List[str]


# 4. Distributor Reconciliation Agent
class DistributorReconciliationInput(BaseModel):
    recall_id: str
    expected_incoming_quantity: int | None = Field(default=None, ge=0)


class DistributorReconciliationOutput(BaseModel):
    recall_id: str
    distributor_org_id: str
    boundary_id: str
    outgoing_shipments: List[ShipmentRecord]
    total_known_outgoing_quantity: int | None
    expected_incoming_quantity: int | None
    reconciliation_status: VerificationStatus
    unresolved_relationships: List[str]


# 5. Retailer Inventory Agent
class RetailerInventoryInput(BaseModel):
    recall_id: str


class RetailerInventoryOutput(BaseModel):
    recall_id: str
    retailer_org_id: str
    boundary_id: str
    inventory_records: List[InventoryRecord]
    records_requiring_human_review: List[str]
    unknown_quantity_record_ids: List[str]
    assessment_status: VerificationStatus
    unresolved_gaps: List[str]


# 6. Independent Verification Agent
class ClaimToVerify(BaseModel):
    field_name: str
    claimed_value: str
    source_evidence_id: str


class IndependentVerificationInput(BaseModel):
    recall_id: str
    primary_evidence_id: str
    corroborating_evidence_id: str | None = None
    expected_quantity: int | None = Field(default=None, ge=0)
    reported_quantity: int | None = Field(default=None, ge=0)
    claims: List[ClaimToVerify] = Field(default_factory=list)
    source_contradictions: List[dict[str, Any]] = Field(default_factory=list)


class IndependentVerificationOutput(BaseModel):
    recall_id: str
    overall_status: VerificationStatus
    policy_satisfied_for_verified: bool
    contradictions_detected: List[str]
    missing_evidence_items: List[str]
    findings_created: List[VerificationFinding]


# 7. Recall Response Coordinator
class CoordinatorInput(BaseModel):
    recall_id: str
    propose_human_review_action: bool = True


class CoordinatorOutput(BaseModel):
    recall_id: str
    recommended_specialist_agents: List[str]
    recent_agent_execution_ids: List[str]
    total_batches: int
    acknowledged_org_count: int
    pending_or_missing_acknowledgements: int
    discrepancy_finding_count: int
    verified_finding_count: int
    proposed_approvals: List[HumanApproval]
    overall_status: VerificationStatus
    unresolved_items: List[str]
