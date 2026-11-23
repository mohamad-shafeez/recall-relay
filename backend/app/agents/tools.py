"""Backend-enforced tool permissions and service-layer tool registry.

An agent can NEVER invoke a tool outside its server-defined ROLE_TOOL_PERMISSIONS
matrix, and boundary-scoped tools verify OrganizationRole on the server.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, FrozenSet, List

from app.agents.audit import list_execution_audits
from app.agents.base import (
    AgentAuthContext,
    AgentBoundaryError,
    AgentRole,
    ToolPermissionError,
)
from app.communication.audit_trail import record_audit_event
from app.db.database import get_db_connection
from app.schemas.domain import (
    Acknowledgement,
    AcknowledgementStatus,
    ApprovalDecision,
    FindingType,
    HumanApproval,
    OrganizationRole,
    VerificationFinding,
    VerificationStatus,
    new_id,
    utc_now_iso,
)
from app.services import (
    evidence_service,
    organization_service,
    recall_service,
    supply_chain_service,
)

# Server-enforced permission matrix: changing request payload or labels cannot bypass this.
ROLE_TOOL_PERMISSIONS: Dict[AgentRole, FrozenSet[str]] = {
    AgentRole.RECALL_SOURCE_ANALYST: frozenset(
        {
            "read_evidence_record",
            "extract_structured_claims_deterministic",
        }
    ),
    AgentRole.RECALL_SCOPE_AGENT: frozenset(
        {
            "read_recall_notice",
            "read_evidence_record",
            "list_recall_batches",
        }
    ),
    AgentRole.MANUFACTURER_OPERATIONS_AGENT: frozenset(
        {
            "read_recall_notice",
            "list_recall_batches",
            "read_manufacturer_shipments",
            "read_recall_acknowledgements",
        }
    ),
    AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT: frozenset(
        {
            "read_recall_notice",
            "list_recall_batches",
            "read_distributor_shipments",
        }
    ),
    AgentRole.RETAILER_INVENTORY_AGENT: frozenset(
        {
            "read_recall_notice",
            "list_recall_batches",
            "read_retailer_inventory",
        }
    ),
    AgentRole.INDEPENDENT_VERIFICATION_AGENT: frozenset(
        {
            "read_evidence_record",
            "read_recall_notice",
            "list_recall_batches",
            "create_verification_finding",
            "read_verification_findings",
            "read_organization_metadata",
        }
    ),
    AgentRole.RECALL_RESPONSE_COORDINATOR: frozenset(
        {
            "read_recall_notice",
            "list_recall_batches",
            "read_recall_acknowledgements",
            "read_verification_findings",
            "list_agent_executions",
            "propose_human_approval",
        }
    ),
}


def _require_org_role(
    auth: AgentAuthContext, required_role: OrganizationRole
) -> None:
    if auth.boundary is None:
        raise AgentBoundaryError(
            f"Authenticated {required_role.value} organization boundary is required."
        )
    if auth.boundary.role != required_role:
        raise AgentBoundaryError(
            f"Organization role '{auth.boundary.role.value}' is not authorized; "
            f"'{required_role.value}' boundary required."
        )


def _tool_read_evidence_record(
    *, db_path: str | None, auth: AgentAuthContext, evidence_id: str
):
    if not auth.is_admin and auth.boundary is None:
        raise AgentBoundaryError("Authorized admin or organization context is required to read evidence.")
    return evidence_service.get_evidence_by_id(db_path, evidence_id)


def _tool_extract_structured_claims_deterministic(
    *, db_path: str | None, auth: AgentAuthContext, source_text: str, evidence_id: str
) -> dict[str, Any]:
    """Deterministic pattern extractor — extracts ONLY explicitly present labeled fields."""
    _ = db_path, auth
    patterns = {
        "recall_number": r"(?:Recall\s*(?:Number|No\.?|ID))\s*[:#-]\s*([A-Za-z0-9\-_/]+)",
        "product_name": r"(?:Product(?:\s*Name)?)\s*[:\-]\s*([^\n;]+)",
        "batch_codes": r"(?:Batch|Lot)(?:\s*(?:Code|Number|ID))?\s*[:#-]\s*([A-Za-z0-9\-_]+)",
        "hazard": r"(?:Hazard|Reason)\s*[:\-]\s*([^\n;]+)",
        "publication_date": r"(?:Date|Published)\s*[:\-]\s*(\d{4}-\d{2}-\d{2})",
    }
    recall_match = re.search(patterns["recall_number"], source_text, re.IGNORECASE)
    product_match = re.search(patterns["product_name"], source_text, re.IGNORECASE)
    batch_matches = re.findall(patterns["batch_codes"], source_text, re.IGNORECASE)
    hazard_match = re.search(patterns["hazard"], source_text, re.IGNORECASE)
    date_match = re.search(patterns["publication_date"], source_text, re.IGNORECASE)

    return {
        "evidence_id": evidence_id,
        "recall_number": recall_match.group(1).strip() if recall_match else None,
        "product_name": product_match.group(1).strip() if product_match else None,
        "batch_codes": sorted(set(b.strip() for b in batch_matches if b.strip())),
        "hazard_description": hazard_match.group(1).strip() if hazard_match else None,
        "publication_date": date_match.group(1).strip() if date_match else None,
    }


def _tool_read_recall_notice(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
):
    _ = auth
    return recall_service.get_recall_by_id(db_path, recall_id)


def _tool_list_recall_batches(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
):
    _ = auth
    return recall_service.list_batches_for_recall(db_path, recall_id)


def _tool_read_organization_metadata(
    *, db_path: str | None, auth: AgentAuthContext, organization_id: str
):
    if not auth.is_admin and auth.boundary is None:
        raise AgentBoundaryError("Authorized context required to inspect organization metadata.")
    return organization_service.get_organization_by_id(db_path, organization_id)


def _tool_read_manufacturer_shipments(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
):
    _require_org_role(auth, OrganizationRole.MANUFACTURER)
    assert auth.boundary is not None
    return supply_chain_service.list_shipments_for_boundary(
        db_path, auth.boundary, recall_id=recall_id
    )


def _tool_read_distributor_shipments(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
):
    _require_org_role(auth, OrganizationRole.DISTRIBUTOR)
    assert auth.boundary is not None
    return supply_chain_service.list_shipments_for_boundary(
        db_path, auth.boundary, recall_id=recall_id
    )


def _tool_read_retailer_inventory(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
):
    _require_org_role(auth, OrganizationRole.RETAILER)
    assert auth.boundary is not None
    return supply_chain_service.list_inventory_for_boundary(
        db_path, auth.boundary, recall_id=recall_id
    )


def _tool_read_recall_acknowledgements(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
) -> List[Acknowledgement]:
    _ = auth
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM acknowledgements WHERE recall_id = ? ORDER BY id ASC",
            (recall_id,),
        ).fetchall()
    return [
        Acknowledgement(
            id=row["id"],
            recall_id=row["recall_id"],
            organization_id=row["organization_id"],
            status=AcknowledgementStatus(row["status"]),
            action_taken=row["action_taken"],
            acknowledged_at=row["acknowledged_at"],
            evidence_id=row["evidence_id"],
        )
        for row in rows
    ]


def _tool_create_verification_finding(
    *,
    db_path: str | None,
    auth: AgentAuthContext,
    recall_id: str,
    finding_type: FindingType,
    status: VerificationStatus,
    summary: str,
    expected_quantity: int | None = None,
    reported_quantity: int | None = None,
    evidence_id: str | None = None,
) -> VerificationFinding:
    normalized_summary = summary.strip()
    with get_db_connection(db_path) as conn:
        existing = conn.execute(
            """
            SELECT * FROM verification_findings
            WHERE recall_id = ?
              AND finding_type = ?
              AND summary = ?
              AND ((evidence_id IS NULL AND ? IS NULL) OR evidence_id = ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (recall_id, finding_type.value, normalized_summary, evidence_id, evidence_id),
        ).fetchone()
        if existing is not None:
            return VerificationFinding(
                id=existing["id"],
                recall_id=existing["recall_id"],
                finding_type=FindingType(existing["finding_type"]),
                status=VerificationStatus(existing["status"]),
                summary=existing["summary"],
                expected_quantity=existing["expected_quantity"],
                reported_quantity=existing["reported_quantity"],
                evidence_id=existing["evidence_id"],
                created_at=existing["created_at"],
            )

        finding_id = new_id("ver")
        created_at = utc_now_iso()
        conn.execute(
            """
            INSERT INTO verification_findings (
                id, recall_id, finding_type, status, summary,
                expected_quantity, reported_quantity, evidence_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                finding_id,
                recall_id,
                finding_type.value,
                status.value,
                normalized_summary,
                expected_quantity,
                reported_quantity,
                evidence_id,
                created_at,
            ),
        )

    actor_id = (
        "agent_independent_verification:admin"
        if auth.is_admin
        else f"agent_independent_verification:{auth.boundary.organization_id if auth.boundary else 'system'}"
    )
    boundary_id = auth.boundary.data_boundary_id if auth.boundary else None
    record_audit_event(
        db_path,
        event_type="VERIFICATION_FINDING_CREATED",
        actor_id=actor_id,
        boundary_id=boundary_id,
        entity_type="verification_finding",
        entity_id=finding_id,
        metadata={
            "recall_id": recall_id,
            "finding_type": finding_type.value,
            "status": status.value,
            "evidence_id": evidence_id,
        },
    )
    return VerificationFinding(
        id=finding_id,
        recall_id=recall_id,
        finding_type=finding_type,
        status=status,
        summary=normalized_summary,
        expected_quantity=expected_quantity,
        reported_quantity=reported_quantity,
        evidence_id=evidence_id,
        created_at=created_at,
    )


def _tool_read_verification_findings(
    *, db_path: str | None, auth: AgentAuthContext, recall_id: str
) -> List[VerificationFinding]:
    _ = auth
    with get_db_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM verification_findings WHERE recall_id = ? ORDER BY created_at DESC, id ASC",
            (recall_id,),
        ).fetchall()
    return [
        VerificationFinding(
            id=row["id"],
            recall_id=row["recall_id"],
            finding_type=FindingType(row["finding_type"]),
            status=VerificationStatus(row["status"]),
            summary=row["summary"],
            expected_quantity=row["expected_quantity"],
            reported_quantity=row["reported_quantity"],
            evidence_id=row["evidence_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def _tool_list_agent_executions(
    *, db_path: str | None, auth: AgentAuthContext, agent_role: str | None = None
):
    _ = auth
    return list_execution_audits(db_path, agent_role=agent_role)


def _tool_propose_human_approval(
    *,
    db_path: str | None,
    auth: AgentAuthContext,
    recall_id: str,
    action_type: str,
    rationale: str,
) -> HumanApproval:
    """Create a strictly PENDING human_approval proposal; agents can NEVER approve actions."""
    _ = auth
    approval_id = new_id("apr")
    created_at = utc_now_iso()
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO human_approvals (
                id, recall_id, action_type, decision, proposing_agent_id, reviewer_name,
                rationale, decided_at, created_at
            ) VALUES (?, ?, ?, 'PENDING', 'agent_response_coordinator', NULL, ?, NULL, ?)
            """,
            (approval_id, recall_id, action_type, rationale, created_at),
        )
    return HumanApproval(
        id=approval_id,
        recall_id=recall_id,
        action_type=action_type,
        decision=ApprovalDecision.PENDING,
        reviewer_name=None,
        rationale=rationale,
        decided_at=None,
        created_at=created_at,
    )


class ToolRegistry:
    """Central backend-enforced tool dispatcher for all specialized agents."""

    def __init__(self) -> None:
        self._handlers: Dict[str, Callable[..., Any]] = {
            "read_evidence_record": _tool_read_evidence_record,
            "extract_structured_claims_deterministic": _tool_extract_structured_claims_deterministic,
            "read_recall_notice": _tool_read_recall_notice,
            "list_recall_batches": _tool_list_recall_batches,
            "read_organization_metadata": _tool_read_organization_metadata,
            "read_manufacturer_shipments": _tool_read_manufacturer_shipments,
            "read_distributor_shipments": _tool_read_distributor_shipments,
            "read_retailer_inventory": _tool_read_retailer_inventory,
            "read_recall_acknowledgements": _tool_read_recall_acknowledgements,
            "create_verification_finding": _tool_create_verification_finding,
            "read_verification_findings": _tool_read_verification_findings,
            "list_agent_executions": _tool_list_agent_executions,
            "propose_human_approval": _tool_propose_human_approval,
        }

    def allowed_tools_for(self, agent_role: AgentRole) -> List[str]:
        return sorted(ROLE_TOOL_PERMISSIONS.get(agent_role, frozenset()))

    def invoke(
        self,
        *,
        agent_role: AgentRole,
        auth: AgentAuthContext,
        tool_name: str,
        db_path: str | None,
        **kwargs: Any,
    ) -> Any:
        permitted = ROLE_TOOL_PERMISSIONS.get(agent_role, frozenset())
        if tool_name not in permitted:
            raise ToolPermissionError(
                f"Agent role '{agent_role.value}' is not permitted to invoke tool '{tool_name}'."
            )
        handler = self._handlers.get(tool_name)
        if handler is None:
            raise ToolPermissionError(f"Tool '{tool_name}' is not registered.")
        return handler(db_path=db_path, auth=auth, **kwargs)
