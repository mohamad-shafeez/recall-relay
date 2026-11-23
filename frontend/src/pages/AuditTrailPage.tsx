import { useCallback, useEffect, useState } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { AuditChainVerification, AuditEvent } from "../types/api";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

export function AuditTrailPage() {
  const { credentials, sessionVersion } = useSession();
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [verification, setVerification] = useState<AuditChainVerification | null>(null);
  const [loading, setLoading] = useState(true);
  const [verifying, setVerifying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadEvents = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const list = await recallRelayApi.listAuditEvents(credentials, signal);
        setEvents(list);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof Error ? err.message : "Failed to load audit events.");
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadEvents(controller.signal);
    return () => controller.abort();
  }, [loadEvents, sessionVersion]);

  async function handleVerifyChain() {
    setVerifying(true);
    setError(null);
    try {
      const res = await recallRelayApi.verifyAuditChain(credentials);
      setVerification(res);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to verify audit chain.");
    } finally {
      setVerifying(false);
    }
  }

  return (
    <div className="space-y-6">
      {error && <FeedbackBanner kind="error" message={error} />}

      <PanelCard
        title="SHA-256 Hash-Chained Audit Trail"
        subtitle="Tamper-evident ledger from GET /api/v1/a2a/audit-events with automatic secret redaction"
        action={
          <button
            type="button"
            disabled={verifying}
            onClick={() => void handleVerifyChain()}
            className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep disabled:opacity-60"
          >
            {verifying ? "Verifying..." : "Verify Hash Chain"}
          </button>
        }
      >
        {verification && (
          <div className="mb-4">
            <FeedbackBanner
              kind={verification.valid ? "success" : "error"}
              message={
                verification.valid
                  ? `Audit hash chain verified intact across ${verification.event_count} event(s).`
                  : `Audit chain integrity failure detected at sequence #${verification.broken_at_seq}.`
              }
            />
          </div>
        )}

        {loading ? (
          <p className="text-xs text-mauve">Loading audit events...</p>
        ) : events.length === 0 ? (
          <EmptyState
            title="No audit events recorded yet"
            description="Messages, task assignments, sharing grants, approvals, and identity reviews are appended here automatically."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2 font-semibold">Seq &amp; Event</th>
                  <th className="px-3 py-2 font-semibold">Actor</th>
                  <th className="px-3 py-2 font-semibold">Entity</th>
                  <th className="px-3 py-2 font-semibold">Hash Link</th>
                  <th className="px-3 py-2 font-semibold">Timestamp</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {events.map((ev) => (
                  <tr key={ev.id}>
                    <td className="px-3 py-2">
                      <span className="mr-2 font-mono text-mauve">#{ev.seq}</span>
                      <StatusBadge value={ev.event_type} />
                    </td>
                    <td className="px-3 py-2 font-medium text-charcoal">{ev.actor_id}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-mauve">
                      {ev.entity_type}: {ev.entity_id}
                    </td>
                    <td className="px-3 py-2 font-mono text-[10px] text-mauve">
                      <p>prev: {ev.prev_hash.slice(0, 12)}...</p>
                      <p className="text-burgundy-deep">hash: {ev.event_hash.slice(0, 12)}...</p>
                    </td>
                    <td className="px-3 py-2 text-[11px] text-mauve">
                      {formatTimestamp(ev.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelCard>
    </div>
  );
}
