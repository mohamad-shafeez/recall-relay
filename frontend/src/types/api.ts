import type {
  AffectedBatch,
  ApprovalDecision,
  ConnectionStatus,
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
} from "./domain";

export type EvidenceStage =
  | "SOURCE_MATERIAL"
  | "AI_EXTRACTED"
  | "INDEPENDENTLY_VERIFIED";

export type IdentityVerificationStatus =
  | "UNVERIFIED"
  | "PENDING_VERIFICATION"
  | "VERIFIED"
  | "REJECTED";

export type AgentRole =
  | "RECALL_SOURCE_ANALYST"
  | "RECALL_SCOPE_AGENT"
  | "MANUFACTURER_OPERATIONS_AGENT"
  | "DISTRIBUTOR_RECONCILIATION_AGENT"
  | "RETAILER_INVENTORY_AGENT"
  | "INDEPENDENT_VERIFICATION_AGENT"
  | "RECALL_RESPONSE_COORDINATOR";

export type A2AMessageType =
  | "TASK_HANDOFF"
  | "EVIDENCE_SHARE_NOTICE"
  | "RESULT_REPORT"
  | "ACK"
  | "ERROR_REPORT";

export type A2AMessageStatus =
  | "PENDING"
  | "DELIVERED"
  | "PROCESSING"
  | "PROCESSED"
  | "FAILED"
  | "EXPIRED";

export type TaskStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "FAILED"
  | "EXPIRED"
  | "BLOCKED_MISSING_EVIDENCE"
  | "BLOCKED_MISSING_AUTH";

export type SharingGrantStatus = "ACTIVE" | "REVOKED" | "EXPIRED";

export interface OrganizationRecord extends Organization {
  identity_verification_status: IdentityVerificationStatus;
}

export interface EvidenceItem {
  id: string;
  source_type: EvidenceSourceType;
  evidence_stage: EvidenceStage;
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

export interface EvidenceRegisterPayload {
  source_type: EvidenceSourceType;
  evidence_stage: "SOURCE_MATERIAL" | "AI_EXTRACTED";
  source_url?: string | null;
  source_document?: string | null;
  document_content?: string | null;
  source_organization: string;
  publication_date?: string | null;
  record_reference: string;
  verification_status?: "AWAITING_EVIDENCE" | "UNKNOWN";
  notes?: string | null;
}

export interface OrganizationRegisterPayload {
  name: string;
  role: OrganizationRole;
  connection_status: ConnectionStatus;
  source_url?: string | null;
  source_document?: string | null;
  source_organization: string;
}

export interface OrganizationWithCredential {
  organization: OrganizationRecord;
  access_token: string;
}

export interface RecallCreatePayload {
  external_recall_number: string;
  title: string;
  issuing_authority: string;
  product_description: string;
  reason: string;
  severity: RecallSeverity;
  status: RecallStatus;
  manufacturer_org_id?: string | null;
  evidence_id: string;
  published_at?: string | null;
}

export interface BatchCreatePayload {
  batch_code: string;
  product_name: string;
  expiration_or_best_by?: string | null;
  reported_quantity?: number | null;
  quantity_unit?: string | null;
  evidence_id: string;
}

export interface ShipmentCreatePayload {
  recall_id: string;
  batch_id: string;
  to_org_id: string;
  quantity_shipped?: number | null;
  quantity_unit?: string | null;
  shipped_at?: string | null;
  evidence_id: string;
}

export interface InventoryCreatePayload {
  recall_id: string;
  batch_id: string;
  location_reference?: string | null;
  quantity_on_hand?: number | null;
  quantity_quarantined?: number | null;
  quantity_unit?: string | null;
  inventory_status: InventoryStatus;
  evidence_id: string;
  reported_at?: string | null;
}

export interface TraceabilityGap {
  gap_type: string;
  status: VerificationStatus;
  message: string;
  related_id: string | null;
}

export interface TraceabilityData {
  recall: RecallNotice;
  originating_evidence: EvidenceItem;
  batches: AffectedBatch[];
  evidence_references: EvidenceItem[];
  caller_boundary_id: string | null;
  visible_shipments: ShipmentRecord[];
  visible_inventory: InventoryRecord[];
  total_recorded_shipments_in_system: number;
  total_recorded_inventory_in_system: number;
  missing_information: TraceabilityGap[];
  unresolved_relationships: TraceabilityGap[];
  traceability_status: VerificationStatus;
}

export interface AgentDescriptor {
  agent_id: string;
  role: AgentRole;
  execution_mode: string;
  allowed_tools: string[];
  requires_org_boundary: boolean;
}

export interface AgentExecutionAudit {
  id: string;
  agent_id: string;
  agent_role: string;
  authorized_boundary_id: string | null;
  idempotency_key: string | null;
  idempotency_scope_hash?: string | null;
  input_references: string[];
  output_references: string[];
  evidence_references: string[];
  execution_status: string;
  started_at: string;
  completed_at: string | null;
  error_message: string | null;
}

export interface AgentExecutionEnvelope {
  execution_id: string;
  agent_id: string;
  agent_role: AgentRole;
  execution_mode: string;
  execution_status: string;
  reused_idempotent_result: boolean;
  authorized_boundary_id: string | null;
  output: Record<string, unknown> | null;
  evidence_references: string[];
  error_message: string | null;
  audit: AgentExecutionAudit;
}

export interface A2AMessage {
  id: string;
  schema_version: string;
  conversation_id: string;
  task_id: string | null;
  recall_id: string;
  sender_agent: AgentRole;
  recipient_agent: AgentRole;
  sender_boundary_id: string | null;
  recipient_boundary_id: string | null;
  message_type: A2AMessageType;
  payload: Record<string, unknown>;
  evidence_references: string[];
  idempotency_key: string | null;
  status: A2AMessageStatus;
  attempt_count: number;
  max_attempts: number;
  claimed_by: string | null;
  claimed_at: string | null;
  delivered_at: string | null;
  acknowledged_at: string | null;
  processed_at: string | null;
  expires_at: string | null;
  error_message: string | null;
  created_at: string;
}

export interface AgentTask {
  id: string;
  parent_task_id: string | null;
  recall_id: string;
  conversation_id: string;
  assigned_agent: AgentRole;
  requested_action: string;
  input_evidence_references: string[];
  required_output_schema: string;
  status: TaskStatus;
  authorized_boundary_id: string | null;
  depends_on_task_ids: string[];
  retry_count: number;
  max_retries: number;
  deadline_at: string | null;
  result_reference: string | null;
  failure_reason: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface HandoffDispatchPayload {
  recall_id: string;
  conversation_id: string;
  sender_agent: AgentRole;
  recipient_agent: AgentRole;
  requested_action: string;
  required_output_schema: string;
  payload: Record<string, unknown>;
  evidence_references: string[];
  idempotency_key?: string | null;
}

export interface HandoffDispatchResponse {
  task: AgentTask;
  message: A2AMessage;
}

export interface SharingGrant {
  id: string;
  recall_id: string;
  owner_org_id: string;
  owner_boundary_id: string;
  recipient_org_id: string;
  recipient_boundary_id: string;
  allowed_fields: string[];
  allowed_evidence_ids: string[];
  purpose: string;
  authorized_by: string;
  status: SharingGrantStatus;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface SharingGrantCreatePayload {
  recall_id: string;
  recipient_org_id: string;
  allowed_fields: string[];
  allowed_evidence_ids: string[];
  purpose: string;
  authorized_by: string;
  expires_at?: string | null;
}

export interface HumanApprovalRecord {
  id: string;
  recall_id: string;
  action_type: string;
  decision: ApprovalDecision;
  proposing_agent_id: string | null;
  reviewer_name: string | null;
  rationale: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface HumanApprovalDecisionPayload {
  decision: "APPROVED" | "REJECTED";
  reviewer_name: string;
  rationale: string;
}

export interface OrgIdentityReviewPayload {
  target_status: IdentityVerificationStatus;
  reviewer_name: string;
  supporting_evidence_id?: string | null;
  notes?: string | null;
}

export interface AuditEvent {
  seq: number;
  id: string;
  event_type: string;
  actor_id: string;
  boundary_id: string | null;
  entity_type: string;
  entity_id: string;
  metadata: Record<string, unknown>;
  created_at: string;
  prev_hash: string;
  event_hash: string;
}

export interface AuditChainVerification {
  valid: boolean;
  event_count: number;
  broken_at_seq: number | null;
}

export type AuthMode = "public" | "admin" | "organization";

export interface SessionStatusResponse {
  authenticated: boolean;
  mode: AuthMode;
  organization_id: string | null;
  organization_role: string | null;
  data_boundary_id: string | null;
  csrf_token: string | null;
  expires_at: string | null;
}

export interface SessionCredentials {
  mode: AuthMode;
  csrfToken: string | null;
  organizationId: string | null;
  organizationRole: string | null;
  dataBoundaryId: string | null;
  expiresAt: string | null;
  label: string;
}

export interface SponsorVerificationEvidence {
  id: string;
  sponsor_name: string;
  operation_type: string;
  external_reference_id: string | null;
  sender_identity: string | null;
  receiver_identity: string | null;
  summary: string;
  verified_at: string;
}

export interface SponsorStatusReport {
  sponsor_name: string;
  capability: string;
  official_docs_url: string;
  free_access_summary: string;
  card_or_wallet_required: boolean;
  implementation_status: string;
  connection_state: string;
  configured: boolean;
  endpoint_host: string | null;
  last_checked_at: string | null;
  last_successful_live_operation?: SponsorVerificationEvidence | null;
  message: string;
}

export interface IntegrationOverviewResponse {
  configured_a2a_transport?: string;
  active_a2a_transport: string;
  band_real_message_succeeded?: boolean;
  lyzr_real_invocation_succeeded?: boolean;
  agent_execution_mode: string;
  sponsors: SponsorStatusReport[];
}

export interface IntegrationUsageLog {
  id: string;
  sponsor_name: string;
  operation: string;
  actor_id: string;
  boundary_id: string | null;
  external_reference_id: string | null;
  status: string;
  error_summary: string | null;
  created_at: string;
}


export interface OrchestrationStepRecord {
  id: string;
  orchestration_id: string;
  step_order: number;
  step_key: string;
  title: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  evidence_references: string[];
  details: Record<string, unknown>;
}

export interface OrchestrationRunResponse {
  id: string;
  recall_id: string;
  idempotency_key: string | null;
  status: string;
  active_transport: string;
  lyzr_used: boolean;
  band_used: boolean;
  reused_idempotent_run: boolean;
  summary: Record<string, unknown>;
  created_at: string;
  completed_at: string | null;
  steps: OrchestrationStepRecord[];
}
