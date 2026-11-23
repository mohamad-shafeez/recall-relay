interface HeaderProps {
  isReachable: boolean | null;
}

export function Header({ isReachable }: HeaderProps) {
  const statusLabel =
    isReachable === null
      ? "Checking API"
      : isReachable
        ? "Backend Connected"
        : "Not Connected";

  const statusClasses =
    isReachable === null
      ? "border-status-pending-border bg-status-pending-bg text-status-pending"
      : isReachable
        ? "border-status-verified-border bg-status-verified-bg text-status-verified"
        : "border-status-discrepancy-border bg-status-discrepancy-bg text-status-discrepancy";

  const dotClasses =
    isReachable === null
      ? "bg-status-pending"
      : isReachable
        ? "bg-status-verified"
        : "bg-status-discrepancy";

  return (
    <header className="border-b border-peach-border bg-white/95 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3.5">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-burgundy text-base font-bold tracking-tight text-white shadow-sm">
            RR
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-burgundy-deep">
              RecallRelay
            </h1>
            <p className="text-sm text-mauve">
              One recall. Multiple organizations. Coordinated response.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <span
            className={`inline-flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-xs font-semibold ${statusClasses}`}
          >
            <span aria-hidden="true" className={`h-2 w-2 rounded-full ${dotClasses}`} />
            {statusLabel}
          </span>
        </div>
      </div>
    </header>
  );
}
