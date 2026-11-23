import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type {
  AgentDescriptor,
  AgentExecutionAudit,
  AgentExecutionEnvelope,
  AgentRole,
} from "../types/api";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

const AGENT_RESPONSIBILITIES: Record<AgentRole, string> = {
  RECALL_SOURCE_ANALYST:
    "Reads authorized source evidence and deterministically extracts explicitly present fields with provenance.",
  RECALL_SCOPE_AGENT:
    "Inspects recall notices, originating evidence, and explicitly linked affected batches without inventing scope.",
  MANUFACTURER_OPERATIONS_AGENT:
    "Operates inside an authenticated MANUFACTURER boundary to inspect dispatches and missing acknowledgements.",
  DISTRIBUTOR_RECONCILIATION_AGENT:
    "Operates inside an authenticated DISTRIBUTOR boundary to reconcile outgoing shipments against known incoming totals.",
  RETAILER_INVENTORY_AGENT:
    "Operates inside an authenticated RETAILER boundary to inspect stock and flag records requiring human review.",
  INDEPENDENT_VERIFICATION_AGENT:
    "Challenges claims against distinct corroborating evidence and persists structured verification findings.",
  RECALL_RESPONSE_COORDINATOR:
    "Consolidates findings, tracks specialist executions, and proposes strictly PENDING human approvals.",
};

export function AgentOperationsPage() {
  const { credentials, sessionVersion } = useSession();
  const [agents, setAgents] = useState<AgentDescriptor[]>([]);
  const [executions, setExecutions] = useState<AgentExecutionAudit[]>([]);
  const [selectedRole, setSelectedRole] = useState<AgentRole>("RECALL_SCOPE_AGENT");
  const [payloadJson, setPayloadJson] = useState('{"recall_id": ""}');
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [lastResult, setLastResult] = useState<AgentExecutionEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  const loadData = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const [agentList, execList] = await Promise.all([
          recallRelayApi.listAgents(credentials, signal),
          recallRelayApi.listAgentExecutions(credentials, signal),
        ]);
        setAgents(agentList);
        setExecutions(execList);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load agent operations data.",
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

  async function handleRunAgent(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    setRunning(true);
    try {
      const parsed = JSON.parse(payloadJson) as Record<string, unknown>;
      const freshKey =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `agent_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
      const effectiveKey = idempotencyKey.trim() || freshKey;
      const res = await recallRelayApi.runAgent(
        credentials,
        selectedRole,
        parsed,
        effectiveKey
      );
      setLastResult(res);
      setFeedback({
        kind: res.execution_status === "COMPLETED" ? "success" : "error",
        message: `Execution ${res.execution_id} finished with status ${res.execution_status}${
          res.reused_idempotent_result ? " (reused idempotent result)" : ""
        }.`,
      });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Agent execution failed.",
      });
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      <FeedbackBanner
        kind="info"
        message="Execution Mode Notice: All seven specialized agents currently run in DETERMINISTIC rule-based mode. No external LLM or sponsor reasoning APIs are connected yet."
      />

      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <PanelCard
        title={`Registered Specialized Agents (${agents.length})`}
        subtitle="Live registry from GET /api/v1/agents with backend-enforced tool permissions"
      >
        {loading ? (
          <p className="text-xs text-mauve">Loading agent registry...</p>
        ) : agents.length === 0 ? (
          <EmptyState
            title="Agent registry unavailable"
            description="No agent descriptors were returned by the backend."
          />
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {agents.map((ag) => {
              const latestExec = executions.find((ex) => ex.agent_role === ag.role);
              return (
                <div
                  key={ag.agent_id}
                  className="flex flex-col justify-between rounded-xl border border-peach-border bg-cream/50 p-4"
                >
                  <div>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-xs font-bold text-burgundy-deep">
                        {ag.agent_id}
                      </span>
                      <StatusBadge value={ag.execution_mode} />
                    </div>
                    <p className="mt-1 text-sm font-semibold text-charcoal">
                      {ag.role.replace(/_/g, " ")}
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-mauve">
                      {AGENT_RESPONSIBILITIES[ag.role]}
                    </p>
                    <div className="mt-3">
                      <p className="text-[10px] font-semibold uppercase tracking-wide text-burgundy">
                        Permitted Tools ({ag.allowed_tools.length})
                      </p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {ag.allowed_tools.map((tool) => (
                          <span
                            key={tool}
                            className="rounded border border-peach-border bg-white px-2 py-0.5 font-mono text-[10px] text-charcoal"
                          >
                            {tool}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="mt-4 flex items-center justify-between border-t border-peach-border pt-3 text-[11px]">
                    <span className="text-mauve">
                      {ag.requires_org_boundary ? "Org Boundary Auth" : "Admin Auth"}
                    </span>
                    {latestExec ? (
                      <StatusBadge value={latestExec.execution_status} />
                    ) : (
                      <span className="text-mauve">No runs yet</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </PanelCard>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <PanelCard
            title="Invoke Specialized Agent"
            subtitle="POST /api/v1/agents/{agent_role}/run — server enforces role and boundary authorization"
          >
            <form onSubmit={(e) => void handleRunAgent(e)} className="space-y-3">
              <div>
                <label htmlFor="agent-role-select" className="block text-xs font-semibold text-charcoal">
                  Target Agent Role
                </label>
                <select
                  id="agent-role-select"
                  value={selectedRole}
                  onChange={(e) => setSelectedRole(e.target.value as AgentRole)}
                  className="mt-1 w-full rounded-lg border border-peach-border bg-cream/60 px-3 py-2 text-xs"
                >
                  {agents.map((a) => (
                    <option key={a.role} value={a.role}>
                      {a.role} ({a.requires_org_boundary ? "Org Bearer" : "Admin Key"})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="agent-payload-input" className="block text-xs font-semibold text-charcoal">
                  Input JSON Payload
                </label>
                <textarea
                  id="agent-payload-input"
                  rows={4}
                  value={payloadJson}
                  onChange={(e) => setPayloadJson(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-peach-border bg-cream/60 px-3 py-2 font-mono text-xs"
                  required
                />
              </div>
              <div>
                <label htmlFor="agent-idem-input" className="block text-xs font-semibold text-charcoal">
                  Optional X-Idempotency-Key (Scoped to Agent + Boundary + Input)
                </label>
                <input
                  id="agent-idem-input"
                  value={idempotencyKey}
                  onChange={(e) => setIdempotencyKey(e.target.value)}
                  placeholder="Optional idempotency key"
                  className="mt-1 w-full rounded-lg border border-peach-border bg-cream/60 px-3 py-2 text-xs"
                />
              </div>
              <button
                type="submit"
                disabled={running}
                className="w-full rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep disabled:opacity-60"
              >
                {running ? "Executing..." : "Run Agent"}
              </button>
            </form>

            {lastResult && (
              <div className="mt-4 rounded-xl border border-peach-border bg-cream/70 p-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[11px] font-semibold text-burgundy-deep">
                    {lastResult.execution_id}
                  </span>
                  <StatusBadge value={lastResult.execution_status} />
                </div>
                <pre className="mt-2 max-h-48 overflow-auto rounded bg-white p-2 text-[11px] text-charcoal">
                  {JSON.stringify(lastResult.output ?? { error: lastResult.error_message }, null, 2)}
                </pre>
              </div>
            )}
          </PanelCard>
        </div>

        <div className="lg:col-span-7">
          <PanelCard
            title={`Audited Execution History (${executions.length})`}
            subtitle="Sanitized records from GET /api/v1/agents/executions (no tokens or private docs stored)"
          >
            {executions.length === 0 ? (
              <EmptyState
                title="No agent executions recorded"
                description="Invoke an authorized specialist agent or process an A2A inbox message to populate execution history."
              />
            ) : (
              <div className="overflow-x-auto rounded-xl border border-peach-border">
                <table className="w-full text-left text-xs">
                  <thead className="bg-peach/40 text-burgundy-deep">
                    <tr>
                      <th className="px-3 py-2 font-semibold">Execution &amp; Role</th>
                      <th className="px-3 py-2 font-semibold">Status</th>
                      <th className="px-3 py-2 font-semibold">Evidence Refs</th>
                      <th className="px-3 py-2 font-semibold">Started</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-peach-border bg-white">
                    {executions.slice(0, 20).map((ex) => (
                      <tr key={ex.id}>
                        <td className="px-3 py-2">
                          <p className="font-mono font-semibold text-charcoal">{ex.id}</p>
                          <p className="text-[11px] text-mauve">{ex.agent_role}</p>
                          {ex.error_message && (
                            <p className="mt-0.5 text-[11px] text-status-discrepancy">
                              {ex.error_message}
                            </p>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          <StatusBadge value={ex.execution_status} />
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-mauve">
                          {ex.evidence_references.length > 0
                            ? ex.evidence_references.join(", ")
                            : "None"}
                        </td>
                        <td className="px-3 py-2 text-[11px] text-mauve">
                          {formatTimestamp(ex.started_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </PanelCard>
        </div>
      </div>
    </div>
  );
}
