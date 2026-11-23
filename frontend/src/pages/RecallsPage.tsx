import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { EvidenceItem, OrganizationRecord } from "../types/api";
import type { AffectedBatch, RecallNotice, RecallSeverity, RecallStatus } from "../types/domain";
import {
  EmptyState,
  FeedbackBanner,
  formatQuantity,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

interface RecallsPageProps {
  onOpenTraceability: (recallId: string) => void;
}

export function RecallsPage({ onOpenTraceability }: RecallsPageProps) {
  const { credentials, sessionVersion } = useSession();
  const [recalls, setRecalls] = useState<RecallNotice[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [batches, setBatches] = useState<AffectedBatch[]>([]);
  const [evidenceList, setEvidenceList] = useState<EvidenceItem[]>([]);
  const [orgs, setOrgs] = useState<OrganizationRecord[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  // Admin Create Recall Form
  const [externalNumber, setExternalNumber] = useState("");
  const [title, setTitle] = useState("");
  const [authority, setAuthority] = useState("");
  const [productDesc, setProductDesc] = useState("");
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState<RecallSeverity>("UNKNOWN");
  const [statusVal, setStatusVal] = useState<RecallStatus>("AWAITING_EVIDENCE");
  const [evidenceId, setEvidenceId] = useState("");
  const [manufacturerOrgId, setManufacturerOrgId] = useState("");

  // Admin Add Batch Form
  const [batchCode, setBatchCode] = useState("");
  const [batchProduct, setBatchProduct] = useState("");
  const [batchQty, setBatchQty] = useState("");
  const [batchUnit, setBatchUnit] = useState("");
  const [batchEvidenceId, setBatchEvidenceId] = useState("");

  const loadRecalls = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const list = await recallRelayApi.listRecalls(credentials, signal);
        setRecalls(list);
        if (list.length > 0 && !selectedId) {
          setSelectedId(list[0].id);
        }
        if (credentials.mode === "admin") {
          const [evs, orgList] = await Promise.all([
            recallRelayApi.listEvidence(credentials, signal),
            recallRelayApi.listOrganizations(credentials, signal),
          ]);
          setEvidenceList(evs);
          setOrgs(orgList);
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load recall notices.",
        });
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials, selectedId]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadRecalls(controller.signal);
    return () => controller.abort();
  }, [loadRecalls, sessionVersion]);

  useEffect(() => {
    if (!selectedId) {
      setBatches([]);
      return;
    }
    const controller = new AbortController();
    recallRelayApi
      .listBatches(credentials, selectedId, controller.signal)
      .then(setBatches)
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setBatches([]);
      });
    return () => controller.abort();
  }, [credentials, selectedId, sessionVersion]);

  const filtered = recalls.filter((r) => {
    const q = search.trim().toLowerCase();
    if (!q) return true;
    return (
      r.external_recall_number.toLowerCase().includes(q) ||
      r.title.toLowerCase().includes(q) ||
      r.issuing_authority.toLowerCase().includes(q)
    );
  });

  const selectedRecall = recalls.find((r) => r.id === selectedId) ?? null;

  async function handleCreateRecall(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    if (statusVal === "CLOSED") {
      setFeedback({
        kind: "error",
        message:
          "Cannot register a new recall as CLOSED without verified downstream evidence and acknowledgements.",
      });
      return;
    }
    try {
      const created = await recallRelayApi.createRecall(credentials, {
        external_recall_number: externalNumber.trim(),
        title: title.trim(),
        issuing_authority: authority.trim(),
        product_description: productDesc.trim(),
        reason: reason.trim(),
        severity,
        status: statusVal,
        evidence_id: evidenceId.trim(),
        manufacturer_org_id: manufacturerOrgId.trim() || null,
      });
      setExternalNumber("");
      setTitle("");
      setAuthority("");
      setProductDesc("");
      setReason("");
      setSelectedId(created.id);
      setFeedback({ kind: "success", message: `Recall notice ${created.external_recall_number} created.` });
      await loadRecalls();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to create recall notice.",
      });
    }
  }

  async function handleAddBatch(e: FormEvent) {
    e.preventDefault();
    if (!selectedRecall) return;
    setFeedback(null);
    const parsedQty = batchQty.trim() === "" ? null : Number(batchQty);
    if (parsedQty !== null && (!Number.isInteger(parsedQty) || parsedQty < 0)) {
      setFeedback({ kind: "error", message: "Quantity must be a non-negative integer or blank (UNKNOWN)." });
      return;
    }
    try {
      await recallRelayApi.createBatch(credentials, selectedRecall.id, {
        batch_code: batchCode.trim(),
        product_name: batchProduct.trim(),
        reported_quantity: parsedQty,
        quantity_unit: batchUnit.trim() || null,
        evidence_id: batchEvidenceId.trim() || selectedRecall.evidence_id,
      });
      setBatchCode("");
      setBatchProduct("");
      setBatchQty("");
      setBatchUnit("");
      const updated = await recallRelayApi.listBatches(credentials, selectedRecall.id);
      setBatches(updated);
      setFeedback({ kind: "success", message: "Affected batch linked to recall." });
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to add batch.",
      });
    }
  }

  async function handleTogglePublicDisclosure(approved: boolean) {
    if (!selectedRecall) return;
    setFeedback(null);
    try {
      await recallRelayApi.setPublicDisclosure(credentials, selectedRecall.id, approved);
      setFeedback({
        kind: "success",
        message: approved
          ? "Recall approved for public disclosure."
          : "Public disclosure revoked for recall.",
      });
      await loadRecalls();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to update disclosure state.",
      });
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <PanelCard
            title="Recall Notices"
            subtitle="Real records returned by GET /api/v1/recalls"
            action={
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter notices..."
                aria-label="Filter recall notices"
                className="rounded-lg border border-peach-border bg-cream/70 px-3 py-1.5 text-xs text-charcoal focus:border-burgundy focus:outline-none"
              />
            }
          >
            {loading ? (
              <p className="text-xs text-mauve">Loading recall notices...</p>
            ) : filtered.length === 0 ? (
              <EmptyState
                title="No recall notices found"
                description="No matching recall notices exist in the backend for your current access level."
              />
            ) : (
              <div className="space-y-2">
                {filtered.map((rec) => {
                  const active = rec.id === selectedId;
                  return (
                    <button
                      key={rec.id}
                      type="button"
                      onClick={() => setSelectedId(rec.id)}
                      className={`w-full rounded-xl border p-3.5 text-left transition ${
                        active
                          ? "border-burgundy bg-peach/40"
                          : "border-peach-border bg-cream/40 hover:bg-peach/20"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-bold text-burgundy-deep">
                          {rec.external_recall_number}
                        </span>
                        <StatusBadge value={rec.severity} />
                      </div>
                      <p className="mt-1 text-sm font-semibold text-charcoal">{rec.title}</p>
                      <p className="mt-0.5 text-xs text-mauve">{rec.issuing_authority}</p>
                    </button>
                  );
                })}
              </div>
            )}
          </PanelCard>
        </div>

        <div className="space-y-6 lg:col-span-7">
          {selectedRecall ? (
            <PanelCard
              title={`${selectedRecall.external_recall_number} — ${selectedRecall.title}`}
              subtitle={`Issuing Authority: ${selectedRecall.issuing_authority} · Evidence ID: ${selectedRecall.evidence_id}`}
              action={
                <button
                  type="button"
                  onClick={() => onOpenTraceability(selectedRecall.id)}
                  className="rounded-lg bg-burgundy px-3.5 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
                >
                  Open Traceability
                </button>
              }
            >
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div className="rounded-xl border border-peach-border bg-cream/60 p-3.5">
                  <p className="text-[11px] font-semibold uppercase text-mauve">Product Scope</p>
                  <p className="mt-1 text-sm text-charcoal">{selectedRecall.product_description}</p>
                </div>
                <div className="rounded-xl border border-peach-border bg-cream/60 p-3.5">
                  <p className="text-[11px] font-semibold uppercase text-mauve">Hazard / Reason</p>
                  <p className="mt-1 text-sm text-charcoal">{selectedRecall.reason}</p>
                </div>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                <StatusBadge value={selectedRecall.severity} />
                <StatusBadge value={selectedRecall.status} />
                {credentials.mode === "admin" && (
                  <div className="ml-auto flex gap-2">
                    <button
                      type="button"
                      onClick={() => void handleTogglePublicDisclosure(true)}
                      className="rounded-lg border border-status-verified-border bg-status-verified-bg px-3 py-1 text-xs font-semibold text-status-verified hover:opacity-90"
                    >
                      Approve Public Disclosure
                    </button>
                    <button
                      type="button"
                      onClick={() => void handleTogglePublicDisclosure(false)}
                      className="rounded-lg border border-peach-border bg-white px-3 py-1 text-xs font-semibold text-charcoal hover:bg-cream"
                    >
                      Revoke Public Disclosure
                    </button>
                  </div>
                )}
              </div>

              <div className="mt-6">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-burgundy">
                  Associated Affected Batches ({batches.length})
                </h3>
                {batches.length === 0 ? (
                  <div className="mt-2">
                    <EmptyState
                      title="No affected batches linked"
                      description="No batch or lot codes have been explicitly associated with this recall yet."
                    />
                  </div>
                ) : (
                  <div className="mt-2 overflow-x-auto rounded-xl border border-peach-border">
                    <table className="w-full text-left text-xs">
                      <thead className="bg-peach/40 text-burgundy-deep">
                        <tr>
                          <th className="px-3 py-2 font-semibold">Batch Code</th>
                          <th className="px-3 py-2 font-semibold">Product</th>
                          <th className="px-3 py-2 font-semibold">Reported Qty</th>
                          <th className="px-3 py-2 font-semibold">Evidence Ref</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-peach-border bg-white">
                        {batches.map((b) => (
                          <tr key={b.id}>
                            <td className="px-3 py-2 font-mono font-semibold text-charcoal">
                              {b.batch_code}
                            </td>
                            <td className="px-3 py-2 text-charcoal">{b.product_name}</td>
                            <td className="px-3 py-2 text-charcoal">
                              {formatQuantity(b.reported_quantity, b.quantity_unit)}
                            </td>
                            <td className="px-3 py-2 font-mono text-mauve">{b.evidence_id}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              {credentials.mode === "admin" && (
                <form
                  onSubmit={(e) => void handleAddBatch(e)}
                  className="mt-5 rounded-xl border border-peach-border bg-cream/60 p-4"
                >
                  <p className="text-xs font-semibold text-burgundy-deep">
                    Associate Affected Batch (Admin)
                  </p>
                  <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                    <input
                      value={batchCode}
                      onChange={(e) => setBatchCode(e.target.value)}
                      placeholder="Batch / Lot code *"
                      required
                      className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs"
                    />
                    <input
                      value={batchProduct}
                      onChange={(e) => setBatchProduct(e.target.value)}
                      placeholder="Product name *"
                      required
                      className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs"
                    />
                    <input
                      value={batchQty}
                      onChange={(e) => setBatchQty(e.target.value)}
                      placeholder="Reported qty (leave blank for UNKNOWN)"
                      className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs"
                    />
                    <input
                      value={batchUnit}
                      onChange={(e) => setBatchUnit(e.target.value)}
                      placeholder="Unit (e.g., cases, units)"
                      className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs"
                    />
                    <input
                      value={batchEvidenceId}
                      onChange={(e) => setBatchEvidenceId(e.target.value)}
                      placeholder={`Evidence ID (defaults to ${selectedRecall.evidence_id})`}
                      className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs sm:col-span-2"
                    />
                  </div>
                  <button
                    type="submit"
                    className="mt-3 rounded-lg bg-burgundy px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-burgundy-deep"
                  >
                    Link Batch
                  </button>
                </form>
              )}
            </PanelCard>
          ) : (
            <PanelCard title="Recall Details">
              <EmptyState
                title="Select a recall notice"
                description="Choose a recall notice on the left to inspect its provenance, affected batches, and traceability."
              />
            </PanelCard>
          )}

          {credentials.mode === "admin" && (
            <PanelCard
              title="Register Official Recall Notice (Admin)"
              subtitle="Requires a registered source evidence_id; defaults to private until public disclosure is approved"
            >
              <form onSubmit={(e) => void handleCreateRecall(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <input
                  value={externalNumber}
                  onChange={(e) => setExternalNumber(e.target.value)}
                  placeholder="Official external recall number *"
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                />
                <input
                  value={authority}
                  onChange={(e) => setAuthority(e.target.value)}
                  placeholder="Issuing authority *"
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                />
                <input
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Recall notice title *"
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs sm:col-span-2"
                />
                <input
                  value={productDesc}
                  onChange={(e) => setProductDesc(e.target.value)}
                  placeholder="Product description *"
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                />
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="Hazard / reason *"
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                />
                <select
                  value={severity}
                  onChange={(e) => setSeverity(e.target.value as RecallSeverity)}
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                >
                  <option value="UNKNOWN">Severity: UNKNOWN</option>
                  <option value="CLASS_I">CLASS_I</option>
                  <option value="CLASS_II">CLASS_II</option>
                  <option value="CLASS_III">CLASS_III</option>
                  <option value="HIGH">HIGH</option>
                  <option value="MEDIUM">MEDIUM</option>
                  <option value="LOW">LOW</option>
                </select>
                <select
                  value={statusVal}
                  onChange={(e) => setStatusVal(e.target.value as RecallStatus)}
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                >
                  <option value="AWAITING_EVIDENCE">AWAITING_EVIDENCE</option>
                  <option value="INITIATED">INITIATED</option>
                  <option value="IN_PROGRESS">IN_PROGRESS</option>
                  <option value="AWAITING_APPROVAL">AWAITING_APPROVAL</option>
                </select>
                <select
                  value={evidenceId}
                  onChange={(e) => setEvidenceId(e.target.value)}
                  required
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                >
                  <option value="">Select supporting evidence_id *</option>
                  {evidenceList.map((ev) => (
                    <option key={ev.id} value={ev.id}>
                      {ev.id} — {ev.record_reference} ({ev.evidence_stage})
                    </option>
                  ))}
                </select>
                <select
                  value={manufacturerOrgId}
                  onChange={(e) => setManufacturerOrgId(e.target.value)}
                  className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
                >
                  <option value="">Optional manufacturer org (Unlinked)</option>
                  {orgs
                    .filter((o) => o.role === "MANUFACTURER")
                    .map((o) => (
                      <option key={o.id} value={o.id}>
                        {o.name} ({o.identity_verification_status})
                      </option>
                    ))}
                </select>
                <div className="sm:col-span-2">
                  <button
                    type="submit"
                    className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
                  >
                    Create Recall Notice
                  </button>
                </div>
              </form>
            </PanelCard>
          )}
        </div>
      </div>
    </div>
  );
}
