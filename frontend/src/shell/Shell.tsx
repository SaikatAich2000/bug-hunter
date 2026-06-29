/**
 * App shell — two-tier chrome:
 *   TopChrome  — brand + view nav + Ask Sleuth / bell / profile
 *   SuperNav   — work-item type tabs + search (list / analytics only)
 *   frame      — Sidebar rail + main (PageHead + content)
 *
 * Only the active view is mounted. Views that own their data (events / audit /
 * sessions / reports) re-fetch on mount; list and analytics re-fetch here
 * because they read shared context rather than fetching privately.
 */
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { VIEW_MIN_ROLE } from "../types";
import { initPushOnBoot } from "../lib/push";
import TopChrome from "./TopChrome";
import SuperNav from "./SuperNav";
import PageHead from "./PageHead";
import Sidebar from "./Sidebar";
import KpiStrip from "./KpiStrip";
import FilterBar from "./FilterBar";
// ListView is eager so the first paint needs no extra round-trip.
// The remaining views are lazy-split; each chunk loads on first navigation.
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

  const [mobileOpen, setMobileOpen] = useState(false);

  // Silently refresh the FCM token on boot so the backend stays current.
  // No-op when push is unsupported or permission was never granted.
  useEffect(() => {
    void initPushOnBoot();
  }, []);

  // Re-fetch shared data on view change; boot() covers the initial load.
  const firstView = useRef(true);
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    if (view === "list") void refreshAll();
    else if (view === "analytics") void refreshStats();
  }, [view, refreshAll, refreshStats]);

  // Hiding the nav button isn't enough — a deep link or stale state can still
  // land an under-privileged user on a gated view, triggering its fetch and an
  // error toast. Bounce them to the list and skip rendering for one frame.
  // The backend is the real authority; this is just UX protection.
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

      {/* TopChrome + SuperNav above; Sidebar rail + scrolling main below. */}
      <TopChrome onOpenMobile={() => setMobileOpen(true)} />
      <SuperNav />

      <div className="frame">
        <Sidebar mobileOpen={mobileOpen} onNavigate={() => setMobileOpen(false)} />

        <main className="main">
          <PageHead />

          <div className="content">
            <KpiStrip />
            <FilterBar />

            {/* Only the active view is mounted; unmounting is the hide mechanism. */}
            <Suspense fallback={null}>
              {view === "list" && <ListView />}
              {view === "analytics" && <AnalyticsView />}
              {view === "audit" && !denied && <AuditView />}
              {view === "events" && <EventsView />}
              {view === "reports" && !denied && <ReportsView />}
              {view === "sessions" && !denied && <SessionsView />}
            </Suspense>
          </div>
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
