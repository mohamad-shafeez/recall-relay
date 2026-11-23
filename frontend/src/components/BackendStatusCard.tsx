import { getHealthEndpointUrl } from "../services/api";
import type { HealthCheckResult } from "../types/health";

interface BackendStatusCardProps {
  result: HealthCheckResult | null;
  isChecking: boolean;
  onRefresh: () => void;
}

export function BackendStatusCard({
  result,
  isChecking,
  onRefresh,
}: BackendStatusCardProps) {
  const endpointUrl = getHealthEndpointUrl();
  const isReachable = result?.reachable ?? false;

  return (
    <section
      aria-labelledby="backend-connectivity-heading"
      className="rounded-2xl border border-peach-border bg-white p-6 shadow-card"
    >
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-burgundy">
            Backend Connectivity
          </p>
          <h2
            id="backend-connectivity-heading"
            className="mt-1 text-lg font-semibold text-charcoal"
          >
            API Health Verification
          </h2>
          <p className="mt-1 text-sm text-mauve">
            Live status from{" "}
            <code className="rounded bg-cream px-1.5 py-0.5 text-xs font-medium text-burgundy-deep">
              GET {endpointUrl}
            </code>
          </p>
        </div>

        <button
          type="button"
          onClick={onRefresh}
          disabled={isChecking}
          className="inline-flex items-center justify-center rounded-lg bg-burgundy px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-burgundy-deep focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-burgundy disabled:cursor-not-allowed disabled:opacity-60"
        >
          {isChecking ? "Checking..." : "Re-check Connection"}
        </button>
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-peach-border bg-cream/70 p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            Reachability
          </p>
          <p
            className={`mt-2 text-base font-semibold ${
              result === null
                ? "text-status-pending"
                : isReachable
                  ? "text-status-verified"
                  : "text-status-discrepancy"
            }`}
          >
            {result === null
              ? "Checking..."
              : isReachable
                ? "Reachable"
                : "NOT CONNECTED"}
          </p>
        </div>

        <div className="rounded-xl border border-peach-border bg-cream/70 p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            Service / Version
          </p>
          <p className="mt-2 text-base font-semibold text-charcoal">
            {result?.data ? `${result.data.service} v${result.data.version}` : "UNKNOWN"}
          </p>
        </div>

        <div className="rounded-xl border border-peach-border bg-cream/70 p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            Response Latency
          </p>
          <p className="mt-2 text-base font-semibold text-charcoal">
            {result?.latencyMs !== null && result?.latencyMs !== undefined
              ? `${result.latencyMs} ms`
              : "UNKNOWN"}
          </p>
        </div>

        <div className="rounded-xl border border-peach-border bg-cream/70 p-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-mauve">
            API Uptime
          </p>
          <p className="mt-2 text-base font-semibold text-charcoal">
            {result?.data ? `${result.data.uptime_seconds.toFixed(1)} s` : "UNKNOWN"}
          </p>
        </div>
      </div>

      {result && !result.reachable && (
        <div
          role="alert"
          className="mt-4 rounded-xl border border-status-discrepancy-border bg-status-discrepancy-bg p-4 text-sm text-status-discrepancy"
        >
          <p className="font-semibold">Backend is currently not connected</p>
          <p className="mt-1 text-charcoal/85">
            {result.errorMessage ?? "Failed to connect to the FastAPI backend."} Ensure the
            FastAPI server is running on{" "}
            <code className="rounded bg-white px-1.5 py-0.5 text-xs font-medium text-burgundy-deep">
              http://127.0.0.1:8000
            </code>{" "}
            and try again.
          </p>
        </div>
      )}

      {result?.checkedAt && (
        <p className="mt-4 text-xs text-mauve">
          Last checked: {new Date(result.checkedAt).toLocaleString()}
        </p>
      )}
    </section>
  );
}
