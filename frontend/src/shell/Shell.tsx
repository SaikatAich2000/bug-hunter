/**
 * App shell — the two-tier chrome:
 *
 *   TopChrome   — brand + horizontal view nav + Ask Sleuth / bell / profile
 *   SuperNav    — work-item type tabs + search (list / analytics only)
 *   frame       — library-rail Sidebar + main(PageHead + content)
 *
 * Mounting is the show/hide mechanism: only the active view is mounted, which
 * also re-fetches on entry for the views that own their data (events / audit /
 * sessions / reports). The list and analytics views read shared context data,
 * so their entry-refresh is done here.
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

  const [mobileOpen, setMobileOpen] = useState(false);

  // If the user already granted web-push permission, silently refresh their FCM
  // token on boot so the backend always has the current one. Never prompts;
  // never throws. No-op when web push is disabled/unsupported.
  useEffect(() => {
    void initPushOnBoot();
  }, []);

  // Re-fetch shared data on view entry for the context-backed views; boot()
  // already covers the initial load.
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

      {/* Two-tier layout: a global chrome bar (brand + view nav + bell +
          profile), a supernav (type tabs + search), then a fixed library-rail
          Sidebar + scrolling main. Sidebar and nav coexist; no collapse. */}
      <TopChrome onOpenMobile={() => setMobileOpen(true)} />
      <SuperNav />

      <div className="frame">
        <Sidebar mobileOpen={mobileOpen} onNavigate={() => setMobileOpen(false)} />

        <main className="main">
          <PageHead />

          <div className="content">
            <KpiStrip />
            <FilterBar />

            {/* Each view component renders its own <section class="view"
                id="viewX"> root. Only the active view is mounted — mounting is
                the show/hide mechanism, and it also re-fetches on entry for the
                views that own their data. */}
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
