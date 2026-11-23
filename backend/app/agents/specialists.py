"""Seven specialized RecallRelay agents (Phase 4).

Each agent has a unique identifier, explicit role, restricted backend-enforced
tools, typed input/output schemas, and deterministic rule-based execution
clearly labeled as ExecutionMode.DETERMINISTIC.
"""

from __future__ import annotations

from typing import Any, Dict, List, Type

from app.agents.base import (
    AgentAuthContext,
    AgentBoundaryError,
    AgentRole,
    BaseAgent,
    ExecutionMode,
)
from app.agents.schemas import (
    CoordinatorInput,
    CoordinatorOutput,
    DistributorReconciliationInput,
    DistributorReconciliationOutput,
    ExtractedClaim,
    IndependentVerificationInput,
    IndependentVerificationOutput,
    ManufacturerOpsInput,
    ManufacturerOpsOutput,
    RecallScopeInput,
    RecallScopeOutput,
    RetailerInventoryInput,
    RetailerInventoryOutput,
    SourceAnalystInput,
    SourceAnalystOutput,
)
from app.agents.tools import ToolRegistry
from app.schemas.domain import (
    AcknowledgementStatus,
    FindingType,
    InventoryStatus,
    OrganizationRole,
    EvidenceSourceType,
    VerificationStatus,
)


class RecallSourceAnalystAgent(BaseAgent[SourceAnalystInput, SourceAnalystOutput]):
    """Agent 1 — Reads authorized source evidence, runs deterministic cross-check, and invokes Lyzr when configured."""

    agent_id = "agent_recall_source_analyst"
    role = AgentRole.RECALL_SOURCE_ANALYST
    input_schema = SourceAnalystInput
    output_schema = SourceAnalystOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def __init__(
        self,
        *,
        db_path: str | None,
        tool_registry: ToolRegistry,
        timeout_seconds: float = 5.0,
        lyzr_adapter: Any = None,
    ) -> None:
        super().__init__(
            db_path=db_path,
            tool_registry=tool_registry,
            timeout_seconds=timeout_seconds,
        )
        self._lyzr_adapter = lyzr_adapter

    def extract_input_references(self, validated_input: SourceAnalystInput) -> List[str]:
        return [validated_input.evidence_id]

    def extract_output_and_evidence_references(
        self, validated_output: SourceAnalystOutput
    ) -> tuple[List[str], List[str]]:
        return [validated_output.evidence_id], [validated_output.evidence_id]

    def execute(
        self, validated_input: SourceAnalystInput, auth: AgentAuthContext
    ) -> SourceAnalystOutput:
        evidence = self.invoke_tool(
            auth, "read_evidence_record", evidence_id=validated_input.evidence_id
        )
        source_ref = evidence.source_url or evidence.source_document or evidence.record_reference

        text_to_scan = validated_input.source_text or evidence.notes or ""
        extracted = self.invoke_tool(
            auth,
            "extract_structured_claims_deterministic",
            source_text=text_to_scan,
            evidence_id=evidence.id,
        )

        claims: List[ExtractedClaim] = []
        missing: List[str] = []

        scalar_fields = [
            ("recall_number", extracted.get("recall_number")),
            ("product_name", extracted.get("product_name")),
            ("hazard_description", extracted.get("hazard_description")),
            ("publication_date", extracted.get("publication_date") or evidence.publication_date),
        ]
        for field_name, value in scalar_fields:
            if value:
                claims.append(
                    ExtractedClaim(
                        field_name=field_name,
                        value=str(value),
                        source_evidence_id=evidence.id,
                        source_reference=source_ref,
                        # Never self-verify extracted claims
                        verification_status=VerificationStatus.AWAITING_EVIDENCE,
                    )
                )
            else:
                missing.append(field_name)

        batch_codes = extracted.get("batch_codes") or []
        if batch_codes:
            for code in batch_codes:
                claims.append(
                    ExtractedClaim(
                        field_name="batch_code",
                        value=str(code),
                        source_evidence_id=evidence.id,
                        source_reference=source_ref,
                        verification_status=VerificationStatus.AWAITING_EVIDENCE,
                    )
                )
        else:
            missing.append("batch_codes")

        execution_mode_used = "DETERMINISTIC"
        lyzr_status = "CONFIGURATION_MISSING"
        lyzr_error: str | None = None
        lyzr_analysis: dict[str, Any] | None = None
        notes_parts = [
            "Deterministic extraction cross-check completed. Extracted claims remain AWAITING_EVIDENCE until independently verified."
        ]

        if self._lyzr_adapter is not None and self._lyzr_adapter.is_reasoning_ready():
            if (
                evidence.source_type == EvidenceSourceType.AUTHORIZED_ORG_UPLOAD
                and not validated_input.allow_external_ai_on_private_evidence
            ):
                lyzr_status = "BLOCKED_PRIVATE_EVIDENCE"
                notes_parts.append(
                    "Lyzr external reasoning skipped: private organization evidence requires explicit sharing authorization."
                )
            elif text_to_scan.strip():
                try:
                    lyzr_analysis = self._lyzr_adapter.analyze_public_evidence(
                        user_id="recallrelay_source_analyst",
                        session_id=f"src_{evidence.id}",
                        evidence_id=evidence.id,
                        authorized_text=text_to_scan,
                        actor_id="admin" if auth.is_admin else (auth.boundary.organization_id if auth.boundary else "system"),
                        db_path=self.db_path,
                        deterministic_claims={
                            "product_name": str(extracted.get("product_name") or ""),
                            "hazard": str(extracted.get("hazard_description") or ""),
                            "publication_date": str(extracted.get("publication_date") or evidence.publication_date or ""),
                            "lot_number": str((extracted.get("batch_codes") or [""])[0]),
                        },
                    )
                    execution_mode_used = "DETERMINISTIC_WITH_LYZR_ASSISTED"
                    lyzr_status = "SUCCEEDED"
                    grounding = (lyzr_analysis or {}).get("grounding_report") or {}
                    grounded_fields = grounding.get("grounded_fields") or {}
                    for g_field, g_info in grounded_fields.items():
                        candidate_val = str(g_info.get("candidate_value") or "").strip()
                        if candidate_val:
                            claims.append(
                                ExtractedClaim(
                                    field_name=f"lyzr_{g_field}",
                                    value=candidate_val,
                                    source_evidence_id=evidence.id,
                                    source_reference=source_ref,
                                    evidence_stage="AI_EXTRACTED",
                                    verbatim_span=str(g_info.get("verbatim_span_normalized") or ""),
                                    verification_status=VerificationStatus.AWAITING_EVIDENCE,
                                )
                            )
                    notes_parts.append(
                        f"Lyzr v3 analysis succeeded ({len(grounded_fields)} grounded claims merged as AI_EXTRACTED / AWAITING_EVIDENCE alongside deterministic cross-check)."
                    )
                except Exception as exc:
                    lyzr_status = "ERROR_FALLBACK_DETERMINISTIC"
                    lyzr_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                    notes_parts.append(
                        "Lyzr invocation failed; preserved deterministic extraction fallback without claiming LYZR_ASSISTED."
                    )
            else:
                lyzr_status = "NOT_CONNECTED"
        elif self._lyzr_adapter is not None and self._lyzr_adapter.is_configured():
            lyzr_status = "CONFIGURATION_MISSING"

        return SourceAnalystOutput(
            evidence_id=evidence.id,
            source_reference=source_ref,
            extracted_claims=claims,
            missing_or_ambiguous_fields=missing,
            # Preserve existing evidence status; Source Analyst (and Lyzr) NEVER promotes to VERIFIED
            evidence_verification_status=evidence.verification_status,
            execution_mode_used=execution_mode_used,
            lyzr_status=lyzr_status,
            lyzr_error=lyzr_error,
            lyzr_analysis=lyzr_analysis,
            deterministic_cross_check_preserved=True,
            notes=" ".join(notes_parts),
        )


class RecallScopeAgent(BaseAgent[RecallScopeInput, RecallScopeOutput]):
    """Agent 2 — Determines explicitly associated affected batches for a recall notice."""

    agent_id = "agent_recall_scope"
    role = AgentRole.RECALL_SCOPE_AGENT
    input_schema = RecallScopeInput
    output_schema = RecallScopeOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(self, validated_input: RecallScopeInput) -> List[str]:
        return [validated_input.recall_id]

    def extract_output_and_evidence_references(
        self, validated_output: RecallScopeOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [validated_output.recall_id] + [b.id for b in validated_output.batches]
        ev_refs = [validated_output.originating_evidence_id] + [
            b.evidence_id for b in validated_output.batches
        ]
        return out_refs, ev_refs

    def execute(
        self, validated_input: RecallScopeInput, auth: AgentAuthContext
    ) -> RecallScopeOutput:
        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        evidence = self.invoke_tool(
            auth, "read_evidence_record", evidence_id=recall.evidence_id
        )
        batches = self.invoke_tool(
            auth, "list_recall_batches", recall_id=validated_input.recall_id
        )

        unknown_qty_ids = [b.id for b in batches if b.reported_quantity is None]
        gaps: List[str] = []

        if not batches:
            gaps.append("No affected batches are explicitly linked to this recall notice.")
        if unknown_qty_ids:
            gaps.append(
                f"{len(unknown_qty_ids)} batch(es) have UNKNOWN (NULL) reported_quantity."
            )
        if evidence.verification_status != VerificationStatus.VERIFIED:
            gaps.append(
                f"Originating evidence '{evidence.id}' status is '{evidence.verification_status.value}'."
            )

        scope_status = (
            VerificationStatus.VERIFIED
            if (batches and not unknown_qty_ids and evidence.verification_status == VerificationStatus.VERIFIED)
            else VerificationStatus.AWAITING_EVIDENCE
        )

        return RecallScopeOutput(
            recall_id=recall.id,
            external_recall_number=recall.external_recall_number,
            originating_evidence_id=evidence.id,
            originating_evidence_status=evidence.verification_status,
            batches=batches,
            unknown_quantity_batch_ids=unknown_qty_ids,
            unresolved_scope_gaps=gaps,
            scope_status=scope_status,
        )


class ManufacturerOperationsAgent(BaseAgent[ManufacturerOpsInput, ManufacturerOpsOutput]):
    """Agent 3 — Inspects manufacturer-owned dispatches strictly inside a MANUFACTURER boundary."""

    agent_id = "agent_manufacturer_ops"
    role = AgentRole.MANUFACTURER_OPERATIONS_AGENT
    input_schema = ManufacturerOpsInput
    output_schema = ManufacturerOpsOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(self, validated_input: ManufacturerOpsInput) -> List[str]:
        return [validated_input.recall_id]

    def extract_output_and_evidence_references(
        self, validated_output: ManufacturerOpsOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [validated_output.recall_id] + [d.id for d in validated_output.dispatches]
        ev_refs = [d.evidence_id for d in validated_output.dispatches]
        return out_refs, ev_refs

    def execute(
        self, validated_input: ManufacturerOpsInput, auth: AgentAuthContext
    ) -> ManufacturerOpsOutput:
        if auth.boundary is None or auth.boundary.role != OrganizationRole.MANUFACTURER:
            raise AgentBoundaryError(
                "ManufacturerOperationsAgent requires an authenticated MANUFACTURER boundary."
            )

        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        batches = self.invoke_tool(
            auth, "list_recall_batches", recall_id=recall.id
        )
        dispatches = self.invoke_tool(
            auth, "read_manufacturer_shipments", recall_id=recall.id
        )
        acknowledgements = self.invoke_tool(
            auth, "read_recall_acknowledgements", recall_id=recall.id
        )

        batch_ids = [b.id for b in batches]
        receiving_org_ids = sorted({d.to_org_id for d in dispatches})
        acked_orgs = {
            ack.organization_id
            for ack in acknowledgements
            if ack.status == AcknowledgementStatus.ACKNOWLEDGED
        }
        missing_ack_orgs = [oid for oid in receiving_org_ids if oid not in acked_orgs]
        unknown_qty_dispatches = [d.id for d in dispatches if d.quantity_shipped is None]

        gaps: List[str] = []
        if not dispatches:
            gaps.append(
                "No manufacturer-owned dispatch records exist in this boundary for the recall."
            )
        if missing_ack_orgs:
            gaps.append(
                f"{len(missing_ack_orgs)} receiving organization(s) have not yet acknowledged."
            )
        if unknown_qty_dispatches:
            gaps.append(
                f"{len(unknown_qty_dispatches)} dispatch(es) have UNKNOWN (NULL) quantity_shipped."
            )

        return ManufacturerOpsOutput(
            recall_id=recall.id,
            manufacturer_org_id=auth.boundary.organization_id,
            boundary_id=auth.boundary.data_boundary_id,
            affected_batch_ids=batch_ids,
            dispatches=dispatches,
            receiving_org_ids=receiving_org_ids,
            missing_acknowledgement_org_ids=missing_ack_orgs,
            unknown_quantity_dispatch_ids=unknown_qty_dispatches,
            unresolved_gaps=gaps,
        )


class DistributorReconciliationAgent(
    BaseAgent[DistributorReconciliationInput, DistributorReconciliationOutput]
):
    """Agent 4 — Reconciles distributor-owned shipments strictly inside a DISTRIBUTOR boundary."""

    agent_id = "agent_distributor_reconciliation"
    role = AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT
    input_schema = DistributorReconciliationInput
    output_schema = DistributorReconciliationOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(
        self, validated_input: DistributorReconciliationInput
    ) -> List[str]:
        return [validated_input.recall_id]

    def extract_output_and_evidence_references(
        self, validated_output: DistributorReconciliationOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [validated_output.recall_id] + [
            s.id for s in validated_output.outgoing_shipments
        ]
        ev_refs = [s.evidence_id for s in validated_output.outgoing_shipments]
        return out_refs, ev_refs

    def execute(
        self, validated_input: DistributorReconciliationInput, auth: AgentAuthContext
    ) -> DistributorReconciliationOutput:
        if auth.boundary is None or auth.boundary.role != OrganizationRole.DISTRIBUTOR:
            raise AgentBoundaryError(
                "DistributorReconciliationAgent requires an authenticated DISTRIBUTOR boundary."
            )

        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        shipments = self.invoke_tool(
            auth, "read_distributor_shipments", recall_id=recall.id
        )

        unresolved: List[str] = []
        known_quantities = [
            s.quantity_shipped for s in shipments if s.quantity_shipped is not None
        ]
        has_unknown_qty = any(s.quantity_shipped is None for s in shipments)

        total_known_out: int | None = (
            sum(known_quantities) if (shipments and not has_unknown_qty) else None
        )

        if not shipments:
            unresolved.append(
                "No distributor-owned outgoing shipment records found for this recall."
            )
        if has_unknown_qty:
            unresolved.append(
                "One or more outgoing shipments have UNKNOWN (NULL) quantity_shipped."
            )
        if validated_input.expected_incoming_quantity is None:
            unresolved.append(
                "Upstream incoming shipment quantity is UNKNOWN (AWAITING_EVIDENCE)."
            )

        if (
            validated_input.expected_incoming_quantity is not None
            and total_known_out is not None
        ):
            if total_known_out != validated_input.expected_incoming_quantity:
                reconciliation_status = VerificationStatus.DISCREPANCY
                unresolved.append(
                    f"Outgoing total ({total_known_out}) does not match expected incoming "
                    f"quantity ({validated_input.expected_incoming_quantity})."
                )
            else:
                reconciliation_status = VerificationStatus.VERIFIED
        else:
            reconciliation_status = VerificationStatus.AWAITING_EVIDENCE

        return DistributorReconciliationOutput(
            recall_id=recall.id,
            distributor_org_id=auth.boundary.organization_id,
            boundary_id=auth.boundary.data_boundary_id,
            outgoing_shipments=shipments,
            total_known_outgoing_quantity=total_known_out,
            expected_incoming_quantity=validated_input.expected_incoming_quantity,
            reconciliation_status=reconciliation_status,
            unresolved_relationships=unresolved,
        )


class RetailerInventoryAgent(BaseAgent[RetailerInventoryInput, RetailerInventoryOutput]):
    """Agent 5 — Inspects retailer-owned inventory strictly inside a RETAILER boundary."""

    agent_id = "agent_retailer_inventory"
    role = AgentRole.RETAILER_INVENTORY_AGENT
    input_schema = RetailerInventoryInput
    output_schema = RetailerInventoryOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(self, validated_input: RetailerInventoryInput) -> List[str]:
        return [validated_input.recall_id]

    def extract_output_and_evidence_references(
        self, validated_output: RetailerInventoryOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [validated_output.recall_id] + [
            rec.id for rec in validated_output.inventory_records
        ]
        ev_refs = [rec.evidence_id for rec in validated_output.inventory_records]
        return out_refs, ev_refs

    def execute(
        self, validated_input: RetailerInventoryInput, auth: AgentAuthContext
    ) -> RetailerInventoryOutput:
        if auth.boundary is None or auth.boundary.role != OrganizationRole.RETAILER:
            raise AgentBoundaryError(
                "RetailerInventoryAgent requires an authenticated RETAILER boundary."
            )

        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        records = self.invoke_tool(
            auth, "read_retailer_inventory", recall_id=recall.id
        )

        review_ids: List[str] = []
        unknown_qty_ids: List[str] = []
        gaps: List[str] = []

        if not records:
            gaps.append(
                "No retailer inventory records exist; missing records do NOT imply zero affected stock."
            )

        for rec in records:
            if rec.quantity_on_hand is None or rec.quantity_quarantined is None:
                unknown_qty_ids.append(rec.id)
            if rec.inventory_status in {
                InventoryStatus.QUARANTINE_REQUIRED,
                InventoryStatus.AWAITING_EVIDENCE,
                InventoryStatus.UNKNOWN,
            }:
                review_ids.append(rec.id)

        if unknown_qty_ids:
            gaps.append(
                f"{len(unknown_qty_ids)} inventory record(s) have UNKNOWN (NULL) stock quantities."
            )

        assessment_status = (
            VerificationStatus.VERIFIED
            if (records and not review_ids and not unknown_qty_ids)
            else VerificationStatus.AWAITING_EVIDENCE
        )

        return RetailerInventoryOutput(
            recall_id=recall.id,
            retailer_org_id=auth.boundary.organization_id,
            boundary_id=auth.boundary.data_boundary_id,
            inventory_records=records,
            records_requiring_human_review=review_ids,
            unknown_quantity_record_ids=unknown_qty_ids,
            assessment_status=assessment_status,
            unresolved_gaps=gaps,
        )


class IndependentVerificationAgent(
    BaseAgent[IndependentVerificationInput, IndependentVerificationOutput]
):
    """Agent 6 — Challenges claims against independent evidence and records verification findings."""

    agent_id = "agent_independent_verification"
    role = AgentRole.INDEPENDENT_VERIFICATION_AGENT
    input_schema = IndependentVerificationInput
    output_schema = IndependentVerificationOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(
        self, validated_input: IndependentVerificationInput
    ) -> List[str]:
        refs = [validated_input.recall_id, validated_input.primary_evidence_id]
        if validated_input.corroborating_evidence_id:
            refs.append(validated_input.corroborating_evidence_id)
        return refs

    def extract_output_and_evidence_references(
        self, validated_output: IndependentVerificationOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [f.id for f in validated_output.findings_created]
        ev_refs = [
            f.evidence_id
            for f in validated_output.findings_created
            if f.evidence_id is not None
        ]
        return out_refs, ev_refs

    def execute(
        self, validated_input: IndependentVerificationInput, auth: AgentAuthContext
    ) -> IndependentVerificationOutput:
        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        primary_ev = self.invoke_tool(
            auth, "read_evidence_record", evidence_id=validated_input.primary_evidence_id
        )

        contradictions: List[str] = []
        missing_items: List[str] = []
        findings = []

        corroborating_ev = None
        if validated_input.corroborating_evidence_id:
            corroborating_ev = self.invoke_tool(
                auth,
                "read_evidence_record",
                evidence_id=validated_input.corroborating_evidence_id,
            )
            if corroborating_ev.id == primary_ev.id:
                contradictions.append(
                    "Corroborating evidence must be distinct from primary evidence."
                )
        else:
            missing_items.append(
                "No independent corroborating evidence_id provided for verification."
            )

        # Check claims against available evidence metadata/notes
        for claim in validated_input.claims:
            if claim.source_evidence_id != primary_ev.id and (
                corroborating_ev is None or claim.source_evidence_id != corroborating_ev.id
            ):
                contradictions.append(
                    f"Claim '{claim.field_name}' cites unverified/unlinked evidence '{claim.source_evidence_id}'."
                )

        # Quantity reconciliation check
        if (
            validated_input.expected_quantity is not None
            and validated_input.reported_quantity is not None
        ):
            if validated_input.expected_quantity != validated_input.reported_quantity:
                msg = (
                    f"Quantity discrepancy: expected {validated_input.expected_quantity} "
                    f"vs reported {validated_input.reported_quantity}."
                )
                contradictions.append(msg)
                findings.append(
                    self.invoke_tool(
                        auth,
                        "create_verification_finding",
                        recall_id=recall.id,
                        finding_type=FindingType.QUANTITY_RECONCILIATION,
                        status=VerificationStatus.DISCREPANCY,
                        summary=msg,
                        expected_quantity=validated_input.expected_quantity,
                        reported_quantity=validated_input.reported_quantity,
                        evidence_id=primary_ev.id,
                    )
                )
        else:
            missing_items.append(
                "Expected and/or reported quantities are UNKNOWN (NULL); cannot reconcile quantities."
            )

        # Record any source-internal contradictions strictly through ToolRegistry (deduplicated & audited)
        for item in validated_input.source_contradictions:
            if not isinstance(item, dict):
                continue
            c_summary = str(item.get("summary") or "").strip()
            if not c_summary:
                continue
            contradictions.append(c_summary)
            findings.append(
                self.invoke_tool(
                    auth,
                    "create_verification_finding",
                    recall_id=recall.id,
                    finding_type=FindingType.DISCREPANCY,
                    status=VerificationStatus.DISCREPANCY,
                    summary=c_summary,
                    evidence_id=primary_ev.id,
                )
            )

        # Policy gate: VERIFIED requires distinct corroborating evidence, zero contradictions, zero missing items
        policy_satisfied = (
            corroborating_ev is not None
            and corroborating_ev.id != primary_ev.id
            and not contradictions
            and not missing_items
        )

        if contradictions:
            overall_status = VerificationStatus.DISCREPANCY
            if not findings:
                findings.append(
                    self.invoke_tool(
                        auth,
                        "create_verification_finding",
                        recall_id=recall.id,
                        finding_type=FindingType.DISCREPANCY,
                        status=VerificationStatus.DISCREPANCY,
                        summary="; ".join(contradictions),
                        evidence_id=primary_ev.id,
                    )
                )
        elif policy_satisfied:
            overall_status = VerificationStatus.VERIFIED
            findings.append(
                self.invoke_tool(
                    auth,
                    "create_verification_finding",
                    recall_id=recall.id,
                    finding_type=FindingType.QUANTITY_RECONCILIATION,
                    status=VerificationStatus.VERIFIED,
                    summary=(
                        f"Independent verification policy satisfied using distinct evidence "
                        f"'{primary_ev.id}' and '{corroborating_ev.id}'."
                    ),
                    expected_quantity=validated_input.expected_quantity,
                    reported_quantity=validated_input.reported_quantity,
                    evidence_id=corroborating_ev.id,
                )
            )
        else:
            overall_status = VerificationStatus.AWAITING_EVIDENCE
            findings.append(
                self.invoke_tool(
                    auth,
                    "create_verification_finding",
                    recall_id=recall.id,
                    finding_type=FindingType.EVIDENCE_MISSING,
                    status=VerificationStatus.AWAITING_EVIDENCE,
                    summary="; ".join(missing_items),
                    evidence_id=primary_ev.id,
                )
            )

        return IndependentVerificationOutput(
            recall_id=recall.id,
            overall_status=overall_status,
            policy_satisfied_for_verified=policy_satisfied,
            contradictions_detected=contradictions,
            missing_evidence_items=missing_items,
            findings_created=findings,
        )


class RecallResponseCoordinatorAgent(BaseAgent[CoordinatorInput, CoordinatorOutput]):
    """Agent 7 — Coordinates workflow status, consolidates findings, and proposes PENDING human approvals."""

    agent_id = "agent_response_coordinator"
    role = AgentRole.RECALL_RESPONSE_COORDINATOR
    input_schema = CoordinatorInput
    output_schema = CoordinatorOutput
    execution_mode = ExecutionMode.DETERMINISTIC

    def extract_input_references(self, validated_input: CoordinatorInput) -> List[str]:
        return [validated_input.recall_id]

    def extract_output_and_evidence_references(
        self, validated_output: CoordinatorOutput
    ) -> tuple[List[str], List[str]]:
        out_refs = [validated_output.recall_id] + [
            p.id for p in validated_output.proposed_approvals
        ]
        return out_refs, []

    def execute(
        self, validated_input: CoordinatorInput, auth: AgentAuthContext
    ) -> CoordinatorOutput:
        recall = self.invoke_tool(
            auth, "read_recall_notice", recall_id=validated_input.recall_id
        )
        batches = self.invoke_tool(
            auth, "list_recall_batches", recall_id=recall.id
        )
        acks = self.invoke_tool(
            auth, "read_recall_acknowledgements", recall_id=recall.id
        )
        findings = self.invoke_tool(
            auth, "read_verification_findings", recall_id=recall.id
        )
        executions = self.invoke_tool(auth, "list_agent_executions")

        acked_count = sum(
            1 for a in acks if a.status == AcknowledgementStatus.ACKNOWLEDGED
        )
        pending_acks = len(acks) - acked_count
        discrepancy_count = sum(
            1 for f in findings if f.status == VerificationStatus.DISCREPANCY
        )
        verified_count = sum(
            1 for f in findings if f.status == VerificationStatus.VERIFIED
        )

        recommended: List[str] = [
            AgentRole.RECALL_SOURCE_ANALYST.value,
            AgentRole.RECALL_SCOPE_AGENT.value,
        ]
        if recall.manufacturer_org_id:
            recommended.append(AgentRole.MANUFACTURER_OPERATIONS_AGENT.value)
        recommended.extend(
            [
                AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT.value,
                AgentRole.RETAILER_INVENTORY_AGENT.value,
                AgentRole.INDEPENDENT_VERIFICATION_AGENT.value,
            ]
        )

        unresolved: List[str] = []
        if not batches:
            unresolved.append("No affected batches linked to recall.")
        if discrepancy_count > 0:
            unresolved.append(f"{discrepancy_count} unresolved discrepancy finding(s).")
        if verified_count == 0:
            unresolved.append("No independently verified findings recorded yet.")
        if acked_count == 0:
            unresolved.append("No organization acknowledgements recorded yet.")

        proposed_approvals = []
        if validated_input.propose_human_review_action:
            rationale = (
                f"Human review required for recall '{recall.external_recall_number}': "
                f"{len(batches)} batch(es), {discrepancy_count} discrepancy finding(s), "
                f"{acked_count} acknowledgement(s)."
            )
            proposed_approvals.append(
                self.invoke_tool(
                    auth,
                    "propose_human_approval",
                    recall_id=recall.id,
                    action_type="RECALL_RESPONSE_HUMAN_REVIEW",
                    rationale=rationale,
                )
            )

        if discrepancy_count > 0:
            overall_status = VerificationStatus.DISCREPANCY
        elif verified_count > 0 and not unresolved:
            overall_status = VerificationStatus.VERIFIED
        else:
            overall_status = VerificationStatus.AWAITING_EVIDENCE

        recent_exec_ids = [
            e.id for e in executions if recall.id in e.input_references
        ][:20]

        return CoordinatorOutput(
            recall_id=recall.id,
            recommended_specialist_agents=recommended,
            recent_agent_execution_ids=recent_exec_ids,
            total_batches=len(batches),
            acknowledged_org_count=acked_count,
            pending_or_missing_acknowledgements=pending_acks,
            discrepancy_finding_count=discrepancy_count,
            verified_finding_count=verified_count,
            proposed_approvals=proposed_approvals,
            overall_status=overall_status,
            unresolved_items=unresolved,
        )


AGENT_CLASSES: Dict[AgentRole, Type[BaseAgent]] = {
    AgentRole.RECALL_SOURCE_ANALYST: RecallSourceAnalystAgent,
    AgentRole.RECALL_SCOPE_AGENT: RecallScopeAgent,
    AgentRole.MANUFACTURER_OPERATIONS_AGENT: ManufacturerOperationsAgent,
    AgentRole.DISTRIBUTOR_RECONCILIATION_AGENT: DistributorReconciliationAgent,
    AgentRole.RETAILER_INVENTORY_AGENT: RetailerInventoryAgent,
    AgentRole.INDEPENDENT_VERIFICATION_AGENT: IndependentVerificationAgent,
    AgentRole.RECALL_RESPONSE_COORDINATOR: RecallResponseCoordinatorAgent,
}


def build_agent(
    role: AgentRole,
    *,
    db_path: str | None,
    tool_registry: ToolRegistry | None = None,
    timeout_seconds: float = 5.0,
    lyzr_adapter: Any = None,
) -> BaseAgent:
    """Instantiate a specialized agent bound to the backend ToolRegistry."""
    registry = tool_registry or ToolRegistry()
    agent_cls = AGENT_CLASSES[role]
    if role == AgentRole.RECALL_SOURCE_ANALYST:
        return agent_cls(
            db_path=db_path,
            tool_registry=registry,
            timeout_seconds=timeout_seconds,
            lyzr_adapter=lyzr_adapter,
        )
    return agent_cls(
        db_path=db_path,
        tool_registry=registry,
        timeout_seconds=timeout_seconds,
    )
