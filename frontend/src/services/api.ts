import type {
  A2AMessage,
  AgentDescriptor,
  AgentExecutionAudit,
  AgentExecutionEnvelope,
  AgentRole,
  AgentTask,
  AuditChainVerification,
  AuditEvent,
  BatchCreatePayload,
  EvidenceItem,
  EvidenceRegisterPayload,
  HandoffDispatchPayload,
  HandoffDispatchResponse,
  HumanApprovalDecisionPayload,
  HumanApprovalRecord,
  IntegrationOverviewResponse,
  IntegrationUsageLog,
  InventoryCreatePayload,
  OrchestrationRunResponse,
  OrganizationRecord,
  OrganizationRegisterPayload,
  OrganizationWithCredential,
  OrgIdentityReviewPayload,
  RecallCreatePayload,
  SessionCredentials,
  SessionStatusResponse,
  SponsorStatusReport,
  SharingGrant,
  SharingGrantCreatePayload,
  ShipmentCreatePayload,
  TraceabilityData,
} from "../types/api";
import type {
  AffectedBatch,
  InventoryRecord,
  RecallNotice,
  ShipmentRecord,
} from "../types/domain";
import type { HealthCheckResult, HealthResponse } from "../types/health";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export class ApiRequestError extends Error {
  statusCode: number;
  code: string;

  constructor(message: string, statusCode: number, code = "api_error") {
    super(message);
    this.name = "ApiRequestError";
    this.statusCode = statusCode;
    this.code = code;
  }
}

export function getHealthEndpointUrl(): string {
  return `${API_BASE_URL}/api/health`;
}

function buildAuthHeaders(creds: SessionCredentials, method: string): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  const upper = method.toUpperCase();
  if (creds.csrfToken && ["POST", "PUT", "PATCH", "DELETE"].includes(upper)) {
    headers["X-CSRF-Token"] = creds.csrfToken;
  }
  return headers;
}

async function requestJson<T>(
  path: string,
  creds: SessionCredentials,
  options: {
    method?: string;
    body?: unknown;
    extraHeaders?: Record<string, string>;
    signal?: AbortSignal;
  } = {}
): Promise<T> {
  const headers: Record<string, string> = {
    ...buildAuthHeaders(creds, options.method ?? "GET"),
    ...(options.extraHeaders ?? {}),
  };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: options.method ?? "GET",
      credentials: "include",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
  } catch (error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiRequestError(
      "Unable to reach the RecallRelay backend service (NOT CONNECTED).",
      0,
      "network_error"
    );
  }

  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}.`;
    let code = "http_error";
    try {
      const errBody = (await response.json()) as {
        error?: { message?: string; code?: string };
        detail?: string;
      };
      if (errBody?.error?.message) {
        message = errBody.error.message;
        code = errBody.error.code ?? code;
      } else if (typeof errBody?.detail === "string") {
        message = errBody.detail;
      }
    } catch {
      // Keep default status message
    }
    throw new ApiRequestError(message, response.status, code);
  }

  return (await response.json()) as T;
}

export async function fetchBackendHealth(
  signal?: AbortSignal
): Promise<HealthCheckResult> {
  const endpoint = getHealthEndpointUrl();
  const startedAt = performance.now();

  try {
    const response = await fetch(endpoint, {
      method: "GET",
      headers: { Accept: "application/json" },
      signal,
    });

    const latencyMs = Math.round(performance.now() - startedAt);
    const checkedAt = new Date().toISOString();

    if (!response.ok) {
      return {
        reachable: false,
        latencyMs,
        checkedAt,
        data: null,
        errorMessage: `Backend responded with HTTP ${response.status}.`,
      };
    }

    const payload = (await response.json()) as HealthResponse;
    return {
      reachable: payload.status === "ok",
      latencyMs,
      checkedAt,
      data: payload,
      errorMessage: null,
    };
  } catch (error: unknown) {
    const checkedAt = new Date().toISOString();
    const message =
      error instanceof Error
        ? error.message
        : "Unable to reach the RecallRelay backend service.";

    return {
      reachable: false,
      latencyMs: null,
      checkedAt,
      data: null,
      errorMessage: message,
    };
  }
}

export const recallRelayApi = {
  // BFF Server-Side Sessions (HttpOnly cookie)
  loginAdmin: (adminKey: string) =>
    requestJson<SessionStatusResponse>(
      "/api/v1/session/login/admin",
      { mode: "public", csrfToken: null, organizationId: null, organizationRole: null, dataBoundaryId: null, expiresAt: null, label: "" },
      { method: "POST", body: { admin_key: adminKey } }
    ),
  loginOrganization: (bearerToken: string) =>
    requestJson<SessionStatusResponse>(
      "/api/v1/session/login/organization",
      { mode: "public", csrfToken: null, organizationId: null, organizationRole: null, dataBoundaryId: null, expiresAt: null, label: "" },
      { method: "POST", body: { bearer_token: bearerToken } }
    ),
  getSessionMe: (signal?: AbortSignal) =>
    requestJson<SessionStatusResponse>(
      "/api/v1/session/me",
      { mode: "public", csrfToken: null, organizationId: null, organizationRole: null, dataBoundaryId: null, expiresAt: null, label: "" },
      { signal }
    ),
  logoutSession: (creds: SessionCredentials) =>
    requestJson<SessionStatusResponse>("/api/v1/session/logout", creds, { method: "POST" }),

  // Sponsor Integrations
  getIntegrationsStatus: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<IntegrationOverviewResponse>("/api/v1/integrations/status", creds, { signal }),
  checkSponsorIntegration: (creds: SessionCredentials, sponsorName: string) =>
    requestJson<SponsorStatusReport>(
      `/api/v1/integrations/${encodeURIComponent(sponsorName)}/check`,
      creds,
      { method: "POST" }
    ),
  listIntegrationLogs: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<IntegrationUsageLog[]>("/api/v1/integrations/logs", creds, { signal }),

  // Recalls & Batches
  listRecalls: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<RecallNotice[]>("/api/v1/recalls", creds, { signal }),
  getRecall: (creds: SessionCredentials, recallId: string, signal?: AbortSignal) =>
    requestJson<RecallNotice>(`/api/v1/recalls/${encodeURIComponent(recallId)}`, creds, { signal }),
  createRecall: (creds: SessionCredentials, payload: RecallCreatePayload) =>
    requestJson<RecallNotice>("/api/v1/recalls", creds, { method: "POST", body: payload }),
  setPublicDisclosure: (creds: SessionCredentials, recallId: string, approved: boolean) =>
    requestJson<RecallNotice>(
      `/api/v1/recalls/${encodeURIComponent(recallId)}/public-disclosure?approved=${approved}`,
      creds,
      { method: "POST" }
    ),
  listBatches: (creds: SessionCredentials, recallId: string, signal?: AbortSignal) =>
    requestJson<AffectedBatch[]>(
      `/api/v1/recalls/${encodeURIComponent(recallId)}/batches`,
      creds,
      { signal }
    ),
  createBatch: (creds: SessionCredentials, recallId: string, payload: BatchCreatePayload) =>
    requestJson<AffectedBatch>(
      `/api/v1/recalls/${encodeURIComponent(recallId)}/batches`,
      creds,
      { method: "POST", body: payload }
    ),
  getTraceability: (creds: SessionCredentials, recallId: string, signal?: AbortSignal) =>
    requestJson<TraceabilityData>(
      `/api/v1/recalls/${encodeURIComponent(recallId)}/traceability`,
      creds,
      { signal }
    ),

  // Evidence
  listEvidence: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<EvidenceItem[]>("/api/v1/evidence", creds, { signal }),
  registerEvidence: (creds: SessionCredentials, payload: EvidenceRegisterPayload) =>
    requestJson<EvidenceItem>("/api/v1/evidence", creds, { method: "POST", body: payload }),

  // Organizations & Identity Review
  listOrganizations: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<OrganizationRecord[]>("/api/v1/organizations", creds, { signal }),
  registerOrganization: (creds: SessionCredentials, payload: OrganizationRegisterPayload) =>
    requestJson<OrganizationWithCredential>("/api/v1/organizations", creds, {
      method: "POST",
      body: payload,
    }),
  reviewOrganizationIdentity: (
    creds: SessionCredentials,
    orgId: string,
    payload: OrgIdentityReviewPayload
  ) =>
    requestJson<OrganizationRecord>(
      `/api/v1/organizations/${encodeURIComponent(orgId)}/identity-review`,
      creds,
      { method: "POST", body: payload }
    ),

  // Boundary-scoped Supply Chain & Sharing
  listShipments: (creds: SessionCredentials, recallId?: string, signal?: AbortSignal) =>
    requestJson<ShipmentRecord[]>(
      recallId
        ? `/api/v1/shipments?recall_id=${encodeURIComponent(recallId)}`
        : "/api/v1/shipments",
      creds,
      { signal }
    ),
  createShipment: (creds: SessionCredentials, payload: ShipmentCreatePayload) =>
    requestJson<ShipmentRecord>("/api/v1/shipments", creds, { method: "POST", body: payload }),
  listInventory: (creds: SessionCredentials, recallId?: string, signal?: AbortSignal) =>
    requestJson<InventoryRecord[]>(
      recallId
        ? `/api/v1/inventory?recall_id=${encodeURIComponent(recallId)}`
        : "/api/v1/inventory",
      creds,
      { signal }
    ),
  createInventory: (creds: SessionCredentials, payload: InventoryCreatePayload) =>
    requestJson<InventoryRecord>("/api/v1/inventory", creds, { method: "POST", body: payload }),
  listSharingGrants: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<SharingGrant[]>("/api/v1/sharing/grants", creds, { signal }),
  createSharingGrant: (creds: SessionCredentials, payload: SharingGrantCreatePayload) =>
    requestJson<SharingGrant>("/api/v1/sharing/grants", creds, {
      method: "POST",
      body: payload,
    }),
  revokeSharingGrant: (creds: SessionCredentials, grantId: string) =>
    requestJson<SharingGrant>(
      `/api/v1/sharing/grants/${encodeURIComponent(grantId)}/revoke`,
      creds,
      { method: "POST" }
    ),

  // Agents
  listAgents: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<AgentDescriptor[]>("/api/v1/agents", creds, { signal }),
  runAgent: (
    creds: SessionCredentials,
    agentRole: AgentRole,
    payload: Record<string, unknown>,
    idempotencyKey?: string
  ) =>
    requestJson<AgentExecutionEnvelope>(
      `/api/v1/agents/${encodeURIComponent(agentRole)}/run`,
      creds,
      {
        method: "POST",
        body: { payload },
        extraHeaders: idempotencyKey ? { "X-Idempotency-Key": idempotencyKey } : undefined,
      }
    ),
  listAgentExecutions: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<AgentExecutionAudit[]>("/api/v1/agents/executions", creds, { signal }),

  // A2A Messages, Tasks & Handoffs
  listMessages: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<A2AMessage[]>("/api/v1/a2a/messages", creds, { signal }),
  deliverMessage: (creds: SessionCredentials, messageId: string) =>
    requestJson<A2AMessage>(
      `/api/v1/a2a/messages/${encodeURIComponent(messageId)}/deliver`,
      creds,
      { method: "POST" }
    ),
  acknowledgeMessage: (creds: SessionCredentials, messageId: string) =>
    requestJson<A2AMessage>(
      `/api/v1/a2a/messages/${encodeURIComponent(messageId)}/acknowledge`,
      creds,
      { method: "POST" }
    ),
  listTasks: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<AgentTask[]>("/api/v1/a2a/tasks", creds, { signal }),
  dispatchHandoff: (
    creds: SessionCredentials,
    payload: HandoffDispatchPayload,
    recipientBoundaryId?: string
  ) =>
    requestJson<HandoffDispatchResponse>(
      recipientBoundaryId
        ? `/api/v1/a2a/handoffs?recipient_boundary_id=${encodeURIComponent(recipientBoundaryId)}`
        : "/api/v1/a2a/handoffs",
      creds,
      { method: "POST", body: payload }
    ),
  processInbox: (creds: SessionCredentials, recipientAgent: AgentRole) =>
    requestJson<Record<string, unknown>>(
      `/api/v1/a2a/inbox/${encodeURIComponent(recipientAgent)}/process`,
      creds,
      { method: "POST" }
    ),

  // Governance & Audit Trail
  listApprovals: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<HumanApprovalRecord[]>("/api/v1/approvals", creds, { signal }),
  decideApproval: (
    creds: SessionCredentials,
    approvalId: string,
    payload: HumanApprovalDecisionPayload
  ) =>
    requestJson<HumanApprovalRecord>(
      `/api/v1/approvals/${encodeURIComponent(approvalId)}/decision`,
      creds,
      { method: "POST", body: payload }
    ),
  listAuditEvents: (creds: SessionCredentials, signal?: AbortSignal) =>
    requestJson<AuditEvent[]>("/api/v1/a2a/audit-events", creds, { signal }),
  verifyAuditChain: (creds: SessionCredentials) =>
    requestJson<AuditChainVerification>("/api/v1/a2a/audit-events/verify", creds),

  runCoordinatedResponse(
    creds: SessionCredentials,
    recallId: string,
    idempotencyKey?: string,
    signal?: AbortSignal
  ): Promise<OrchestrationRunResponse> {
    return requestJson<OrchestrationRunResponse>("/api/v1/orchestrations/run", creds, {
      method: "POST",
      body: { recall_id: recallId, idempotency_key: idempotencyKey ?? null },
      signal,
    });
  },

  listOrchestrations(
    creds: SessionCredentials,
    recallId?: string,
    signal?: AbortSignal
  ): Promise<OrchestrationRunResponse[]> {
    const query = recallId ? `?recall_id=${encodeURIComponent(recallId)}` : "";
    return requestJson<OrchestrationRunResponse[]>(`/api/v1/orchestrations${query}`, creds, {
      signal,
    });
  },

  getOrchestration(
    creds: SessionCredentials,
    orchestrationId: string,
    signal?: AbortSignal
  ): Promise<OrchestrationRunResponse> {
    return requestJson<OrchestrationRunResponse>(
      `/api/v1/orchestrations/${encodeURIComponent(orchestrationId)}`,
      creds,
      { signal }
    );
  },
};
