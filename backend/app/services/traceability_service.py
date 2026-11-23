"""Recall traceability service honoring organization boundaries and missing-evidence gaps."""

from __future__ import annotations

from typing import List

from app.db.database import get_db_connection
from app.schemas.api_v1 import (
    EvidenceResponse,
    TraceabilityGap,
    TraceabilityResponse,
)
from app.schemas.domain import (
    InventoryRecord,
    OrganizationBoundaryContext,
    ShipmentRecord,
    VerificationStatus,
)
from app.services.evidence_service import _row_to_evidence, get_evidence_by_id
from app.services.recall_service import get_recall_by_id, list_batches_for_recall
from app.services.supply_chain_service import (
    list_inventory_for_boundary,
    list_shipments_for_boundary,
)


def build_recall_traceability(
    db_path: str | None,
    recall_id: str,
    caller: OrganizationBoundaryContext | None = None,
) -> TraceabilityResponse:
    """Trace a recall from originating evidence through batches and boundary-authorized records.

    Never manufactures missing shipments/inventory and never exposes private records
    belonging to other organizations' data boundaries.
    """
    recall = get_recall_by_id(db_path, recall_id)
    originating_evidence = get_evidence_by_id(db_path, recall.evidence_id)
    batches = list_batches_for_recall(db_path, recall_id)

    visible_shipments: List[ShipmentRecord] = (
        list_shipments_for_boundary(db_path, caller, recall_id=recall_id)
        if caller is not None
        else []
    )
    visible_inventory: List[InventoryRecord] = (
        list_inventory_for_boundary(db_path, caller, recall_id=recall_id)
        if caller is not None
        else []
    )

    with get_db_connection(db_path) as conn:
        total_shipments = conn.execute(
            "SELECT COUNT(*) AS c FROM shipment_records WHERE recall_id = ?",
            (recall_id,),
        ).fetchone()["c"]
        total_inventory = conn.execute(
            "SELECT COUNT(*) AS c FROM inventory_records WHERE recall_id = ?",
            (recall_id,),
        ).fetchone()["c"]

        evidence_ids = {recall.evidence_id}
        evidence_ids.update(b.evidence_id for b in batches)
        evidence_ids.update(s.evidence_id for s in visible_shipments)
        evidence_ids.update(i.evidence_id for i in visible_inventory)

        placeholders = ",".join("?" for _ in evidence_ids)
        ev_rows = conn.execute(
            f"SELECT * FROM evidence_records WHERE id IN ({placeholders}) ORDER BY id ASC",
            tuple(sorted(evidence_ids)),
        ).fetchall()
        evidence_references: List[EvidenceResponse] = [
            _row_to_evidence(row) for row in ev_rows
        ]

    missing_information: List[TraceabilityGap] = []
    unresolved_relationships: List[TraceabilityGap] = []

    if originating_evidence.verification_status != VerificationStatus.VERIFIED:
        missing_information.append(
            TraceabilityGap(
                gap_type="UNVERIFIED_SOURCE_EVIDENCE",
                status=originating_evidence.verification_status,
                message=(
                    f"Originating evidence '{originating_evidence.id}' is currently "
                    f"'{originating_evidence.verification_status.value}' "
                    f"(stage: {originating_evidence.evidence_stage.value})."
                ),
                related_id=originating_evidence.id,
            )
        )

    if recall.manufacturer_org_id is None:
        unresolved_relationships.append(
            TraceabilityGap(
                gap_type="UNLINKED_MANUFACTURER",
                status=VerificationStatus.UNKNOWN,
                message="Recall notice is not linked to a registered manufacturer organization.",
                related_id=recall.id,
            )
        )

    if not batches:
        missing_information.append(
            TraceabilityGap(
                gap_type="NO_AFFECTED_BATCHES",
                status=VerificationStatus.AWAITING_EVIDENCE,
                message="No affected batches have been recorded for this recall yet.",
                related_id=recall.id,
            )
        )

    for batch in batches:
        if batch.reported_quantity is None:
            missing_information.append(
                TraceabilityGap(
                    gap_type="UNKNOWN_BATCH_QUANTITY",
                    status=VerificationStatus.UNKNOWN,
                    message=f"Batch '{batch.batch_code}' reported_quantity is UNKNOWN (NULL).",
                    related_id=batch.id,
                )
            )

    if total_shipments == 0:
        unresolved_relationships.append(
            TraceabilityGap(
                gap_type="NO_DOWNSTREAM_SHIPMENTS",
                status=VerificationStatus.AWAITING_EVIDENCE,
                message="No downstream shipment records exist for this recall.",
                related_id=recall.id,
            )
        )
    elif caller is None or len(visible_shipments) < total_shipments:
        unresolved_relationships.append(
            TraceabilityGap(
                gap_type="BOUNDARY_RESTRICTED_SHIPMENTS",
                status=VerificationStatus.NOT_CONNECTED,
                message=(
                    "One or more shipment records belong to external organization boundaries "
                    "and are not visible to the current caller."
                ),
                related_id=recall.id,
            )
        )

    if total_inventory == 0:
        missing_information.append(
            TraceabilityGap(
                gap_type="NO_INVENTORY_REPORTS",
                status=VerificationStatus.AWAITING_EVIDENCE,
                message="No organization inventory reports exist for this recall.",
                related_id=recall.id,
            )
        )
    elif caller is None or len(visible_inventory) < total_inventory:
        unresolved_relationships.append(
            TraceabilityGap(
                gap_type="BOUNDARY_RESTRICTED_INVENTORY",
                status=VerificationStatus.NOT_CONNECTED,
                message=(
                    "One or more inventory records belong to external organization boundaries "
                    "and are not visible to the current caller."
                ),
                related_id=recall.id,
            )
        )

    if not missing_information and not unresolved_relationships:
        traceability_status = VerificationStatus.VERIFIED
    elif any(
        gap.status == VerificationStatus.NOT_CONNECTED
        for gap in unresolved_relationships
    ):
        traceability_status = VerificationStatus.NOT_CONNECTED
    else:
        traceability_status = VerificationStatus.AWAITING_EVIDENCE

    return TraceabilityResponse(
        recall=recall,
        originating_evidence=originating_evidence,
        batches=batches,
        evidence_references=evidence_references,
        caller_boundary_id=caller.data_boundary_id if caller else None,
        visible_shipments=visible_shipments,
        visible_inventory=visible_inventory,
        total_recorded_shipments_in_system=total_shipments,
        total_recorded_inventory_in_system=total_inventory,
        missing_information=missing_information,
        unresolved_relationships=unresolved_relationships,
        traceability_status=traceability_status,
    )
