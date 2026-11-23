"""One-click coordinated response orchestration service (async-capable, persisted, idempotent, zero fabricated data).

Executes the genuine RecallRelay workflow on an existing registered recall:
1. Retrieve registered source evidence
2. Run deterministic extraction & source-internal date contradiction check (full extracted text)
3. Invoke Lyzr reasoning ONLY when configured and authorized
4. Evaluate Lyzr token-bounded verbatim-span grounding report
5. Run Recall Scope Agent
6. Inspect authorized organization records (Manufacturer / Distributor / Retailer)
   — strictly BLOCKED when no real downstream org records exist
7. Dispatch A2A Scope -> Verification communication (two-way BAND ONLY when A2A_TRANSPORT=band
   and sender+receiver credentials are configured; BAND receiver genuinely executes
   IndependentVerificationAgent via ToolRegistry; otherwise InternalSQLiteTransport)
8. Independent Verification Agent (consumes real BAND receiver execution when present, or executes
   IndependentVerificationAgent directly via ToolRegistry — zero raw SQL inserts, zero duplicates)
9. Run Recall Response Coordinator to propose a strictly PENDING human approval (never self-approves)

State-machine guarantees:
- `POST /api/v1/orchestrations/run` returns HTTP 202 immediately with `orchestration_id`.
- Every step transition is persisted (`PENDING` -> `RUNNING` -> terminal state).
- Top-level exception handling guarantees a run NEVER stays permanently `RUNNING`.
- Server startup marks any stale `PENDING`/`RUNNING` runs `INTERRUPTED`.
- Concurrent duplicate runs on the same recall return `HTTP 409 Conflict`.
- Idempotency keys are scoped to `(recall_id, requester_scope, idempotency_key)`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, List

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.agents.base import AgentAuthContext, AgentRole
from app.agents.specialists import build_agent
from app.agents.tools import ToolRegistry
from app.communication.audit_trail import record_audit_event
from app.communication.schemas import A2AMessageCreate, A2AMessageType
from app.communication.transport import InternalSQLiteTransport
from app.core.config import get_settings
from app.db.database import get_db_connection
from app.integrations.band_adapter import BandMessageTransport
from app.integrations.registry import IntegrationRegistry
from app.schemas.domain import new_id, utc_now_iso
from app.services import evidence_service, recall_service, traceability_service
from app.services.fda_page_importer import detect_source_date_contradictions

_STEP_DEFINITIONS: List[tuple[int, str, str]] = [
    (1, "evidence_retrieval", "Source Evidence Retrieved"),
    (2, "deterministic_extraction", "Deterministic Extraction & Source Contradiction Check"),
    (3, "lyzr_evidence_analysis", "Lyzr Evidence Analysis (AI_EXTRACTED)"),
    (4, "lyzr_grounding_check", "Lyzr Verbatim-Span Grounding Verification"),
    (5, "recall_scope_analysis", "Recall Scope Analysis"),
    (6, "organization_investigations", "Manufacturer / Distributor / Retailer Investigations"),
    (7, "a2a_band_communication", "A2A Agent Handoff (Scope -> Verification)"),
    (8, "independent_verification", "Independent Verification & Contradiction Check"),
    (9, "response_coordinator_proposal", "Human Approval Proposal Created (PENDING)"),
]


class OrchestrationRunRequest(BaseModel):
    recall_id: str = Field(..., min_length=3)
    idempotency_key: str | None = Field(default=None, max_length=128)


class OrchestrationStepRecord(BaseModel):
    id: str
    orchestration_id: str
    step_order: int
    step_key: str
    title: str
    status: str
    started_at: str
    completed_at: str | None = None
    evidence_references: List[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class OrchestrationRunResponse(BaseModel):
    id: str
    recall_id: str
    idempotency_key: str | None = None
    status: str
    active_transport: str
    lyzr_used: bool
    band_used: bool
    reused_idempotent_run: bool = False
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    completed_at: str | None = None
    steps: List[OrchestrationStepRecord] = Field(default_factory=list)


def _compute_idempotency_scope_hash(
    recall_id: str, requester_scope: str, idempotency_key: str | None
) -> str | None:
    if not idempotency_key or not idempotency_key.strip():
        return None
    raw = f"{recall_id}|{requester_scope}|{idempotency_key.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_step(row: sqlite3.Row) -> OrchestrationStepRecord:
    return OrchestrationStepRecord(
        id=row["id"],
        orchestration_id=row["orchestration_id"],
        step_order=row["step_order"],
        step_key=row["step_key"],
        title=row["title"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        evidence_references=json.loads(row["evidence_references_json"]),
        details=json.loads(row["details_json"]),
    )


def _load_orchestration(
    db_path: str | None, orchestration_id: str, *, reused: bool = False
) -> OrchestrationRunResponse:
    with get_db_connection(db_path) as conn:
        run_row = conn.execute(
            "SELECT * FROM orchestration_runs WHERE id = ?", (orchestration_id,)
        ).fetchone()
        if run_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Orchestration run '{orchestration_id}' was not found.",
            )
        step_rows = conn.execute(
            "SELECT * FROM orchestration_steps WHERE orchestration_id = ? ORDER BY step_order ASC",
            (orchestration_id,),
        ).fetchall()

    return OrchestrationRunResponse(
        id=run_row["id"],
        recall_id=run_row["recall_id"],
        idempotency_key=run_row["idempotency_key"],
        status=run_row["status"],
        active_transport=run_row["active_transport"],
        lyzr_used=bool(run_row["lyzr_used"]),
        band_used=bool(run_row["band_used"]),
        reused_idempotent_run=reused,
        summary=json.loads(run_row["summary_json"]),
        created_at=run_row["created_at"],
        completed_at=run_row["completed_at"],
        steps=[_row_to_step(r) for r in step_rows],
    )


def get_orchestration(db_path: str | None, orchestration_id: str) -> OrchestrationRunResponse:
    return _load_orchestration(db_path, orchestration_id, reused=False)


def list_orchestrations(
    db_path: str | None, *, recall_id: str | None = None, limit: int = 20
) -> List[OrchestrationRunResponse]:
    with get_db_connection(db_path) as conn:
        if recall_id:
            rows = conn.execute(
                "SELECT id FROM orchestration_runs WHERE recall_id = ? ORDER BY created_at DESC LIMIT ?",
                (recall_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM orchestration_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_load_orchestration(db_path, r["id"], reused=False) for r in rows]


def _update_step(
    db_path: str | None,
    *,
    orchestration_id: str,
    step_order: int,
    step_status: str,
    started_at: str | None = None,
    completed_at: str | None = None,
    evidence_references: List[str] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    with get_db_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT id, started_at, evidence_references_json, details_json FROM orchestration_steps WHERE orchestration_id = ? AND step_order = ?",
            (orchestration_id, step_order),
        ).fetchone()
        if existing is None:
            step_key = f"step_{step_order}"
            title = f"Step {step_order}"
            for order, key, lbl in _STEP_DEFINITIONS:
                if order == step_order:
                    step_key, title = key, lbl
                    break
            conn.execute(
                """
                INSERT INTO orchestration_steps (
                    id, orchestration_id, step_order, step_key, title, status,
                    started_at, completed_at, evidence_references_json, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("ostep"),
                    orchestration_id,
                    step_order,
                    step_key,
                    title,
                    step_status,
                    started_at or utc_now_iso(),
                    completed_at,
                    json.dumps(evidence_references or []),
                    json.dumps(details or {}, default=str),
                ),
            )
            return

        conn.execute(
            """
            UPDATE orchestration_steps
            SET status = ?,
                started_at = ?,
                completed_at = ?,
                evidence_references_json = ?,
                details_json = ?
            WHERE id = ?
            """,
            (
                step_status,
                started_at or existing["started_at"],
                completed_at,
                json.dumps(evidence_references) if evidence_references is not None else existing["evidence_references_json"],
                json.dumps(details, default=str) if details is not None else existing["details_json"],
                existing["id"],
            ),
        )


def start_coordinated_response(
    db_path: str | None,
    payload: OrchestrationRunRequest,
    *,
    registry: IntegrationRegistry | None = None,
    actor_id: str = "admin",
    requester_scope: str | None = None,
) -> tuple[OrchestrationRunResponse, bool]:
    """Create a persisted PENDING orchestration run (returns `(run, should_execute)`).

    - Scopes idempotency to `(recall_id, requester_scope, idempotency_key)`.
    - Prevents concurrent simultaneous execution on the same recall (`HTTP 409 Conflict`).
    - Pre-populates all 9 steps as `PENDING` so clients polling immediately see the timeline.
    """
    scope = (requester_scope or actor_id or "admin").strip()
    scope_hash = _compute_idempotency_scope_hash(
        payload.recall_id, scope, payload.idempotency_key
    )
    if scope_hash:
        with get_db_connection(db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM orchestration_runs WHERE idempotency_scope_hash = ?",
                (scope_hash,),
            ).fetchone()
        if existing is not None:
            return _load_orchestration(db_path, existing["id"], reused=True), False

    # Verify the recall genuinely exists (never invent a recall)
    recall = recall_service.get_recall_by_id(db_path, payload.recall_id, public_only=False)

    # Prevent duplicate simultaneous execution on the same recall
    with get_db_connection(db_path) as conn:
        active = conn.execute(
            """
            SELECT id, status FROM orchestration_runs
            WHERE recall_id = ? AND status IN ('PENDING', 'RUNNING')
            ORDER BY created_at DESC LIMIT 1
            """,
            (recall.id,),
        ).fetchone()
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An orchestration run ({active['id']}) is already {active['status']} "
                f"for recall '{recall.id}'. Poll GET /api/v1/orchestrations/{active['id']}."
            ),
        )

    settings = get_settings()
    active_registry = registry or IntegrationRegistry(db_path=db_path, settings=settings)
    overview = active_registry.overview()

    orch_id = new_id("orch")
    created_at = utc_now_iso()
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO orchestration_runs (
                id, recall_id, requester_scope, idempotency_key, idempotency_scope_hash,
                status, active_transport, lyzr_used, band_used, summary_json,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, 0, 0, '{}', ?, NULL)
            """,
            (
                orch_id,
                recall.id,
                scope,
                payload.idempotency_key.strip() if payload.idempotency_key else None,
                scope_hash,
                overview.active_a2a_transport,
                created_at,
            ),
        )
        for order, key, title in _STEP_DEFINITIONS:
            conn.execute(
                """
                INSERT INTO orchestration_steps (
                    id, orchestration_id, step_order, step_key, title, status,
                    started_at, completed_at, evidence_references_json, details_json
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, NULL, '[]', '{}')
                """,
                (new_id("ostep"), orch_id, order, key, title, created_at),
            )

    return _load_orchestration(db_path, orch_id, reused=False), True


def execute_orchestration_in_background(
    db_path: str | None,
    orchestration_id: str,
    *,
    registry: IntegrationRegistry | None = None,
    actor_id: str = "admin",
) -> OrchestrationRunResponse:
    """Execute all 9 steps for a persisted orchestration run with strict terminal-state guarantees."""
    run = _load_orchestration(db_path, orchestration_id, reused=False)
    recall_id = run.recall_id

    with get_db_connection(db_path) as conn:
        conn.execute(
            "UPDATE orchestration_runs SET status = 'RUNNING' WHERE id = ?",
            (orchestration_id,),
        )

    try:
        recall = recall_service.get_recall_by_id(db_path, recall_id, public_only=False)
        settings = get_settings()
        active_registry = registry or IntegrationRegistry(db_path=db_path, settings=settings)
        active_transport = active_registry.resolve_a2a_transport(db_path)
        lyzr_adapter = active_registry.get_adapter("lyzr")

        admin_auth = AgentAuthContext(is_admin=True, boundary=None)
        tool_reg = ToolRegistry()
        had_partial_failure = False
        had_gaps_or_blocks = False
        lyzr_used = False
        band_used = False
        band_external_id: str | None = None
        band_reply_id: str | None = None
        proposed_approval_id: str | None = None

        # STEP 1: Retrieve registered source evidence
        s1_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=1, step_status="RUNNING", started_at=s1_start)
        evidence = evidence_service.get_evidence_by_id(db_path, recall.evidence_id)
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=1,
            step_status="COMPLETED",
            started_at=s1_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "evidence_id": evidence.id,
                "source_type": evidence.source_type.value,
                "evidence_stage": evidence.evidence_stage.value,
                "sha256_hash": evidence.sha256_hash,
                "source_url": evidence.source_url,
            },
        )

        # STEP 2: Deterministic extraction & full-text source contradiction check
        s2_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=2, step_status="RUNNING", started_at=s2_start)
        source_text = evidence.notes or ""
        date_contradictions = detect_source_date_contradictions(source_text)
        contradiction_dicts = [c.model_dump() for c in date_contradictions]
        analyst_agent = build_agent(
            AgentRole.RECALL_SOURCE_ANALYST,
            db_path=db_path,
            tool_registry=tool_reg,
            timeout_seconds=settings.agent_default_timeout_seconds,
            lyzr_adapter=lyzr_adapter,
        )
        analyst_env = analyst_agent.run(
            {"evidence_id": evidence.id, "source_text": source_text},
            admin_auth,
        )
        analyst_out = analyst_env.output or {}
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=2,
            step_status="COMPLETED" if analyst_env.execution_status == "COMPLETED" else "FAILED",
            started_at=s2_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "execution_id": analyst_env.execution_id,
                "extracted_claim_count": len(analyst_out.get("extracted_claims", [])),
                "missing_fields": analyst_out.get("missing_or_ambiguous_fields", []),
                "source_date_contradictions": contradiction_dicts,
            },
        )

        # STEP 3: Lyzr evidence analysis (honest status + sanitized error if failed)
        s3_start = utc_now_iso()
        lyzr_status = analyst_out.get("lyzr_status", "CONFIGURATION_MISSING")
        lyzr_analysis = analyst_out.get("lyzr_analysis")
        lyzr_error = analyst_out.get("lyzr_error")
        if lyzr_status == "SUCCEEDED" and lyzr_analysis:
            lyzr_used = True
            s3_status = "COMPLETED"
        elif lyzr_status == "ERROR_FALLBACK_DETERMINISTIC":
            had_partial_failure = True
            s3_status = "FAILED"
        elif lyzr_status == "BLOCKED_PRIVATE_EVIDENCE":
            had_gaps_or_blocks = True
            s3_status = "BLOCKED"
        else:
            s3_status = "SKIPPED"
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=3,
            step_status=s3_status,
            started_at=s3_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "lyzr_status": lyzr_status,
                "lyzr_error": lyzr_error,
                "execution_mode_used": analyst_out.get("execution_mode_used", "DETERMINISTIC"),
                "can_self_verify": False,
            },
        )

        # STEP 4: Lyzr token-bounded verbatim-span grounding verification
        s4_start = utc_now_iso()
        grounding = (lyzr_analysis or {}).get("grounding_report") if isinstance(lyzr_analysis, dict) else None
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=4,
            step_status="COMPLETED" if grounding is not None else "SKIPPED",
            started_at=s4_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details=grounding or {"reason": "Lyzr did not execute; deterministic extraction used directly."},
        )

        # STEP 5: Recall Scope Agent
        s5_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=5, step_status="RUNNING", started_at=s5_start)
        scope_agent = build_agent(
            AgentRole.RECALL_SCOPE_AGENT,
            db_path=db_path,
            tool_registry=tool_reg,
            timeout_seconds=settings.agent_default_timeout_seconds,
        )
        scope_env = scope_agent.run({"recall_id": recall.id}, admin_auth)
        scope_out = scope_env.output or {}
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=5,
            step_status="COMPLETED" if scope_env.execution_status == "COMPLETED" else "FAILED",
            started_at=s5_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "execution_id": scope_env.execution_id,
                "batch_count": len(scope_out.get("batches", [])),
                "unknown_quantity_batch_ids": scope_out.get("unknown_quantity_batch_ids", []),
                "unresolved_scope_gaps": scope_out.get("unresolved_scope_gaps", []),
            },
        )

        # STEP 6: Organization Investigations (strictly real registered data — NEVER fabricated)
        s6_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=6, step_status="RUNNING", started_at=s6_start)
        trace = traceability_service.build_recall_traceability(db_path, recall.id, None)
        has_shipments = trace.total_recorded_shipments_in_system > 0
        has_inventory = trace.total_recorded_inventory_in_system > 0
        if not has_shipments and not has_inventory:
            had_gaps_or_blocks = True
            s6_status = "BLOCKED"
        else:
            s6_status = "COMPLETED"
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=6,
            step_status=s6_status,
            started_at=s6_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "recorded_shipments_in_system": trace.total_recorded_shipments_in_system,
                "recorded_inventory_in_system": trace.total_recorded_inventory_in_system,
                "missing_information": [g.model_dump() for g in trace.missing_information],
                "note": (
                    "Downstream organization investigations are blocked because no authorized "
                    "manufacturer, distributor, or retailer records exist for this recall (zero fabricated data)."
                    if s6_status == "BLOCKED"
                    else "Authorized downstream organization records present."
                ),
            },
        )

        # Helper to run the genuine IndependentVerificationAgent via ToolRegistry
        def _execute_real_verification_agent() -> dict[str, Any]:
            ver_agent = build_agent(
                AgentRole.INDEPENDENT_VERIFICATION_AGENT,
                db_path=db_path,
                tool_registry=tool_reg,
                timeout_seconds=settings.agent_default_timeout_seconds,
            )
            ver_env = ver_agent.run(
                {
                    "recall_id": recall.id,
                    "primary_evidence_id": evidence.id,
                    "corroborating_evidence_id": None,
                    "source_contradictions": contradiction_dicts,
                },
                admin_auth,
            )
            ver_out = ver_env.output or {}
            findings = ver_out.get("findings_created") or []
            finding_ids = [
                f.get("id") for f in findings if isinstance(f, dict) and f.get("id")
            ]
            return {
                "execution_id": ver_env.execution_id,
                "execution_status": ver_env.execution_status,
                "overall_status": ver_out.get("overall_status", "AWAITING_EVIDENCE"),
                "policy_satisfied_for_verified": bool(ver_out.get("policy_satisfied_for_verified", False)),
                "contradictions_detected": ver_out.get("contradictions_detected", []),
                "missing_evidence_items": ver_out.get("missing_evidence_items", []),
                "finding_ids": finding_ids,
                "executed_by": AgentRole.INDEPENDENT_VERIFICATION_AGENT.value,
            }

        # STEP 7: A2A Communication (BAND receiver executes genuine IndependentVerificationAgent)
        s7_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=7, step_status="RUNNING", started_at=s7_start)
        handoff_msg_payload = A2AMessageCreate(
            conversation_id=f"conv_{orchestration_id}",
            recall_id=recall.id,
            sender_agent=AgentRole.RECALL_SCOPE_AGENT,
            recipient_agent=AgentRole.INDEPENDENT_VERIFICATION_AGENT,
            message_type=A2AMessageType.TASK_HANDOFF,
            payload={
                "recall_id": recall.id,
                "primary_evidence_id": evidence.id,
                "source_date_contradictions_count": len(date_contradictions),
            },
            evidence_references=[evidence.id],
            idempotency_key=f"orch_msg_{orchestration_id}",
        )
        a2a_details: dict[str, Any] = {"configured_transport": active_registry.configured_a2a_transport}
        receiver_verification_result: dict[str, Any] | None = None
        try:
            if isinstance(active_transport, BandMessageTransport) and active_transport.is_two_way_configured():
                def _band_receiver_handler(_inbound: dict[str, Any]) -> dict[str, Any]:
                    nonlocal receiver_verification_result
                    receiver_verification_result = _execute_real_verification_agent()
                    return receiver_verification_result

                two_way = active_transport.execute_two_way_handoff(
                    handoff_msg_payload,
                    sender_boundary_id=None,
                    actor_id=actor_id,
                    receiver_handler=_band_receiver_handler,
                )
                band_used = True
                band_external_id = two_way["external_message_id"]
                band_reply_id = two_way["external_reply_message_id"]
                s7_status = "COMPLETED"
                a2a_details.update(
                    {
                        "transport_used": "band_two_way",
                        "local_message_id": two_way["message"].id,
                        "external_message_id": band_external_id,
                        "external_reply_message_id": band_reply_id,
                        "sender_agent_id": two_way["sender_agent_id"],
                        "receiver_agent_id": two_way["receiver_agent_id"],
                        "receiver_executed_real_agent": True,
                        "receiver_execution_id": (
                            receiver_verification_result.get("execution_id")
                            if receiver_verification_result
                            else None
                        ),
                    }
                )
            elif isinstance(active_transport, BandMessageTransport):
                sent = active_transport.send_message(
                    handoff_msg_payload, sender_boundary_id=None, actor_id=actor_id
                )
                band_used = True
                band_external_id = sent.external_message_id
                s7_status = "COMPLETED"
                a2a_details.update(
                    {
                        "transport_used": "band_outbound_only",
                        "local_message_id": sent.id,
                        "external_message_id": band_external_id,
                        "note": "Receiver BAND credentials not configured; outbound BAND send recorded.",
                    }
                )
            else:
                local_t = InternalSQLiteTransport(db_path)
                sent = local_t.send_message(
                    handoff_msg_payload, sender_boundary_id=None, actor_id=actor_id
                )
                local_t.deliver_message(sent.id, actor_id=actor_id)
                s7_status = "COMPLETED"
                a2a_details.update(
                    {
                        "transport_used": "internal_sqlite_transport",
                        "local_message_id": sent.id,
                        "band_status": "NOT_CONNECTED",
                        "note": "Delivered durably via InternalSQLiteTransport (BAND not active).",
                    }
                )
        except Exception as exc:
            had_partial_failure = True
            s7_status = "FAILED"
            a2a_details["error"] = str(exc)[:300]

        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=7,
            step_status=s7_status,
            started_at=s7_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details=a2a_details,
        )

        # STEP 8: Independent Verification (consume real BAND receiver execution if present, else execute directly)
        s8_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=8, step_status="RUNNING", started_at=s8_start)
        if receiver_verification_result is not None:
            ver_result = receiver_verification_result
            ver_source = "band_two_way_receiver_execution"
        else:
            ver_result = _execute_real_verification_agent()
            ver_source = "direct_verification_agent_execution"

        finding_ids = ver_result.get("finding_ids") or []
        overall_ver_status = ver_result.get("overall_status", "AWAITING_EVIDENCE")
        s8_status = (
            "COMPLETED"
            if (finding_ids or overall_ver_status == "DISCREPANCY")
            else "AWAITING_EVIDENCE"
        )
        if s8_status == "AWAITING_EVIDENCE":
            had_gaps_or_blocks = True
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=8,
            step_status=s8_status,
            started_at=s8_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "execution_id": ver_result.get("execution_id"),
                "execution_source": ver_source,
                "overall_status": overall_ver_status,
                "policy_satisfied_for_verified": ver_result.get("policy_satisfied_for_verified", False),
                "source_contradiction_finding_ids": finding_ids,
                "contradictions_detected": ver_result.get("contradictions_detected", []),
                "missing_evidence_items": ver_result.get("missing_evidence_items", []),
            },
        )

        # STEP 9: Recall Response Coordinator (proposes strictly PENDING human approval; NEVER self-approves)
        s9_start = utc_now_iso()
        _update_step(db_path, orchestration_id=orchestration_id, step_order=9, step_status="RUNNING", started_at=s9_start)
        coord_agent = build_agent(
            AgentRole.RECALL_RESPONSE_COORDINATOR,
            db_path=db_path,
            tool_registry=tool_reg,
            timeout_seconds=settings.agent_default_timeout_seconds,
        )
        coord_env = coord_agent.run(
            {"recall_id": recall.id, "propose_human_review_action": True},
            admin_auth,
        )
        coord_out = coord_env.output or {}
        proposed = coord_out.get("proposed_approvals") or []
        if proposed:
            proposed_approval_id = proposed[0].get("id")
        _update_step(
            db_path,
            orchestration_id=orchestration_id,
            step_order=9,
            step_status="COMPLETED" if coord_env.execution_status == "COMPLETED" else "FAILED",
            started_at=s9_start,
            completed_at=utc_now_iso(),
            evidence_references=[evidence.id],
            details={
                "execution_id": coord_env.execution_id,
                "proposed_approval_id": proposed_approval_id,
                "decision": proposed[0].get("decision") if proposed else "NONE",
                "self_approved": False,
                "unresolved_items": coord_out.get("unresolved_items", []),
            },
        )

        final_status = (
            "PARTIAL_FAILURE"
            if had_partial_failure
            else ("COMPLETED_WITH_GAPS" if had_gaps_or_blocks else "COMPLETED")
        )
        summary = {
            "recall_id": recall.id,
            "evidence_id": evidence.id,
            "lyzr_used": lyzr_used,
            "band_used": band_used,
            "band_external_message_id": band_external_id,
            "band_external_reply_message_id": band_reply_id,
            "source_date_contradictions_found": len(date_contradictions),
            "verification_finding_ids": finding_ids,
            "proposed_approval_id": proposed_approval_id,
            "self_approved": False,
        }
        completed_at = utc_now_iso()
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE orchestration_runs
                SET status = ?, lyzr_used = ?, band_used = ?, summary_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    final_status,
                    1 if lyzr_used else 0,
                    1 if band_used else 0,
                    json.dumps(summary),
                    completed_at,
                    orchestration_id,
                ),
            )

        record_audit_event(
            db_path,
            event_type="COORDINATED_RESPONSE_EXECUTED",
            actor_id=actor_id,
            entity_type="orchestration_run",
            entity_id=orchestration_id,
            metadata={
                "recall_id": recall.id,
                "status": final_status,
                "lyzr_used": lyzr_used,
                "band_used": band_used,
                "band_external_message_id": band_external_id,
                "band_external_reply_message_id": band_reply_id,
            },
        )
        return _load_orchestration(db_path, orchestration_id, reused=False)

    except Exception as exc:
        # Terminal-state guarantee: an orchestration can NEVER remain permanently RUNNING
        failed_at = utc_now_iso()
        safe_err = f"{type(exc).__name__}: {str(exc)[:300]}"
        with get_db_connection(db_path) as conn:
            conn.execute(
                """
                UPDATE orchestration_runs
                SET status = 'FAILED', summary_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps({"recall_id": recall_id, "error": safe_err}),
                    failed_at,
                    orchestration_id,
                ),
            )
            conn.execute(
                """
                UPDATE orchestration_steps
                SET status = 'FAILED', completed_at = ?
                WHERE orchestration_id = ? AND status = 'RUNNING'
                """,
                (failed_at, orchestration_id),
            )
        record_audit_event(
            db_path,
            event_type="COORDINATED_RESPONSE_FAILED",
            actor_id=actor_id,
            entity_type="orchestration_run",
            entity_id=orchestration_id,
            metadata={"recall_id": recall_id, "error": safe_err},
        )
        return _load_orchestration(db_path, orchestration_id, reused=False)


def run_coordinated_response(
    db_path: str | None,
    payload: OrchestrationRunRequest,
    *,
    registry: IntegrationRegistry | None = None,
    actor_id: str = "admin",
    requester_scope: str | None = None,
) -> OrchestrationRunResponse:
    """Synchronous convenience wrapper (starts and executes to completion)."""
    run, should_execute = start_coordinated_response(
        db_path,
        payload,
        registry=registry,
        actor_id=actor_id,
        requester_scope=requester_scope,
    )
    if not should_execute:
        return run
    return execute_orchestration_in_background(
        db_path,
        run.id,
        registry=registry,
        actor_id=actor_id,
    )
