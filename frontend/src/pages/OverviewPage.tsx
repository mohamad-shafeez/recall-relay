import { useEffect, useState } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type {
  AgentExecutionAudit,
  AgentTask,
  AuditEvent,
  HumanApprovalRecord,
  OrchestrationRunResponse,
  OrganizationRecord,
} from "../types/api";
import type { InventoryRecord, RecallNotice, ShipmentRecord } from "../types/domain";
import type { HealthCheckResult } from "../types/health";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";
import type { NavSectionId } from "../components/navigation";

interface OverviewPageProps {
  health: HealthCheckResult | null;
  onNavigate: (section: NavSectionId) => void;
}

export function OverviewPage({ health, onNavigate }: OverviewPageProps) {
  const { credentials, sessionVersion } = useSession();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [recalls, setRecalls] = useState<RecallNotice[]>([]);
  const [orgs, setOrgs] = useState<OrganizationRecord[] | null>(null);
  const [shipments, setShipments] = useState<ShipmentRecord[] | null>(null);
  const [inventory, setInventory] = useState<InventoryRecord[] | null>(null);
  const [tasks, setTasks] = useState<AgentTask[] | null>(null);
  const [approvals, setApprovals] = useState<HumanApprovalRecord[] | null>(null);
  const [executions, setExecutions] = useState<AgentExecutionAudit[] | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[] | null>(null);
  const [selectedRecallId, setSelectedRecallId] = useState<string>("");
  const [orchRunning, setOrchRunning] = useState(false);
  const [orchResult, setOrchResult] = useState<OrchestrationRunResponse | null>(null);
  const [orchError, setOrchError] = useState<string | null>(null);

  const isOrchInFlight =
    orchResult !== null &&
    (orchResult.status === "PENDING" || orchResult.status === "RUNNING");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);

    async function load() {
      try {
        const recallList = await recallRelayApi.listRecalls(credentials, controller.signal);
        setRecalls(recallList);
        if (recallList.length > 0) {
          setSelectedRecallId((prev) => prev || recallList[0].id);
        }

        if (credentials.mode === "admin") {
          const [orgList, taskList, approvalList, execList, auditList, orchList] = await Promise.all([
            recallRelayApi.listOrganizations(credentials, controller.signal),
            recallRelayApi.listTasks(credentials, controller.signal),
            recallRelayApi.listApprovals(credentials, controller.signal),
            recallRelayApi.listAgentExecutions(credentials, controller.signal),
            recallRelayApi.listAuditEvents(credentials, controller.signal),
            recallRelayApi.listOrchestrations(credentials, undefined, controller.signal),
          ]);
          setOrgs(orgList);
          setTasks(taskList);
          setApprovals(approvalList);
          setExecutions(execList);
          setAuditEvents(auditList.slice(0, 8));
          setShipments(null);
          setInventory(null);
          if (orchList.length > 0) {
            setOrchResult((prev) => prev ?? orchList[0]);
          }
        } else if (credentials.mode === "organization") {
          const [orgList, shipList, invList, taskList, execList] = await Promise.all([
            recallRelayApi.listOrganizations(credentials, controller.signal),
            recallRelayApi.listShipments(credentials, undefined, controller.signal),
            recallRelayApi.listInventory(credentials, undefined, controller.signal),
            recallRelayApi.listTasks(credentials, controller.signal),
            recallRelayApi.listAgentExecutions(credentials, controller.signal),
          ]);
          setOrgs(orgList);
          setShipments(shipList);
          setInventory(invList);
          setTasks(taskList);
          setExecutions(execList);
          setApprovals(null);
          setAuditEvents(null);
        } else {
          setOrgs(null);
          setShipments(null);
          setInventory(null);
          setTasks(null);
          setApprovals(null);
          setExecutions(null);
          setAuditEvents(null);
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") {
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load overview metrics.");
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    }

    void load();
    return () => controller.abort();
  }, [credentials, sessionVersion]);

  const pendingTasks = tasks ? tasks.filter((t) => t.status === "PENDING").length : null;
  const blockedOrFailedTasks = tasks
    ? tasks.filter(
        (t) =>
          t.status === "FAILED" ||
          t.status === "EXPIRED" ||
          t.status === "BLOCKED_MISSING_EVIDENCE" ||
          t.status === "BLOCKED_MISSING_AUTH"
      ).length
    : null;
  const pendingApprovals = approvals
    ? approvals.filter((a) => a.decision === "PENDING").length
    : null;


  useEffect(() => {
    if (!isOrchInFlight || !orchResult || credentials.mode !== "admin") {
      return;
    }
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      void recallRelayApi
        .getOrchestration(credentials, orchResult.id, controller.signal)
        .then((latest) => {
          setOrchResult(latest);
          if (latest.status !== "PENDING" && latest.status !== "RUNNING") {
            setOrchRunning(false);
          }
        })
        .catch((err: unknown) => {
          if (err instanceof DOMException && err.name === "AbortError") {
            return;
          }
          setOrchError(
            err instanceof Error ? err.message : "Failed to poll orchestration status."
          );
          setOrchRunning(false);
        });
    }, 800);

    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [isOrchInFlight, orchResult?.id, credentials]);

  async function handleRunCoordinatedResponse() {
    if (!selectedRecallId) return;
    setOrchRunning(true);
    setOrchError(null);
    try {
      const freshKey =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `orch_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
      const accepted = await recallRelayApi.runCoordinatedResponse(
        credentials,
        selectedRecallId,
        freshKey
      );
      setOrchResult(accepted);
      if (accepted.status !== "PENDING" && accepted.status !== "RUNNING") {
        setOrchRunning(false);
      }
    } catch (err: unknown) {
      setOrchError(
        err instanceof Error ? err.message : "Failed to start coordinated response."
      );
      setOrchRunning(false);
    }
  }

  return (
    <div className="space-y-6">
      {error && <FeedbackBanner kind="error" message={error} />}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-2xl border border-peach-border bg-white p-5 shadow-card">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            {credentials.mode === "public"
              ? "Publicly Approved Recalls"
              : "Accessible Recall Notices"}
          </p>
          <p className="mt-2 text-2xl font-bold text-burgundy-deep">
            {loading ? "..." : recalls.length}
          </p>
          <p className="mt-1 text-xs text-mauve">
            Scope: {credentials.mode === "public" ? "Public disclosure only" : credentials.label}
          </p>
        </div>

        <div className="rounded-2xl border border-peach-border bg-white p-5 shadow-card">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            Registered Organizations
          </p>
          <p className="mt-2 text-2xl font-bold text-charcoal">
            {loading ? "..." : orgs !== null ? orgs.length : "Restricted"}
          </p>
          <p className="mt-1 text-xs text-mauve">
            {orgs !== null
              ? `${orgs.filter((o) => o.identity_verification_status === "VERIFIED").length} verified / ${orgs.length} total`
              : "Requires Admin or Organization session"}
          </p>
        </div>

        <div className="rounded-2xl border border-peach-border bg-white p-5 shadow-card">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            {credentials.mode === "organization"
              ? "Org Boundary Shipments / Inventory"
              : "Agent Tasks (Pending / Blocked)"}
          </p>
          <p className="mt-2 text-2xl font-bold text-charcoal">
            {loading
              ? "..."
              : credentials.mode === "organization"
                ? `${shipments?.length ?? 0} / ${inventory?.length ?? 0}`
                : tasks !== null
                  ? `${pendingTasks} / ${blockedOrFailedTasks}`
                  : "Restricted"}
          </p>
          <p className="mt-1 text-xs text-mauve">
            {credentials.mode === "organization"
              ? "Strictly scoped to your organization boundary"
              : "Requires authenticated session"}
          </p>
        </div>

        <div className="rounded-2xl border border-peach-border bg-white p-5 shadow-card">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            {credentials.mode === "admin" ? "Pending Human Approvals" : "Backend Health"}
          </p>
          <p className="mt-2 text-2xl font-bold text-charcoal">
            {credentials.mode === "admin"
              ? loading
                ? "..."
                : pendingApprovals ?? 0
              : health?.reachable
                ? "Connected"
                : "NOT CONNECTED"}
          </p>
          <p className="mt-1 text-xs text-mauve">
            {health?.data
              ? `API ${health.data.checks.api} · DB ${health.data.checks.database}`
              : "Awaiting backend health check"}
          </p>
        </div>
      </div>


      {credentials.mode === "admin" && (
        <PanelCard
          title="Coordinated Recall Response Pipeline"
          subtitle="Select an existing registered recall and run the real multi-agent workflow (POST /api/v1/orchestrations/run)"
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <select
              aria-label="Select recall notice for coordinated response"
              value={selectedRecallId}
              onChange={(e) => setSelectedRecallId(e.target.value)}
              disabled={recalls.length === 0 || orchRunning}
              className="flex-1 min-w-0 w-full truncate rounded-xl border border-peach-border bg-cream/60 px-3 py-2 text-sm text-charcoal focus:border-burgundy focus:outline-none"
            >
              {recalls.length === 0 ? (
                <option value="">No registered recalls available</option>
              ) : (
                recalls.map((rec) => {
                  const label = `${rec.external_recall_number} — ${rec.title}`;
                  const displayLabel = label.length > 85 ? `${label.slice(0, 85)}...` : label;
                  return (
                    <option key={rec.id} value={rec.id} title={label}>
                      {displayLabel}
                    </option>
                  );
                })
              )}
            </select>
            <button
              type="button"
              disabled={!selectedRecallId || orchRunning}
              onClick={() => void handleRunCoordinatedResponse()}
              className="shrink-0 rounded-xl bg-burgundy px-4 py-2 text-xs font-bold uppercase tracking-wider text-white shadow-xs hover:bg-burgundy-deep disabled:opacity-50"
            >
              {orchRunning ? "Running Pipeline..." : "RUN COORDINATED RESPONSE"}
            </button>
          </div>
          {orchError && <div className="mt-3"><FeedbackBanner kind="error" message={orchError} /></div>}
          {orchResult && (
            <div className="mt-4 space-y-3 rounded-xl border border-peach-border bg-cream/50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold text-burgundy-deep">
                  Orchestration {orchResult.id} · Transport: {orchResult.active_transport}
                </p>
                <StatusBadge value={orchResult.status} />
              </div>
              <ol className="divide-y divide-peach-border/70">
                {orchResult.steps.map((step) => (
                  <li key={step.id} className="flex flex-wrap items-center justify-between gap-2 py-2 text-xs">
                    <div className="min-w-0">
                      <span className="font-mono font-semibold text-burgundy">
                        {String(step.step_order).padStart(2, "0")}
                      </span>{" "}
                      <span className="font-semibold text-charcoal">{step.title}</span>
                      {typeof step.details?.external_message_id === "string" && (
                        <span className="ml-2 rounded bg-peach px-1.5 py-0.5 font-mono text-[11px] text-burgundy-deep">
                          BAND Out: {step.details.external_message_id}
                        </span>
                      )}
                      {typeof step.details?.external_reply_message_id === "string" && (
                        <span className="ml-1.5 rounded bg-peach px-1.5 py-0.5 font-mono text-[11px] text-burgundy-deep">
                          BAND Reply: {step.details.external_reply_message_id}
                        </span>
                      )}
                      {typeof step.details?.lyzr_error === "string" && step.details.lyzr_error && (
                        <p className="mt-1 text-[11px] text-burgundy">
                          Lyzr error (sanitized): {step.details.lyzr_error}
                        </p>
                      )}
                      {typeof step.details?.error === "string" && step.details.error && (
                        <p className="mt-1 text-[11px] text-burgundy">
                          Error: {step.details.error}
                        </p>
                      )}
                    </div>
                    <StatusBadge value={step.status} />
                  </li>
                ))}
              </ol>
            </div>
          )}
        </PanelCard>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <PanelCard
          title="Recent Recall Notices"
          subtitle="Live records from GET /api/v1/recalls"
          action={
            <button
              type="button"
              onClick={() => onNavigate("recalls")}
              className="rounded-lg border border-peach-border bg-cream px-3 py-1.5 text-xs font-semibold text-burgundy-deep hover:bg-peach/60"
            >
              Open Recall Management
            </button>
          }
        >
          {recalls.length === 0 ? (
            <EmptyState
              title="No recall notices available in this access scope"
              description={
                credentials.mode === "public"
                  ? "No recall notices are currently approved for public disclosure. Sign in as Administrator or Organization to inspect internal recalls."
                  : "No recall notices have been registered yet. Register traceable source evidence first, then create a recall notice."
              }
            />
          ) : (
            <ul className="divide-y divide-peach-border">
              {recalls.slice(0, 5).map((rec) => (
                <li key={rec.id} className="flex items-center justify-between gap-3 py-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-charcoal">
                      {rec.external_recall_number} — {rec.title}
                    </p>
                    <p className="truncate text-xs text-mauve">
                      {rec.issuing_authority} · Evidence: {rec.evidence_id}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <StatusBadge value={rec.severity} />
                    <StatusBadge value={rec.status} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </PanelCard>

        <PanelCard
          title={
            credentials.mode === "admin"
              ? "Recent Coordination Audit Events"
              : "Recent Agent Execution Activity"
          }
          subtitle={
            credentials.mode === "admin"
              ? "Hash-chained events from GET /api/v1/a2a/audit-events"
              : "Sanitized executions from GET /api/v1/agents/executions"
          }
        >
          {credentials.mode === "public" ? (
            <EmptyState
              title="Operational activity requires authentication"
              description="Internal agent executions and audit events are not exposed publicly. Authenticate with an Administrator key or Organization bearer token to inspect authorized activity."
              badge="NOT CONNECTED"
            />
          ) : credentials.mode === "admin" ? (
            auditEvents && auditEvents.length > 0 ? (
              <ul className="divide-y divide-peach-border">
                {auditEvents.map((ev) => (
                  <li key={ev.id} className="flex items-center justify-between gap-3 py-2.5">
                    <div className="min-w-0">
                      <p className="truncate text-xs font-semibold text-charcoal">
                        #{ev.seq} {ev.event_type}
                      </p>
                      <p className="truncate text-[11px] text-mauve">
                        Actor: {ev.actor_id} · {ev.entity_type} ({ev.entity_id})
                      </p>
                    </div>
                    <span className="shrink-0 text-[11px] text-mauve">
                      {formatTimestamp(ev.created_at)}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                title="No coordination audit events recorded yet"
                description="Audit events are automatically appended when messages, tasks, sharing grants, approvals, or identity reviews occur."
              />
            )
          ) : executions && executions.length > 0 ? (
            <ul className="divide-y divide-peach-border">
              {executions.slice(0, 6).map((ex) => (
                <li key={ex.id} className="flex items-center justify-between gap-3 py-2.5">
                  <div className="min-w-0">
                    <p className="truncate text-xs font-semibold text-charcoal">
                      {ex.agent_role}
                    </p>
                    <p className="truncate text-[11px] text-mauve">
                      ID: {ex.id} · {formatTimestamp(ex.started_at)}
                    </p>
                  </div>
                  <StatusBadge value={ex.execution_status} />
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No boundary agent executions recorded yet"
              description="Run an authorized organization specialist agent in Agent Operations to generate audited execution records."
            />
          )}
        </PanelCard>
      </div>
    </div>
  );
}
