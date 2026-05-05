/* ============================================================
 * Bug Hunter — frontend SPA
 * ============================================================ */
(() => {
"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const STATE = {
  meta:     { statuses: [], priorities: [], environments: [] },
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
    environment: [], assignee_id: [],
    reporter_id: "", q: "",
  },
  view: "list",
  currentBugId: null,
  detailTab: "info",
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
    // Session expired or otherwise rejected — bounce to login. Use replace()
    // so the broken state isn't in browser history. Throw a flag-error so
    // callers can swallow it without showing user-visible toasts.
    if (res.status === 401 && path !== "/auth/login") {
      const next = encodeURIComponent(location.pathname + location.search);
      location.replace("/login.html?next=" + next);
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

  await loadHealth();
  await loadMeta();
  await loadUsers();
  await loadProjects();
  // Multi-select dropdowns depend on STATE.users / STATE.projects / STATE.meta
  // being populated, so initialise them after the loaders above.
  initMultiSelects();
  await refreshAll();
  bindGlobalListeners();
  scheduleVersionCheck();
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
        toast("New version available — reload the page when ready.", "info");
      }
    } catch { /* ignore */ }
  }, 5 * 60 * 1000);
}

async function loadMeta() {
  STATE.meta = await api("/meta");
  // Multi-select panels are repopulated by refreshMultiSelects(); the legacy
  // <select> filters were removed in favour of the new dropdowns.
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
  STATE.stats = await api("/stats");
  // KPI strip: Total | Open | Resolved | Closed | Resolve Later. We
  // defensively coalesce missing fields to 0 so an older server that
  // hasn't shipped the new schema yet doesn't render `undefined`.
  const s = STATE.stats || {};
  $("#kpiBugs").textContent = s.bugs ?? 0;
  $("#kpiOpen").textContent = s.open ?? 0;
  $("#kpiResolved").textContent = s.resolved ?? 0;
  $("#kpiClosed").textContent = s.closed ?? (s.by_status?.Closed ?? 0);
  $("#kpiResolveLater").textContent = s.resolve_later ?? (s.by_status?.["Resolve Later"] ?? 0);
  if (STATE.view === "analytics") renderCharts();
}

// ---------------------------------------------------------------------------
// Bug list
// ---------------------------------------------------------------------------
async function refreshBugs() {
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
  const data = await api("/bugs?" + params.toString());
  STATE.bugs = data.items;
  STATE.total = data.total;
  STATE.totalPages = data.pages;
  renderBugTable();
  renderPagination();
}

function renderBugTable() {
  const tbody = $("#bugTableBody");
  tbody.innerHTML = "";
  $("#emptyState").hidden = STATE.bugs.length > 0;

  const frag = document.createDocumentFragment();
  for (const bug of STATE.bugs) {
    const tr = document.createElement("tr");
    tr.dataset.bugId = String(bug.id);
    const assigneesHtml = bug.assignees.length
      ? bug.assignees.map(a => `<span class="assignee-chip" title="${escapeHtml(a.email)}"><span class="avatar">${initials(a.name)}</span>${escapeHtml(a.name)}</span>`).join("")
      : `<span class="muted">—</span>`;
    // Title cell carries the bug's `updated_at` as a small timestamp under
    // the title, so we can drop the dedicated "Updated" column without
    // losing the freshness signal entirely.
    const canDeleteBug = ["admin", "manager"].includes(STATE.currentUser?.role);
    tr.innerHTML = `
      <td class="col-id">#${bug.id}</td>
      <td class="col-title">
        <div class="title-cell">
          <strong class="title-text" title="${escapeHtml(bug.title)}">${escapeHtml(bug.title)}</strong>
          <span class="title-meta">Updated ${formatDate(bug.updated_at)}</span>
        </div>
      </td>
      <td class="col-project">${escapeHtml(bug.project_name || "")}</td>
      <td class="col-status"><span class="badge" data-status="${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span></td>
      <td class="col-priority"><span class="badge" data-priority="${escapeHtml(bug.priority)}">${escapeHtml(bug.priority)}</span></td>
      <td class="col-env"><span class="badge" data-env="${escapeHtml(bug.environment)}">${escapeHtml(bug.environment)}</span></td>
      <td class="col-assignees"><div class="assignee-stack">${assigneesHtml}</div></td>
      <td class="col-att">${bug.attachment_count > 0 ? `<span class="att-count">📎 ${bug.attachment_count}</span>` : '<span class="muted">—</span>'}</td>
      <td class="col-actions">
        <div class="row-actions">
          ${bug.can_edit ? `
          <button class="icon-btn" data-act="edit" data-id="${bug.id}" title="Edit">✎</button>
          ${canDeleteBug ? `<button class="icon-btn danger" data-act="delete" data-id="${bug.id}" title="Delete">🗑</button>` : ""}
          ` : `<span class="muted small" title="You don't have permission to edit this bug">🔒</span>`}
        </div>
      </td>`;
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
    ul.innerHTML = `<li class="side-item muted no-cursor">No projects — click + to add.</li>`;
    return;
  }
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
        <button class="icon-btn" data-act="edit-project" data-id="${p.id}" title="Edit">✎</button>
        <button class="icon-btn danger" data-act="delete-project" data-id="${p.id}" title="Delete">🗑</button>
      </span>`;
    ul.appendChild(li);
  }
}

function renderUserList() {
  const ul = $("#userList");
  ul.innerHTML = "";
  const active = STATE.users.filter(u => u.is_active);
  if (!active.length) {
    ul.innerHTML = `<li class="side-item muted no-cursor">No users yet — click + to add.</li>`;
    return;
  }
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
        <button class="icon-btn" data-act="edit-user" data-id="${u.id}" title="Edit">✎</button>
        <button class="icon-btn danger" data-act="delete-user" data-id="${u.id}" title="Delete">🗑</button>
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
  status:      "All Statuses",
  priority:    "All Priorities",
  environment: "All Envs",
  assignee_id: "All Assignees",
};
const MS_NOUNS = {
  project_id: "Projects", status: "Statuses", priority: "Priorities",
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
  return [];
}

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
  $("#viewAnalytics").hidden = view !== "analytics";
  $("#viewAudit").hidden = view !== "audit";
  $("#filterBar").hidden = view !== "list";
  $("#pageTitle").textContent = ({
    list: "All Bugs", analytics: "Analytics", audit: "Audit Trail",
  }[view] || "Bug Hunter");
  if (view === "analytics") renderCharts();
  if (view === "audit") refreshAudit();
}

// ---------------------------------------------------------------------------
// Charts
// ---------------------------------------------------------------------------
function renderCharts() {
  if (!STATE.stats) return;
  const s = STATE.stats;
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
// Bug form
// ---------------------------------------------------------------------------
function openBugForm(bug = null) {
  const form = $("#formBug");
  form.reset();
  $("#modalBugTitle").textContent = bug ? `Edit Bug #${bug.id}` : "New Bug";
  $("#bugSubmitBtn").textContent = bug ? "Save" : "Create";
  form.elements.id.value = bug ? bug.id : "";

  fillFormSelect(form.elements.project_id, STATE.projects.map(p => [p.id, p.name]),
                 bug ? bug.project_id : "");
  // Reporter dropdown: label is just the name; the email (which can be long
  // and was causing the field to spill out of the modal during edit) goes
  // into the option's `title` so it's still discoverable on hover, but no
  // longer drives the field's intrinsic width.
  fillFormSelect(form.elements.reporter_id,
                 STATE.users.filter(u => u.is_active).map(u => [u.id, u.name, u.email]),
                 bug ? (bug.reporter ? bug.reporter.id : "") : (STATE.currentUser?.id || ""));
  fillFormSelect(form.elements.status, STATE.meta.statuses.map(s => [s, s]),
                 bug ? bug.status : "New");
  fillFormSelect(form.elements.priority, STATE.meta.priorities.map(s => [s, s]),
                 bug ? bug.priority : "Medium");

  // Environment - already DEV/UAT/PROD options in the HTML, just set value
  form.elements.environment.value = bug ? bug.environment : "DEV";

  const assignedIds = new Set((bug && bug.assignees ? bug.assignees.map(a => a.id) : []));
  renderChips("#assigneePicker",
    STATE.users.filter(u => u.is_active),
    (u) => ({ id: u.id, label: u.name, sub: u.role }),
    assignedIds);

  if (bug) {
    form.elements.title.value = bug.title || "";
    form.elements.description.value = bug.description || "";
    form.elements.due_date.value = bug.due_date || "";
  }
  openModal("modalBug");
  setTimeout(() => form.elements.title.focus(), 50);
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
  const payload = {
    project_id: parseInt(form.elements.project_id.value, 10),
    title: form.elements.title.value.trim(),
    description: form.elements.description.value,
    reporter_id: form.elements.reporter_id.value ? parseInt(form.elements.reporter_id.value, 10) : null,
    status: form.elements.status.value,
    priority: form.elements.priority.value,
    environment: form.elements.environment.value,
    due_date: form.elements.due_date.value || null,
    assignee_ids: readChips("#assigneePicker"),
  };
  if (!payload.project_id) { toast("Please pick a project", "error"); return; }
  if (!payload.title) { toast("Title is required", "error"); return; }
  if (!payload.reporter_id) { toast("Reporter is required", "error"); return; }

  try {
    if (id) {
      await api(`/bugs/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast(`Bug #${id} updated`, "success");
    } else {
      await api("/bugs", { method: "POST", body: JSON.stringify(payload) });
      toast("Bug created", "success");
    }
    closeModal("modalBug");
    await refreshAll();
  } catch (err) {
    toastError(err);
  }
}

// ---------------------------------------------------------------------------
// Bug detail (with attachments)
// ---------------------------------------------------------------------------
async function openBugDetail(bugId) {
  STATE.currentBugId = bugId;
  STATE.detailTab = "info";
  try {
    const bug = await api(`/bugs/${bugId}`);
    renderBugDetail(bug);
    openModal("modalDetail");
  } catch (err) {
    toastError(err);
  }
}

function renderBugDetail(bug) {
  $("#detailTitle").textContent = `#${bug.id} — ${bug.title}`;
  // Show / hide edit + delete based on permission flag from API.
  const canDeleteBug = ["admin", "manager"].includes(STATE.currentUser?.role);
  $("#detailDeleteBtn").style.display = canDeleteBug ? "" : "none";

  const reporter = bug.reporter
    ? `<span class="assignee-chip"><span class="avatar">${initials(bug.reporter.name)}</span>${escapeHtml(bug.reporter.name)} <span class="muted small"> ${escapeHtml(bug.reporter.email)}</span></span>`
    : '<span class="muted">—</span>';
  const assignees = bug.assignees.length
    ? bug.assignees.map(a => `<span class="assignee-chip" title="${escapeHtml(a.email)}"><span class="avatar">${initials(a.name)}</span>${escapeHtml(a.name)}</span>`).join("")
    : '<span class="muted">—</span>';

  const meta = `
    <div class="detail-grid">
      <div class="detail-meta-item"><div class="k">Project</div><div class="v">${escapeHtml(bug.project_name || "—")}</div></div>
      <div class="detail-meta-item"><div class="k">Status</div><div class="v"><span class="badge" data-status="${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span></div></div>
      <div class="detail-meta-item"><div class="k">Priority</div><div class="v"><span class="badge" data-priority="${escapeHtml(bug.priority)}">${escapeHtml(bug.priority)}</span></div></div>
      <div class="detail-meta-item"><div class="k">Environment</div><div class="v"><span class="badge" data-env="${escapeHtml(bug.environment)}">${escapeHtml(bug.environment)}</span></div></div>
      <div class="detail-meta-item"><div class="k">Reporter</div><div class="v">${reporter}</div></div>
      <div class="detail-meta-item"><div class="k">Assignees</div><div class="v"><div class="assignee-stack">${assignees}</div></div></div>
      <div class="detail-meta-item"><div class="k">Due Date</div><div class="v">${escapeHtml(bug.due_date || "—")}</div></div>
      <div class="detail-meta-item"><div class="k">Created</div><div class="v">${formatDate(bug.created_at)}</div></div>
      <div class="detail-meta-item"><div class="k">Updated</div><div class="v">${formatDate(bug.updated_at)}</div></div>
    </div>`;

  const tabs = `
    <div class="detail-tabs">
      <button class="detail-tab ${STATE.detailTab === "info" ? "active" : ""}" data-detail-tab="info">Info</button>
      <button class="detail-tab ${STATE.detailTab === "comments" ? "active" : ""}" data-detail-tab="comments">Comments (${bug.comments.length})</button>
      <button class="detail-tab ${STATE.detailTab === "attachments" ? "active" : ""}" data-detail-tab="attachments">Attachments (${bug.attachments.length})</button>
      <button class="detail-tab ${STATE.detailTab === "activity" ? "active" : ""}" data-detail-tab="activity">Activity (${bug.activities.length})</button>
    </div>`;

  let tabBody = "";
  if (STATE.detailTab === "info") {
    tabBody = bug.description
      ? `<div class="detail-section"><h3>Description</h3><p>${escapeHtml(bug.description)}</p></div>`
      : '<p class="no-content">No description provided.</p>';
  } else if (STATE.detailTab === "comments") {
    const list = bug.comments.length
      ? bug.comments.map(c => {
          const atts = (c.attachments || []).map(a => renderAttachmentCard(a, false)).join("");
          return `
            <div class="comment">
              <div class="comment-head">
                <div class="comment-head-left">
                  <span class="avatar">${initials(c.author_name)}</span>
                  <span class="comment-author">${escapeHtml(c.author_name)}</span>
                </div>
                <span class="comment-time">${formatDate(c.created_at)}</span>
              </div>
              <div class="comment-body">${escapeHtml(c.body)}</div>
              ${atts ? `<div class="comment-attachments"><div class="attachment-grid">${atts}</div></div>` : ""}
            </div>`;
        }).join("")
      : '<p class="no-content">No comments yet — be the first to add one.</p>';

    tabBody = `
      ${list}
      <form class="comment-form" id="commentForm" enctype="multipart/form-data">
        <textarea name="body" placeholder="Add a comment…" required></textarea>
        <div class="comment-form-row">
          <label class="comment-attach-btn" title="Attach files">
            📎 <span id="fileLabel">Attach files</span>
            <input type="file" name="files" multiple id="commentFiles" />
          </label>
          <div class="attach-staged-list" id="filePreview"></div>
          <button type="submit" class="btn primary" style="margin-left:auto">Post</button>
        </div>
      </form>`;
  } else if (STATE.detailTab === "attachments") {
    tabBody = `
      <div class="upload-zone" id="uploadZone">
        <form id="bugAttachForm" enctype="multipart/form-data">
          <label class="upload-cta" for="bugAttachInput">
            <div class="upload-icon">📎</div>
            <div class="upload-title">Click or drop files here</div>
            <div class="upload-sub">PDF, image, video — up to 50 MB each. Stored permanently in the database.</div>
            <input type="file" name="files" multiple id="bugAttachInput" />
          </label>
        </form>
      </div>
      <div class="attachment-grid">
        ${bug.attachments.length
          ? bug.attachments.map(a => renderAttachmentCard(a, true)).join("")
          : '<p class="no-content" style="grid-column:1/-1">No bug-level attachments yet.</p>'}
      </div>`;
  } else if (STATE.detailTab === "activity") {
    tabBody = bug.activities.length
      ? bug.activities.map(a => renderActivityRow(a)).join("")
      : '<p class="no-content">No activity yet.</p>';
  }

  $("#detailBody").innerHTML = meta + tabs + `<div id="detailTabBody">${tabBody}</div>`;

  // Wire up dynamic content
  if (STATE.detailTab === "comments") {
    const fileInput = $("#commentFiles");
    fileInput?.addEventListener("change", () => updateFilePreview(fileInput, "#filePreview", "#fileLabel"));
  }
  if (STATE.detailTab === "attachments") {
    const inp = $("#bugAttachInput");
    const zone = $("#uploadZone");
    inp?.addEventListener("change", () => uploadFiles(inp.files, null));
    // drag-drop
    zone?.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("drag-over"); });
    zone?.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
    zone?.addEventListener("drop", e => {
      e.preventDefault();
      zone.classList.remove("drag-over");
      uploadFiles(e.dataTransfer.files, null);
    });
  }
}

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
  if (action.includes("created")) return "✨";
  if (action.includes("delete")) return "🗑";
  if (action.includes("comment")) return "💬";
  if (action.includes("attachment")) return "📎";
  if (action.includes("status")) return "🔄";
  if (action.includes("assign")) return "👥";
  return "📝";
}

function updateFilePreview(input, previewSel, labelSel) {
  const preview = $(previewSel);
  const label = $(labelSel);
  preview.innerHTML = "";
  if (!input.files || !input.files.length) {
    label.textContent = "Attach files";
    return;
  }
  label.textContent = `${input.files.length} file${input.files.length > 1 ? "s" : ""}`;
  for (const f of input.files) {
    const div = document.createElement("span");
    div.className = "attach-staged";
    div.innerHTML = `${fileIcon(f.type, f.name)} ${escapeHtml(f.name)} <span class="muted small">(${formatBytes(f.size)})</span>`;
    preview.appendChild(div);
  }
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
  // Refresh detail
  const bug = await api(`/bugs/${STATE.currentBugId}`);
  renderBugDetail(bug);
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
    if (id) {
      await api(`/projects/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("Project updated", "success");
    } else {
      await api("/projects", { method: "POST", body: JSON.stringify(payload) });
      toast("Project created", "success");
    }
    closeModal("modalProject");
    await loadProjects();
    await refreshAll();
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
    $("#userPasswordHint").textContent = "Leave blank to keep current password.";
    $("#userPasswordField").querySelector(".js-required")?.classList.add("hidden");
  } else {
    form.elements.role.value = "user";
    form.elements.is_active.checked = true;
    // On create, password is REQUIRED
    form.elements.password.required = true;
    form.elements.password.placeholder = "Min 8 characters";
    $("#userPasswordHint").textContent = "At least 8 characters.";
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
    if (id) {
      await api(`/users/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("User updated", "success");
    } else {
      await api("/users", { method: "POST", body: JSON.stringify(payload) });
      toast("User created", "success");
    }
    closeModal("modalUser");
    await loadUsers();
    await refreshAll();
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
  const ok = await confirmDialog(`Delete bug #${bugId}? This will also delete its comments and attachments. Cannot be undone.`);
  if (!ok) return;
  try {
    await api(`/bugs/${bugId}`, { method: "DELETE" });
    toast(`Bug #${bugId} deleted`, "success");
    closeModal("modalDetail");
    await refreshAll();
  } catch (err) { toastError(err); }
}

async function handleDeleteProject(id) {
  const project = STATE.projects.find(p => p.id === id);
  const name = project ? project.name : `#${id}`;
  const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs.`);
  if (!ok) return;
  try {
    await api(`/projects/${id}`, { method: "DELETE" });
    toast(`Project "${name}" deleted`, "success");
    // Drop the deleted project from the multi-select filter so we don't
    // keep filtering by a no-longer-existing id.
    const sid = String(id);
    STATE.filters.project_id = (STATE.filters.project_id || []).filter(v => v !== sid);
    await loadProjects();
    await refreshAll();
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
    `Delete user "${name}"?\nThis user will be removed from all bug assignments.\nReports they filed will become "unassigned reporter".`,
  );
  if (!ok) return;
  try {
    await api(`/users/${id}`, { method: "DELETE" });
    toast(`User "${name}" deleted`, "success");
    await loadUsers();
    await refreshAll();
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
    await api(`/bugs/${STATE.currentBugId}/attachments/${attId}`, { method: "DELETE" });
    toast("Attachment deleted", "success");
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    renderBugDetail(bug);
    await refreshBugs();
  } catch (err) { toastError(err); }
}

async function postComment(form) {
  const body = form.elements.body.value.trim();
  if (!body) return;
  try {
    const comment = await api(`/bugs/${STATE.currentBugId}/comments`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });

    // Upload any attached files to this comment
    const files = form.elements.files?.files;
    if (files && files.length) {
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("comment_id", String(comment.id));
        try {
          await api(`/bugs/${STATE.currentBugId}/attachments`, { method: "POST", body: fd });
        } catch (err) {
          toast(`Attachment ${f.name}: ${err.message}`, "error");
        }
      }
    }

    toast("Comment posted", "success");
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    renderBugDetail(bug);
    await refreshBugs();
  } catch (err) { toastError(err); }
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
    if (!rows.length) { host.innerHTML = '<p class="no-content">No audit events match.</p>'; return; }
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
  // Top-bar buttons
  $("#newBugBtn").addEventListener("click", () => openBugForm());
  $("#newProjectBtn").addEventListener("click", () => openProjectForm());
  $("#newUserBtn").addEventListener("click", () => openUserForm());
  $("#exportCsvBtn").addEventListener("click", () => { window.location.href = "/api/bugs/export.csv"; });
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
      await api("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ current_password: cur, new_password: next }),
      });
      toast("Password updated", "success");
      closeModal("modalChangePassword");
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
      environment: [], assignee_id: [],
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

  // Bug table
  $("#bugTableBody").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (btn) {
      e.stopPropagation();
      const id = parseInt(btn.dataset.id, 10);
      if (btn.dataset.act === "edit") return handleEditBug(id);
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

  // Detail modal — edit, delete, tab switching, comment form, attachment delete
  $("#detailEditBtn").addEventListener("click", async () => {
    if (!STATE.currentBugId) return;
    closeModal("modalDetail");
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    openBugForm(bug);
  });
  $("#detailDeleteBtn").addEventListener("click", () => {
    if (STATE.currentBugId) handleDeleteBug(STATE.currentBugId);
  });
  $("#detailBody").addEventListener("click", async (e) => {
    const tab = e.target.closest("[data-detail-tab]");
    if (tab) {
      STATE.detailTab = tab.dataset.detailTab;
      const bug = await api(`/bugs/${STATE.currentBugId}`);
      renderBugDetail(bug);
      return;
    }
    const delAtt = e.target.closest("[data-act='delete-attachment']");
    if (delAtt) {
      e.stopPropagation();
      handleDeleteAttachment(parseInt(delAtt.dataset.id, 10));
    }
  });
  $("#detailBody").addEventListener("submit", (e) => {
    if (e.target.id === "commentForm") {
      e.preventDefault();
      postComment(e.target);
    }
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