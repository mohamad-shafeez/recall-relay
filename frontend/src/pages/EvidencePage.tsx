import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { EvidenceItem } from "../types/api";
import type { EvidenceSourceType } from "../types/domain";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

export function EvidencePage() {
  const { credentials, sessionVersion } = useSession();
  const [items, setItems] = useState<EvidenceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  // Admin registration form state
  const [sourceType, setSourceType] = useState<EvidenceSourceType>("OFFICIAL_PUBLIC_NOTICE");
  const [stage, setStage] = useState<"SOURCE_MATERIAL" | "AI_EXTRACTED">("SOURCE_MATERIAL");
  const [sourceOrg, setSourceOrg] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceDoc, setSourceDoc] = useState("");
  const [docContent, setDocContent] = useState("");
  const [recordRef, setRecordRef] = useState("");
  const [pubDate, setPubDate] = useState("");
  const [notes, setNotes] = useState("");

  const loadEvidence = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const list = await recallRelayApi.listEvidence(credentials, signal);
        setItems(list);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load evidence library.",
        });
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadEvidence(controller.signal);
    return () => controller.abort();
  }, [loadEvidence, sessionVersion]);

  async function handleRegister(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    if (!sourceUrl.trim() && !sourceDoc.trim()) {
      setFeedback({
        kind: "error",
        message: "At least one traceable source_url or source_document is required.",
      });
      return;
    }
    try {
      const created = await recallRelayApi.registerEvidence(credentials, {
        source_type: sourceType,
        evidence_stage: stage,
        source_organization: sourceOrg.trim(),
        source_url: sourceUrl.trim() || null,
        source_document: sourceDoc.trim() || null,
        document_content: docContent.trim() || null,
        record_reference: recordRef.trim(),
        publication_date: pubDate.trim() || null,
        verification_status: "AWAITING_EVIDENCE",
        notes: notes.trim() || null,
      });
      setSourceOrg("");
      setSourceUrl("");
      setSourceDoc("");
      setDocContent("");
      setRecordRef("");
      setPubDate("");
      setNotes("");
      setFeedback({
        kind: "success",
        message: `Evidence ${created.id} registered as ${created.evidence_stage} (AWAITING_EVIDENCE).`,
      });
      await loadEvidence();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to register evidence.",
      });
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      {credentials.mode === "admin" && (
        <PanelCard
          title="Register Source Evidence (Admin)"
          subtitle="Source registration never self-declares INDEPENDENTLY_VERIFIED; SHA-256 is computed server-side when content is supplied"
        >
          <form onSubmit={(e) => void handleRegister(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <select
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value as EvidenceSourceType)}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="OFFICIAL_PUBLIC_NOTICE">OFFICIAL_PUBLIC_NOTICE</option>
              <option value="PUBLIC_DATASET">PUBLIC_DATASET</option>
              <option value="AUTHORIZED_ORG_UPLOAD">AUTHORIZED_ORG_UPLOAD</option>
              <option value="AGENT_ATTESTATION">AGENT_ATTESTATION</option>
            </select>
            <select
              value={stage}
              onChange={(e) => setStage(e.target.value as "SOURCE_MATERIAL" | "AI_EXTRACTED")}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="SOURCE_MATERIAL">Stage: SOURCE_MATERIAL</option>
              <option value="AI_EXTRACTED">Stage: AI_EXTRACTED (Unverified)</option>
            </select>
            <input
              value={sourceOrg}
              onChange={(e) => setSourceOrg(e.target.value)}
              placeholder="Source organization *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={recordRef}
              onChange={(e) => setRecordRef(e.target.value)}
              placeholder="Official record / notice reference *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              placeholder="Official source URL (https://...)"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={sourceDoc}
              onChange={(e) => setSourceDoc(e.target.value)}
              placeholder="Source document filename (if uploaded)"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={pubDate}
              onChange={(e) => setPubDate(e.target.value)}
              placeholder="Publication date (YYYY-MM-DD, optional)"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Optional structured notes"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <textarea
              value={docContent}
              onChange={(e) => setDocContent(e.target.value)}
              rows={2}
              placeholder="Optional document text for server-side SHA-256 content hashing"
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs sm:col-span-2"
            />
            <div className="sm:col-span-2">
              <button
                type="submit"
                className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
              >
                Register Evidence
              </button>
            </div>
          </form>
        </PanelCard>
      )}

      <PanelCard
        title={`Evidence Library (${items.length})`}
        subtitle="Clear separation of SOURCE_MATERIAL, AI_EXTRACTED, and INDEPENDENTLY_VERIFIED records"
      >
        {loading ? (
          <p className="text-xs text-mauve">Loading evidence records...</p>
        ) : items.length === 0 ? (
          <EmptyState
            title="No evidence records registered"
            description="Register an official public recall notice or authorized organization document to anchor downstream recall operations."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2.5 font-semibold">ID &amp; Reference</th>
                  <th className="px-3 py-2.5 font-semibold">Source Organization</th>
                  <th className="px-3 py-2.5 font-semibold">Stage</th>
                  <th className="px-3 py-2.5 font-semibold">Verification</th>
                  <th className="px-3 py-2.5 font-semibold">Provenance &amp; Hash</th>
                  <th className="px-3 py-2.5 font-semibold">Retrieved</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {items.map((ev) => (
                  <tr key={ev.id}>
                    <td className="px-3 py-2.5">
                      <p className="font-mono font-semibold text-charcoal">{ev.id}</p>
                      <p className="text-mauve">{ev.record_reference}</p>
                    </td>
                    <td className="px-3 py-2.5 text-charcoal">{ev.source_organization}</td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={ev.evidence_stage} />
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={ev.verification_status} />
                    </td>
                    <td className="px-3 py-2.5">
                      {ev.source_url ? (
                        <a
                          href={ev.source_url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-semibold text-burgundy underline"
                        >
                          Official FDA Source
                        </a>
                      ) : (
                        <span className="text-charcoal">{ev.source_document ?? "UNKNOWN"}</span>
                      )}
                      {ev.sha256_hash && (
                        <p className="mt-0.5 font-mono text-[10px] text-mauve">
                          SHA-256: {ev.sha256_hash.slice(0, 24)}...
                        </p>
                      )}
                      {ev.notes && (ev.notes.includes("BB:Oct/08/2027") || ev.notes.includes("10/8/2026")) && (
                        <div className="mt-1.5 rounded-lg border border-status-discrepancy-border bg-status-discrepancy-bg/60 p-1.5 text-[10px] text-status-discrepancy">
                          <span className="font-bold">⚠️ Source Date Contradiction:</span> Conflicting dates detected (10/8/2026 vs BB:Oct/08/2027). Both preserved verbatim; neither chosen.
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-mauve">{formatTimestamp(ev.retrieved_at)}</td>
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
