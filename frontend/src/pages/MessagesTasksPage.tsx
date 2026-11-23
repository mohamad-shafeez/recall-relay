import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type { A2AMessage, AgentRole, AgentTask } from "../types/api";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

const AGENT_ROLES: AgentRole[] = [
  "RECALL_SOURCE_ANALYST",
  "RECALL_SCOPE_AGENT",
  "MANUFACTURER_OPERATIONS_AGENT",
  "DISTRIBUTOR_RECONCILIATION_AGENT",
  "RETAILER_INVENTORY_AGENT",
  "INDEPENDENT_VERIFICATION_AGENT",
  "RECALL_RESPONSE_COORDINATOR",
];

export function MessagesTasksPage() {
  const { credentials, sessionVersion } = useSession();
  const [messages, setMessages] = useState<A2AMessage[]>([]);
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [inboxRole, setInboxRole] = useState<AgentRole>("RECALL_SCOPE_AGENT");
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  // Admin handoff form
  const [recallId, setRecallId] = useState("");
  const [conversationId, setConversationId] = useState("conv_001");
  const [senderAgent, setSenderAgent] = useState<AgentRole>("RECALL_SOURCE_ANALYST");
  const [recipientAgent, setRecipientAgent] = useState<AgentRole>("RECALL_SCOPE_AGENT");
  const [requestedAction, setRequestedAction] = useState("evaluate_recall_scope");
  const [outputSchema, setOutputSchema] = useState("RecallScopeOutput");
  const [payloadText, setPayloadText] = useState('{"recall_id": ""}');

  const loadData = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const [msgList, taskList] = await Promise.all([
          recallRelayApi.listMessages(credentials, signal),
          recallRelayApi.listTasks(credentials, signal),
        ]);
        setMessages(msgList);
        setTasks(taskList);
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load A2A messages and tasks.",
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

  async function handleDispatchHandoff(e: FormEvent) {
    e.preventDefault();
    setFeedback(null);
    try {
      const parsedPayload = JSON.parse(payloadText) as Record<string, unknown>;
      const freshKey =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `handoff_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
      const res = await recallRelayApi.dispatchHandoff(credentials, {
        recall_id: recallId.trim(),
        conversation_id: conversationId.trim(),
        sender_agent: senderAgent,
        recipient_agent: recipientAgent,
        requested_action: requestedAction.trim(),
        required_output_schema: outputSchema.trim(),
        payload: parsedPayload,
        evidence_references: [],
        idempotency_key: freshKey,
      });
      setFeedback({
        kind: "success",
        message: `Task ${res.task.id} (${res.task.status}) and message ${res.message.id} (${res.message.status}) dispatched.`,
      });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Handoff dispatch failed.",
      });
    }
  }

  async function handleProcessInbox() {
    setFeedback(null);
    try {
      const res = await recallRelayApi.processInbox(credentials, inboxRole);
      setFeedback({
        kind: "success",
        message: res.processed
          ? `Processed inbox message for ${inboxRole}.`
          : `No deliverable messages waiting in ${inboxRole} inbox.`,
      });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Inbox processing failed.",
      });
    }
  }

  async function handleAckMessage(messageId: string) {
    setFeedback(null);
    try {
      await recallRelayApi.acknowledgeMessage(credentials, messageId);
      setFeedback({ kind: "success", message: `Message ${messageId} acknowledged.` });
      await loadData();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Failed to acknowledge message.",
      });
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <PanelCard
        title="Process Agent Inbox (Internal SQLite Transport)"
        subtitle="Atomically claims the next DELIVERED message for the selected agent, runs the specialist, and publishes a RESULT_REPORT"
      >
        <div className="flex flex-wrap items-center gap-3">
          <select
            aria-label="Select agent inbox"
            value={inboxRole}
            onChange={(e) => setInboxRole(e.target.value as AgentRole)}
            className="rounded-lg border border-peach-border bg-cream px-3 py-2 text-xs font-semibold text-charcoal"
          >
            {AGENT_ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void handleProcessInbox()}
            className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
          >
            Claim &amp; Process Next Message
          </button>
        </div>
      </PanelCard>

      {credentials.mode === "admin" && (
        <PanelCard
          title="Dispatch Agent Task Handoff (Admin)"
          subtitle="Creates a durable agent_task and delivers a versioned TASK_HANDOFF message (schema v1.0)"
        >
          <form onSubmit={(e) => void handleDispatchHandoff(e)} className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <input value={recallId} onChange={(e) => setRecallId(e.target.value)} placeholder="Recall ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs" />
            <input value={conversationId} onChange={(e) => setConversationId(e.target.value)} placeholder="Conversation ID *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs" />
            <input value={requestedAction} onChange={(e) => setRequestedAction(e.target.value)} placeholder="Requested action *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs" />
            <select value={senderAgent} onChange={(e) => setSenderAgent(e.target.value as AgentRole)} className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs">
              {AGENT_ROLES.map((r) => (
                <option key={r} value={r}>Sender: {r}</option>
              ))}
            </select>
            <select value={recipientAgent} onChange={(e) => setRecipientAgent(e.target.value as AgentRole)} className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs">
              {AGENT_ROLES.map((r) => (
                <option key={r} value={r}>Recipient: {r}</option>
              ))}
            </select>
            <input value={outputSchema} onChange={(e) => setOutputSchema(e.target.value)} placeholder="Required output schema *" required className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 text-xs" />
            <textarea value={payloadText} onChange={(e) => setPayloadText(e.target.value)} rows={2} placeholder="JSON payload" className="rounded-lg border border-peach-border bg-cream/50 px-3 py-2 font-mono text-xs sm:col-span-3" />
            <button type="submit" className="rounded-lg bg-burgundy px-4 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep sm:col-span-3">
              Dispatch Task Handoff
            </button>
          </form>
        </PanelCard>
      )}

      <PanelCard title={`Durable Agent Tasks (${tasks.length})`} subtitle="Actual statuses from GET /api/v1/a2a/tasks">
        {loading ? (
          <p className="text-xs text-mauve">Loading tasks...</p>
        ) : tasks.length === 0 ? (
          <EmptyState
            title="No agent tasks queued"
            description="Dispatch a task handoff above to coordinate specialist agents."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2 font-semibold">Task ID</th>
                  <th className="px-3 py-2 font-semibold">Assigned Agent</th>
                  <th className="px-3 py-2 font-semibold">Action</th>
                  <th className="px-3 py-2 font-semibold">Status</th>
                  <th className="px-3 py-2 font-semibold">Result / Failure</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {tasks.map((t) => (
                  <tr key={t.id}>
                    <td className="px-3 py-2 font-mono font-semibold">{t.id}</td>
                    <td className="px-3 py-2">{t.assigned_agent}</td>
                    <td className="px-3 py-2 text-mauve">{t.requested_action}</td>
                    <td className="px-3 py-2">
                      <StatusBadge value={t.status} />
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-mauve">
                      {t.result_reference ?? t.failure_reason ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelCard>

      <PanelCard
        title={`A2A Messages (${messages.length})`}
        subtitle="Boundary-filtered durable messages from GET /api/v1/a2a/messages"
      >
        {messages.length === 0 ? (
          <EmptyState
            title="No A2A messages visible"
            description="Only messages authorized for your current access boundary are displayed."
          />
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2 font-semibold">Message</th>
                  <th className="px-3 py-2 font-semibold">Route</th>
                  <th className="px-3 py-2 font-semibold">Type &amp; Status</th>
                  <th className="px-3 py-2 font-semibold">Timestamps</th>
                  <th className="px-3 py-2 font-semibold">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {messages.map((m) => (
                  <tr key={m.id}>
                    <td className="px-3 py-2">
                      <p className="font-mono font-semibold">{m.id}</p>
                      <p className="text-[11px] text-mauve">Task: {m.task_id ?? "None"}</p>
                    </td>
                    <td className="px-3 py-2 text-[11px]">
                      <span className="font-semibold">{m.sender_agent}</span>
                      <span className="mx-1 text-mauve">&rarr;</span>
                      <span className="font-semibold">{m.recipient_agent}</span>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap gap-1">
                        <StatusBadge value={m.message_type} />
                        <StatusBadge value={m.status} />
                      </div>
                    </td>
                    <td className="px-3 py-2 text-[11px] text-mauve">
                      <p>Created: {formatTimestamp(m.created_at)}</p>
                      {m.processed_at && <p>Processed: {formatTimestamp(m.processed_at)}</p>}
                    </td>
                    <td className="px-3 py-2">
                      {m.status === "DELIVERED" && !m.acknowledged_at && (
                        <button
                          type="button"
                          onClick={() => void handleAckMessage(m.id)}
                          className="rounded border border-peach-border bg-cream px-2.5 py-1 text-[11px] font-semibold text-burgundy-deep hover:bg-peach/50"
                        >
                          Acknowledge
                        </button>
                      )}
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
