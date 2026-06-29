/**
 * Top bar: hamburger, brand mark, horizontal nav (role-gated), notifications,
 * profile menu.
 *
 * Nav visibility uses VIEW_MIN_ROLE from types.ts — the same map the Shell uses
 * for view mounting and the Sidebar uses for its mobile drawer, so all three
 * surfaces stay in sync automatically.
 */
import { useApp } from "../state/AppContext";
import NotificationsBell from "./NotificationsBell";
import ProfileMenu from "./ProfileMenu";
import type { ViewName } from "../types";
import { VIEW_MIN_ROLE } from "../types";
import { NAV_ITEMS } from "./navItems";

interface Props {
  readonly onOpenMobile: () => void;
}

export default function TopChrome({ onOpenMobile }: Props) {
  const { currentUser, view, setView, roleRank, health } = useApp();

  // Shares VIEW_MIN_ROLE with the Shell and Sidebar so gating can't drift.
  const allowed = (v: ViewName): boolean => {
    const need = VIEW_MIN_ROLE[v];
    return !need || roleRank(currentUser.role) >= roleRank(need);
  };

  return (
    <header className="chrome">
      <button className="icon-btn menu-btn" id="menuBtn" aria-label="Open menu" onClick={onOpenMobile}>
        ☰
      </button>

      {/* Brand mark */}
      <div className="brandmark" id="brandMark">
        <img className="logo" src="/static/icon.png" alt="Bug Hunter" />
        <div className="wm">
          <b>BUG<span>HUNTER</span></b>
          <small id="brandVersion">{health ? `Version ${health.version}` : "Version 3.1"}</small>
        </div>
      </div>

      <nav className="nav" aria-label="Main sections">
        {NAV_ITEMS.filter((item) => allowed(item.view)).map((item) => (
          <button
            key={item.view}
            className={`nav-btn${view === item.view ? " active" : ""}`}
            data-view={item.view}
            onClick={() => setView(item.view)}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="spacer"></div>

      <div className="chrome-right">
        {/* Mobile Sleuth entry point. The FAB is hidden on small screens; this
            forwards to it so panel open/close logic stays in one place. */}
        <button
          type="button"
          className="chrome-sleuth-btn"
          aria-label="Ask Sleuth"
          title="Ask Sleuth — the AI assistant"
          onClick={() => document.getElementById("sleuthFab")?.click()}
        >
          <img className="chrome-sleuth-logo" src="/static/sleuth.svg" alt="" draggable={false} />
          <span>Ask Sleuth</span>
        </button>
        <NotificationsBell />
        <ProfileMenu />
      </div>
    </header>
  );
}
