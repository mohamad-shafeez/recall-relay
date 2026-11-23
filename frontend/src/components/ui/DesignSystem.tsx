import type { ReactNode } from "react";

export function StatusBadge({ value }: { value: string }) {
  const normalized = value.toUpperCase();

  let classes = "border-peach-border bg-peach/50 text-burgundy-deep";
  if (
    normalized === "VERIFIED" ||
    normalized === "COMPLETED" ||
    normalized === "PROCESSED" ||
    normalized === "APPROVED" ||
    normalized === "CONNECTED" ||
    normalized === "ACTIVE" ||
    normalized === "HEALTHY" ||
    normalized === "OK" ||
    normalized === "INDEPENDENTLY_VERIFIED"
  ) {
    classes =
      "border-status-verified-border bg-status-verified-bg text-status-verified";
  } else if (
    normalized.includes("PENDING") ||
    normalized.includes("AWAITING") ||
    normalized === "RUNNING" ||
    normalized === "DELIVERED" ||
    normalized === "PROCESSING" ||
    normalized === "AI_EXTRACTED" ||
    normalized === "SOURCE_MATERIAL" ||
    normalized === "DETERMINISTIC" ||
    normalized === "UNKNOWN" ||
    normalized === "UNVERIFIED"
  ) {
    classes =
      "border-status-pending-border bg-status-pending-bg text-status-pending";
  } else if (
    normalized.includes("DISCREPANCY") ||
    normalized.includes("FAILED") ||
    normalized.includes("REJECTED") ||
    normalized.includes("REVOKED") ||
    normalized.includes("EXPIRED") ||
    normalized.includes("BLOCKED") ||
    normalized === "NOT_CONNECTED" ||
    normalized === "TIMED_OUT" ||
    normalized === "CLASS_I" ||
    normalized === "HIGH"
  ) {
    classes =
      "border-status-discrepancy-border bg-status-discrepancy-bg text-status-discrepancy";
  }

  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-semibold tracking-wide ${classes}`}
    >
      {value.replace(/_/g, " ")}
    </span>
  );
}

export function PanelCard({
  title,
  subtitle,
  action,
  children,
}: {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-2xl border border-peach-border bg-white p-6 shadow-card">
      {(title || action) && (
        <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            {title && (
              <h2 className="text-base font-semibold text-charcoal">{title}</h2>
            )}
            {subtitle && <p className="mt-0.5 text-xs text-mauve">{subtitle}</p>}
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function EmptyState({
  title,
  description,
  badge = "AWAITING EVIDENCE",
}: {
  title: string;
  description: string;
  badge?: string;
}) {
  return (
    <div className="rounded-xl border border-dashed border-peach-border bg-cream/70 px-6 py-10 text-center">
      <div className="mb-3 flex justify-center">
        <StatusBadge value={badge} />
      </div>
      <h3 className="text-sm font-semibold text-charcoal">{title}</h3>
      <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-mauve">
        {description}
      </p>
    </div>
  );
}

export function FeedbackBanner({
  kind,
  message,
}: {
  kind: "error" | "success" | "info";
  message: string;
}) {
  const styles =
    kind === "error"
      ? "border-status-discrepancy-border bg-status-discrepancy-bg text-status-discrepancy"
      : kind === "success"
        ? "border-status-verified-border bg-status-verified-bg text-status-verified"
        : "border-peach-border bg-peach/50 text-burgundy-deep";

  return (
    <div role={kind === "error" ? "alert" : "status"} className={`rounded-xl border px-4 py-3 text-sm ${styles}`}>
      {message}
    </div>
  );
}

export function formatQuantity(value: number | null | undefined, unit?: string | null): string {
  if (value === null || value === undefined) {
    return "UNKNOWN";
  }
  return unit ? `${value} ${unit}` : String(value);
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) {
    return "UNKNOWN";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
