export function FoundationShell() {
  return (
    <section
      aria-labelledby="workspace-shell-heading"
      className="rounded-2xl border border-peach-border bg-white p-6 shadow-card"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-burgundy">
            Operations Workspace Shell
          </p>
          <h2
            id="workspace-shell-heading"
            className="mt-1 text-lg font-semibold text-charcoal"
          >
            Phase 1 Foundation Active
          </h2>
        </div>
        <span className="w-fit rounded-full border border-peach-border bg-peach/70 px-3 py-1 text-xs font-semibold text-burgundy-deep">
          Phase 1 of 9
        </span>
      </div>

      <p className="mt-3 max-w-3xl text-sm leading-relaxed text-mauve">
        This initial workspace establishes the verified frontend-to-backend foundation for
        RecallRelay. Official public recall ingestion, multi-organization evidence
        tracking, autonomous coordination agents, and the operational dashboard will be
        built in upcoming phases using real, traceable records only.
      </p>

      <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm font-semibold text-charcoal">Recall Notice Intake</p>
            <span className="rounded-full border border-status-pending-border bg-status-pending-bg px-2 py-0.5 text-[11px] font-semibold text-status-pending">
              AWAITING EVIDENCE
            </span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-mauve">
            Reserved for official public recall notices and verified batch references.
          </p>
        </div>

        <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm font-semibold text-charcoal">
              Organization Coordination
            </p>
            <span className="rounded-full border border-peach-border bg-peach/60 px-2 py-0.5 text-[11px] font-semibold text-burgundy-deep">
              NOT CONNECTED
            </span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-mauve">
            Reserved for authorized manufacturer, distributor, and retailer records.
          </p>
        </div>

        <div className="rounded-xl border border-peach-border bg-cream/60 p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm font-semibold text-charcoal">
              Verification &amp; Approval
            </p>
            <span className="rounded-full border border-status-pending-border bg-status-pending-bg px-2 py-0.5 text-[11px] font-semibold text-status-pending">
              AWAITING EVIDENCE
            </span>
          </div>
          <p className="mt-2 text-xs leading-relaxed text-mauve">
            Reserved for evidence reconciliation and human approval gates.
          </p>
        </div>
      </div>
    </section>
  );
}
