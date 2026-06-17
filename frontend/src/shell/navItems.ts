/**
 * NAV_ITEMS — the single source of truth for the app's top-level view nav.
 *
 * Shared by TopChrome (the desktop horizontal chrome nav) and the Sidebar
 * (the mobile drawer nav). Keeping one list means the desktop bar and the
 * mobile drawer can never drift; role gating still flows from VIEW_MIN_ROLE
 * in types.ts, consulted by both consumers.
 */
import type { ViewName } from "../types";

export interface NavItem {
  view: ViewName;
  icon: string;
  label: string;
}

export const NAV_ITEMS: NavItem[] = [
  { view: "list", icon: "📋", label: "Work Items" },
  { view: "events", icon: "📅", label: "Events" },
  { view: "analytics", icon: "📊", label: "Analytics" },
  { view: "reports", icon: "📈", label: "Reports" },
  { view: "audit", icon: "🛡", label: "Audit Trail" },
  { view: "sessions", icon: "🔐", label: "Sessions" },
];
