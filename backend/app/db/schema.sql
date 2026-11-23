-- RecallRelay Phase 2 Multi-Organization Relational Schema (SQLite)
-- Enforces foreign keys, traceable evidence provenance, and explicit unknown/unconnected states.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    role TEXT NOT NULL CHECK (
        role IN ('MANUFACTURER', 'DISTRIBUTOR', 'RETAILER', 'REGULATOR', 'COORDINATOR')
    ),
    connection_status TEXT NOT NULL DEFAULT 'NOT_CONNECTED' CHECK (
        connection_status IN ('CONNECTED', 'NOT_CONNECTED', 'AWAITING_AUTHORIZATION')
    ),
    data_boundary_id TEXT NOT NULL UNIQUE CHECK (length(trim(data_boundary_id)) > 0),
    source_url TEXT,
    source_document TEXT,
    source_organization TEXT NOT NULL CHECK (length(trim(source_organization)) > 0),
    retrieved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (source_url IS NOT NULL OR source_document IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (
        source_type IN (
            'OFFICIAL_PUBLIC_NOTICE',
            'PUBLIC_DATASET',
            'AUTHORIZED_ORG_UPLOAD',
            'AGENT_ATTESTATION'
        )
    ),
    source_url TEXT,
    source_document TEXT,
    source_organization TEXT NOT NULL CHECK (length(trim(source_organization)) > 0),
    publication_date TEXT,
    retrieved_at TEXT NOT NULL,
    record_reference TEXT NOT NULL CHECK (length(trim(record_reference)) > 0),
    verification_status TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE' CHECK (
        verification_status IN (
            'VERIFIED',
            'AWAITING_EVIDENCE',
            'UNKNOWN',
            'DISCREPANCY',
            'NOT_CONNECTED'
        )
    ),
    sha256_hash TEXT,
    notes TEXT,
    CHECK (source_url IS NOT NULL OR source_document IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS recall_notices (
    id TEXT PRIMARY KEY,
    external_recall_number TEXT NOT NULL UNIQUE CHECK (length(trim(external_recall_number)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    issuing_authority TEXT NOT NULL CHECK (length(trim(issuing_authority)) > 0),
    product_description TEXT NOT NULL CHECK (length(trim(product_description)) > 0),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    severity TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
        severity IN ('CLASS_I', 'CLASS_II', 'CLASS_III', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN')
    ),
    status TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE' CHECK (
        status IN (
            'INITIATED',
            'IN_PROGRESS',
            'AWAITING_EVIDENCE',
            'AWAITING_APPROVAL',
            'CLOSED',
            'UNKNOWN'
        )
    ),
    manufacturer_org_id TEXT REFERENCES organizations(id) ON DELETE SET NULL,
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    published_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affected_batches (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    batch_code TEXT NOT NULL CHECK (length(trim(batch_code)) > 0),
    product_name TEXT NOT NULL CHECK (length(trim(product_name)) > 0),
    expiration_or_best_by TEXT,
    reported_quantity INTEGER CHECK (reported_quantity IS NULL OR reported_quantity >= 0),
    quantity_unit TEXT,
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    UNIQUE (recall_id, batch_code)
);

CREATE TABLE IF NOT EXISTS shipment_records (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES affected_batches(id) ON DELETE CASCADE,
    from_org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    to_org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    owner_boundary_id TEXT NOT NULL CHECK (length(trim(owner_boundary_id)) > 0),
    quantity_shipped INTEGER CHECK (quantity_shipped IS NULL OR quantity_shipped >= 0),
    quantity_unit TEXT,
    shipped_at TEXT,
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    CHECK (from_org_id <> to_org_id)
);

CREATE TABLE IF NOT EXISTS inventory_records (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL REFERENCES affected_batches(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    owner_boundary_id TEXT NOT NULL CHECK (length(trim(owner_boundary_id)) > 0),
    location_reference TEXT,
    quantity_on_hand INTEGER CHECK (quantity_on_hand IS NULL OR quantity_on_hand >= 0),
    quantity_quarantined INTEGER CHECK (quantity_quarantined IS NULL OR quantity_quarantined >= 0),
    quantity_unit TEXT,
    inventory_status TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE' CHECK (
        inventory_status IN (
            'QUARANTINE_REQUIRED',
            'QUARANTINED',
            'NO_AFFECTED_STOCK',
            'AWAITING_EVIDENCE',
            'UNKNOWN',
            'NOT_CONNECTED'
        )
    ),
    evidence_id TEXT NOT NULL REFERENCES evidence_records(id) ON DELETE RESTRICT,
    reported_at TEXT
);

CREATE TABLE IF NOT EXISTS acknowledgements (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE' CHECK (
        status IN ('ACKNOWLEDGED', 'AWAITING_EVIDENCE', 'NOT_CONNECTED', 'DECLINED', 'UNKNOWN')
    ),
    action_taken TEXT,
    acknowledged_at TEXT,
    evidence_id TEXT REFERENCES evidence_records(id) ON DELETE RESTRICT,
    UNIQUE (recall_id, organization_id),
    CHECK (status <> 'ACKNOWLEDGED' OR evidence_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS verification_findings (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    finding_type TEXT NOT NULL CHECK (
        finding_type IN (
            'QUANTITY_RECONCILIATION',
            'CHAIN_COVERAGE',
            'ACKNOWLEDGEMENT_GAP',
            'EVIDENCE_MISSING',
            'DISCREPANCY'
        )
    ),
    status TEXT NOT NULL DEFAULT 'AWAITING_EVIDENCE' CHECK (
        status IN ('VERIFIED', 'AWAITING_EVIDENCE', 'DISCREPANCY', 'UNKNOWN', 'NOT_CONNECTED')
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    expected_quantity INTEGER CHECK (expected_quantity IS NULL OR expected_quantity >= 0),
    reported_quantity INTEGER CHECK (reported_quantity IS NULL OR reported_quantity >= 0),
    evidence_id TEXT REFERENCES evidence_records(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    correlation_id TEXT NOT NULL CHECK (length(trim(correlation_id)) > 0),
    sender_agent TEXT NOT NULL CHECK (length(trim(sender_agent)) > 0),
    recipient_agent TEXT NOT NULL CHECK (length(trim(recipient_agent)) > 0),
    sender_boundary_id TEXT NOT NULL CHECK (length(trim(sender_boundary_id)) > 0),
    recipient_boundary_id TEXT NOT NULL CHECK (length(trim(recipient_boundary_id)) > 0),
    message_type TEXT NOT NULL CHECK (
        message_type IN (
            'RECALL_ALERT',
            'TRACE_REQUEST',
            'TRACE_RESPONSE',
            'INVENTORY_REQUEST',
            'INVENTORY_RESPONSE',
            'VERIFICATION_REPORT',
            'APPROVAL_REQUEST',
            'ACK'
        )
    ),
    delivery_status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (
        delivery_status IN ('QUEUED', 'DELIVERED', 'ACKNOWLEDGED', 'FAILED', 'NOT_CONNECTED')
    ),
    payload_json TEXT NOT NULL,
    evidence_id TEXT REFERENCES evidence_records(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_approvals (
    id TEXT PRIMARY KEY,
    recall_id TEXT NOT NULL REFERENCES recall_notices(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL CHECK (length(trim(action_type)) > 0),
    decision TEXT NOT NULL DEFAULT 'PENDING' CHECK (
        decision IN ('PENDING', 'APPROVED', 'REJECTED')
    ),
    reviewer_name TEXT,
    rationale TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recall_notices_status ON recall_notices(status);
CREATE INDEX IF NOT EXISTS idx_affected_batches_recall ON affected_batches(recall_id);
CREATE INDEX IF NOT EXISTS idx_shipment_records_recall_boundary ON shipment_records(recall_id, owner_boundary_id);
CREATE INDEX IF NOT EXISTS idx_inventory_records_recall_boundary ON inventory_records(recall_id, owner_boundary_id);
CREATE INDEX IF NOT EXISTS idx_acknowledgements_recall ON acknowledgements(recall_id);
CREATE INDEX IF NOT EXISTS idx_verification_findings_recall ON verification_findings(recall_id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_recall_corr ON agent_messages(recall_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_human_approvals_recall ON human_approvals(recall_id);
