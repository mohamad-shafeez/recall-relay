import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { HumanApprovalRecord } from "../types/api";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

export function ApprovalsPage() {
  const { credentials, sessionVersion } = useSession();
  const [approvals, setApprovals] = useState<HumanApprovalRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [decision, setDecision] = useState<"APPROVED" | "REJECTED">("APPROVED");
  const [reviewerName, setReviewerName] = useState("");
  const [rationale, setRationale] = useState("");
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  const loadApprovals = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const list = await recallRelayApi.listApprovals(credentials, signal);
        setApprovals(list);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load approval requests.",
        });
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadApprovals(controller.signal);
    return () => controller.abort();
  }, [loadApprovals, sessionVersion]);

  async function handleDecide(e: FormEvent) {
    e.preventDefault();
    if (!activeId) return;
    setFeedback(null);
    try {
      const updated = await recallRelayApi.decideApproval(credentials, activeId, {
        decision,
        reviewer_name: reviewerName.trim(),
        rationale: rationale.trim(),
      });
      setActiveId(null);
      setReviewerName("");
      setRationale("");
      setFeedback({
        kind: "success",
        message: `Approval ${updated.id} recorded as ${updated.decision} by ${updated.reviewer_name}. Note: approval records governance sign-off only and does not auto-execute external actions.`,
      });
      await loadApprovals();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to record decision.",
      });
    }
  }

  return (
    <div className="space-y-6">
      <FeedbackBanner
        kind="info"
        message="Governance Boundary: A proposal is not an approved action, and an approved decision is not proof of external completion. Proposing agents are strictly blocked from self-approval."
      />
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <PanelCard
        title={`Human Approval Queue (${approvals.length})`}
        subtitle="Admin-only governance records from GET /api/v1/approvals"
      >
        {loading ? (
          <p className="text-xs text-mauve">Loading approval requests...</p>
        ) : approvals.length === 0 ? (
          <EmptyState
            title="No human approval requests"
            description="When the Recall Response Coordinator proposes a consequential action, it will appear here in PENDING status."
          />
        ) : (
          <div className="space-y-3">
            {approvals.map((apr) => (
              <div
                key={apr.id}
                className="rounded-xl border border-peach-border bg-cream/50 p-4"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <span className="font-mono text-xs font-bold text-burgundy-deep">
                      {apr.id}
                    </span>
                    <span className="mx-2 text-xs text-mauve">·</span>
                    <span className="text-sm font-semibold text-charcoal">{apr.action_type}</span>
                  </div>
                  <StatusBadge value={apr.decision} />
                </div>
                <p className="mt-1.5 text-xs text-charcoal">{apr.rationale ?? "No rationale provided."}</p>
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-mauve">
                  <span>
                    Recall: {apr.recall_id} · Proposed by: {apr.proposing_agent_id ?? "UNKNOWN"} ·
                    Created: {formatTimestamp(apr.created_at)}
                  </span>
                  {apr.decision === "PENDING" ? (
                    <button
                      type="button"
                      onClick={() => setActiveId(apr.id)}
                      className="rounded-lg bg-burgundy px-3 py-1.5 text-xs font-semibold text-white hover:bg-burgundy-deep"
                    >
                      Record Human Decision
                    </button>
                  ) : (
                    <span>
                      Decided by {apr.reviewer_name ?? "UNKNOWN"} at{" "}
                      {formatTimestamp(apr.decided_at)}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </PanelCard>

      {activeId && (
        <PanelCard
          title={`Record Decision for ${activeId}`}
          subtitle="Requires an authorized human reviewer name (agent_* identities are rejected with 403)"
          action={
            <button
              type="button"
              onClick={() => setActiveId(null)}
              className="rounded-lg border border-peach-border px-3 py-1 text-xs font-semibold text-mauve"
            >
              Cancel
            </button>
          }
        >
          <form onSubmit={(e) => void handleDecide(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <select
              value={decision}
              onChange={(e) => setDecision(e.target.value as "APPROVED" | "REJECTED")}
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            >
              <option value="APPROVED">APPROVED</option>
              <option value="REJECTED">REJECTED</option>
            </select>
            <input
              value={reviewerName}
              onChange={(e) => setReviewerName(e.target.value)}
              placeholder="Authorized human reviewer name *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs"
            />
            <textarea
              value={rationale}
              onChange={(e) => setRationale(e.target.value)}
              rows={2}
              placeholder="Decision rationale *"
              required
              className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs sm:col-span-2"
            />
            <button
              type="submit"
              className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep sm:col-span-2"
            >
              Submit Decision
            </button>
          </form>
        </PanelCard>
      )}
    </div>
  );
}
