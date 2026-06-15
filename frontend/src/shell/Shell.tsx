/**
 * App shell — composes the chrome from index.html: sidebar, topbar, type
 * tabs, KPI strip, filter bar, the six view sections and the shared modals.
 *
 * View sections keep their vanilla ids (#viewList etc., index.html
 * L202-501) and the vanilla show/hide mechanism (`hidden` attribute, port
 * of _toggleViewPanels, app.js L2328-2337); the React component for a view
 * only mounts while that view is active, which also replicates the
 * "re-fetch on entry" behaviour of _VIEW_REFRESHERS for the views that own
 * their data (events / audit / sessions / reports). The list + analytics
 * views read shared context data, so the entry-refresh for those is done
 * here (port of _VIEW_REFRESHERS.list / .analytics, app.js L2313-2326).
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { VIEW_MIN_ROLE } from "../types";
import { initPushOnBoot } from "../lib/push";
import Sidebar from "./Sidebar";
import Topbar from "./Topbar";
import TypeTabs from "./TypeTabs";
import KpiStrip from "./KpiStrip";
import FilterBar from "./FilterBar";
// ListView is the default view → keep it eager so first paint needs no extra
// network round-trip. The other five views are route-split: each becomes its
// own lazy chunk fetched on first navigation, shrinking the initial bundle.
import ListView from "../views/ListView";
const EventsView = lazy(() => import("../views/EventsView"));
const AnalyticsView = lazy(() => import("../views/AnalyticsView"));
const AuditView = lazy(() => import("../views/AuditView"));
const SessionsView = lazy(() => import("../views/SessionsView"));
const ReportsView = lazy(() => import("../views/ReportsView"));
import BugModal from "../modals/BugModal";
import ProjectModal from "../modals/ProjectModal";
import UserModal from "../modals/UserModal";
import ChangePasswordModal from "../modals/ChangePasswordModal";

export default function Shell() {
  const { view, setView, refreshAll, refreshStats, roleRank, currentUser } = useApp();

  // Mobile sidebar open state (port of #menuBtn / #sidebarBackdrop /
  // closeSidebar, app.js L4456-4461, L4782).
  const [mobileOpen, setMobileOpen] = useState(false);

  // If the user already granted web-push permission, silently refresh their FCM
  // token on boot so the backend always has the current one. Never prompts;
  // never throws. No-op when web push is disabled/unsupported.
  useEffect(() => {
    void initPushOnBoot();
  }, []);

  // Re-fetch shared data on view entry (port of _VIEW_REFRESHERS for the
  // context-backed views; boot() already covers the initial load).
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    if (view === "list") void refreshAll();
    else if (view === "analytics") void refreshStats();
  }, [view, refreshAll, refreshStats]);

  // Role gate: hiding the nav button isn't enough — if `view` is ever set to a
  // privileged view (deep link, stale state, a notification), the view would
  // still mount AND fire its on-entry fetch (+ error toast) for an
  // under-privileged user. Compute denial, bounce back to the list, and refuse
  // to render the gated branch even for one frame. The backend stays the real
  // authority (these routes are role-gated server-side).
  const need = VIEW_MIN_ROLE[view];
  const denied = need != null && roleRank(currentUser.role) < roleRank(need);
  useEffect(() => {
    if (denied) setView("list");
  }, [denied, setView]);

  return (
    <>
      <div
        id="sidebarBackdrop"
        className="sidebar-backdrop"
        hidden={!mobileOpen}
        onClick={() => setMobileOpen(false)}
      ></div>

      <div className="app">
        <Sidebar mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)} />

        <main className="main">
          <Topbar onOpenMobile={() => setMobileOpen(true)} />
          <TypeTabs />
          <KpiStrip />
          <FilterBar />

          {/* Each view component renders its own <section class="view"
              id="viewX"> root (vanilla ids preserved). Only the active view
              is mounted — mounting IS the show/hide mechanism, and it also
              replicates the "re-fetch on entry" behaviour of
              _VIEW_REFRESHERS for the views that own their data. Wrapping
              them in a second section here would duplicate the ids. */}
          <Suspense fallback={null}>
            {view === "list" && <ListView />}
            {view === "analytics" && <AnalyticsView />}
            {view === "audit" && !denied && <AuditView />}
            {view === "events" && <EventsView />}
            {view === "reports" && !denied && <ReportsView />}
            {view === "sessions" && !denied && <SessionsView />}
          </Suspense>
        </main>
      </div>

      {/* Shared modals — context-driven; each renders nothing when closed. */}
      <BugModal />
      <ProjectModal />
      <UserModal />
      <ChangePasswordModal />
    </>
  );
}
