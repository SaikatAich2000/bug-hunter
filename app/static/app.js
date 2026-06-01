/* ============================================================
 * Bug Hunter — frontend SPA
 * ============================================================ */
(() => {
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const STATE = {
  // v2.5: statuses_by_type maps each item_type to its valid status set.
  // We default to a sensible Bug-only fallback so legacy clients without
  // a fresh /api/meta call still render something meaningful.
  meta:     {
    statuses: [], priorities: [], environments: [],
    item_types: ["Bug", "Requirement", "Task"],
    statuses_by_type: {
      Bug: ["New", "In Progress", "Resolved", "Closed", "Reopened", "Not a Bug", "Resolve Later"],
      Requirement: ["New", "In Review", "Approved", "Implemented", "Rejected", "Deferred"],
      Task: ["New", "In Progress", "Done", "Blocked", "Cancelled"],
    },
  },
  users:    [],
  projects: [],
  stats:    null,
  bugs:     [],
  page:     1,
  pageSize: 50,
  totalPages: 1,
  total: 0,
  // Filters: each enum-like filter is now an ARRAY (multi-select). The free-
  // text search `q` and the legacy single-value `reporter_id` stay scalar.
  filters: {
    project_id: [], status: [], priority: [],
    environment: [], assignee_id: [], item_type: [],
    reporter_id: "", q: "",
  },
  // The "+ New" split button remembers what kind of item to default to.
  // Persisted to localStorage so reopening the app keeps the last choice.
  defaultNewType: (() => {
    try { return localStorage.getItem("defaultNewType") || "Bug"; }
    catch { return "Bug"; }
  })(),
  // Active work-items tab: "all" / "Bug" / "Requirement" / "Task".
  // Drives which columns render in the table and what implicit
  // item_type filter is applied to /api/bugs requests.
  activeTab: (() => {
    try { return localStorage.getItem("activeTab") || "all"; }
    catch { return "all"; }
  })(),
  // Events list + drill-in state.
  events: [],
  currentEventId: null,    // null = list mode; id = detail mode
  currentEvent: null,      // populated when in detail mode
  view: "list",
  currentBugId: null,
  // Detail tabs are gone in v3.1 — bug detail is now a single inline
  // screen (Jira-style). detailTab kept here as a no-op for backward
  // compat in case any external code path still touches it.
  detailTab: "info",
  sessions: [],
  currentUser: null,   // populated from /api/auth/me at boot
  // Asset hash served by /api/health at boot; if it changes later we
  // know the server has been redeployed.
  bootAssetVersion: null,
  versionDriftWarned: false,
  // Sidebar collapsed flag. Persisted to localStorage so the layout the
  // user picked survives page reloads.
  sidebarCollapsed: false,
};

const API = "/api";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

const debounce = (fn, ms = 250) => {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
};

const initials = (name) => {
  const parts = String(name || "?").trim().split(/\s+/);
  return ((parts[0]?.[0] || "?") + (parts[1]?.[0] || "")).toUpperCase();
};

const formatDate = (iso) => {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay
      ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
  } catch { return iso; }
};

const formatBytes = (n) => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
};

const fileIcon = (ct, name) => {
  ct = (ct || "").toLowerCase();
  name = (name || "").toLowerCase();
  if (ct.startsWith("image/")) return "🖼";
  if (ct.startsWith("video/")) return "🎬";
  if (ct === "application/pdf" || name.endsWith(".pdf")) return "📕";
  if (ct.startsWith("audio/")) return "🎵";
  if (ct.includes("zip") || name.endsWith(".zip")) return "📦";
  return "📎";
};

// ---------------------------------------------------------------------------
// API client
// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  // Don't auto-set Content-Type for FormData (browser sets boundary)
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(API + path, {
    ...opts,
    headers,
    credentials: "include",   // send/receive session cookies
  });
  if (!res.ok) {
    // Session expired or otherwise rejected — bounce to login. We delegate
    // to bounceToLogin() so multiple in-flight 401s during a session
    // revocation only trigger one redirect (sessionRedirectInFlight guard).
    if (res.status === 401 && path !== "/auth/login") {
      bounceToLogin();
      const err = new Error("Not authenticated");
      err.status = 401;
      err.silent = true;
      throw err;
    }
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (Array.isArray(body.detail)) {
        detail = body.detail.map(d => `${(d.loc || []).slice(1).join(".") || "field"}: ${d.msg}`).join("; ");
      } else if (body.detail) {
        detail = body.detail;
      }
    } catch { /* not JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

// ---------------------------------------------------------------------------
// Global blocking loader (v2.5)
//
// Any action button that triggers a server round-trip should run inside
// withLoader() so the user sees an "in flight" overlay and can't
// double-submit by mashing buttons or clicking somewhere else mid-call.
// The counter pattern handles concurrent actions: the overlay only
// disappears when EVERY in-flight op has finished.
// ---------------------------------------------------------------------------
let _loaderPending = 0;
function _refreshLoader() {
  const el = document.getElementById("globalLoader");
  if (!el) return;
  const shouldShow = _loaderPending > 0;
  el.hidden = !shouldShow;
  el.setAttribute("aria-hidden", shouldShow ? "false" : "true");
  // Reflect on <body> so CSS can ::disable interactions on background
  // tooltips / focus rings without us having to bind listeners.
  document.body.classList.toggle("is-loading", shouldShow);
}
function showLoader(message) {
  _loaderPending++;
  const txt = document.getElementById("globalLoaderText");
  if (txt && message) txt.textContent = message;
  _refreshLoader();
}
function hideLoader() {
  _loaderPending = Math.max(0, _loaderPending - 1);
  _refreshLoader();
}
async function withLoader(thunk, message) {
  showLoader(message || "Working…");
  try {
    return await thunk();
  } finally {
    hideLoader();
  }
}

// ---------------------------------------------------------------------------
// Toast + Modal helpers
// ---------------------------------------------------------------------------
let toastTimer = null;
function toast(msg, type = "info") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// Show an error toast UNLESS the error is a silent auth-redirect from api().
// This prevents the brief flash of "Not authenticated" toasts during the
// navigation from / to /login.html when a session expires.
function toastError(err) {
  if (err && err.silent) return;
  toast(err?.message || "Something went wrong", "error");
}

function openModal(id) {
  const m = document.getElementById(id);
  if (m) m.hidden = false;
}
function closeModal(id) {
  const m = document.getElementById(id);
  if (m) m.hidden = true;
}
function closeTopModal() {
  const open = $$(".modal:not([hidden])");
  if (open.length) open[open.length - 1].hidden = true;
}

function confirmDialog(message, { title = "Confirm", okLabel = "Delete", danger = true } = {}) {
  // Track the in-flight resolve so Escape / backdrop-click handlers can
  // also resolve the promise (as cancel). Without this, dismissing the
  // dialog with Escape leaves the await dangling forever AND the next
  // confirmDialog stacks new listeners on top of the stale ones, so
  // clicking OK fires both old and new resolves — silently triggering
  // the previously-abandoned action (e.g. an unintended delete).
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmMessage").textContent = message;
    const ok = $("#confirmOk");
    const cancel = $("#confirmCancel");
    const modalEl = document.getElementById("modalConfirm");
    ok.textContent = okLabel;
    ok.className = "btn " + (danger ? "danger" : "primary");
    let settled = false;
    const settle = (value) => {
      if (settled) return;
      settled = true;
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      document.getElementById("confirmClose").removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey, true);
      closeModal("modalConfirm");
      resolve(value);
    };
    const onOk      = () => settle(true);
    const onCancel  = () => settle(false);
    const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); settle(false); } };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    document.getElementById("confirmClose").addEventListener("click", onCancel);
    // Use capture so we beat the global Escape handler at lower layer.
    document.addEventListener("keydown", onKey, true);
    openModal("modalConfirm");
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  const theme = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", theme);

  // Restore the sidebar's collapsed state BEFORE first paint to avoid a
  // visible flash of the wrong layout. The CSS class is what actually
  // changes the grid columns; we just make sure it's on the body before
  // the user sees anything.
  STATE.sidebarCollapsed = localStorage.getItem("sidebarCollapsed") === "1";
  if (STATE.sidebarCollapsed) {
    document.body.classList.add("sidebar-collapsed");
  }

  // Auth check first. Use a direct fetch (not api()) so we control the
  // 401 path explicitly: redirect before *any* other code can run, so the
  // user never sees error toasts from cookie-less follow-up calls.
  let me;
  try {
    const res = await fetch(API + "/auth/me", { credentials: "include" });
    if (!res.ok) {
      location.replace("/login.html");
      return;
    }
    me = await res.json();
  } catch {
    location.replace("/login.html");
    return;
  }
  STATE.currentUser = me;

  applyRoleVisibility();
  renderAccountCard();

  // Perf v3.2.1: these four loaders are independent of each other — meta
  // is a static enum, users and projects come from their own tables,
  // health is a tiny status ping. Running them sequentially was costing
  // sum-of-latencies on every page load. Running them concurrently with
  // Promise.all collapses that to max-of-latencies (~4× faster on a
  // slow connection). Errors from any one still surface via the api()
  // helper's 401 redirect path / toastError chain.
  await Promise.all([
    loadHealth(),
    loadMeta(),
    loadUsers(),
    loadProjects(),
  ]);
  // Multi-select dropdowns depend on STATE.users / STATE.projects / STATE.meta
  // being populated, so initialise them after the loaders above.
  initMultiSelects();
  await refreshAll();
  bindGlobalListeners();
  scheduleVersionCheck();
  // Polls /api/auth/me every 15 s so admin session-revocation kicks the
  // user out within seconds, not only when they next click something.
  scheduleSessionPoll();
}

function applyRoleVisibility() {
  const role = STATE.currentUser?.role || "";
  // role rank: admin > manager > user
  const rank = { admin: 3, manager: 2, user: 1 }[role] || 0;
  $$("[data-needs-role]").forEach(el => {
    const need = el.getAttribute("data-needs-role");
    const needRank = { admin: 3, manager: 2, user: 1 }[need] || 0;
    if (rank >= needRank) {
      // Drop the attribute so `[data-needs-role] { display:none }` no longer
      // matches. Setting style.display = "" alone is not enough — that CSS
      // rule still wins on specificity.
      el.removeAttribute("data-needs-role");
    } else {
      el.style.display = "none";
    }
  });
}

function renderAccountCard() {
  const u = STATE.currentUser;
  if (!u) return;
  $("#accountAvatar").textContent = initials(u.name);
  $("#accountName").textContent = u.name;
  $("#accountRole").textContent = u.role;
  $("#accountEmail").textContent = u.email;
}

async function loadHealth() {
  try {
    const h = await api("/health");
    $("#brandVersion").textContent = "v" + h.version;
    // Note the asset_version we booted under so we can detect server
    // redeploys later (see scheduleVersionCheck).
    if (h.asset_version) STATE.bootAssetVersion = h.asset_version;
  } catch { /* ignore */ }
}

// If the server gets redeployed while a tab is open, future API calls
// continue to work but the in-page JS can be subtly stale. Poll
// /api/health every 5 minutes; if asset_version changes, the next page
// navigation should pull the fresh HTML+JS. We just notify the user;
// don't auto-reload because they might have unsaved input.
function scheduleVersionCheck() {
  setInterval(async () => {
    try {
      const h = await fetch("/api/health", { credentials: "include" }).then(r => r.json());
      if (
        STATE.bootAssetVersion &&
        h.asset_version &&
        h.asset_version !== STATE.bootAssetVersion &&
        !STATE.versionDriftWarned
      ) {
        STATE.versionDriftWarned = true;
        toast("New version available — reload the page when ready", "info");
      }
    } catch { /* ignore */ }
  }, 5 * 60 * 1000);
}

// ---------------------------------------------------------------------------
// Session-validity poll — Keycloak-style revocation should kick the user
// out of the SPA quickly, not only when they happen to make an API call.
// We hit /api/auth/me every 15 seconds (cheap — single indexed DB lookup
// + maybe one last_seen_at update). On 401, we redirect to /login.html.
//
// We also re-check on tab visibility change, so a user who tabs back to
// the app gets bounced immediately rather than after the next interval.
// ---------------------------------------------------------------------------
const SESSION_POLL_MS = 15 * 1000;
let sessionPollTimer = null;
let sessionRedirectInFlight = false;

function bounceToLogin() {
  if (sessionRedirectInFlight) return;
  sessionRedirectInFlight = true;
  // Stop the poll so we don't queue further requests during the redirect.
  if (sessionPollTimer) { clearInterval(sessionPollTimer); sessionPollTimer = null; }
  // Best-effort toast — won't always be visible (we're navigating away).
  try { toast("Your session ended. Redirecting to login…", "info"); } catch {}
  // location.replace is preferred so the broken-state URL isn't in history.
  // Fall back to .href in case replace is blocked for any reason.
  try { location.replace("/login.html"); }
  catch { location.href = "/login.html"; }
}

async function checkSessionValid() {
  try {
    const res = await fetch(API + "/auth/me", {
      credentials: "include",
      // Skip the browser cache so a revoked session can't be hidden by a
      // stale 200 response.
      cache: "no-store",
      headers: { "X-Session-Check": "1" },
    });
    if (res.status === 401 || res.status === 403) {
      bounceToLogin();
      return false;
    }
    return res.ok;
  } catch {
    // Network error — don't kick the user out for a transient blip.
    return true;
  }
}

function scheduleSessionPoll() {
  if (sessionPollTimer) clearInterval(sessionPollTimer);
  sessionPollTimer = setInterval(checkSessionValid, SESSION_POLL_MS);
  // Also re-check whenever the tab becomes visible — covers the case
  // where the laptop slept for an hour and the interval didn't fire.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkSessionValid();
  });
}

async function loadMeta() {
  const remote = await api("/meta");
  // Keep the v2.5 statuses_by_type fallback if an older server doesn't
  // ship the field. Without this, a stale build would render an empty
  // status dropdown (the select would only contain the "— select —"
  // placeholder and silently break form submission).
  const fallback = STATE.meta.statuses_by_type || {};
  STATE.meta = {
    ...remote,
    statuses_by_type: remote.statuses_by_type || fallback,
  };
  // Multi-select panels are repopulated by refreshMultiSelects(); the legacy
  // <select> filters were removed in favour of the new dropdowns.
}

// Returns the valid statuses for a given item_type, falling back to the
// global union if the server didn't ship the per-type map (very old
// build) or the type isn't recognized.
function statusesForType(itype) {
  const byType = (STATE.meta && STATE.meta.statuses_by_type) || {};
  return byType[itype || "Bug"] || (STATE.meta && STATE.meta.statuses) || [];
}

async function loadUsers() {
  STATE.users = await api("/users");
  renderUserList();
  fillAuditActorSelect();
  refreshMultiSelects();
}

async function loadProjects() {
  STATE.projects = await api("/projects");
  renderProjectList();
  refreshMultiSelects();
}

async function refreshAll() {
  await Promise.all([refreshBugs(), refreshStats()]);
}

// ---------------------------------------------------------------------------
// Stats / KPIs
// ---------------------------------------------------------------------------
async function refreshStats() {
  // KPI strip is scoped by the active tab. The server filters every
  // aggregation (status / priority / env / project / assignee / timeline)
  // when item_type is supplied, but always returns by_type unfiltered so
  // the four tab badges (All / Bug / Requirement / Task) keep showing the
  // global counts.
  const path = (STATE.activeTab && STATE.activeTab !== "all")
    ? `/stats?item_type=${encodeURIComponent(STATE.activeTab)}`
    : "/stats";
  STATE.stats = await api(path);
  // KPI strip: Total | Open | Resolved | Closed | Resolve Later. We
  // defensively coalesce missing fields to 0 so an older server that
  // hasn't shipped the new schema yet doesn't render `undefined`.
  const s = STATE.stats || {};
  $("#kpiBugs").textContent = s.bugs ?? 0;
  $("#kpiOpen").textContent = s.open ?? 0;
  $("#kpiResolved").textContent = s.resolved ?? 0;
  $("#kpiClosed").textContent = s.closed ?? (s.by_status?.Closed ?? 0);
  $("#kpiResolveLater").textContent = s.resolve_later ?? (s.by_status?.["Resolve Later"] ?? 0);
  // by_type (always global) → tab-count badges. "All" sums every
  // non-event type.
  const byType = s.by_type || {};
  const tabAll = (byType.Bug ?? 0) + (byType.Requirement ?? 0) + (byType.Task ?? 0);
  const elAll = $("#typeTabCountAll");          if (elAll) elAll.textContent = tabAll;
  const elBug = $("#typeTabCountBug");          if (elBug) elBug.textContent = byType.Bug ?? 0;
  const elReq = $("#typeTabCountRequirement");  if (elReq) elReq.textContent = byType.Requirement ?? 0;
  const elTsk = $("#typeTabCountTask");         if (elTsk) elTsk.textContent = byType.Task ?? 0;
  refreshKpiActiveState();
  refreshTypeTabActiveState();
  if (STATE.view === "analytics") renderCharts();
}

// ---------------------------------------------------------------------------
// Bug list
// ---------------------------------------------------------------------------
async function refreshBugs() {
  // Reflect current status filter in the KPI tile highlight. Runs on every
  // bug refresh so any filter change (multi-select, KPI click, Clear,
  // sidebar project click) keeps the KPI active state in sync.
  refreshKpiActiveState();
  refreshTypeTabActiveState();
  const params = new URLSearchParams();
  params.set("page", String(STATE.page));
  params.set("page_size", String(STATE.pageSize));
  // Multi-value filters: append each value as its own query param so the
  // backend sees `?status=A&status=B`. FastAPI parses repeated params
  // into a list. Scalar filters (q, reporter_id) are appended once.
  for (const [k, v] of Object.entries(STATE.filters)) {
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item !== "" && item != null) params.append(k, String(item));
      }
    } else if (v !== "" && v != null) {
      params.set(k, String(v));
    }
  }
  // Implicit tab filter: if the user is on the Bugs / Requirements / Tasks
  // tab, layer that on top of whatever's in STATE.filters.item_type. This
  // lets the user still multi-select extra types from the dropdown if
  // they want, but the tab provides the default narrowing.
  if (STATE.activeTab && STATE.activeTab !== "all") {
    const explicit = STATE.filters.item_type || [];
    if (!explicit.includes(STATE.activeTab)) {
      // Don't mutate STATE.filters — the tab is implicit, not a sticky filter.
      params.append("item_type", STATE.activeTab);
    }
  }
  const data = await api("/bugs?" + params.toString());
  STATE.bugs = data.items;
  STATE.total = data.total;
  STATE.totalPages = data.pages;
  renderBugTable();
  renderPagination();
}

function refreshTypeTabActiveState() {
  $$(".type-tab[data-tab]").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === STATE.activeTab);
    b.setAttribute("aria-selected", b.dataset.tab === STATE.activeTab ? "true" : "false");
  });
}

// Switch tabs. Doesn't write item_type into STATE.filters (the implicit
// filter applies at request time in refreshBugs). Persists to
// localStorage so a reload lands you back on the same tab.
function setActiveTab(tab) {
  STATE.activeTab = tab;
  try { localStorage.setItem("activeTab", tab); } catch {}
  STATE.page = 1;
  refreshTypeTabActiveState();
  // Filter bar reacts to the tab (Env hides on Req/Task, Type hides on
  // any non-All tab). The function is a no-op until #filterBar is in
  // the DOM, so it's safe to call early.
  refreshFilterBarVisibility();
  // Notify the "+ New" button (and anything else interested) so its label
  // can flip to match the new tab.
  document.dispatchEvent(new CustomEvent("bh:tab-change", { detail: { tab } }));
  // Both the work-items table AND the KPI strip / analytics charts are
  // tab-scoped, so we always refresh stats too. refreshBugs reloads the
  // table; refreshStats reloads the KPI tiles and (if visible) charts.
  refreshBugs();
  refreshStats();
}

// Hide filters that don't apply on the current tab:
//   - "All Types": redundant when a specific tab is active
//   - "All Envs":  Requirements / Tasks don't have an environment
function refreshFilterBarVisibility() {
  const tab = STATE.activeTab || "all";
  const typeFilter = document.querySelector('.ms-wrap[data-filter="item_type"]');
  if (typeFilter) typeFilter.style.display = tab === "all" ? "" : "none";
  const envFilter = document.querySelector('.ms-wrap[data-filter="environment"]');
  if (envFilter) envFilter.style.display = (tab === "Requirement" || tab === "Task") ? "none" : "";
}

// Column sets per tab. Each entry is one column descriptor used to
// build both the <th> row in <thead> and the matching <td> in each row.
// Keeping these as a data structure rather than inline JSX makes adding
// a future tab (Epic? Sub-task?) a one-liner.
const TAB_COLUMNS = {
  all: [
    "id", "title-with-type", "project", "status", "priority", "env", "assignees", "att", "actions",
  ],
  Bug: [
    "id", "title", "project", "status", "priority", "env", "assignees", "att", "actions",
  ],
  Requirement: [
    "id", "title", "project", "status", "priority", "assignees", "att", "actions",
  ],
  Task: [
    "id", "title", "project", "status", "priority", "due", "event", "assignees", "actions",
  ],
};

const COL_HEAD_LABEL = {
  id: "#",
  title: "Title",
  "title-with-type": "Title",
  project: "Project",
  status: "Status",
  priority: "Priority",
  env: "Env",
  due: "Due",
  event: "Event",
  assignees: "Assignees",
  att: "📎",
  actions: "Actions",
};

function _renderCell(col, bug) {
  switch (col) {
    case "id":
      return `<td class="col-id">#${bug.id}</td>`;
    case "title": {
      // For per-type tabs the type prefix is redundant — the tab IS the type.
      return `
        <td class="col-title">
          <div class="title-cell">
            <strong class="title-text" title="${escapeHtml(bug.title)}">${escapeHtml(bug.title)}</strong>
            <span class="title-meta">Updated ${formatDate(bug.updated_at)}</span>
          </div>
        </td>`;
    }
    case "title-with-type": {
      const itype = bug.item_type || "Bug";
      return `
        <td class="col-title">
          <div class="title-cell">
            <strong class="title-text" title="${escapeHtml(bug.title)}"><span class="inline-type" data-type="${escapeHtml(itype)}" title="${escapeHtml(itype)}">${itemTypeEmoji(itype)}</span> ${escapeHtml(bug.title)}</strong>
            <span class="title-meta">${escapeHtml(itype)} · Updated ${formatDate(bug.updated_at)}</span>
          </div>
        </td>`;
    }
    case "project":
      return `<td class="col-project">${escapeHtml(bug.project_name || "")}</td>`;
    case "status":
      return `<td class="col-status"><span class="badge" data-status="${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span></td>`;
    case "priority":
      return `<td class="col-priority"><span class="badge" data-priority="${escapeHtml(bug.priority)}">${escapeHtml(bug.priority)}</span></td>`;
    case "env":
      return `<td class="col-env"><span class="badge" data-env="${escapeHtml(bug.environment)}">${escapeHtml(bug.environment)}</span></td>`;
    case "due":
      return `<td class="col-due">${bug.due_date ? escapeHtml(bug.due_date) : '<span class="muted">—</span>'}</td>`;
    case "event":
      return `<td class="col-event">${bug.event_name ? `<span class="event-pill" title="${escapeHtml(bug.event_name)}">📅 ${escapeHtml(bug.event_name)}</span>` : '<span class="muted">—</span>'}</td>`;
    case "assignees": {
      const html = bug.assignees.length
        ? bug.assignees.map(a => `<span class="assignee-chip" title="${escapeHtml(a.email)}"><span class="avatar">${initials(a.name)}</span><span class="assignee-chip-name">${escapeHtml(a.name)}</span></span>`).join("")
        : `<span class="muted">—</span>`;
      return `<td class="col-assignees"><div class="assignee-stack">${html}</div></td>`;
    }
    case "att":
      return `<td class="col-att">${bug.attachment_count > 0 ? `<span class="att-count">📎 ${bug.attachment_count}</span>` : '<span class="muted">—</span>'}</td>`;
    case "actions": {
      const isAdmin = STATE.currentUser?.role === "admin";
      return `
        <td class="col-actions">
          <div class="row-actions">
            ${isAdmin ? `<button class="icon-btn danger" data-act="delete" data-id="${bug.id}" title="Delete">🗑</button>` : ""}
          </div>
        </td>`;
    }
    default:
      return "<td></td>";
  }
}

function _renderTableHead(cols) {
  return "<tr>" + cols.map(c => `<th class="col-${c.replace('title-with-type','title')}">${COL_HEAD_LABEL[c] ?? ""}</th>`).join("") + "</tr>";
}

function renderBugTable() {
  const thead = $("#bugTableHead");
  const tbody = $("#bugTableBody");
  if (!tbody) return;
  const cols = TAB_COLUMNS[STATE.activeTab] || TAB_COLUMNS.all;
  thead.innerHTML = _renderTableHead(cols);
  tbody.innerHTML = "";
  $("#emptyState").hidden = STATE.bugs.length > 0;

  const frag = document.createDocumentFragment();
  for (const bug of STATE.bugs) {
    const tr = document.createElement("tr");
    tr.dataset.bugId = String(bug.id);
    tr.innerHTML = cols.map(c => _renderCell(c, bug)).join("");
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);
}

function renderPagination() {
  const bar = $("#paginationBar");
  if (STATE.totalPages <= 1) { bar.innerHTML = ""; return; }
  bar.innerHTML = `
    <button id="pgPrev" ${STATE.page <= 1 ? "disabled" : ""}>← Prev</button>
    <span>Page ${STATE.page} of ${STATE.totalPages} (${STATE.total} bugs)</span>
    <button id="pgNext" ${STATE.page >= STATE.totalPages ? "disabled" : ""}>Next →</button>`;
  $("#pgPrev")?.addEventListener("click", () => { STATE.page--; refreshBugs(); });
  $("#pgNext")?.addEventListener("click", () => { STATE.page++; refreshBugs(); });
}

// ---------------------------------------------------------------------------
// Sidebar lists
// ---------------------------------------------------------------------------
function renderProjectList() {
  const ul = $("#projectList");
  ul.innerHTML = "";
  if (!STATE.projects.length) {
    ul.innerHTML = `<li class="side-item muted no-cursor">No projects — click + to add</li>`;
    return;
  }
  // v3.1 permissions:
  //   • edit project  : admin or manager
  //   • delete project: admin only
  // (Plain users see neither button — the side rail header is hidden
  // for them via data-needs-role on the section itself.)
  const role = STATE.currentUser?.role || "";
  const canManage = role === "admin" || role === "manager";
  const canDelete = role === "admin";
  // Active = the project's id is currently in the multi-select filter array.
  const activeIds = new Set((STATE.filters.project_id || []).map(String));
  for (const p of STATE.projects) {
    const li = document.createElement("li");
    li.className = "side-item" + (activeIds.has(String(p.id)) ? " active" : "");
    li.dataset.projectId = String(p.id);
    li.title = p.name;
    li.innerHTML = `
      <span class="swatch" style="background:${escapeHtml(p.color)}"></span>
      <span class="label-text" data-act="filter">${escapeHtml(p.name)}</span>
      <span class="row-actions">
        ${canManage ? `<button class="icon-btn" data-act="edit-project" data-id="${p.id}" title="Edit">✎</button>` : ""}
        ${canDelete ? `<button class="icon-btn danger" data-act="delete-project" data-id="${p.id}" title="Delete">🗑</button>` : ""}
      </span>`;
    ul.appendChild(li);
  }
}

function renderUserList() {
  const ul = $("#userList");
  ul.innerHTML = "";
  const active = STATE.users.filter(u => u.is_active);
  if (!active.length) {
    ul.innerHTML = `<li class="side-item muted no-cursor">No users yet — click + to add</li>`;
    return;
  }
  // v3.1 permissions:
  //   • edit user  : admin or manager  (managers can't edit admins; the
  //                  backend enforces it. We still show the button so a
  //                  manager can edit non-admins; if they click on an
  //                  admin row, they'll get a 403 toast.)
  //   • delete user: admin only.
  // The Users sidebar section is gated on data-needs-role="manager" so
  // plain users never see this list at all.
  const role = STATE.currentUser?.role || "";
  const canEdit = role === "admin" || role === "manager";
  const canDelete = role === "admin";
  for (const u of active) {
    const li = document.createElement("li");
    li.className = "side-item";
    li.dataset.userId = String(u.id);
    li.title = `${u.email}${u.role ? " — " + u.role : ""}`;
    li.innerHTML = `
      <span class="avatar">${initials(u.name)}</span>
      <span class="label-text" data-act="filter-user">
        ${escapeHtml(u.name)}
        ${u.role ? `<span class="meta"> · ${escapeHtml(u.role)}</span>` : ""}
      </span>
      <span class="row-actions">
        ${canEdit ? `<button class="icon-btn" data-act="edit-user" data-id="${u.id}" title="Edit">✎</button>` : ""}
        ${canDelete ? `<button class="icon-btn danger" data-act="delete-user" data-id="${u.id}" title="Delete">🗑</button>` : ""}
      </span>`;
    ul.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// Selects (form-level only — filter bar uses the multi-select widgets below)
// ---------------------------------------------------------------------------
function fillAuditActorSelect() {
  const sel = $("#auditActorFilter");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">All actors</option>` +
    STATE.users.map(u => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join("");
  if (cur) sel.value = cur;
}

// ---------------------------------------------------------------------------
// Multi-select dropdowns (filter bar)
//
// One panel per filter, each driven by `STATE.filters[<key>]` which is
// always an array. Clicking a row toggles that value's membership in the
// array. The panel header button shows a summary ("All X" / "X (n)" /
// the single value) and is the click target for opening / closing the panel.
// ---------------------------------------------------------------------------
const MS_LABELS = {
  project_id:  "All Projects",
  item_type:   "All Types",
  status:      "All Statuses",
  priority:    "All Priorities",
  environment: "All Envs",
  assignee_id: "All Assignees",
};
const MS_NOUNS = {
  project_id: "Projects", item_type: "Types",
  status: "Statuses", priority: "Priorities",
  environment: "Envs",    assignee_id: "Assignees",
};

function _msOptions(key) {
  // Each option is [value, label]. value is what we send to the API,
  // label is what the user sees.
  if (key === "project_id") {
    return STATE.projects.map(p => [String(p.id), p.name]);
  }
  if (key === "assignee_id") {
    return STATE.users.filter(u => u.is_active).map(u => [String(u.id), u.name]);
  }
  if (key === "status")      return (STATE.meta.statuses     || []).map(s => [s, s]);
  if (key === "priority")    return (STATE.meta.priorities   || []).map(s => [s, s]);
  if (key === "environment") return (STATE.meta.environments || ["DEV","UAT","PROD"]).map(s => [s, s]);
  if (key === "item_type")   return (STATE.meta.item_types   || ["Bug","Requirement","Task"]).map(t => [t, t]);
  return [];
}

// Per-type emoji marker. Used everywhere we render an item-type badge.
const ITEM_TYPE_EMOJI = { Bug: "🐞", Requirement: "📐", Task: "✅" };
function itemTypeEmoji(t) { return ITEM_TYPE_EMOJI[t] || "📝"; }

function initMultiSelects() {
  $$(".ms-wrap").forEach(wrap => {
    const key = wrap.dataset.filter;
    const toggle = wrap.querySelector("[data-ms-toggle]");
    const panel = wrap.querySelector(".ms-panel");
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      // Close any other open panels first — only one open at a time.
      $$(".ms-panel").forEach(p => { if (p !== panel) p.hidden = true; });
      $$(".ms-btn").forEach(b => { if (b !== toggle) b.setAttribute("aria-expanded", "false"); });
      const willOpen = panel.hidden;
      panel.hidden = !willOpen;
      toggle.setAttribute("aria-expanded", String(willOpen));
    });
    panel.addEventListener("click", (e) => {
      const row = e.target.closest("[data-ms-value]");
      if (!row) return;
      e.stopPropagation();
      const v = row.dataset.msValue;
      const cur = STATE.filters[key];
      const idx = cur.indexOf(v);
      if (idx >= 0) cur.splice(idx, 1);
      else cur.push(v);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
      // If the panel had a project click, also restyle the sidebar so the
      // active dot matches.
      if (key === "project_id") renderProjectList();
    });
  });
  // Click outside to close any open panel.
  document.addEventListener("click", () => {
    $$(".ms-panel").forEach(p => { p.hidden = true; });
    $$(".ms-btn").forEach(b => b.setAttribute("aria-expanded", "false"));
  });
  refreshMultiSelects();
  // Apply tab-aware visibility on first paint — without this, Env / Type
  // filters stay visible until the user clicks a tab.
  refreshFilterBarVisibility();
}

function refreshMultiSelects() {
  $$(".ms-wrap").forEach(wrap => {
    const key = wrap.dataset.filter;
    const opts = _msOptions(key);
    const selected = new Set(STATE.filters[key] || []);
    const panel = wrap.querySelector(".ms-panel");
    const labelEl = wrap.querySelector(".ms-btn-label");
    const btn = wrap.querySelector(".ms-btn");

    // Render rows. Building HTML once via join() is faster than appendChild
    // in a loop for the small option sets we deal with.
    panel.innerHTML = opts.length
      ? opts.map(([v, lbl]) => {
          const isOn = selected.has(v);
          return `<div class="ms-row${isOn ? " on" : ""}" data-ms-value="${escapeHtml(v)}" role="option" aria-selected="${isOn}">
            <span class="ms-check">${isOn ? "✓" : ""}</span>
            <span class="ms-text">${escapeHtml(lbl)}</span>
          </div>`;
        }).join("")
      : `<div class="ms-empty">No options</div>`;

    // Update header label and "active" outline.
    if (selected.size === 0) {
      labelEl.textContent = MS_LABELS[key] || "All";
      btn.classList.remove("active");
    } else if (selected.size === 1) {
      const only = [...selected][0];
      const match = opts.find(([v]) => v === only);
      labelEl.textContent = match ? match[1] : only;
      btn.classList.add("active");
    } else {
      labelEl.textContent = `${MS_NOUNS[key] || "Items"} (${selected.size})`;
      btn.classList.add("active");
    }
  });
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function setView(view) {
  STATE.view = view;
  $$(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  $("#viewList").hidden = view !== "list";
  $("#viewEvents").hidden = view !== "events";
  $("#viewAnalytics").hidden = view !== "analytics";
  $("#viewAudit").hidden = view !== "audit";
  $("#viewSessions").hidden = view !== "sessions";
  $("#filterBar").hidden = view !== "list";
  // Search input is the work-item search — only show it on the list view.
  // KPI strip is item-only too; keep it on analytics so the snapshot is
  // visible alongside the charts, but hide it on audit & sessions where
  // it's noise. The "+ New" CTA only makes sense on the work-item list,
  // so we hide it on every other view too.
  const searchWrap = document.querySelector(".search-wrap");
  if (searchWrap) searchWrap.style.display = view === "list" ? "" : "none";
  const kpiStrip = $("#kpiStrip");
  if (kpiStrip) kpiStrip.style.display = (view === "list" || view === "analytics") ? "" : "none";
  // Type tabs are the global type-context switch — they scope both the
  // list KPIs/table AND the analytics charts. Hidden on audit / sessions
  // / events (those views aren't item-typed).
  const typeTabs = $("#typeTabs");
  if (typeTabs) typeTabs.style.display = (view === "list" || view === "analytics") ? "" : "none";
  const newItemWrap = document.querySelector(".new-item-wrap");
  if (newItemWrap) newItemWrap.style.display = view === "list" ? "" : "none";
  $("#pageTitle").textContent = ({
    list: "All Work Items", events: "Events", analytics: "Analytics",
    audit: "Audit Trail", sessions: "Active Sessions",
  }[view] || "Bug Hunter");
  // Re-fetch on entry. Without this, anything created from another view —
  // a task added inside an event, a stat changed by Sleuth, etc. — would
  // require a manual page reload to show up. The fetches are cheap and the
  // user expects the data to be current the moment a view opens.
  if (view === "list") {
    // refreshAll = bugs + stats (used by the KPI strip + type pills).
    refreshAll();
  }
  if (view === "analytics") {
    // refreshStats already updates STATE.stats; renderCharts reads it. We
    // refresh first so a stale dataset doesn't get drawn for a frame.
    refreshStats().then(renderCharts);
  }
  if (view === "audit") refreshAudit();
  if (view === "sessions") refreshSessions();
  if (view === "events") {
    // Default to list mode whenever the nav button is clicked.
    STATE.currentEventId = null;
    STATE.currentEvent = null;
    showEventsListMode();
    refreshEvents();
  }
}

// KPI → status filter mapping. "total" clears the filter, the others
// drop a specific set of statuses into the multi-select.
const KPI_FILTER_MAP = {
  total:         [],
  open:          ["New", "In Progress", "Reopened"],
  resolved:      ["Resolved"],
  closed:        ["Closed"],
  resolve_later: ["Resolve Later"],
};

function _arraysEqualAsSets(a, b) {
  if (a.length !== b.length) return false;
  const sa = new Set(a);
  for (const x of b) if (!sa.has(x)) return false;
  return true;
}

function refreshKpiActiveState() {
  const cur = STATE.filters.status || [];
  $$("#kpiStrip .kpi").forEach(btn => {
    const key = btn.dataset.kpi;
    const target = KPI_FILTER_MAP[key];
    if (!target) return;
    // "Total" is active when no status filter is set.
    const active = key === "total"
      ? cur.length === 0
      : _arraysEqualAsSets(cur, target);
    btn.classList.toggle("active", active);
  });
}

function handleKpiClick(key) {
  const target = KPI_FILTER_MAP[key];
  if (!target) return;
  const cur = STATE.filters.status || [];
  // Toggle: clicking the active filter clears it back to "all bugs".
  if (_arraysEqualAsSets(cur, target) && target.length > 0) {
    STATE.filters.status = [];
  } else {
    STATE.filters.status = [...target];
  }
  STATE.page = 1;
  // Make sure we're showing the list so the user can see the result.
  if (STATE.view !== "list") setView("list");
  refreshMultiSelects();
  refreshKpiActiveState();
  refreshBugs();
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
const _TAB_NOUNS = {
  all: "Items",
  Bug: "Bugs",
  Requirement: "Requirements",
  Task: "Tasks",
};

function renderCharts() {
  if (!STATE.stats) return;
  const s = STATE.stats;
  const tab = STATE.activeTab || "all";
  const noun = _TAB_NOUNS[tab] || "Items";
  // Update chart titles per tab so "By Status" reads as "By Status (Bugs)".
  const setTitle = (id, text) => { const el = $(id); if (el) el.textContent = text; };
  setTitle("#chartTimelineTitle",    `${noun} over the last 14 days`);
  setTitle("#chartStatusTitle",      `By Status (${noun})`);
  setTitle("#chartPriorityTitle",    `By Priority (${noun})`);
  setTitle("#chartEnvironmentTitle", `By Environment (${noun})`);
  setTitle("#chartProjectTitle",     `By Project (${noun})`);
  setTitle("#chartAssigneeTitle",    `Top Assignees (${noun})`);
  // Environment doesn't apply to Requirements / Tasks — hide the card.
  const envCard = $("#chartEnvironmentCard");
  if (envCard) envCard.hidden = (tab === "Requirement" || tab === "Task");
  drawTimeline("#chartTimeline", s.timeline);
  drawBars("#chartStatus", s.by_status, "status");
  drawBars("#chartPriority", s.by_priority, "priority");
  drawBars("#chartEnvironment", s.by_environment, "env");
  drawProjectBars("#chartProject", s.by_project);
  drawAssigneeBars("#chartAssignee", s.by_assignee);
}

function drawTimeline(sel, data) {
  const host = $(sel); host.innerHTML = "";
  if (!data || !data.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const W = 600, H = 200, P = 30;
  const max = Math.max(1, ...data.map(d => d.count));
  const stepX = (W - 2 * P) / Math.max(1, data.length - 1);
  const points = data.map((d, i) => {
    const x = P + i * stepX;
    const y = H - P - (d.count / max) * (H - 2 * P);
    return [x, y];
  });
  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${p[0]} ${p[1]}`).join(" ");
  const area = `M ${P} ${H - P} ` + points.map(p => `L ${p[0]} ${p[1]}`).join(" ") + ` L ${W - P} ${H - P} Z`;
  const labels = data.map((d, i) => i % 3 === 0
    ? `<text x="${P + i * stepX}" y="${H - 8}" text-anchor="middle" fill="currentColor" font-size="10" opacity="0.6">${d.date.slice(5)}</text>`
    : "").join("");
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="color:var(--accent)">
    <path d="${area}" fill="currentColor" opacity="0.18"/>
    <path d="${path}" stroke="currentColor" stroke-width="2" fill="none"/>
    ${points.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="currentColor"><title>${data[i].date}: ${data[i].count}</title></circle>`).join("")}
    ${labels}
  </svg>`;
}

function drawBars(sel, obj, kind) {
  const host = $(sel); host.innerHTML = "";
  const entries = Object.entries(obj || {});
  if (!entries.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const W = 600, H = 200, P = 30;
  const max = Math.max(1, ...entries.map(e => e[1]));
  const bw = (W - 2 * P) / entries.length - 8;
  const bars = entries.map(([k, v], i) => {
    const x = P + i * ((W - 2 * P) / entries.length);
    const h = (v / max) * (H - 2 * P);
    const y = H - P - h;
    const colorVar = kindColor(kind, k);
    return `
      <rect x="${x}" y="${y}" width="${bw}" height="${h}" fill="${colorVar}" rx="3">
        <title>${escapeHtml(k)}: ${v}</title>
      </rect>
      <text x="${x + bw / 2}" y="${H - 12}" text-anchor="middle" fill="currentColor" font-size="10" opacity="0.7">${escapeHtml(k)}</text>
      <text x="${x + bw / 2}" y="${y - 4}" text-anchor="middle" fill="currentColor" font-size="11" font-weight="600">${v}</text>`;
  }).join("");
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${bars}</svg>`;
}

function kindColor(kind, key) {
  const map = {
    status:   {
      "New": "#5a9fd4", "In Progress": "#d4a05a", "Resolved": "#7ca860",
      "Closed": "#8b8270", "Reopened": "#a87fb8",
      "Not a Bug": "#64748b", "Resolve Later": "#f59e0b",
    },
    priority: { Low: "#8b8270", Medium: "#5a9fd4", High: "#d4a05a", Critical: "#c5524a" },
    env:      { DEV: "#5a9fd4", UAT: "#d4a05a", PROD: "#c5524a" },
  };
  return (map[kind] && map[kind][key]) || "#8b8270";
}

function drawProjectBars(sel, rows) {
  const host = $(sel); host.innerHTML = "";
  if (!rows || !rows.length) { host.innerHTML = '<p class="muted">No data</p>'; return; }
  const max = Math.max(1, ...rows.map(r => r.count));
  host.innerHTML = rows.map(r => `
    <div class="bar-row">
      <div class="bar-label">
        <span><span class="swatch dot" style="background:${escapeHtml(r.color)}"></span>${escapeHtml(r.name)}</span>
        <span>${r.count}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.count/max)*100}%;background:${escapeHtml(r.color)}"></div></div>
    </div>`).join("");
}

function drawAssigneeBars(sel, rows) {
  const host = $(sel); host.innerHTML = "";
  if (!rows || !rows.length) { host.innerHTML = '<p class="muted">No assignments yet</p>'; return; }
  const max = Math.max(1, ...rows.map(r => r.count));
  host.innerHTML = rows.map(r => `
    <div class="bar-row">
      <div class="bar-label">
        <span><span class="avatar mini">${initials(r.name)}</span>${escapeHtml(r.name)}</span>
        <span>${r.count}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:${(r.count/max)*100}%;background:var(--accent)"></div></div>
    </div>`).join("");
}

// ---------------------------------------------------------------------------
// Bug modal — unified create / edit / view (Jira-style single screen).
//
// One modal handles three modes:
//   • Create       — bug == null, no comments / attachments / activity
//                    sections rendered (we don't have a bug id yet).
//   • Edit / View  — bug != null, all fields editable inline; comments,
//                    attachments and activity rendered below.
//
// On submit we PUT/POST the form, then if it was an edit we re-fetch the
// bug detail and re-render the inline sections in place — without
// closing the modal — so the user sees the updated bug straight away.
// ---------------------------------------------------------------------------
function openBugForm(bug = null) {
  const form = $("#formBug");
  // Edit mode requires a real bug id — a bare `{ _defaultType: "Task" }`
  // hint object from the "+ New" menu is NOT an edit.
  const isEdit = !!(bug && bug.id);
  STATE.currentBugId = isEdit ? bug.id : null;
  form.reset();

  // Header: short numeric label + the saved title as a faded subtitle so
  // the user can see what they originally filed without it getting
  // muddled with the editable input below.
  if (isEdit) {
    const t = bug.item_type || "Bug";
    $("#modalBugTitle").textContent = `${itemTypeEmoji(t)} ${t} #${bug.id}`;
    $("#modalBugSubtitle").textContent = bug.title || "";
    $("#bugSubmitBtn").textContent = "Save changes";
  } else {
    const t = bug?._defaultType || STATE.defaultNewType || "Bug";
    $("#modalBugTitle").textContent = `${itemTypeEmoji(t)} New ${t}`;
    $("#modalBugSubtitle").textContent = "";
    $("#bugSubmitBtn").textContent = "Create";
  }
  form.elements.id.value = isEdit ? bug.id : "";

  // Delete button — admin only, edit mode only. The HTML already has
  // data-needs-role="admin" on it; applyRoleVisibility() at boot stripped
  // that attribute for admins, so we just need to flip its hidden state
  // for create/edit modes.
  const delBtn = $("#bugDeleteBtn");
  if (delBtn) {
    const isAdmin = STATE.currentUser?.role === "admin";
    delBtn.hidden = !(isEdit && isAdmin);
  }

  fillFormSelect(form.elements.project_id, STATE.projects.map(p => [p.id, p.name]),
                 isEdit ? bug.project_id : "");
  // Reporter is fixed to whoever is currently logged in. We populate the
  // (disabled) select with just one option — the current user — so it
  // always shows their name. The actual reporter_id sent on submit comes
  // from STATE.currentUser.id, not from this select, so even if a
  // browser oddly omits disabled-select values we still send something
  // valid. For an existing bug, we additionally inject the original
  // reporter as a second option so the bug's true reporter still
  // displays correctly when someone else opens it.
  const me = STATE.currentUser;
  let reporterOptions = me ? [[me.id, me.name, me.email]] : [];
  if (isEdit && bug.reporter && (!me || bug.reporter.id !== me.id)) {
    reporterOptions = [[bug.reporter.id, bug.reporter.name, bug.reporter.email]];
  }
  fillFormSelect(form.elements.reporter_id, reporterOptions,
                 isEdit && bug.reporter ? bug.reporter.id : (me ? me.id : ""));
  // Status options come from the per-type set so e.g. "Not a Bug" is
  // never offered for a Task or Requirement. The chosen-type at this
  // point is from bug.item_type (edit) or the default-new-type (create)
  // computed below; we re-populate the dropdown whenever the type
  // changes (see the change-listener wired in bindGlobalListeners).
  // If the row was carrying a status that's no longer valid for its
  // type (e.g. a Task that historically held "Resolved"), we keep the
  // current value as a one-off option so the user can SEE it and SWITCH
  // it without the form blowing up.
  const initialStatusType = isEdit ? (bug.item_type || "Bug") : (bug?._defaultType || STATE.defaultNewType || "Bug");
  _renderStatusSelect(form.elements.status, initialStatusType, isEdit ? bug.status : "New");
  fillFormSelect(form.elements.priority, STATE.meta.priorities.map(s => [s, s]),
                 isEdit ? bug.priority : "Medium");
  // Environment - already DEV/UAT/PROD options in the HTML, just set value
  form.elements.environment.value = isEdit ? bug.environment : "DEV";
  // Item-type selector. In edit mode we use whatever the row already had
  // (default "Bug" if the column is older than this feature). In create
  // mode we use bug._defaultType if openBugForm was called from the
  // "+ New <Type>" menu, otherwise the user's last-chosen default.
  const presetType = isEdit
    ? (bug.item_type || "Bug")
    : (bug?._defaultType || STATE.defaultNewType || "Bug");
  fillFormSelect(form.elements.item_type,
                 (STATE.meta.item_types || ["Bug","Requirement","Task"]).map(t => [t, t]),
                 presetType);

  // Event selector. Empty-value option means "no event"; on create the
  // selection defaults to whatever _defaultEventId was passed in (used by
  // the "+ Add Task" button inside an event-detail view). On edit it
  // picks up the bug's current event_id.
  if (form.elements.event_id) {
    const presetEventId = isEdit
      ? (bug.event_id || "")
      : (bug?._defaultEventId || "");
    // Lightweight pre-render with a placeholder option, then fill async.
    form.elements.event_id.innerHTML =
      `<option value="">— No event —</option>`;
    if (isEdit && bug.event_id && bug.event_name) {
      // Seed the option immediately so the user sees the current event
      // even if the events list hasn't loaded yet.
      const opt = document.createElement("option");
      opt.value = String(bug.event_id);
      opt.textContent = bug.event_name;
      opt.selected = true;
      form.elements.event_id.appendChild(opt);
    }
    // Fetch a fresh list (cheap GET) so the dropdown is current.
    api("/events").then((events) => {
      if (!form.elements.event_id) return;
      const sel = form.elements.event_id;
      const current = String(sel.value || presetEventId || "");
      sel.innerHTML = `<option value="">— No event —</option>` +
        (events || []).map(ev => {
          const label = ev.scheduled_for
            ? `${ev.name} · ${ev.scheduled_for}`
            : ev.name;
          return `<option value="${ev.id}">${escapeHtml(label)}</option>`;
        }).join("");
      if (current) sel.value = current;
    }).catch(() => { /* leave the placeholder if /events fails */ });
  }

  const assignedIds = new Set(isEdit && bug.assignees ? bug.assignees.map(a => a.id) : []);
  renderChips("#assigneePicker",
    STATE.users.filter(u => u.is_active),
    (u) => ({ id: u.id, label: u.name, sub: u.role }),
    assignedIds);

  if (isEdit) {
    form.elements.title.value = bug.title || "";
    form.elements.description.value = bug.description || "";
    form.elements.due_date.value = bug.due_date || "";
    // Read-only timestamps in the side rail.
    $("#bugSideMeta").hidden = false;
    $("#bugMetaCreated").textContent = formatDate(bug.created_at);
    $("#bugMetaUpdated").textContent = formatDate(bug.updated_at);
    // Create-mode upload control is hidden when editing — attachments
    // go through comments here, or via the legacy bug-level section.
    const createAttach = $("#bugCreateAttachSection");
    if (createAttach) {
      createAttach.hidden = true;
      clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
      const cf = $("#createBugFiles"); if (cf) cf.value = "";
    }
    // Reset the COMMENT staged bucket too — opening a different bug
    // shouldn't carry over half-typed attachments from a previous one.
    clearStagedFiles("comment", "#filePreview", "#fileLabel");
    // Render the inline detail sections (comments, attachments, activity).
    renderBugInlineSections(bug);
  } else {
    // Create mode — hide all detail sections (comments need a saved bug
    // id to attach to). Reset side meta panel.
    $("#bugSideMeta").hidden = true;
    $("#bugCommentsSection").hidden = true;
    $("#bugAttachmentsSection").hidden = true;
    $("#bugActivitySection").hidden = true;
    // Show the create-mode attachment uploader and reset its state so
    // files from a previous create session don't linger.
    const createAttach = $("#bugCreateAttachSection");
    if (createAttach) {
      createAttach.hidden = false;
      clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
      const cf = $("#createBugFiles"); if (cf) cf.value = "";
    }
  }

  // Apply per-role read-only mode: regular users can edit Bugs but not
  // Tasks / Requirements (the backend enforces this too with a 403; the
  // frontend disable mirrors the rule so the form makes the constraint
  // obvious instead of letting the user type and then erroring out).
  applyBugFormReadOnly(form, bug, isEdit);

  openModal("modalBug");
  if (!form.dataset.readOnly) {
    setTimeout(() => form.elements.title.focus(), 50);
  }
}

// Mirror the backend can_edit_bug rule on the frontend. Regular users
// can edit Bugs (legacy behavior); Tasks and Requirements require an
// admin or manager role.
function canEditItem(item) {
  const role = STATE.currentUser?.role || "";
  if (role === "admin" || role === "manager") return true;
  const t = (item && item.item_type) || "Bug";
  return t === "Bug";
}

function applyBugFormReadOnly(form, bug, isEdit) {
  // Create mode is never read-only — anyone can file an item. Only an
  // existing record can be locked down.
  const readOnly = isEdit && !canEditItem(bug);
  form.dataset.readOnly = readOnly ? "1" : "";
  const fields = form.querySelectorAll("input, select, textarea");
  fields.forEach(el => {
    if (el.name === "id") return;
    // The reporter select is already disabled by design; leave it.
    if (el.classList.contains("reporter-select")) return;
    el.disabled = readOnly ? true : (el.dataset.persistDisabled === "1" ? true : el.disabled);
    if (!readOnly) {
      // If we previously set readonly, only clear what WE set — never
      // un-disable the always-disabled reporter.
      if (el.dataset.roSetByUs === "1") {
        el.disabled = false;
        el.dataset.roSetByUs = "";
      }
    } else {
      el.dataset.roSetByUs = "1";
    }
  });
  // Chip pickers (assignees, event managers) — block clicks via CSS class.
  const picker = $("#assigneePicker");
  if (picker) picker.classList.toggle("locked", readOnly);
  // Submit / Delete buttons + comment composer + create-mode attach uploader.
  const submit = $("#bugSubmitBtn");
  if (submit) submit.hidden = readOnly;
  const delBtn = $("#bugDeleteBtn");
  if (delBtn && readOnly) delBtn.hidden = true;
  const composer = $("#commentForm");
  if (composer) composer.hidden = readOnly;
  // Add / remove a read-only banner so the user understands what they're
  // seeing instead of wondering why nothing accepts input.
  let banner = form.querySelector(".bug-readonly-banner");
  if (readOnly && !banner) {
    banner = document.createElement("div");
    banner.className = "bug-readonly-banner";
    const itype = (bug?.item_type || "item").toLowerCase();
    banner.textContent = `Read-only — only admins and managers can edit ${itype}s.`;
    form.insertBefore(banner, form.firstChild);
  } else if (!readOnly && banner) {
    banner.remove();
  }
}

// Populate the status <select> with the options valid for `itype`.
// Preserves the current selection if it still fits; otherwise keeps it
// as a "legacy" option marked with a † suffix so the user can see and
// change it without seeing an "empty" dropdown.
function _renderStatusSelect(selEl, itype, current) {
  const opts = statusesForType(itype);
  const includesCurrent = current && opts.includes(current);
  const items = opts.map(s => [s, s]);
  if (current && !includesCurrent) {
    // Carry-forward case: a row stored a status no longer valid for its
    // type. Pin it as an explicit option so the dropdown isn't blank.
    items.push([current, `${current} (legacy)`]);
  }
  fillFormSelect(selEl, items, current || (opts[0] || "New"));
}

// Inline render of comments + attachments + activity inside the bug
// modal. Replaces the old separate "detail modal with tabs" — everything
// lives in one screen now.
function renderBugInlineSections(bug) {
  const isAdmin = STATE.currentUser?.role === "admin";

  // ----- Comments -----
  // When the bug has no comments, we render NOTHING in the list — the
  // composer below is enough of a prompt on its own, and any larger
  // empty-state placeholder just pushes the composer down without
  // adding signal. The section heading + (0) count still tells the user
  // there are no comments, so we don't lose information.
  $("#bugCommentsSection").hidden = false;
  $("#commentsCount").textContent = `(${bug.comments.length})`;
  const commentsList = $("#bugCommentsList");
  if (bug.comments.length) {
    commentsList.innerHTML = bug.comments.map(c => {
      // v2.5: comment attachments are also admin-only deletable. The
      // role gate is the same as for bug-level attachments.
      const atts = (c.attachments || []).map(a => renderAttachmentCard(a, isAdmin)).join("");
      // Comment body may be empty if the user chose to share files only
      // — render the body block only when there's text to show.
      const bodyHtml = (c.body || "").trim()
        ? `<div class="comment-body" data-comment-body="${c.id}">${escapeHtml(c.body)}</div>`
        : "";
      // Admin-only ✎ / 🗑 actions on the right of the head row. Hidden
      // entirely for everyone else so the head stays clean.
      const adminActions = isAdmin ? `
        <span class="comment-admin-actions">
          ${(c.body || "").trim() ? `<button type="button" class="icon-btn" data-act="edit-comment" data-id="${c.id}" title="Edit comment">✎</button>` : ""}
          <button type="button" class="icon-btn danger" data-act="delete-comment" data-id="${c.id}" title="Delete comment">🗑</button>
        </span>` : "";
      return `
        <div class="comment" data-comment-id="${c.id}">
          <div class="comment-head">
            <div class="comment-head-left">
              <span class="avatar">${initials(c.author_name)}</span>
              <span class="comment-author">${escapeHtml(c.author_name)}</span>
            </div>
            <span class="comment-head-right">
              <span class="comment-time">${formatDate(c.created_at)}</span>
              ${adminActions}
            </span>
          </div>
          ${bodyHtml}
          ${atts ? `<div class="comment-attachments"><div class="attachment-grid">${atts}</div></div>` : ""}
        </div>`;
    }).join("");
    commentsList.hidden = false;
  } else {
    commentsList.innerHTML = "";
    commentsList.hidden = true;
  }
  // The comment form lives in the static HTML (now a <div>, not a
  // <form> — see the long note in index.html for why). Clear any
  // leftover input from a previous bug.
  const bodyEl = $("#commentBody");
  const filesEl = $("#commentFiles");
  if (bodyEl) bodyEl.value = "";
  if (filesEl) filesEl.value = "";
  $("#filePreview").innerHTML = "";
  $("#fileLabel").textContent = "Attach files";

  // ----- Attachments (bug-level) -----
  // v2.5: section is ALWAYS visible in edit mode so users can see
  // existing files AND upload new ones via the section-head uploader.
  // Deletion stays admin-only; uploads are open to anyone who can
  // edit this item type (managers/admins for Task/Requirement, any
  // user for Bug).
  const itypeForAtt = (bug && bug.item_type) || "Bug";
  $("#bugAttachmentsSection").hidden = false;
  $("#attachmentsCount").textContent = bug.attachments.length
    ? `(${bug.attachments.length})`
    : "";
  if (bug.attachments.length) {
    $("#bugAttachmentsGrid").innerHTML =
      bug.attachments.map(a => renderAttachmentCard(a, isAdmin)).join("");
    $("#bugAttachEmpty").hidden = true;
  } else {
    $("#bugAttachmentsGrid").innerHTML = "";
    // The empty placeholder is only useful when the user CAN add (it
    // prompts them to use the uploader above). Otherwise it would be
    // misleading.
    $("#bugAttachEmpty").hidden = !canEditItem(bug);
  }
  // Show / hide the post-creation uploader based on edit perms.
  const addLabel = $("#bugAttachAddLabel");
  if (addLabel) addLabel.hidden = !canEditItem(bug);
  // Reset any half-staged files from a previous open so attaching to
  // bug A then opening bug B doesn't carry the leftovers.
  clearStagedFiles("bugAttach", "#bugAttachAddPreview", "#bugAttachAddText");
  const ai = $("#bugAttachAddInput");
  if (ai) ai.value = "";

  // ----- Activity (collapsible <details>) -----
  $("#bugActivitySection").hidden = false;
  $("#activityCount").textContent = `(${bug.activities.length})`;
  $("#bugActivityList").innerHTML = bug.activities.length
    ? bug.activities.map(a => renderActivityRow(a)).join("")
    : '<p class="no-content">No activity yet</p>';
}

function fillFormSelect(selEl, items, current = "") {
  // Items can be [value, label] or [value, label, title]. The optional
  // 3rd element becomes the option's `title` attr (hover tooltip) so we
  // can keep the visible label short without losing extra context.
  selEl.innerHTML = `<option value="">— select —</option>` +
    items.map((row) => {
      const [v, lbl, ttl] = row;
      const titleAttr = ttl ? ` title="${escapeHtml(ttl)}"` : "";
      return `<option value="${v}"${titleAttr}>${escapeHtml(lbl)}</option>`;
    }).join("");
  if (current !== "" && current != null) selEl.value = current;
}

function renderChips(sel, items, mapFn, selectedIds) {
  const host = $(sel);
  host.innerHTML = "";
  if (!items.length) {
    host.innerHTML = '<span class="chip-empty">— none available —</span>';
    return;
  }
  for (const item of items) {
    const m = mapFn(item);
    const chip = document.createElement("span");
    chip.className = "chip" + (selectedIds.has(m.id) ? " selected" : "");
    chip.dataset.id = String(m.id);
    chip.innerHTML = escapeHtml(m.label) +
      (m.sub ? ` <span class="chip-sub">· ${escapeHtml(m.sub)}</span>` : "");
    chip.addEventListener("click", () => chip.classList.toggle("selected"));
    host.appendChild(chip);
  }
}

function readChips(sel) {
  return $$(`${sel} .chip.selected`).map(c => parseInt(c.dataset.id, 10));
}

async function submitBugForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  // Reporter is always the logged-in user — the field in the modal is
  // disabled and we read the id from STATE here so the request is
  // independent of the form element's state.
  // For EDIT, we preserve whoever the original reporter was: the disabled
  // select still carries `bug.reporter.id` (set by openBugForm), so
  // form.elements.reporter_id.value is the right value.
  const reporterFromForm = form.elements.reporter_id.value
    ? parseInt(form.elements.reporter_id.value, 10) : null;
  const reporterFromMe = STATE.currentUser?.id || null;
  // For NEW bugs use the current user; for EDIT use whatever the form
  // already has (which is the bug's existing reporter).
  const rawEvent = form.elements.event_id ? form.elements.event_id.value : "";
  const payload = {
    project_id: parseInt(form.elements.project_id.value, 10),
    title: form.elements.title.value.trim(),
    description: form.elements.description.value,
    reporter_id: id ? (reporterFromForm || reporterFromMe) : reporterFromMe,
    item_type: form.elements.item_type ? form.elements.item_type.value || "Bug" : "Bug",
    status: form.elements.status.value,
    priority: form.elements.priority.value,
    environment: form.elements.environment.value,
    due_date: form.elements.due_date.value || null,
    // event_id: "" or "0" means "no event" — send null so the server
    // treats it as an explicit unlink in the EDIT path.
    event_id: rawEvent && rawEvent !== "0" ? parseInt(rawEvent, 10) : null,
    assignee_ids: readChips("#assigneePicker"),
  };
  // Remember the chosen type on create so the next "+ New" defaults to it.
  if (!id) {
    STATE.defaultNewType = payload.item_type;
    try { localStorage.setItem("defaultNewType", payload.item_type); } catch {}
  }
  if (!payload.project_id) { toast("Please pick a project", "error"); return; }
  if (!payload.title) { toast("Title is required", "error"); return; }
  if (!payload.reporter_id) { toast("Reporter is required", "error"); return; }

  try {
    if (id) {
      // EDIT — save, then close the modal and return to the list.
      // (Earlier v3.1 builds kept the modal open Jira-style; reverted
      // here because users prefer the explicit close-and-return flow.)
      const result = await withLoader(async () => {
        const updated = await api(`/bugs/${id}`, { method: "PUT", body: JSON.stringify(payload) });
        closeModal("modalBug");
        if (STATE.view === "events" && STATE.currentEventId) {
          await openEventDetail(STATE.currentEventId);
        } else {
          setView("list");
          await refreshAll();
        }
        return updated;
      }, "Saving changes…");
      const utype = result?.item_type || payload.item_type || "Bug";
      toast(`${utype} #${id} updated`, "success");
    } else {
      // CREATE — POST the item, then upload any files selected in the
      // create-mode attachment picker before closing the modal. We do the
      // upload here (not on the server side of POST /bugs) so the
      // create payload stays JSON and we can keep the existing
      // /bugs/{id}/attachments endpoint as the single attachment-upload
      // path. Failures on individual files are toasted but don't abort
      // the create flow — the item itself is already saved.
      await withLoader(async () => {
        const created = await api("/bugs", { method: "POST", body: JSON.stringify(payload) });
        const ctype = created?.item_type || payload.item_type || "Bug";
        // Files come from the staged array (the user may have pulled some
        // out via the X button before submitting).
        const files = STATE.stagedFiles.createBug || [];
        if (files.length && created && created.id) {
          let done = 0;
          let failed = 0;
          for (const f of files) {
            const fd = new FormData();
            fd.append("file", f);
            try {
              await api(`/bugs/${created.id}/attachments`, { method: "POST", body: fd });
              done++;
            } catch (err) {
              failed++;
              // Show per-file errors so the user knows which one didn't
              // make it (e.g. a 50 MB cap hit on one big file).
              toast(`Attachment ${f.name}: ${err.message}`, "error");
            }
          }
          if (done) toast(`${ctype} #${created.id} created · ${done} file(s) attached`, "success");
          else if (failed) toast(`${ctype} #${created.id} created (no attachments saved)`, "info");
          else toast(`${ctype} created`, "success");
        } else {
          toast(`${ctype} created`, "success");
        }
        // Clear the staged array + free blob URLs so reopening starts clean.
        clearStagedFiles("createBug", "#createFilePreview", "#createFileLabel");
        const fEl = $("#createBugFiles"); if (fEl) fEl.value = "";

        closeModal("modalBug");
        if (STATE.view === "events" && STATE.currentEventId) {
          await openEventDetail(STATE.currentEventId);
        } else {
          setView("list");
          await refreshAll();
        }
      }, "Creating item…");
    }
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Bug detail (kept as a thin alias for callers that previously opened
// the now-removed separate detail modal — fetches the bug and routes
// straight into the unified modal in edit/view mode).
// ---------------------------------------------------------------------------
async function openBugDetail(bugId) {
  STATE.currentBugId = bugId;
  STATE.detailTab = "info";  // legacy field; not read anywhere now
  try {
    const bug = await api(`/bugs/${bugId}`);
    openBugForm(bug);
  } catch (err) {
    toastError(err);
  }
}

// (renderBugDetail removed — its responsibilities are now split between
//  openBugForm — which fills the editable form — and
//  renderBugInlineSections — which renders the read-only sections.)

function renderAttachmentCard(a, deletable) {
  const url = `/api/bugs/${STATE.currentBugId}/attachments/${a.id}/download`;
  const ct = (a.content_type || "").toLowerCase();
  let preview = "";
  // Inline rendering is safe for raster images and video. SVG is a vector
  // image but can carry inline JS (server already downgrades it on
  // download), so we treat it like any other downloadable file rather
  // than embedding it as <img>.
  const isRasterImg = ct.startsWith("image/") && ct !== "image/svg+xml";
  if (isRasterImg) {
    preview = `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${escapeHtml(a.filename)}" loading="lazy"/></a>`;
  } else if (ct.startsWith("video/")) {
    preview = `<video controls preload="metadata"><source src="${url}" type="${escapeHtml(a.content_type)}"/></video>`;
  } else {
    preview = `<a href="${url}" target="_blank" rel="noopener" class="file-icon">${fileIcon(a.content_type, a.filename)}</a>`;
  }
  return `
    <div class="attach-card" data-att-id="${a.id}">
      <div class="attach-preview">${preview}</div>
      <div class="attach-meta">
        <div class="attach-name" title="${escapeHtml(a.filename)}">${escapeHtml(a.filename)}</div>
        <div class="attach-info">
          <span>${formatBytes(a.size_bytes)}</span>
          <span>${escapeHtml(a.uploader_name)}</span>
        </div>
      </div>
      <div class="attach-actions">
        <a href="${url}" target="_blank" rel="noopener">View</a>
        <a href="${url}" download="${escapeHtml(a.filename)}">Download</a>
        ${deletable ? `<button class="danger" data-act="delete-attachment" data-id="${a.id}">Delete</button>` : ""}
      </div>
    </div>`;
}

function renderActivityRow(a) {
  return `
    <div class="activity-row">
      <span class="activity-icon">${activityIcon(a.action)}</span>
      <div class="activity-text">
        <div><span class="activity-actor">${escapeHtml(a.actor_name)}</span><span class="activity-action">${escapeHtml(a.action)}</span></div>
        ${a.detail ? `<div class="activity-detail">${escapeHtml(a.detail)}</div>` : ""}
      </div>
      <span class="activity-time">${formatDate(a.created_at)}</span>
    </div>`;
}

function activityIcon(action) {
  if (action.includes("session")) return "🔐";
  if (action.includes("login")) return "🔑";
  if (action.includes("logout")) return "👋";
  if (action.includes("password")) return "🔒";
  if (action.includes("created")) return "✨";
  if (action.includes("delete")) return "🗑";
  if (action.includes("comment")) return "💬";
  if (action.includes("attachment")) return "📎";
  if (action.includes("status")) return "🔄";
  if (action.includes("assign")) return "👥";
  return "📝";
}

// ---------------------------------------------------------------------------
// Attachment staging
//
// `input.files` is a FileList — read-only and not removable. To let the
// user pull a file out of a pending upload (the "X on hover" UX) we copy
// the selection into a plain Array stored on a sentinel STATE key, then
// drive both the preview render and the eventual upload off that array.
// One array per logical attachment slot:
//   STATE.stagedFiles.createBug    — "+ New X" modal's bug-level attach
//   STATE.stagedFiles.comment      — comment composer's attach
// ---------------------------------------------------------------------------
STATE.stagedFiles = { createBug: [], comment: [], bugAttach: [] };

function _stagedBucketForInput(inputId) {
  if (inputId === "createBugFiles")  return "createBug";
  if (inputId === "commentFiles")    return "comment";
  if (inputId === "bugAttachAddInput") return "bugAttach";
  return null;
}

function _renderStagedFiles(bucket, previewSel, labelSel) {
  const preview = $(previewSel);
  const label = $(labelSel);
  const files = STATE.stagedFiles[bucket] || [];
  preview.innerHTML = "";
  // Notify listeners after the render so the "Upload N" button on the
  // post-creation attach uploader can be added/removed.
  const notify = () => document.dispatchEvent(new CustomEvent("bh:staged-changed", { detail: { bucket } }));
  if (!files.length) {
    if (label) {
      const isAddBtn = labelSel === "#bugAttachAddText";
      label.textContent = isAddBtn ? "Add attachment" : "Attach files";
    }
    notify();
    return;
  }
  if (label) label.textContent = `${files.length} file${files.length > 1 ? "s" : ""}`;
  files.forEach((f, idx) => {
    const isImage = (f.type || "").startsWith("image/");
    const url = URL.createObjectURL(f);
    const wrap = document.createElement("span");
    wrap.className = "attach-staged";
    wrap.dataset.bucket = bucket;
    wrap.dataset.idx = String(idx);
    wrap.dataset.objUrl = url;
    // Image gets a thumbnail; other types get the generic icon. Either
    // way the inner element is clickable to preview, and the ✕ removes
    // the file from the staged array.
    const previewInner = isImage
      ? `<a class="attach-staged-link" href="${url}" target="_blank" rel="noopener"><img class="attach-staged-thumb" src="${url}" alt="${escapeHtml(f.name)}" /></a>`
      : `<a class="attach-staged-link" href="${url}" target="_blank" rel="noopener" title="Open ${escapeHtml(f.name)}">${fileIcon(f.type, f.name)}</a>`;
    wrap.innerHTML = `
      ${previewInner}
      <span class="attach-staged-meta">
        <span class="attach-staged-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span class="attach-staged-size muted small">${formatBytes(f.size)}</span>
      </span>
      <button type="button" class="attach-staged-remove" aria-label="Remove ${escapeHtml(f.name)}" title="Remove (not yet uploaded)">✕</button>`;
    preview.appendChild(wrap);
  });
  // Fire the "stage changed" event so a section-head Upload button can
  // appear (post-creation attach uploader).
  notify();
}

function clearStagedFiles(bucket, previewSel, labelSel) {
  // Free the blob URLs we created — otherwise they stay alive for the
  // life of the page. Then drop the array reference itself.
  const preview = $(previewSel);
  if (preview) {
    preview.querySelectorAll(".attach-staged[data-obj-url]").forEach(el => {
      try { URL.revokeObjectURL(el.dataset.objUrl); } catch {}
    });
  }
  STATE.stagedFiles[bucket] = [];
  if (preview) preview.innerHTML = "";
  if (labelSel) {
    const label = $(labelSel);
    if (label) {
      // The bug-attach button's idle label differs from the others.
      label.textContent = labelSel === "#bugAttachAddText" ? "Add attachment" : "Attach files";
    }
  }
  document.dispatchEvent(new CustomEvent("bh:staged-changed", { detail: { bucket } }));
}

function handleStagedInputChange(inputEl, previewSel, labelSel) {
  const bucket = _stagedBucketForInput(inputEl.id);
  if (!bucket) return;
  // ADD to (not replace) the bucket so a user can pick files in two
  // batches. The FileList we just got is consumed by reading once.
  const arr = STATE.stagedFiles[bucket] || [];
  for (const f of inputEl.files || []) arr.push(f);
  STATE.stagedFiles[bucket] = arr;
  // Reset the input so re-selecting the same file fires another change.
  inputEl.value = "";
  _renderStagedFiles(bucket, previewSel, labelSel);
}

function handleStagedListClick(e, previewSel, labelSel) {
  const removeBtn = e.target.closest(".attach-staged-remove");
  if (!removeBtn) return;
  // Don't navigate via the wrapping link.
  e.preventDefault();
  const card = removeBtn.closest(".attach-staged");
  if (!card) return;
  const bucket = card.dataset.bucket;
  const idx = parseInt(card.dataset.idx, 10);
  const objUrl = card.dataset.objUrl;
  if (objUrl) { try { URL.revokeObjectURL(objUrl); } catch {} }
  if (bucket && !Number.isNaN(idx)) {
    STATE.stagedFiles[bucket].splice(idx, 1);
    _renderStagedFiles(bucket, previewSel, labelSel);
  }
}

// Kept as a thin alias so callers that still pass the input element
// (the old API) keep working without churn.
function updateFilePreview(input, previewSel, labelSel) {
  handleStagedInputChange(input, previewSel, labelSel);
}

// v2.5 — flush the bug-level attachment staging bucket (post-creation
// upload). Runs whenever the user picks files in the bug modal's
// section-head 📎 button. Files were added to the bucket by the
// handleStagedInputChange call wired in bindGlobalListeners; this
// function POSTs each one and refreshes the inline sections. Wrapped
// in withLoader at the call site so the user can't double-submit.
async function flushBugAttachStaging() {
  const files = STATE.stagedFiles.bugAttach || [];
  if (!files.length || !STATE.currentBugId) return;
  let done = 0, failed = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      failed++;
      toast(`Attachment ${f.name}: ${err.message}`, "error");
    }
  }
  clearStagedFiles("bugAttach", "#bugAttachAddPreview", "#bugAttachAddText");
  const ai = $("#bugAttachAddInput"); if (ai) ai.value = "";
  if (done) toast(`${done} file${done > 1 ? "s" : ""} attached`, "success");
  // Refresh inline sections + table cell so the new attachment_count
  // shows up immediately without a manual reload.
  const bug = await api(`/bugs/${STATE.currentBugId}`);
  renderBugInlineSections(bug);
  await refreshBugs();
}

async function uploadFiles(files, commentId) {
  if (!files || !files.length) return;
  const total = files.length;
  let done = 0;
  toast(`Uploading ${total} file(s)…`, "info");
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    if (commentId) fd.append("comment_id", String(commentId));
    try {
      await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      toast(`Failed to upload ${f.name}: ${err.message}`, "error");
    }
  }
  if (done) toast(`Uploaded ${done}/${total} file(s)`, "success");
  // Refresh the unified modal's inline sections in place — no detail
  // modal re-open dance.
  const bug = await api(`/bugs/${STATE.currentBugId}`);
  renderBugInlineSections(bug);
  await refreshBugs(); // update attachment_count in list
}

// ---------------------------------------------------------------------------
// Project / User forms
// ---------------------------------------------------------------------------
function openProjectForm(project = null) {
  const form = $("#formProject");
  form.reset();
  $("#modalProjectTitle").textContent = project ? `Edit "${project.name}"` : "New Project";
  form.elements.id.value = project ? project.id : "";
  if (project) {
    form.elements.name.value = project.name;
    form.elements.color.value = project.color;
    form.elements.description.value = project.description;
  } else {
    form.elements.color.value = "#c9764f";
  }
  openModal("modalProject");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitProjectForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    color: form.elements.color.value,
    description: form.elements.description.value,
  };
  try {
    await withLoader(async () => {
      if (id) {
        await api(`/projects/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/projects", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalProject");
      setView("list");
      await loadProjects();
      await refreshAll();
    }, id ? "Saving project…" : "Creating project…");
    toast(id ? "Project updated" : "Project created", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Event form (create / edit) + delete
// ---------------------------------------------------------------------------
function openEventForm(event = null) {
  const form = $("#formEvent");
  form.reset();
  const isEdit = !!(event && event.id);
  $("#modalEventTitle").textContent = isEdit ? `Edit "${event.name}"` : "New Event";
  form.elements.id.value = isEdit ? event.id : "";
  if (isEdit) {
    form.elements.name.value = event.name || "";
    form.elements.description.value = event.description || "";
    form.elements.scheduled_for.value = event.scheduled_for || "";
  } else {
    // Default scheduled date to today so the morning-standup case is one click.
    form.elements.scheduled_for.value = new Date().toISOString().slice(0, 10);
  }
  // Manager picker: filter to admin/manager users only (the backend
  // rejects regular users in that slot, so don't even show them as
  // options to avoid a confusing 400 response).
  const eligibleManagers = (STATE.users || []).filter(
    u => u.is_active && (u.role === "admin" || u.role === "manager")
  );
  const selectedManagerIds = new Set(
    (event && event.managers ? event.managers : []).map(m => m.id)
  );
  renderChips("#eventManagerPicker", eligibleManagers,
    (u) => ({ id: u.id, label: u.name, sub: u.role }),
    selectedManagerIds);

  openModal("modalEvent");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitEventForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    description: form.elements.description.value,
    scheduled_for: form.elements.scheduled_for.value || null,
    manager_ids: readChips("#eventManagerPicker"),
  };
  if (!payload.name) { toast("Event name is required", "error"); return; }
  try {
    const saved = await withLoader(async () => {
      let result;
      if (id) {
        result = await api(`/events/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        result = await api("/events", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalEvent");
      await refreshEvents();
      if (id && STATE.currentEventId === parseInt(id, 10)) {
        await openEventDetail(parseInt(id, 10));
      }
      if (!id && result && result.id) {
        await openEventDetail(result.id);
      }
      return result;
    }, id ? "Saving event…" : "Creating event…");
    toast(id ? "Event updated" : "Event created", "success");
  } catch (err) {
    toastError(err);
  }
}

async function handleDeleteEvent(event) {
  const ok = await confirmDialog(
    `Delete event "${event.name}"? Its tasks will be kept but unlinked from this event. Cannot be undone`,
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/events/${event.id}`, { method: "DELETE" });
      showEventsListMode();
      await refreshEvents();
    }, "Deleting event…");
    toast(`Event "${event.name}" deleted`, "success");
  } catch (err) {
    toastError(err);
  }
}

function openUserForm(user = null) {
  const form = $("#formUser");
  form.reset();
  $("#modalUserTitle").textContent = user ? `Edit ${user.name}` : "New User";
  form.elements.id.value = user ? user.id : "";

  if (user) {
    form.elements.name.value = user.name;
    form.elements.email.value = user.email;
    form.elements.role.value = user.role || "user";
    form.elements.is_active.checked = user.is_active;
    // On edit, password is OPTIONAL — leave blank to keep current
    form.elements.password.required = false;
    form.elements.password.value = "";
    form.elements.password.placeholder = "Leave blank to keep current password";
    $("#userPasswordHint").textContent = "Leave blank to keep current password";
    $("#userPasswordField").querySelector(".js-required")?.classList.add("hidden");
  } else {
    form.elements.role.value = "user";
    form.elements.is_active.checked = true;
    // On create, password is REQUIRED
    form.elements.password.required = true;
    form.elements.password.placeholder = "Min 8 characters";
    $("#userPasswordHint").textContent = "At least 8 characters";
    $("#userPasswordField").querySelector(".js-required")?.classList.remove("hidden");
  }
  openModal("modalUser");
  setTimeout(() => form.elements.name.focus(), 50);
}

async function submitUserForm(e) {
  e.preventDefault();
  const form = e.target;
  const id = form.elements.id.value;
  const payload = {
    name: form.elements.name.value.trim(),
    email: form.elements.email.value.trim(),
    role: form.elements.role.value,
    is_active: form.elements.is_active.checked,
  };
  // Only include password if user typed one (on edit, blank = keep current)
  const pw = form.elements.password.value;
  if (pw) {
    if (pw.length < 8) {
      toast("Password must be at least 8 characters", "error");
      return;
    }
    payload.password = pw;
  } else if (!id) {
    toast("Password is required for new users", "error");
    return;
  }

  try {
    await withLoader(async () => {
      if (id) {
        await api(`/users/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      } else {
        await api("/users", { method: "POST", body: JSON.stringify(payload) });
      }
      closeModal("modalUser");
      await loadUsers();
      await refreshAll();
    }, id ? "Saving user…" : "Creating user…");
    toast(id ? "User updated" : "User created", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------
async function handleEditBug(bugId) {
  try {
    const bug = await api(`/bugs/${bugId}`);
    openBugForm(bug);
  } catch (err) { toastError(err); }
}

async function handleDeleteBug(bugId) {
  // Look up the row from cached state so the confirm prompt + success toast
  // use the right noun ("task" / "requirement" / "bug"). Falls back to "Bug"
  // if we can't find the row (e.g. it was paginated out).
  const cached = (STATE.bugs || []).find(b => b.id === bugId);
  const itype = cached?.item_type || "Bug";
  const noun = itype.toLowerCase();
  const ok = await confirmDialog(
    `Delete ${noun} #${bugId}? This will also delete its comments and attachments. Cannot be undone`
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${bugId}`, { method: "DELETE" });
      closeModal("modalBug");
      await refreshAll();
    }, `Deleting ${noun}…`);
    toast(`${itype} #${bugId} deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleDeleteProject(id) {
  const project = STATE.projects.find(p => p.id === id);
  const name = project ? project.name : `#${id}`;
  const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs`);
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/projects/${id}`, { method: "DELETE" });
      // Drop the deleted project from the multi-select filter so we don't
      // keep filtering by a no-longer-existing id.
      const sid = String(id);
      STATE.filters.project_id = (STATE.filters.project_id || []).filter(v => v !== sid);
      await loadProjects();
      await refreshAll();
    }, "Deleting project…");
    toast(`Project "${name}" deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleEditProject(id) {
  const p = STATE.projects.find(x => x.id === id);
  if (p) openProjectForm(p);
}

async function handleDeleteUser(id) {
  const user = STATE.users.find(u => u.id === id);
  const name = user ? user.name : `#${id}`;
  const ok = await confirmDialog(
    `Delete user "${name}"?\nThis user will be removed from all bug assignments.\nReports they filed will become "unassigned reporter"`,
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/users/${id}`, { method: "DELETE" });
      await loadUsers();
      await refreshAll();
    }, "Deleting user…");
    toast(`User "${name}" deleted`, "success");
  } catch (err) { toastError(err); }
}

async function handleEditUser(id) {
  const u = STATE.users.find(x => x.id === id);
  if (u) openUserForm(u);
}

async function handleDeleteAttachment(attId) {
  const ok = await confirmDialog("Delete this attachment?");
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/attachments/${attId}`, { method: "DELETE" });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, "Deleting attachment…");
    toast("Attachment deleted", "success");
  } catch (err) { toastError(err); }
}

// v2.5 — admin-only delete/edit of a comment. Both handlers use the
// confirm dialog (delete) or an inline prompt (edit) and gate behind
// the loader so the user can't double-click.
async function handleDeleteComment(commentId) {
  const ok = await confirmDialog("Delete this comment? Its attachments will be removed too. Cannot be undone");
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/comments/${commentId}`, { method: "DELETE" });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, "Deleting comment…");
    toast("Comment deleted", "success");
  } catch (err) { toastError(err); }
}

async function handleEditComment(commentId) {
  const commentEl = document.querySelector(`.comment[data-comment-id="${commentId}"]`);
  if (!commentEl) return;
  const bodyEl = commentEl.querySelector(`[data-comment-body="${commentId}"]`);
  const current = bodyEl ? bodyEl.textContent : "";
  // Lightweight inline editor — replace the body div with a textarea +
  // Save/Cancel row. Keeps the comment thread layout intact so the
  // admin doesn't lose context while editing.
  if (!bodyEl) return;
  const editor = document.createElement("div");
  editor.className = "comment-edit-row";
  editor.innerHTML = `
    <textarea class="comment-edit-input" maxlength="10000" rows="3"></textarea>
    <div class="comment-edit-actions">
      <button type="button" class="btn ghost" data-act="cancel-edit-comment" data-id="${commentId}">Cancel</button>
      <button type="button" class="btn primary" data-act="save-edit-comment" data-id="${commentId}">Save</button>
    </div>`;
  bodyEl.replaceWith(editor);
  const ta = editor.querySelector(".comment-edit-input");
  if (ta) {
    ta.value = current;
    ta.focus();
  }
}

async function handleSaveEditComment(commentId) {
  const editor = document.querySelector(`.comment[data-comment-id="${commentId}"] .comment-edit-row`);
  if (!editor) return;
  const ta = editor.querySelector(".comment-edit-input");
  const body = (ta?.value || "").trim();
  if (!body) { toast("Comment body can't be empty", "error"); return; }
  try {
    await withLoader(async () => {
      await api(`/bugs/${STATE.currentBugId}/comments/${commentId}`, {
        method: "PUT",
        body: JSON.stringify({ body }),
      });
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
    }, "Saving comment…");
    toast("Comment updated", "success");
  } catch (err) { toastError(err); }
}

async function handleCancelEditComment(commentId) {
  // Re-render the inline sections from the cached bug so the editor goes
  // away and the original body re-appears in place. Cheaper than fetching
  // again — the row we cached at openBugDetail still has the old body.
  try {
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    renderBugInlineSections(bug);
  } catch (err) { toastError(err); }
}

async function postComment() {
  // Comment form is no longer a <form> element (nested forms are illegal
  // in HTML5). We read the textarea + file input directly by id.
  //
  // Either-or: posting works with body, files, or both. If only files are
  // attached they upload as bug-level attachments (no comment record) so
  // the user isn't forced to type a meaningless body just to share a file.
  const bodyEl = $("#commentBody");
  const body = (bodyEl?.value || "").trim();
  // Files come from the staged array (the user may have removed a few
  // via the X button), NOT from the input element's read-only FileList.
  const files = STATE.stagedFiles.comment || [];
  if (!body && files.length === 0) {
    toast("Add a comment or attach a file", "error");
    bodyEl?.focus();
    return;
  }
  try {
    await withLoader(async () => {
      let commentId = null;
      if (body) {
        const comment = await api(`/bugs/${STATE.currentBugId}/comments`, {
          method: "POST",
          body: JSON.stringify({ body }),
        });
        commentId = comment.id;
      }

      // Upload files. With body → attach to that comment; without body →
      // upload as bug-level so they show in the "Bug attachments" section.
      let failed = 0;
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        if (commentId) fd.append("comment_id", String(commentId));
        try {
          await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
        } catch (err) {
          failed++;
          toast(`Attachment ${f.name}: ${err.message}`, "error");
        }
      }

      if (body && files.length) toast("Comment posted", "success");
      else if (body) toast("Comment posted", "success");
      else if (files.length && !failed) toast(`${files.length} file${files.length > 1 ? "s" : ""} attached`, "success");

      // Clear the inputs so the next post starts fresh.
      if (bodyEl) bodyEl.value = "";
      clearStagedFiles("comment", "#filePreview", "#fileLabel");

      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugInlineSections(bug);
      await refreshBugs();
    }, body && files.length ? "Posting comment and uploading…" : body ? "Posting comment…" : "Uploading file(s)…");
  } catch (err) { toastError(err); }
}

// ---------------------------------------------------------------------------
// Sessions admin view
//
// Lists every active session row with user, role, IP, browser, when it
// was created, when it was last seen, when it expires. Admin-only —
// the nav button has data-needs-role="admin" so non-admins never see
// it; the API also enforces this (403 for non-admins) so direct URL
// access is also blocked.
// ---------------------------------------------------------------------------
function shortenUserAgent(ua) {
  // The full UA string is awful to read. We pull out a short browser /
  // OS hint instead. Anything we don't recognise falls back to the
  // first 60 chars so the column doesn't explode.
  if (!ua) return "Unknown";
  const lower = ua.toLowerCase();
  let browser = "Unknown";
  if (lower.includes("edg/")) browser = "Edge";
  else if (lower.includes("chrome/")) browser = "Chrome";
  else if (lower.includes("firefox/")) browser = "Firefox";
  else if (lower.includes("safari/") && !lower.includes("chrome/")) browser = "Safari";
  else if (lower.includes("curl/")) browser = "curl";
  else if (lower.includes("python-")) browser = "Python";
  else if (lower.includes("postman")) browser = "Postman";
  let os = "";
  if (lower.includes("windows")) os = "Windows";
  else if (lower.includes("mac os") || lower.includes("macintosh")) os = "macOS";
  else if (lower.includes("linux")) os = "Linux";
  else if (lower.includes("android")) os = "Android";
  else if (lower.includes("iphone") || lower.includes("ios")) os = "iOS";
  return os ? `${browser} on ${os}` : browser;
}

// ---------------------------------------------------------------------------
// Events view
//
// Two modes:
//   • list   — card grid of every event
//   • detail — one event open: header, items list, back button
//
// Items can be opened from the detail panel via the standard bug modal.
// Items can be moved in/out of an event via the Event select in that modal.
// ---------------------------------------------------------------------------
function showEventsListMode() {
  $("#eventsListMode").hidden = false;
  $("#eventsDetailMode").hidden = true;
  STATE.currentEventId = null;
  STATE.currentEvent = null;
}

function showEventsDetailMode() {
  $("#eventsListMode").hidden = true;
  $("#eventsDetailMode").hidden = false;
}

async function refreshEvents() {
  const grid = $("#eventsGrid");
  const empty = $("#eventsEmpty");
  if (!grid) return;
  grid.innerHTML = `<div class="events-loading muted">Loading events…</div>`;
  if (empty) empty.hidden = true;
  try {
    STATE.events = await api("/events");
    renderEvents();
  } catch (err) {
    grid.innerHTML = "";
    toastError(err);
  }
}

function renderEvents() {
  const grid = $("#eventsGrid");
  const empty = $("#eventsEmpty");
  if (!grid) return;
  const rows = STATE.events || [];
  // Roll-up KPIs for the page header. We can't safely sum assignee_counts
  // across events to get a unique-people count (the same person can be
  // double-counted across events), so report a "people involved" range:
  // exact for 0–1 events, otherwise an approximate sum prefixed with "~".
  const totalItems = rows.reduce((n, ev) => n + (ev.item_count || 0), 0);
  const totalPeople = rows.reduce((n, ev) => n + (ev.assignee_count || 0), 0);
  const summaryEl = $("#eventsSummary");
  if (summaryEl) {
    if (rows.length === 0) {
      summaryEl.textContent = "";
    } else {
      const peopleLabel = rows.length === 1 ? `${totalPeople}` : `~${totalPeople}`;
      summaryEl.textContent =
        `${rows.length} event${rows.length === 1 ? "" : "s"} · ` +
        `${totalItems} item${totalItems === 1 ? "" : "s"} · ` +
        `${peopleLabel} ${totalPeople === 1 ? "person" : "people"}`;
    }
  }
  if (rows.length === 0) {
    grid.innerHTML = "";
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  grid.innerHTML = rows.map(ev => {
    const sched = ev.scheduled_for
      ? `<span class="event-card-sched">📅 ${escapeHtml(ev.scheduled_for)}</span>`
      : `<span class="event-card-sched muted small">No date</span>`;
    const created = ev.created_by_name
      ? `<span class="muted small">by ${escapeHtml(ev.created_by_name)}</span>`
      : "";
    return `
      <section class="event-card" data-event-id="${ev.id}" tabindex="0" role="button" aria-label="Open event ${escapeHtml(ev.name)}">
        <div class="event-card-head">
          <h3 class="event-card-name">${escapeHtml(ev.name)}</h3>
          ${sched}
        </div>
        ${ev.description ? `<p class="event-card-desc">${escapeHtml(ev.description)}</p>` : ""}
        <div class="event-card-foot">
          <span class="event-card-count">${ev.item_count} item${ev.item_count === 1 ? "" : "s"}</span>
          <span class="event-card-people">👥 ${ev.assignee_count}</span>
          ${created}
        </div>
      </section>`;
  }).join("");
}

async function openEventDetail(eventId) {
  STATE.currentEventId = eventId;
  showEventsDetailMode();
  $("#eventDetailItems").innerHTML = `<div class="muted">Loading…</div>`;
  $("#eventDetailName").textContent = "Event";
  $("#eventDetailMeta").innerHTML = "";
  try {
    const ev = await api(`/events/${eventId}`);
    STATE.currentEvent = ev;
    renderEventDetail(ev);
  } catch (err) {
    toastError(err);
    showEventsListMode();
  }
}

function renderEventDetail(ev) {
  $("#eventDetailName").textContent = ev.name;
  const metaBits = [];
  if (ev.scheduled_for) metaBits.push(`📅 ${escapeHtml(ev.scheduled_for)}`);
  if (ev.created_by_name) metaBits.push(`by ${escapeHtml(ev.created_by_name)}`);
  metaBits.push(`${ev.item_count} item${ev.item_count === 1 ? "" : "s"}`);
  metaBits.push(`${ev.assignee_count} people`);
  const desc = ev.description
    ? `<p class="event-detail-desc">${escapeHtml(ev.description)}</p>`
    : "";
  const managersHtml = (ev.managers || []).length
    ? `<div class="event-detail-managers"><span class="muted small">Managers:</span> ${
        ev.managers.map(m => `<span class="assignee-chip" title="${escapeHtml(m.email)}"><span class="avatar">${initials(m.name)}</span><span class="assignee-chip-name">${escapeHtml(m.name)}</span></span>`).join("")
      }</div>`
    : "";
  $("#eventDetailMeta").innerHTML =
    `<div class="event-detail-bits">${metaBits.join(" · ")}</div>${desc}${managersHtml}`;

  const items = ev.items || [];
  const list = $("#eventDetailItems");
  if (items.length === 0) {
    list.innerHTML = `
      <div class="event-detail-empty muted">
        <p>No items yet. Click <strong>+ Add Task</strong> to create one inside this event, or open any existing work item and assign it to this event from its Event field</p>
      </div>`;
    return;
  }
  // Render the items as a full table — same look as the main work-items
  // list (item #5 of the v2.3 wish list). We use the per-type "All"
  // column set so every flavor shows its type prefix in the title cell.
  // Drop the "actions" column (deletion happens from the bug modal).
  const cols = ["id", "title-with-type", "project", "status", "priority", "due", "assignees", "att"];
  const head = "<tr>" + cols.map(c => `<th class="col-${c.replace('title-with-type','title')}">${COL_HEAD_LABEL[c] ?? ""}</th>`).join("") + "</tr>";
  const rows = items.map(b => {
    const tds = cols.map(c => _renderCell(c, b)).join("");
    return `<tr data-bug-id="${b.id}" tabindex="0">${tds}</tr>`;
  }).join("");
  list.innerHTML = `
    <div class="table-scroll">
      <table class="bug-table"><thead>${head}</thead><tbody>${rows}</tbody></table>
    </div>`;
}

async function refreshSessions() {
  try {
    STATE.sessions = await api("/sessions");
    renderSessions();
  } catch (err) {
    toastError(err);
  }
}

function renderSessions() {
  const host = $("#sessionsList");
  const rows = STATE.sessions || [];
  if (!rows.length) {
    host.innerHTML = `<div class="sessions-empty">No active sessions</div>`;
    return;
  }
  host.innerHTML = rows.map(s => {
    const ua = shortenUserAgent(s.user_agent);
    const ip = s.ip_address || "(unknown IP)";
    const role = s.user_role
      ? `<span class="session-role-pill">${escapeHtml(s.user_role)}</span>`
      : "";
    const currentTag = s.is_current
      ? `<span class="session-current-flag" title="The session you're using right now — can't be revoked from here">This is you</span>`
      : "";
    return `
      <div class="session-row${s.is_current ? " is-current" : ""}" data-session-id="${s.id}">
        <span class="session-avatar">${initials(s.user_name || "?")}</span>
        <div class="session-main">
          <div class="session-line1">
            <span class="session-name">${escapeHtml(s.user_name || "(deleted user)")}</span>
            <span class="muted small">${escapeHtml(s.user_email || "")}</span>
            ${role}
            ${currentTag}
          </div>
          <div class="session-line2">${escapeHtml(ua)} · ${escapeHtml(ip)}</div>
          <div class="session-line3">
            Started ${formatDate(s.created_at)} ·
            Last seen ${formatDate(s.last_seen_at)} ·
            Expires ${formatDate(s.expires_at)}
          </div>
        </div>
        <div class="session-actions">
          <button class="btn danger" data-act="revoke-session" data-id="${s.id}"
            ${s.is_current ? "disabled title='Use Log out from the sidebar to end your own session'" : ""}>
            Revoke
          </button>
        </div>
      </div>`;
  }).join("");
}

async function handleRevokeSession(sessionId) {
  const sess = (STATE.sessions || []).find(s => s.id === sessionId);
  const who = sess && sess.user_name
    ? `${sess.user_name} <${sess.user_email}>`
    : `session #${sessionId}`;
  const ok = await confirmDialog(
    `Revoke this session for ${who}?\n\n` +
    `That device will be immediately logged out. Other sessions for the ` +
    `same user are not affected`,
    { title: "Revoke session", okLabel: "Revoke", danger: true },
  );
  if (!ok) return;
  try {
    await withLoader(async () => {
      await api(`/sessions/${sessionId}`, { method: "DELETE" });
      await refreshSessions();
    }, "Revoking session…");
    toast("Session revoked", "success");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Audit view
// ---------------------------------------------------------------------------
async function refreshAudit() {
  const params = new URLSearchParams();
  const ent = $("#auditEntityFilter")?.value;
  const actor = $("#auditActorFilter")?.value;
  const q = $("#auditSearch")?.value.trim();
  if (ent) params.set("entity_type", ent);
  if (actor) params.set("actor_user_id", actor);
  if (q) params.set("q", q);
  params.set("limit", "300");
  try {
    const rows = await api("/audit?" + params.toString());
    const host = $("#auditList");
    if (!rows.length) { host.innerHTML = '<p class="no-content">No audit events match</p>'; return; }
    host.innerHTML = rows.map(r => `
      <div class="audit-row">
        <span class="audit-icon">${activityIcon(r.action)}</span>
        <div class="audit-text">
          <div>
            <span class="audit-actor">${escapeHtml(r.actor_name)}</span>
            <span class="audit-action">${escapeHtml(r.action)}</span>
            ${r.entity_type ? `<span class="audit-entity">${escapeHtml(r.entity_type)}${r.entity_id ? "#" + r.entity_id : ""}</span>` : ""}
          </div>
          ${r.detail ? `<div class="audit-detail">${escapeHtml(r.detail)}</div>` : ""}
        </div>
        <span class="audit-time">${formatDate(r.created_at)}</span>
      </div>`).join("");
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Global listeners (event delegation)
// ---------------------------------------------------------------------------
function bindGlobalListeners() {
  // Top-bar buttons. The "+ New" button is a split button: clicking the
  // main label opens the form preset to the user's last-chosen default
  // (Bug / Requirement / Task), and the caret opens a menu to pick a
  // different type explicitly.
  // Resolve the right default type for the "+ New" button. On the All
  // tab we honour the user's last-chosen type; on a per-type tab we
  // default to that tab so "+ New" on the Tasks tab files a Task.
  const resolveNewType = () => (
    STATE.activeTab && STATE.activeTab !== "all"
      ? STATE.activeTab
      : (STATE.defaultNewType || "Bug")
  );
  const setNewBtnLabel = () => {
    const t = resolveNewType();
    const el = $("#newBugBtn");
    if (el) el.textContent = `+ New ${t}`;
  };
  setNewBtnLabel();
  // Refresh the label whenever the user changes tab.
  document.addEventListener("bh:tab-change", setNewBtnLabel);
  $("#newBugBtn").addEventListener("click", () => {
    openBugForm({ _defaultType: resolveNewType() });
  });
  $("#newItemCaretBtn")?.addEventListener("click", (e) => {
    e.stopPropagation();
    const menu = $("#newItemMenu");
    if (!menu) return;
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    e.currentTarget.setAttribute("aria-expanded", String(willOpen));
  });
  $("#newItemMenu")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-new-type]");
    if (!btn) return;
    e.stopPropagation();
    const t = btn.dataset.newType;
    STATE.defaultNewType = t;
    try { localStorage.setItem("defaultNewType", t); } catch {}
    setNewBtnLabel();
    $("#newItemMenu").hidden = true;
    $("#newItemCaretBtn")?.setAttribute("aria-expanded", "false");
    openBugForm({ _defaultType: t });
  });
  // Close the new-item menu when clicking outside it.
  document.addEventListener("click", (e) => {
    const menu = $("#newItemMenu");
    if (!menu || menu.hidden) return;
    if (e.target.closest("#newItemMenu") || e.target.closest("#newItemCaretBtn")) return;
    menu.hidden = true;
    $("#newItemCaretBtn")?.setAttribute("aria-expanded", "false");
  });
  $("#newProjectBtn").addEventListener("click", () => openProjectForm());
  $("#newUserBtn").addEventListener("click", () => openUserForm());
  $("#exportCsvBtn").addEventListener("click", () => { window.location.href = "/api/bugs/export.csv"; });

  // ----- Events view -----
  $("#eventsRefreshBtn")?.addEventListener("click", refreshEvents);
  $("#newEventBtn")?.addEventListener("click", () => openEventForm());
  $("#eventBackBtn")?.addEventListener("click", () => {
    showEventsListMode();
    refreshEvents();
  });
  $("#editEventBtn")?.addEventListener("click", () => {
    if (STATE.currentEvent) openEventForm(STATE.currentEvent);
  });
  $("#deleteEventBtn")?.addEventListener("click", () => {
    if (STATE.currentEvent) handleDeleteEvent(STATE.currentEvent);
  });
  // Drill in by clicking a card (or pressing Enter while focused).
  $("#eventsGrid")?.addEventListener("click", (e) => {
    const card = e.target.closest("[data-event-id]");
    if (!card) return;
    openEventDetail(parseInt(card.dataset.eventId, 10));
  });
  $("#eventsGrid")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const card = e.target.closest("[data-event-id]");
    if (!card) return;
    e.preventDefault();
    openEventDetail(parseInt(card.dataset.eventId, 10));
  });
  // Click an item row inside the detail panel — opens the work-item modal.
  $("#eventDetailItems")?.addEventListener("click", (e) => {
    const row = e.target.closest("[data-bug-id]");
    if (!row) return;
    openBugDetail(parseInt(row.dataset.bugId, 10));
  });
  $("#eventDetailItems")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const row = e.target.closest("[data-bug-id]");
    if (!row) return;
    e.preventDefault();
    openBugDetail(parseInt(row.dataset.bugId, 10));
  });
  // "+ Add Task" inside an event: open the bug modal pre-set to Task type
  // and the current event.
  $("#addItemToEventBtn")?.addEventListener("click", () => {
    if (!STATE.currentEventId) return;
    openBugForm({
      _defaultType: "Task",
      _defaultEventId: STATE.currentEventId,
    });
  });
  // Event create / edit modal submit.
  $("#formEvent")?.addEventListener("submit", submitEventForm);
  $("#themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", nxt);
    localStorage.setItem("theme", nxt);
  });

  // Logout
  $("#logoutBtn").addEventListener("click", async () => {
    const ok = await confirmDialog("Log out now?", { title: "Log out", okLabel: "Log out", danger: false });
    if (!ok) return;
    try {
      await api("/auth/logout", { method: "POST" });
    } catch { /* ignore */ }
    location.href = "/login.html";
  });

  // Change password
  $("#changePasswordBtn").addEventListener("click", () => {
    const form = $("#formChangePassword");
    form.reset();
    openModal("modalChangePassword");
    setTimeout(() => form.elements.current_password.focus(), 50);
  });
  $("#formChangePassword").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const cur = f.elements.current_password.value;
    const next = f.elements.new_password.value;
    const conf = f.elements.confirm_password.value;
    if (next !== conf) {
      toast("New passwords don't match", "error");
      return;
    }
    if (next.length < 8) {
      toast("Password must be at least 8 characters", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api("/auth/change-password", {
          method: "POST",
          body: JSON.stringify({ current_password: cur, new_password: next }),
        });
        closeModal("modalChangePassword");
      }, "Updating password…");
      toast("Password updated", "success");
    } catch (err) {
      toastError(err);
    }
  });

  // Mobile hamburger
  $("#menuBtn").addEventListener("click", () => {
    $("#sidebar").classList.add("open");
    $("#sidebarBackdrop").hidden = false;
  });
  $("#sidebarBackdrop").addEventListener("click", closeSidebar);

  // Sidebar collapse / expand. Toggling a body class is the cheapest way
  // to flip the grid template + contents (CSS does the rest), and the new
  // state survives reload via localStorage.
  $("#sidebarCollapseBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    STATE.sidebarCollapsed = !STATE.sidebarCollapsed;
    document.body.classList.toggle("sidebar-collapsed", STATE.sidebarCollapsed);
    localStorage.setItem("sidebarCollapsed", STATE.sidebarCollapsed ? "1" : "0");
    e.currentTarget.title = STATE.sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar";
    e.currentTarget.textContent = STATE.sidebarCollapsed ? "»" : "«";
  });
  // Reflect the initial state on the button glyph too.
  if (STATE.sidebarCollapsed) {
    const btn = $("#sidebarCollapseBtn");
    if (btn) { btn.textContent = "»"; btn.title = "Expand sidebar"; }
  }

  // Nav buttons
  $$(".nav-btn").forEach(b => b.addEventListener("click", () => { setView(b.dataset.view); closeSidebar(); }));

  // Filter bar — clear all
  $("#clearFiltersBtn").addEventListener("click", () => {
    STATE.filters = {
      project_id: [], status: [], priority: [],
      environment: [], assignee_id: [], item_type: [],
      reporter_id: "", q: "",
    };
    $("#search").value = "";
    STATE.page = 1;
    refreshMultiSelects();
    renderProjectList();
    refreshBugs();
  });
  $("#search").addEventListener("input", debounce((e) => {
    STATE.filters.q = e.target.value.trim();
    STATE.page = 1; refreshBugs();
  }, 300));

  // Audit filters
  $("#auditEntityFilter").addEventListener("change", refreshAudit);
  $("#auditActorFilter").addEventListener("change", refreshAudit);
  $("#auditSearch").addEventListener("input", debounce(refreshAudit, 300));
  $("#auditRefreshBtn").addEventListener("click", refreshAudit);
  $("#auditClearBtn")?.addEventListener("click", () => {
    const ent = $("#auditEntityFilter"); if (ent) ent.value = "";
    const act = $("#auditActorFilter"); if (act) act.value = "";
    const q = $("#auditSearch"); if (q) q.value = "";
    refreshAudit();
  });

  // KPI strip — each tile is a clickable filter. Event delegation on the
  // strip so we don't bind 5 separate listeners.
  $("#kpiStrip")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".kpi[data-kpi]");
    if (!btn) return;
    handleKpiClick(btn.dataset.kpi);
  });
  // Tabs: All / Bugs / Requirements / Tasks at the top of the page.
  $("#typeTabs")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".type-tab[data-tab]");
    if (!btn) return;
    setActiveTab(btn.dataset.tab);
  });

  // Bug table — row click opens the unified modal in edit/view mode;
  // delete button (admin-only) handled separately. The pencil edit
  // button is gone; clicking the row itself is the way to open a bug.
  $("#bugTableBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) {
      e.stopPropagation();
      const id = parseInt(btn.dataset.id, 10);
      if (btn.dataset.act === "delete") return handleDeleteBug(id);
    }
    const tr = e.target.closest("tr[data-bug-id]");
    if (tr) openBugDetail(parseInt(tr.dataset.bugId, 10));
  });

  // Sidebar projects
  $("#projectList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const id = parseInt(btn.dataset.id, 10);
    if (btn.dataset.act === "edit-project") return handleEditProject(id);
    if (btn.dataset.act === "delete-project") return handleDeleteProject(id);
    if (btn.dataset.act === "filter") {
      const li = btn.closest("[data-project-id]");
      const pid = String(li.dataset.projectId);
      // Toggle the project in the multi-select array.
      const arr = STATE.filters.project_id;
      const idx = arr.indexOf(pid);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(pid);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
      renderProjectList();
    }
  });

  // Sidebar users
  $("#userList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const id = parseInt(btn.dataset.id, 10);
    if (btn.dataset.act === "edit-user") return handleEditUser(id);
    if (btn.dataset.act === "delete-user") return handleDeleteUser(id);
    if (btn.dataset.act === "filter-user") {
      const li = btn.closest("[data-user-id]");
      const uid = String(li.dataset.userId);
      const arr = STATE.filters.assignee_id;
      const idx = arr.indexOf(uid);
      if (idx >= 0) arr.splice(idx, 1); else arr.push(uid);
      STATE.page = 1;
      refreshMultiSelects();
      refreshBugs();
    }
  });

  // Forms
  $("#formBug").addEventListener("submit", submitBugForm);
  $("#formProject").addEventListener("submit", submitProjectForm);
  $("#formUser").addEventListener("submit", submitUserForm);

  // ----- Unified bug modal: delete + inline comments / attachments -----
  // The Delete button now lives inside the bug modal head (admin-only).
  $("#bugDeleteBtn")?.addEventListener("click", () => {
    if (STATE.currentBugId) handleDeleteBug(STATE.currentBugId);
  });

  // Comment "form" is now a <div> (HTML5 forbids nested <form> elements
  // and the old nesting was silently breaking the bug-create submit).
  // We trigger postComment from the button click and a Ctrl/Cmd+Enter
  // shortcut in the textarea.
  $("#commentPostBtn")?.addEventListener("click", () => postComment());
  $("#commentBody")?.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      postComment();
    }
  });
  $("#commentFiles")?.addEventListener("change", (e) => {
    handleStagedInputChange(e.target, "#filePreview", "#fileLabel");
  });
  $("#filePreview")?.addEventListener("click", (e) => {
    handleStagedListClick(e, "#filePreview", "#fileLabel");
  });

  // Create-mode bug attachment picker — same staging machinery used for
  // comment uploads, just pointed at the create-mode targets. The
  // submitBugForm() handler reads STATE.stagedFiles.createBug at submit
  // time and uploads each one after the bug row is created. Files can
  // be removed via the X button rendered next to each preview tile.
  $("#createBugFiles")?.addEventListener("change", (e) => {
    handleStagedInputChange(e.target, "#createFilePreview", "#createFileLabel");
  });
  $("#createFilePreview")?.addEventListener("click", (e) => {
    handleStagedListClick(e, "#createFilePreview", "#createFileLabel");
  });

  // Bug-level upload handlers used to live here (drag-drop zone + file
  // picker firing uploadFiles(..., null)). Removed in v3.2 along with
  // the dropzone HTML — new attachments go through the comment composer.
  // The bug-level attachment delete handler stays so legacy attachments
  // remain deletable.

  // Attachment delete buttons inside the bug modal (delegation).
  $("#bugAttachmentsGrid")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='delete-attachment']");
    if (btn) {
      e.stopPropagation();
      handleDeleteAttachment(parseInt(btn.dataset.id, 10));
    }
  });
  // v2.5 — comment row actions (admin only). The buttons themselves
  // are rendered conditionally in renderBugInlineSections; we delegate
  // here so they pick up automatically on every re-render.
  $("#bugCommentsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    e.stopPropagation();
    const act = btn.dataset.act;
    const id = parseInt(btn.dataset.id, 10);
    if (act === "delete-attachment") return handleDeleteAttachment(id);
    if (act === "delete-comment")    return handleDeleteComment(id);
    if (act === "edit-comment")      return handleEditComment(id);
    if (act === "save-edit-comment") return handleSaveEditComment(id);
    if (act === "cancel-edit-comment") return handleCancelEditComment(id);
  });
  // Comment textarea Cmd/Ctrl+Enter inside the inline editor saves.
  $("#bugCommentsList")?.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      const ta = e.target.closest(".comment-edit-input");
      if (!ta) return;
      const saveBtn = ta.parentElement?.querySelector("[data-act='save-edit-comment']");
      const id = saveBtn ? parseInt(saveBtn.dataset.id, 10) : NaN;
      if (!Number.isNaN(id)) {
        e.preventDefault();
        handleSaveEditComment(id);
      }
    }
  });

  // v2.5 — post-creation attachment uploader inside the bug modal.
  $("#bugAttachAddInput")?.addEventListener("change", (e) => {
    handleStagedInputChange(e.target, "#bugAttachAddPreview", "#bugAttachAddText");
  });
  $("#bugAttachAddPreview")?.addEventListener("click", async (e) => {
    // ✕ button removes a staged file; clicking the thumb opens it
    // (handleStagedListClick handles both).
    handleStagedListClick(e, "#bugAttachAddPreview", "#bugAttachAddText");
  });
  // Flush staged files: clicking the section-head 📎 label re-opens the
  // file dialog; once the user has files staged we add an inline
  // "Upload N file(s)" button so the upload doesn't happen on every
  // file pick — they can stage multiple, review, then confirm.
  $("#bugAttachAddPreview")?.addEventListener("click", (e) => {
    const goBtn = e.target.closest(".attach-staged-upload");
    if (!goBtn) return;
    e.preventDefault();
    e.stopPropagation();
    withLoader(flushBugAttachStaging, "Uploading attachment(s)…").catch(toastError);
  });
  // After every staged-files render, append a "Upload" button if any
  // files are pending. We use a small wrapper to call _renderStagedFiles
  // and then add the button — but the simpler approach is to listen for
  // input changes and append the button when needed.
  // (Implemented via a MutationObserver-free approach: re-run after
  // every change event.)
  const _ensureUploadBtn = () => {
    const host = $("#bugAttachAddPreview");
    if (!host) return;
    const has = STATE.stagedFiles.bugAttach && STATE.stagedFiles.bugAttach.length;
    const existing = host.querySelector(".attach-staged-upload");
    if (has && !existing) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn primary attach-staged-upload";
      btn.textContent = `Upload ${STATE.stagedFiles.bugAttach.length} file${STATE.stagedFiles.bugAttach.length > 1 ? "s" : ""}`;
      host.appendChild(btn);
    } else if (has && existing) {
      existing.textContent = `Upload ${STATE.stagedFiles.bugAttach.length} file${STATE.stagedFiles.bugAttach.length > 1 ? "s" : ""}`;
    } else if (!has && existing) {
      existing.remove();
    }
  };
  // Hook after the staged-list render path:
  document.addEventListener("bh:staged-changed", _ensureUploadBtn);

  // v2.5 — when the user changes item_type inside the bug modal, refresh
  // the status dropdown so it only shows statuses valid for the new
  // type. The currently-selected status is preserved if it still fits;
  // otherwise it carries forward as a "(legacy)" option.
  $("#formBug")?.elements?.item_type?.addEventListener("change", (e) => {
    const newType = e.target.value || "Bug";
    const statusEl = $("#formBug").elements.status;
    const current = statusEl.value;
    _renderStatusSelect(statusEl, newType, current);
  });

  // ----- Sessions admin view -----
  $("#sessionsRefreshBtn")?.addEventListener("click", refreshSessions);
  $("#sessionsList")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act='revoke-session']");
    if (!btn || btn.disabled) return;
    e.stopPropagation();
    handleRevokeSession(parseInt(btn.dataset.id, 10));
  });

  // Universal modal close: ✕ buttons, Cancel buttons, click outside, Escape
  document.addEventListener("click", (e) => {
    const closeBtn = e.target.closest("[data-close-modal]");
    if (closeBtn) {
      const modal = closeBtn.closest(".modal");
      if (modal) modal.hidden = true;
      return;
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Don't close if focused on input — let user blur first
      if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) {
        e.target.blur();
        return;
      }
      closeTopModal();
    }
  });

  // Sleuth chatbot integration: when the user clicks a bug in chat results,
  // chatbot.js dispatches this CustomEvent. We claim it (preventDefault)
  // and open the bug detail modal via the existing route.
  window.addEventListener("sleuth:open-bug", (e) => {
    const bugId = e.detail && e.detail.bugId;
    if (!bugId) return;
    e.preventDefault();
    openBugDetail(parseInt(bugId, 10));
  });
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebarBackdrop").hidden = true;
}

// ---------------------------------------------------------------------------
// Go!
// ---------------------------------------------------------------------------
boot().catch(err => {
  console.error("Boot failed:", err);
  toast("Failed to load: " + err.message, "error");
});

})();