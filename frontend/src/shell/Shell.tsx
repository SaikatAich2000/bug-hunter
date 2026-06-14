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
import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { initPushOnBoot } from "../lib/push";
import Sidebar from "./Sidebar";
import Topbar from "./Topbar";
import TypeTabs from "./TypeTabs";
import KpiStrip from "./KpiStrip";
import FilterBar from "./FilterBar";
import ListView from "../views/ListView";
import EventsView from "../views/EventsView";
import AnalyticsView from "../views/AnalyticsView";
import AuditView from "../views/AuditView";
import SessionsView from "../views/SessionsView";
import ReportsView from "../views/ReportsView";
import BugModal from "../modals/BugModal";
import ProjectModal from "../modals/ProjectModal";
import UserModal from "../modals/UserModal";
import ChangePasswordModal from "../modals/ChangePasswordModal";

export default function Shell() {
  const { view, refreshAll, refreshStats } = useApp();

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
          {view === "list" && <ListView />}
          {view === "analytics" && <AnalyticsView />}
          {view === "audit" && <AuditView />}
          {view === "events" && <EventsView />}
          {view === "reports" && <ReportsView />}
          {view === "sessions" && <SessionsView />}
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
