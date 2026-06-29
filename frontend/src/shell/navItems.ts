/**
 * Single source of truth for top-level navigation entries.
 * Both the desktop TopChrome and the mobile Sidebar draw from this list
 * so the two can never drift. Role gating is handled separately via
 * VIEW_MIN_ROLE in types.ts.
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
