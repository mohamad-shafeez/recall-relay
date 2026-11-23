import { useCallback, useEffect, useState } from "react";

import { useSession } from "../context/SessionContext";
import { recallRelayApi } from "../services/api";
import type {
  IntegrationOverviewResponse,
  IntegrationUsageLog,
} from "../types/api";
import type { HealthCheckResult } from "../types/health";
import { BackendStatusCard } from "../components/BackendStatusCard";
import {
  EmptyState,
  FeedbackBanner,
  formatTimestamp,
  PanelCard,
  StatusBadge,
} from "../components/ui/DesignSystem";

interface SettingsPageProps {
  health: HealthCheckResult | null;
  isCheckingHealth: boolean;
  onRefreshHealth: () => void;
  onOpenAuthModal: () => void;
}

export function SettingsPage({
  health,
  isCheckingHealth,
  onRefreshHealth,
  onOpenAuthModal,
}: SettingsPageProps) {
  const { credentials, sessionVersion, signOut } = useSession();
  const [integrations, setIntegrations] = useState<IntegrationOverviewResponse | null>(null);
  const [usageLogs, setUsageLogs] = useState<IntegrationUsageLog[]>([]);
  const [checkingSponsor, setCheckingSponsor] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "error" | "success"; message: string } | null>(
    null
  );

  const loadIntegrations = useCallback(
    async (signal?: AbortSignal) => {
      if (credentials.mode === "public") {
        setIntegrations(null);
        setUsageLogs([]);
        return;
      }
      try {
        const overview = await recallRelayApi.getIntegrationsStatus(credentials, signal);
        setIntegrations(overview);
        if (credentials.mode === "admin") {
          const logs = await recallRelayApi.listIntegrationLogs(credentials, signal);
          setUsageLogs(logs);
        }
      } catch (err: unknown) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setFeedback({
          kind: "error",
          message: err instanceof Error ? err.message : "Failed to load sponsor integration status.",
        });
      }
    },
    [credentials]
  );

  useEffect(() => {
    const controller = new AbortController();
    void loadIntegrations(controller.signal);
    return () => controller.abort();
  }, [loadIntegrations, sessionVersion]);

  async function handleCheckSponsor(sponsorName: string) {
    setCheckingSponsor(sponsorName);
    setFeedback(null);
    try {
      const report = await recallRelayApi.checkSponsorIntegration(
        credentials,
        sponsorName.toLowerCase()
      );
      setFeedback({
        kind: report.connection_state === "CONNECTED" ? "success" : "error",
        message: `${report.sponsor_name}: ${report.implementation_status} (${report.connection_state}) — ${report.message}`,
      });
      await loadIntegrations();
    } catch (err: unknown) {
      setFeedback({
        kind: "error",
        message: err instanceof Error ? err.message : "Sponsor connectivity check failed.",
      });
    } finally {
      setCheckingSponsor(null);
    }
  }

  return (
    <div className="space-y-6">
      {feedback && <FeedbackBanner kind={feedback.kind} message={feedback.message} />}

      <PanelCard
        title="Active Session & BFF Cookie Security"
        subtitle="Server-issued HttpOnly SameSite=Strict session cookie + X-CSRF-Token; admin keys and org tokens are never retained in browser JS"
        action={
          <div className="flex gap-2">
            <button
              type="button"
              onClick={onOpenAuthModal}
              className="rounded-lg bg-burgundy px-3.5 py-2 text-xs font-semibold text-white hover:bg-burgundy-deep"
            >
              {credentials.mode === "public" ? "Sign In" : "Switch Session"}
            </button>
            {credentials.mode !== "public" && (
              <button
                type="button"
                onClick={() => void signOut()}
                className="rounded-lg border border-peach-border bg-white px-3.5 py-2 text-xs font-semibold text-charcoal hover:bg-cream"
              >
                Sign Out
              </button>
            )}
          </div>
        }
      >
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
            <p className="text-xs font-semibold uppercase text-mauve">Current Access Mode</p>
            <div className="mt-2">
              <StatusBadge value={credentials.mode} />
            </div>
            {credentials.expiresAt && (
              <p className="mt-1 text-[11px] text-mauve">
                Expires: {formatTimestamp(credentials.expiresAt)}
              </p>
            )}
          </div>
          <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
            <p className="text-xs font-semibold uppercase text-mauve">Credential Storage</p>
            <p className="mt-2 text-sm font-semibold text-charcoal">Server HttpOnly Cookie</p>
            <p className="mt-0.5 text-xs text-mauve">
              Raw admin keys and org bearer tokens are discarded immediately after login.
            </p>
          </div>
          <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
            <p className="text-xs font-semibold uppercase text-mauve">Active A2A Transport</p>
            <p className="mt-2 font-mono text-xs font-semibold text-burgundy-deep">
              {integrations?.active_a2a_transport ?? "internal_sqlite_transport"}
            </p>
            <p className="mt-0.5 text-xs text-mauve">
              Mode: {integrations?.agent_execution_mode ?? "DETERMINISTIC"}
            </p>
          </div>
        </div>
      </PanelCard>

      <PanelCard
        title="Sponsor Integrations (Honest Runtime Status)"
        subtitle="Zero-payment/no-card policy enforced; adapters without live verified credentials are never shown as CONNECTED"
      >
        {credentials.mode === "public" ? (
          <EmptyState
            title="Authenticate to inspect sponsor integration status"
            description="Sign in as Administrator or Organization to view server-side sponsor connection states."
            badge="NOT_CONNECTED"
          />
        ) : !integrations ? (
          <p className="text-xs text-mauve">Loading sponsor integration status...</p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-peach-border">
            <table className="w-full text-left text-xs">
              <thead className="bg-peach/40 text-burgundy-deep">
                <tr>
                  <th className="px-3 py-2.5 font-semibold">Sponsor &amp; Capability</th>
                  <th className="px-3 py-2.5 font-semibold">Implementation</th>
                  <th className="px-3 py-2.5 font-semibold">Connection</th>
                  <th className="px-3 py-2.5 font-semibold">Status Details</th>
                  {credentials.mode === "admin" && (
                    <th className="px-3 py-2.5 font-semibold">Live Probe</th>
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-peach-border bg-white">
                {integrations.sponsors.map((sp) => (
                  <tr key={sp.sponsor_name}>
                    <td className="px-3 py-2.5">
                      <p className="font-semibold text-charcoal">{sp.sponsor_name}</p>
                      <p className="text-[11px] text-mauve">{sp.capability}</p>
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={sp.implementation_status} />
                    </td>
                    <td className="px-3 py-2.5">
                      <StatusBadge value={sp.connection_state} />
                    </td>
                    <td className="px-3 py-2.5">
                      <p className="text-charcoal">{sp.message}</p>
                      {sp.last_checked_at && (
                        <p className="mt-0.5 text-[10px] text-mauve">
                          Last checked: {formatTimestamp(sp.last_checked_at)}
                        </p>
                      )}
                    </td>
                    {credentials.mode === "admin" && (
                      <td className="px-3 py-2.5">
                        <button
                          type="button"
                          disabled={checkingSponsor === sp.sponsor_name}
                          onClick={() => void handleCheckSponsor(sp.sponsor_name)}
                          className="rounded-lg border border-peach-border bg-cream px-2.5 py-1 text-xs font-semibold text-burgundy-deep hover:bg-peach/60 disabled:opacity-60"
                        >
                          {checkingSponsor === sp.sponsor_name ? "Checking..." : "Run Check"}
                        </button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelCard>

      {credentials.mode === "admin" && usageLogs.length > 0 && (
        <PanelCard
          title={`Recent External Integration Activity (${usageLogs.length})`}
          subtitle="Sanitized logs from GET /api/v1/integrations/logs (zero credentials stored)"
        >
          <ul className="divide-y divide-peach-border text-xs">
            {usageLogs.slice(0, 10).map((log) => (
              <li key={log.id} className="flex items-center justify-between gap-2 py-2">
                <div>
                  <span className="font-semibold text-charcoal">{log.sponsor_name}</span>
                  <span className="mx-1.5 text-mauve">·</span>
                  <span className="font-mono text-mauve">{log.operation}</span>
                  {log.error_summary && (
                    <span className="ml-2 text-status-discrepancy">{log.error_summary}</span>
                  )}
                </div>
                <StatusBadge value={log.status} />
              </li>
            ))}
          </ul>
        </PanelCard>
      )}

      <BackendStatusCard
        result={health}
        isChecking={isCheckingHealth}
        onRefresh={onRefreshHealth}
      />
    </div>
  );
}
