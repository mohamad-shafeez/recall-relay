import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { SharingGrant } from "../types/api";
import type { InventoryRecord, InventoryStatus, ShipmentRecord } from "../types/domain";
import {
  EmptyState,
  FeedbackBanner,
  formatQuantity,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

export function SupplyChainPage() {
  const { credentials, sessionVersion } = useSession();
  const [shipments, setShipments] = useState<ShipmentRecord[]>([]);
  const [inventory, setInventory] = useState<InventoryRecord[]>([]);
  const [grants, setGrants] = useState<SharingGrant[]>([]);
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  // Shipment form
  const [sRecallId, setSRecallId] = useState("");
  const [sBatchId, setSBatchId] = useState("");
  const [sToOrgId, setSToOrgId] = useState("");
  const [sQty, setSQty] = useState("");
  const [sUnit, setSUnit] = useState("");
  const [sEvidenceId, setSEvidenceId] = useState("");

  // Inventory form
  const [iRecallId, setIRecallId] = useState("");
  const [iBatchId, setIBatchId] = useState("");
  const [iLocation, setILocation] = useState("");
  const [iOnHand, setIOnHand] = useState("");
  const [iQuarantined, setIQuarantined] = useState("");
  const [iStatus, setIStatus] = useState<InventoryStatus>("AWAITING_EVIDENCE");
  const [iEvidenceId, setIEvidenceId] = useState("");

  // Sharing grant form
  const [gRecallId, setGRecallId] = useState("");
  const [gRecipientOrgId, setGRecipientOrgId] = useState("");
  const [gFields, setGFields] = useState("quantity_shipped,batch_code");
  const [gPurpose, setGPurpose] = useState("");
  const [gAuthorizedBy, setGAuthorizedBy] = useState("");

  const loadData = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const [shipList, invList, grantList] = await Promise.all([
          recallRelayApi.listShipments(credentials, undefined, signal),
          recallRelayApi.listInventory(credentials, undefined, signal),
          recallRelayApi.listSharingGrants(credentials, signal),
        ]);
        setShipments(shipList);
        setInventory(invList);
        setGrants(grantList);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load boundary supply chain data.",
        });
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadData(controller.signal);
    return () => controller.abort();
  }, [loadData, sessionVersion]);

  async function handleCreateShipment(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    const qty = sQty.trim() === "" ? null : Number(sQty);
    try {
      await recallRelayApi.createShipment(credentials, {
        recall_id: sRecallId.trim(),
        batch_id: sBatchId.trim(),
        to_org_id: sToOrgId.trim(),
        quantity_shipped: qty,
        quantity_unit: sUnit.trim() || null,
        evidence_id: sEvidenceId.trim(),
      });
      setSQty("");
      setFeedback({ kind: "success", message: "Boundary-owned shipment recorded." });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to record shipment.",
      });
    }
  }

  async function handleCreateInventory(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    try {
      await recallRelayApi.createInventory(credentials, {
        recall_id: iRecallId.trim(),
        batch_id: iBatchId.trim(),
        location_reference: iLocation.trim() || null,
        quantity_on_hand: iOnHand.trim() === "" ? null : Number(iOnHand),
        quantity_quarantined: iQuarantined.trim() === "" ? null : Number(iQuarantined),
        inventory_status: iStatus,
        evidence_id: iEvidenceId.trim(),
      });
      setIOnHand("");
      setIQuarantined("");
      setFeedback({ kind: "success", message: "Boundary-owned inventory record saved." });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to record inventory.",
      });
    }
  }

  async function handleCreateGrant(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    try {
      await recallRelayApi.createSharingGrant(credentials, {
        recall_id: gRecallId.trim(),
        recipient_org_id: gRecipientOrgId.trim(),
        allowed_fields: gFields
          .split(",")
          .map((f) => f.trim())
          .filter(Boolean),
        allowed_evidence_ids: [],
        purpose: gPurpose.trim(),
        authorized_by: gAuthorizedBy.trim(),
      });
      setGPurpose("");
      setFeedback({ kind: "success", message: "Cross-organization sharing grant authorized." });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to create sharing grant.",
      });
    }
  }

  async function handleRevokeGrant(grantId: string) {
    setFeedback(null);
    try {
      await recallRelayApi.revokeSharingGrant(credentials, grantId);
      setFeedback({ kind: "success", message: `Sharing grant ${grantId} revoked.` });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to revoke grant.",
      });
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <PanelCard
          title={`Boundary Shipments (${shipments.length})`}
          subtitle="Strictly scoped to your server-resolved organization boundary; blank quantities remain UNKNOWN"
        >
          {loading ? (
            <p className="text-xs text-mauve">Loading shipments...</p>
          ) : shipments.length === 0 ? (
            <EmptyState
              title="No shipment records in your boundary"
              description="No dispatches have been recorded inside your organization's data boundary."
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-peach-border">
              <table className="w-full text-left text-xs">
                <thead className="bg-peach/40 text-burgundy-deep">
                  <tr>
                    <th className="px-3 py-2 font-semibold">ID</th>
                    <th className="px-3 py-2 font-semibold">Batch</th>
                    <th className="px-3 py-2 font-semibold">To Org</th>
                    <th className="px-3 py-2 font-semibold">Qty</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-peach-border bg-white">
                  {shipments.map((s) => (
                    <tr key={s.id}>
                      <td className="px-3 py-2 font-mono">{s.id}</td>
                      <td className="px-3 py-2 font-mono text-mauve">{s.batch_id}</td>
                      <td className="px-3 py-2 font-mono">{s.to_org_id}</td>
                      <td className="px-3 py-2 font-semibold">
                        {formatQuantity(s.quantity_shipped, s.quantity_unit)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <form onSubmit={(e) => void handleCreateShipment(e)} className="mt-4 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <input value={sRecallId} onChange={(e) => setSRecallId(e.target.value)} placeholder="Recall ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={sBatchId} onChange={(e) => setSBatchId(e.target.value)} placeholder="Batch ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={sToOrgId} onChange={(e) => setSToOrgId(e.target.value)} placeholder="Receiving Org ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={sEvidenceId} onChange={(e) => setSEvidenceId(e.target.value)} placeholder="Evidence ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={sQty} onChange={(e) => setSQty(e.target.value)} placeholder="Qty shipped (blank = UNKNOWN)" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={sUnit} onChange={(e) => setSUnit(e.target.value)} placeholder="Unit" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <button type="submit" className="rounded-lg bg-burgundy px-3.5 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep sm:col-span-2">
              Record Boundary Shipment
            </button>
          </form>
        </PanelCard>

        <PanelCard
          title={`Boundary Inventory (${inventory.length})`}
          subtitle="Recorded stock only — never implies verified physical quarantine without confirmed records"
        >
          {loading ? (
            <p className="text-xs text-mauve">Loading inventory...</p>
          ) : inventory.length === 0 ? (
            <EmptyState
              title="No inventory records in your boundary"
              description="Missing inventory records do not imply zero affected stock."
            />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-peach-border">
              <table className="w-full text-left text-xs">
                <thead className="bg-peach/40 text-burgundy-deep">
                  <tr>
                    <th className="px-3 py-2 font-semibold">Batch</th>
                    <th className="px-3 py-2 font-semibold">On Hand</th>
                    <th className="px-3 py-2 font-semibold">Quarantined</th>
                    <th className="px-3 py-2 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-peach-border bg-white">
                  {inventory.map((inv) => (
                    <tr key={inv.id}>
                      <td className="px-3 py-2 font-mono text-mauve">{inv.batch_id}</td>
                      <td className="px-3 py-2 font-semibold">
                        {formatQuantity(inv.quantity_on_hand, inv.quantity_unit)}
                      </td>
                      <td className="px-3 py-2">
                        {formatQuantity(inv.quantity_quarantined, inv.quantity_unit)}
                      </td>
                      <td className="px-3 py-2">
                        <StatusBadge value={inv.inventory_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <form onSubmit={(e) => void handleCreateInventory(e)} className="mt-4 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <input value={iRecallId} onChange={(e) => setIRecallId(e.target.value)} placeholder="Recall ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={iBatchId} onChange={(e) => setIBatchId(e.target.value)} placeholder="Batch ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={iLocation} onChange={(e) => setILocation(e.target.value)} placeholder="Location reference" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={iEvidenceId} onChange={(e) => setIEvidenceId(e.target.value)} placeholder="Evidence ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={iOnHand} onChange={(e) => setIOnHand(e.target.value)} placeholder="Qty on hand (blank = UNKNOWN)" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <input value={iQuarantined} onChange={(e) => setIQuarantined(e.target.value)} placeholder="Qty quarantined (blank = UNKNOWN)" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
            <select value={iStatus} onChange={(e) => setIStatus(e.target.value as InventoryStatus)} className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs sm:col-span-2">
              <option value="AWAITING_EVIDENCE">AWAITING_EVIDENCE</option>
              <option value="QUARANTINE_REQUIRED">QUARANTINE_REQUIRED</option>
              <option value="QUARANTINED">QUARANTINED</option>
              <option value="NO_AFFECTED_STOCK">NO_AFFECTED_STOCK</option>
              <option value="UNKNOWN">UNKNOWN</option>
            </select>
            <button type="submit" className="rounded-lg bg-burgundy px-3.5 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep sm:col-span-2">
              Record Boundary Inventory
            </button>
          </form>
        </PanelCard>
      </div>

      <PanelCard
        title={`Cross-Organization Information-Sharing Grants (${grants.length})`}
        subtitle="Default DENY: explicit owner authorization is required to disclose limited fields to another organization"
      >
        {grants.length === 0 ? (
          <EmptyState
            title="No active or historical sharing grants"
            description="Cross-organization disclosure is denied by default until the owning organization authorizes a bounded grant."
          />
        ) : (
          <div className="mb-4 overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2 font-semibold">Grant ID</th>
                  <th className="px-3 py-2 font-semibold">Recipient Org</th>
                  <th className="px-3 py-2 font-semibold">Allowed Fields</th>
                  <th className="px-3 py-2 font-semibold">Status</th>
                  <th className="px-3 py-2 font-semibold">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {grants.map((g) => (
                  <tr key={g.id}>
                    <td className="px-3 py-2 font-mono">{g.id}</td>
                    <td className="px-3 py-2 font-mono">{g.recipient_org_id}</td>
                    <td className="px-3 py-2">{g.allowed_fields.join(", ")}</td>
                    <td className="px-3 py-2">
                      <StatusBadge value={g.status} />
                    </td>
                    <td className="px-3 py-2">
                      {g.status === "ACTIVE" && (
                        <button
                          type="button"
                          onClick={() => void handleRevokeGrant(g.id)}
                          className="rounded border border-status-discrepancy-border bg-status-discrepancy-bg px-2.5 py-1 text-[11px] font-semibold text-status-discrepancy"
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <form onSubmit={(e) => void handleCreateGrant(e)} className="grid grid-cols-1 gap-2.5 sm:grid-cols-3">
          <input value={gRecallId} onChange={(e) => setGRecallId(e.target.value)} placeholder="Recall ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
          <input value={gRecipientOrgId} onChange={(e) => setGRecipientOrgId(e.target.value)} placeholder="Recipient Org ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
          <input value={gFields} onChange={(e) => setGFields(e.target.value)} placeholder="Allowed fields (comma-separated) *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
          <input value={gPurpose} onChange={(e) => setGPurpose(e.target.value)} placeholder="Sharing purpose *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs sm:col-span-2" />
          <input value={gAuthorizedBy} onChange={(e) => setGAuthorizedBy(e.target.value)} placeholder="Authorized by (human name) *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-1.5 text-xs" />
          <button type="submit" className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep sm:col-span-3">
            Authorize Bounded Sharing Grant
          </button>
        </form>
      </PanelCard>
    </div>
  );
}
