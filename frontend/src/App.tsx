import { useCallback, useEffect, useState } from "react";

import { AuthModal } from "./components/AuthModal";
import { DemoModeBanner } from "./components/DemoModeBanner";
import { getAuthorizedNavItems } from "./components/navigation";
import type { NavSectionId } from "./components/navigation";
import { StatusBadge } from "./components/ui/DesignSystem";
import { SessionProvider, useSession } from "./context/SessionContext";
import { AgentOperationsPage } from "./pages/AgentOperationsPage";
import { ApprovalsPage } from "./pages/ApprovalsPage";
import { AuditTrailPage } from "./pages/AuditTrailPage";
import { EvidencePage } from "./pages/EvidencePage";
import { MessagesTasksPage } from "./pages/MessagesTasksPage";
import { OrganizationsPage } from "./pages/OrganizationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RecallsPage } from "./pages/RecallsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SupplyChainPage } from "./pages/SupplyChainPage";
import { TraceabilityPage } from "./pages/TraceabilityPage";
import { fetchBackendHealth } from "./services/api";
import type { HealthCheckResult } from "./types/health";

function RecallRelayShell() {
  const { credentials, signOut } = useSession();
  const [activeSection, setActiveSection] = useState<NavSectionId>("overview");
  const [traceRecallId, setTraceRecallId] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthCheckResult | null>(null);
  const [isCheckingHealth, setIsCheckingHealth] = useState(true);
  const [authModalOpen, setAuthModalOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const authorizedNav = getAuthorizedNavItems(credentials.mode);

  // If signing out or switching context removes access to current section, return safely to overview.
  useEffect(() => {
    if (!authorizedNav.some((item) => item.id === activeSection)) {
      setActiveSection("overview");
    }
  }, [authorizedNav, activeSection]);

  const runHealthCheck = useCallback(async (signal?: AbortSignal) => {
    setIsCheckingHealth(true);
    const result = await fetchBackendHealth(signal);
    if (!signal?.aborted) {
      setHealth(result);
      setIsCheckingHealth(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void runHealthCheck(controller.signal);
    return () => controller.abort();
  }, [runHealthCheck]);

  const currentNavItem =
    authorizedNav.find((item) => item.id === activeSection) ?? authorizedNav[0];

  function handleOpenTraceability(recallId: string) {
    setTraceRecallId(recallId);
    setActiveSection("traceability");
  }

  return (
    <div className="flex min-h-screen flex-col bg-cream text-charcoal">
      <DemoModeBanner environment={health?.data?.environment ?? "development"} />

      {/* Top Navigation Bar */}
      <header className="sticky top-0 z-30 border-b border-peach-border bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3.5">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setMobileNavOpen((prev) => !prev)}
              className="rounded-lg border border-peach-border bg-cream px-2.5 py-1.5 text-xs font-semibold text-burgundy-deep lg:hidden"
            >
              Menu
            </button>
            <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-burgundy text-sm font-bold text-white shadow-xs">
              RR
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-base font-bold tracking-tight text-burgundy-deep">
                  RecallRelay
                </span>
                <span className="hidden text-xs text-mauve sm:inline">
                  / {currentNavItem?.label}
                </span>
              </div>
              <p className="hidden text-[11px] text-mauve md:block">
                One recall. Multiple organizations. Coordinated response.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <StatusBadge
              value={
                health === null
                  ? "PENDING"
                  : health.reachable
                    ? "CONNECTED"
                    : "NOT_CONNECTED"
              }
            />
            <span className="hidden rounded-full border border-peach-border bg-cream px-3 py-1 text-xs font-medium text-charcoal sm:inline">
              {credentials.label}
            </span>
            <button
              type="button"
              onClick={() => setAuthModalOpen(true)}
              className="rounded-lg bg-burgundy px-3 py-1.5 text-xs font-semibold text-white hover:bg-burgundy-deep"
            >
              {credentials.mode === "public" ? "Sign In" : "Switch Access"}
            </button>
            {credentials.mode !== "public" && (
              <button
                type="button"
                onClick={() => void signOut()}
                className="rounded-lg border border-peach-border bg-white px-3 py-1.5 text-xs font-semibold text-charcoal hover:bg-cream"
              >
                Sign Out
              </button>
            )}
          </div>
        </div>
      </header>

      {/* Main Responsive App Shell */}
      <div className="mx-auto flex w-full max-w-7xl flex-1 gap-6 px-4 py-6">
        {/* Sidebar Navigation */}
        <aside
          className={`${
            mobileNavOpen ? "block" : "hidden"
          } w-64 shrink-0 lg:block`}
        >
          <nav
            aria-label="Primary navigation"
            className="sticky top-24 space-y-1 rounded-2xl border border-peach-border bg-white p-3 shadow-card"
          >
            {authorizedNav.map((item) => {
              const active = item.id === activeSection;
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => {
                    setActiveSection(item.id);
                    setMobileNavOpen(false);
                  }}
                  className={`flex w-full flex-col rounded-xl px-3 py-2.5 text-left transition ${
                    active
                      ? "bg-burgundy text-white shadow-xs"
                      : "text-charcoal hover:bg-peach/45"
                  }`}
                >
                  <span className="text-xs font-semibold">{item.label}</span>
                  <span
                    className={`mt-0.5 text-[11px] ${
                      active ? "text-peach" : "text-mauve"
                    }`}
                  >
                    {item.description}
                  </span>
                </button>
              );
            })}
          </nav>
        </aside>

        {/* Main Content Area */}
        <main className="min-w-0 flex-1">
          <div className="mb-5">
            <h1 className="text-xl font-bold tracking-tight text-burgundy-deep">
              {currentNavItem?.label}
            </h1>
            <p className="text-xs text-mauve">{currentNavItem?.description}</p>
          </div>

          {activeSection === "overview" && (
            <OverviewPage health={health} onNavigate={setActiveSection} />
          )}
          {activeSection === "recalls" && (
            <RecallsPage onOpenTraceability={handleOpenTraceability} />
          )}
          {activeSection === "evidence" && <EvidencePage />}
          {activeSection === "organizations" && <OrganizationsPage />}
          {activeSection === "supply-chain" && <SupplyChainPage />}
          {activeSection === "agents" && <AgentOperationsPage />}
          {activeSection === "messages-tasks" && <MessagesTasksPage />}
          {activeSection === "traceability" && (
            <TraceabilityPage initialRecallId={traceRecallId} />
          )}
          {activeSection === "approvals" && <ApprovalsPage />}
          {activeSection === "audit-trail" && <AuditTrailPage />}
          {activeSection === "settings" && (
            <SettingsPage
              health={health}
              isCheckingHealth={isCheckingHealth}
              onRefreshHealth={() => {
                void runHealthCheck();
              }}
              onOpenAuthModal={() => setAuthModalOpen(true)}
            />
          )}
        </main>
      </div>

      <footer className="border-t border-peach-border bg-white/80 py-4 text-center text-xs text-mauve">
        RecallRelay &middot; Real-Data-Only Multi-Agent Recall Coordination Network &middot;
        Server-side HttpOnly BFF sessions with CSRF protection
      </footer>

      <AuthModal isOpen={authModalOpen} onClose={() => setAuthModalOpen(false)} />
    </div>
  );
}

export function App() {
  return (
    <SessionProvider>
      <RecallRelayShell />
    </SessionProvider>
  );
}

export default App;
