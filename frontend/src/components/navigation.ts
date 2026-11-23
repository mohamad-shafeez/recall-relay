import type { AuthMode } from "../types/api";

export type NavSectionId =
  | "overview"
  | "recalls"
  | "evidence"
  | "organizations"
  | "supply-chain"
  | "agents"
  | "messages-tasks"
  | "traceability"
  | "approvals"
  | "audit-trail"
  | "settings";

export interface NavItem {
  id: NavSectionId;
  label: string;
  description: string;
  allowedModes: AuthMode[];
}

export const NAV_ITEMS: NavItem[] = [
  {
    id: "overview",
    label: "Overview",
    description: "Operational metrics & live status",
    allowedModes: ["public", "admin", "organization"],
  },
  {
    id: "recalls",
    label: "Recall Management",
    description: "Official notices, batches & disclosure",
    allowedModes: ["public", "admin", "organization"],
  },
  {
    id: "traceability",
    label: "Traceability",
    description: "Evidence-to-response chain & gaps",
    allowedModes: ["public", "admin", "organization"],
  },
  {
    id: "evidence",
    label: "Evidence Library",
    description: "Provenance, hashes & stages",
    allowedModes: ["admin", "organization"],
  },
  {
    id: "organizations",
    label: "Organizations",
    description: "Directory & identity verification",
    allowedModes: ["admin", "organization"],
  },
  {
    id: "supply-chain",
    label: "Supply Chain",
    description: "Boundary shipments, inventory & sharing",
    allowedModes: ["organization"],
  },
  {
    id: "agents",
    label: "Agent Operations",
    description: "7 deterministic specialist agents",
    allowedModes: ["admin", "organization"],
  },
  {
    id: "messages-tasks",
    label: "Messages & Tasks",
    description: "Durable A2A handoffs & inboxes",
    allowedModes: ["admin", "organization"],
  },
  {
    id: "approvals",
    label: "Approvals",
    description: "Human-in-the-loop decisions",
    allowedModes: ["admin"],
  },
  {
    id: "audit-trail",
    label: "Audit Trail",
    description: "SHA-256 hash-chained ledger",
    allowedModes: ["admin"],
  },
  {
    id: "settings",
    label: "Settings",
    description: "Session & backend diagnostics",
    allowedModes: ["public", "admin", "organization"],
  },
];

export function getAuthorizedNavItems(mode: AuthMode): NavItem[] {
  return NAV_ITEMS.filter((item) => item.allowedModes.includes(mode));
}
