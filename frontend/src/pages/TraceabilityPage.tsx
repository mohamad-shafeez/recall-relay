import { useEffect, useState } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { TraceabilityData } from "../types/api";
import type { RecallNotice } from "../types/domain";
import {
  EmptyState,
  FeedbackBanner,
  formatQuantity,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

interface TraceabilityPageProps {
  initialRecallId: string | null;
}

export function TraceabilityPage({ initialRecallId }: TraceabilityPageProps) {
  const { credentials, sessionVersion } = useSession();
  const [recalls, setRecalls] = useState<RecallNotice[]>([]);
  const [selectedRecallId, setSelectedRecallId] = useState<string>(initialRecallId ?? "");
  const [trace, setTrace] = useState<TraceabilityData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (initialRecallId) {
      setSelectedRecallId(initialRecallId);
    }
  }, [initialRecallId]);

  useEffect(() => {
    const controller = new AbortController();
    recallRelayApi
      .listRecalls(credentials, controller.signal)
      .then((list) => {
        setRecalls(list);
        if (!selectedRecallId && list.length > 0) {
          setSelectedRecallId(list[0].id);
        }
      })
      .catch(() => {
        setRecalls([]);
      });
    return () => controller.abort();
  }, [credentials, sessionVersion, selectedRecallId]);

  useEffect(() => {
    if (!selectedRecallId) {
      setTrace(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    recallRelayApi
      .getTraceability(credentials, selectedRecallId, controller.signal)
      .then((data) => {
        setTrace(data);
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setTrace(null);
        setError(err instanceof Error ? err.message : "Failed to load recall traceability.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [credentials, selectedRecallId, sessionVersion]);

  return (
    <div className="space-y-6">
      <PanelCard
        title="End-to-End Recall Traceability Chain"
        subtitle="Renders only verified relationships returned by GET /api/v1/recalls/{id}/traceability; missing links remain explicit gaps"
        action={
          <select
            aria-label="Select recall for traceability"
            value={selectedRecallId}
            onChange={(e) => setSelectedRecallId(e.target.value)}
            className="rounded-lg border border-peach-border bg-cream px-3 py-2 text-xs font-semibold text-charcoal focus:border-burgundy focus:outline-none"
          >
            <option value="">Select recall notice...</option>
            {recalls.map((r) => (
              <option key={r.id} value={r.id}>
                {r.external_recall_number} — {r.title}
              </option>
            ))}
          </select>
        }
      >
        {error && <FeedbackBanner kind="error" message={error} />}

        {loading ? (
          <p className="text-xs text-mauve">Loading traceability chain...</p>
        ) : !trace ? (
          <EmptyState
            title="No traceability graph available"
            description="Select an accessible recall notice above to inspect its evidence-to-response chain and unresolved gaps."
          />
        ) : (
          <div className="space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-peach-border bg-peach/35 p-4">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-burgundy">
                  Overall Traceability Status
                </p>
                <p className="mt-0.5 text-sm font-bold text-charcoal">
                  {trace.recall.external_recall_number} — {trace.recall.title}
                </p>
                <p className="text-xs text-mauve">
                  Caller Boundary: {trace.caller_boundary_id ?? "Public / Unbound"} · Recorded
                  Shipments in Network: {trace.total_recorded_shipments_in_system} · Recorded
                  Inventory in Network: {trace.total_recorded_inventory_in_system}
                </p>
              </div>
              <StatusBadge value={trace.traceability_status} />
            </div>

            {/* Visual Stage Chain */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-5">
              {/* Stage 1: Originating Evidence */}
              <div className="rounded-xl border border-peach-border bg-white p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-burgundy">
                  1. Source Evidence
                </p>
                <p className="mt-2 font-mono text-xs font-semibold text-charcoal">
                  {trace.originating_evidence.id}
                </p>
                <p className="mt-1 text-xs text-mauve">
                  {trace.originating_evidence.source_organization}
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <StatusBadge value={trace.originating_evidence.evidence_stage} />
                  <StatusBadge value={trace.originating_evidence.verification_status} />
                </div>
              </div>

              {/* Stage 2: Recall Notice */}
              <div className="rounded-xl border border-peach-border bg-white p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-burgundy">
                  2. Recall Notice
                </p>
                <p className="mt-2 text-xs font-semibold text-charcoal">
                  {trace.recall.external_recall_number}
                </p>
                <p className="mt-1 text-xs text-mauve">{trace.recall.reason}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <StatusBadge value={trace.recall.severity} />
                  <StatusBadge value={trace.recall.status} />
                </div>
              </div>

              {/* Stage 3: Affected Batches */}
              <div className="rounded-xl border border-peach-border bg-white p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-burgundy">
                  3. Affected Batches ({trace.batches.length})
                </p>
                {trace.batches.length === 0 ? (
                  <p className="mt-2 text-xs text-status-pending">AWAITING EVIDENCE</p>
                ) : (
                  <ul className="mt-2 space-y-1.5 text-xs">
                    {trace.batches.map((b) => (
                      <li key={b.id} className="rounded bg-cream px-2 py-1">
                        <span className="font-mono font-semibold">{b.batch_code}</span>
                        <span className="block text-[11px] text-mauve">
                          Qty: {formatQuantity(b.reported_quantity, b.quantity_unit)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {/* Stage 4: Visible Shipments */}
              <div className="rounded-xl border border-peach-border bg-white p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-burgundy">
                  4. Boundary Shipments ({trace.visible_shipments.length})
                </p>
                {trace.visible_shipments.length === 0 ? (
                  <p className="mt-2 text-xs text-mauve">
                    {trace.total_recorded_shipments_in_system > 0
                      ? "NOT CONNECTED (Restricted to external org boundaries)"
                      : "AWAITING EVIDENCE (None recorded)"}
                  </p>
                ) : (
                  <ul className="mt-2 space-y-1.5 text-xs">
                    {trace.visible_shipments.map((s) => (
                      <li key={s.id} className="rounded bg-cream px-2 py-1">
                        <span className="font-mono text-[11px]">To: {s.to_org_id}</span>
                        <span className="block text-[11px] text-mauve">
                          Qty: {formatQuantity(s.quantity_shipped, s.quantity_unit)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {/* Stage 5: Visible Inventory */}
              <div className="rounded-xl border border-peach-border bg-white p-4 shadow-xs">
                <p className="text-[11px] font-bold uppercase tracking-wider text-burgundy">
                  5. Boundary Inventory ({trace.visible_inventory.length})
                </p>
                {trace.visible_inventory.length === 0 ? (
                  <p className="mt-2 text-xs text-mauve">
                    {trace.total_recorded_inventory_in_system > 0
                      ? "NOT CONNECTED (Restricted to external org boundaries)"
                      : "AWAITING EVIDENCE (None recorded)"}
                  </p>
                ) : (
                  <ul className="mt-2 space-y-1.5 text-xs">
                    {trace.visible_inventory.map((inv) => (
                      <li key={inv.id} className="rounded bg-cream px-2 py-1">
                        <StatusBadge value={inv.inventory_status} />
                        <span className="mt-1 block text-[11px] text-mauve">
                          On hand: {formatQuantity(inv.quantity_on_hand, inv.quantity_unit)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            {/* Explicit Missing Evidence & Unresolved Relationship Gaps */}
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="rounded-xl border border-dashed border-status-pending-border bg-status-pending-bg/50 p-4">
                <h3 className="text-xs font-bold uppercase tracking-wide text-status-pending">
                  Missing Information ({trace.missing_information.length})
                </h3>
                {trace.missing_information.length === 0 ? (
                  <p className="mt-2 text-xs text-status-verified">No missing information flags.</p>
                ) : (
                  <ul className="mt-2 space-y-2 text-xs text-charcoal">
                    {trace.missing_information.map((gap, idx) => (
                      <li key={`${gap.gap_type}-${idx}`} className="flex items-start justify-between gap-2">
                        <span>{gap.message}</span>
                        <StatusBadge value={gap.status} />
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="rounded-xl border border-dashed border-status-discrepancy-border bg-status-discrepancy-bg/50 p-4">
                <h3 className="text-xs font-bold uppercase tracking-wide text-status-discrepancy">
                  Unresolved Relationships ({trace.unresolved_relationships.length})
                </h3>
                {trace.unresolved_relationships.length === 0 ? (
                  <p className="mt-2 text-xs text-status-verified">
                    No unresolved cross-organization relationship gaps.
                  </p>
                ) : (
                  <ul className="mt-2 space-y-2 text-xs text-charcoal">
                    {trace.unresolved_relationships.map((gap, idx) => (
                      <li key={`${gap.gap_type}-${idx}`} className="flex items-start justify-between gap-2">
                        <span>{gap.message}</span>
                        <StatusBadge value={gap.status} />
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </div>
        )}
      </PanelCard>
    </div>
  );
}
