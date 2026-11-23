interface DemoModeBannerProps {
  environment: string;
  activeTransport?: string;
  executionMode?: string;
  liveVerifiedSponsors?: number;
  totalSponsors?: number;
}

export function DemoModeBanner({
  environment,
  activeTransport,
  executionMode,
  liveVerifiedSponsors = 0,
  totalSponsors = 5,
}: DemoModeBannerProps) {
  const isProd = environment.toLowerCase() === "production";
  const transportLabel =
    activeTransport === "band_transport"
      ? "BAND A2A transport"
      : "Internal SQLite A2A transport";
  const modeLabel = executionMode || "DETERMINISTIC";

  return (
    <div
      role="status"
      className="border-b border-peach-border bg-peach/70 px-4 py-2.5 text-sm text-charcoal"
    >
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2.5">
          <span
            aria-hidden="true"
            className="inline-block h-2 w-2 rounded-full bg-status-pending"
          />
          <span className="font-semibold tracking-wide text-burgundy-deep">
            {isProd ? "RECALLRELAY v1.0.0" : "RECALLRELAY v1.0.0 — REAL-DATA POLICY"}
          </span>
          <span className="text-charcoal/85">
            Active transport: <strong>{transportLabel}</strong> · Execution:{" "}
            <strong>{modeLabel}</strong> · Live-verified sponsors:{" "}
            <strong>
              {liveVerifiedSponsors}/{totalSponsors}
            </strong>
            . Unverified records remain AWAITING_EVIDENCE.
          </span>
        </div>
        <span className="rounded-full border border-burgundy/20 bg-white/80 px-2.5 py-0.5 text-xs font-semibold uppercase tracking-wider text-burgundy-deep">
          {environment}
        </span>
      </div>
    </div>
  );
}
