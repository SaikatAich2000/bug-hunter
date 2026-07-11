// Single source of truth for nav entries (TopChrome + mobile Sidebar); role gating is VIEW_MIN_ROLE.
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
