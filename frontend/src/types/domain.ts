export type OrganizationRole =
  | "MANUFACTURER"
  | "DISTRIBUTOR"
  | "RETAILER"
  | "REGULATOR"
  | "COORDINATOR";

export type ConnectionStatus =
  | "CONNECTED"
  | "NOT_CONNECTED"
  | "AWAITING_AUTHORIZATION";

export type EvidenceSourceType =
  | "OFFICIAL_PUBLIC_NOTICE"
  | "PUBLIC_DATASET"
  | "AUTHORIZED_ORG_UPLOAD"
  | "AGENT_ATTESTATION";

export type VerificationStatus =
  | "VERIFIED"
  | "AWAITING_EVIDENCE"
  | "UNKNOWN"
  | "DISCREPANCY"
  | "NOT_CONNECTED";

export type RecallSeverity =
  | "CLASS_I"
  | "CLASS_II"
  | "CLASS_III"
  | "HIGH"
  | "MEDIUM"
  | "LOW"
  | "UNKNOWN";

export type RecallStatus =
  | "INITIATED"
  | "IN_PROGRESS"
  | "AWAITING_EVIDENCE"
  | "AWAITING_APPROVAL"
  | "CLOSED"
  | "UNKNOWN";

export type InventoryStatus =
  | "QUARANTINE_REQUIRED"
  | "QUARANTINED"
  | "NO_AFFECTED_STOCK"
  | "AWAITING_EVIDENCE"
  | "UNKNOWN"
  | "NOT_CONNECTED";

export type AcknowledgementStatus =
  | "ACKNOWLEDGED"
  | "AWAITING_EVIDENCE"
  | "NOT_CONNECTED"
  | "DECLINED"
  | "UNKNOWN";

export type FindingType =
  | "QUANTITY_RECONCILIATION"
  | "CHAIN_COVERAGE"
  | "ACKNOWLEDGEMENT_GAP"
  | "EVIDENCE_MISSING"
  | "DISCREPANCY";

export type AgentMessageType =
  | "RECALL_ALERT"
  | "TRACE_REQUEST"
  | "TRACE_RESPONSE"
  | "INVENTORY_REQUEST"
  | "INVENTORY_RESPONSE"
  | "VERIFICATION_REPORT"
  | "APPROVAL_REQUEST"
  | "ACK";

export type DeliveryStatus =
  | "QUEUED"
  | "DELIVERED"
  | "ACKNOWLEDGED"
  | "FAILED"
  | "NOT_CONNECTED";

export type ApprovalDecision = "PENDING" | "APPROVED" | "REJECTED";

export interface EvidenceRecord {
  id: string;
  source_type: EvidenceSourceType;
  source_url: string | null;
  source_document: string | null;
  source_organization: string;
  publication_date: string | null;
  retrieved_at: string;
  record_reference: string;
  verification_status: VerificationStatus;
  sha256_hash: string | null;
  notes: string | null;
}

export interface Organization {
  id: string;
  name: string;
  role: OrganizationRole;
  connection_status: ConnectionStatus;
  data_boundary_id: string;
  source_url: string | null;
  source_document: string | null;
  source_organization: string;
  retrieved_at: string;
  created_at: string;
}

export interface RecallNotice {
  id: string;
  external_recall_number: string;
  title: string;
  issuing_authority: string;
  product_description: string;
  reason: string;
  severity: RecallSeverity;
  status: RecallStatus;
  manufacturer_org_id: string | null;
  evidence_id: string;
  published_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AffectedBatch {
  id: string;
  recall_id: string;
  batch_code: string;
  product_name: string;
  expiration_or_best_by: string | null;
  reported_quantity: number | null;
  quantity_unit: string | null;
  evidence_id: string;
}

export interface VerificationFinding {
  id: string;
  recall_id: string;
  finding_type: FindingType;
  status: VerificationStatus;
  summary: string;
  expected_quantity: number | null;
  reported_quantity: number | null;
  evidence_id: string | null;
  created_at: string;
}

export interface HumanApproval {
  id: string;
  recall_id: string;
  action_type: string;
  decision: ApprovalDecision;
  reviewer_name: string | null;
  rationale: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface ShipmentRecord {
  id: string;
  recall_id: string;
  batch_id: string;
  from_org_id: string;
  to_org_id: string;
  owner_boundary_id: string;
  quantity_shipped: number | null;
  quantity_unit: string | null;
  shipped_at: string | null;
  evidence_id: string;
}

export interface InventoryRecord {
  id: string;
  recall_id: string;
  batch_id: string;
  organization_id: string;
  owner_boundary_id: string;
  location_reference: string | null;
  quantity_on_hand: number | null;
  quantity_quarantined: number | null;
  quantity_unit: string | null;
  inventory_status: InventoryStatus;
  evidence_id: string;
  reported_at: string | null;
}
