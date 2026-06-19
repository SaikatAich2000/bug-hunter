/**
 * TopChrome — the global chrome bar: the mobile menu button + the brand mark +
 * the horizontal view nav (role-gated) + the right cluster (notifications bell,
 * profile menu).
 *
 * The brand mark and version live here (left edge), so the bar reads
 * "BUGHUNTER · Version X" in full and is never truncated. Role gating flows
 * from VIEW_MIN_ROLE in types.ts — the Shell consults the same map for view
 * mounting + fetch, and the Sidebar's mobile drawer renders the same
 * NAV_ITEMS — so the three nav surfaces can't drift.
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

  // Same VIEW_MIN_ROLE source of truth the Shell uses to gate rendering.
  const allowed = (v: ViewName): boolean => {
    const need = VIEW_MIN_ROLE[v];
    return !need || roleRank(currentUser.role) >= roleRank(need);
  };

  return (
    <header className="chrome">
      <button className="icon-btn menu-btn" id="menuBtn" aria-label="Open menu" onClick={onOpenMobile}>
        ☰
      </button>

      {/* Brand mark — the wordmark ("BUG" + accent "HUNTER") + version. */}
      <div className="brandmark" id="brandMark">
        <img className="logo" src="/static/icon.png" alt="Bug Hunter" />
        <div className="wm">
          <b>BUG<span>HUNTER</span></b>
          <small id="brandVersion">{health ? `Version ${health.version}` : "Version 3.0"}</small>
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
        {/* Mobile-only Sleuth trigger. On phones the bottom-right floating FAB
            is hidden; this button opens the same panel by forwarding the click
            to the FAB, keeping the panel's open/close logic in one place. */}
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
