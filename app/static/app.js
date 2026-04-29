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
  filters: {
    project_id: "", status: "", priority: "",
    environment: "", reporter_id: "", assignee_id: "", q: "",
  },
  view: "list",
  currentBugId: null,
  detailTab: "info",
  actorUserId: null,
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
  const apiKey = localStorage.getItem("apiKey");
  if (apiKey) headers["X-API-Key"] = apiKey;

  const res = await fetch(API + path, { ...opts, headers });
  if (!res.ok) {
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
  return new Promise((resolve) => {
    $("#confirmTitle").textContent = title;
    $("#confirmMessage").textContent = message;
    const ok = $("#confirmOk");
    const cancel = $("#confirmCancel");
    ok.textContent = okLabel;
    ok.className = "btn " + (danger ? "danger" : "primary");
    const cleanup = () => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      closeModal("modalConfirm");
    };
    const onOk = () => { cleanup(); resolve(true); };
    const onCancel = () => { cleanup(); resolve(false); };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    openModal("modalConfirm");
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
async function boot() {
  const theme = localStorage.getItem("theme") || "dark";
  document.documentElement.setAttribute("data-theme", theme);

  const stored = localStorage.getItem("actorUserId");
  if (stored) STATE.actorUserId = parseInt(stored, 10);

  await loadHealth();
  await loadMeta();
  await loadUsers();
  await loadProjects();
  await refreshAll();
  bindGlobalListeners();
}

async function loadHealth() {
  try {
    const h = await api("/health");
    $("#brandVersion").textContent = "v" + h.version;
  } catch { /* ignore */ }
}

async function loadMeta() {
  STATE.meta = await api("/meta");
  fillSelect("filterStatus", "All Statuses", STATE.meta.statuses);
  fillSelect("filterPriority", "All Priorities", STATE.meta.priorities);
}

async function loadUsers() {
  STATE.users = await api("/users");
  renderUserList();
  fillUserFilterSelect();
  fillActorSelect();
  fillAuditActorSelect();
}

async function loadProjects() {
  STATE.projects = await api("/projects");
  renderProjectList();
  fillProjectFilterSelect();
}

async function refreshAll() {
  await Promise.all([refreshBugs(), refreshStats()]);
}

// ---------------------------------------------------------------------------
// Stats / KPIs
// ---------------------------------------------------------------------------
async function refreshStats() {
  STATE.stats = await api("/stats");
  $("#kpiBugs").textContent = STATE.stats.bugs;
  $("#kpiOpen").textContent = STATE.stats.open;
  $("#kpiResolved").textContent = STATE.stats.resolved;
  $("#kpiUsers").textContent = STATE.stats.users;
  $("#kpiProjects").textContent = STATE.stats.projects;
  if (STATE.view === "analytics") renderCharts();
}

// ---------------------------------------------------------------------------
// Bug list
// ---------------------------------------------------------------------------
async function refreshBugs() {
  const params = new URLSearchParams({
    page: String(STATE.page),
    page_size: String(STATE.pageSize),
  });
  for (const [k, v] of Object.entries(STATE.filters)) {
    if (v !== "" && v != null) params.set(k, v);
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
    tr.innerHTML = `
      <td class="col-id">#${bug.id}</td>
      <td><strong>${escapeHtml(bug.title)}</strong></td>
      <td>${escapeHtml(bug.project_name || "")}</td>
      <td><span class="badge" data-status="${escapeHtml(bug.status)}">${escapeHtml(bug.status)}</span></td>
      <td><span class="badge" data-priority="${escapeHtml(bug.priority)}">${escapeHtml(bug.priority)}</span></td>
      <td><span class="badge" data-env="${escapeHtml(bug.environment)}">${escapeHtml(bug.environment)}</span></td>
      <td><div class="assignee-stack">${assigneesHtml}</div></td>
      <td>${bug.attachment_count > 0 ? `<span class="att-count">📎 ${bug.attachment_count}</span>` : '<span class="muted">—</span>'}</td>
      <td>${formatDate(bug.updated_at)}</td>
      <td class="col-actions">
        <div class="row-actions">
          <button class="icon-btn" data-act="edit" data-id="${bug.id}" title="Edit">✎</button>
          <button class="icon-btn danger" data-act="delete" data-id="${bug.id}" title="Delete">🗑</button>
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
  for (const p of STATE.projects) {
    const li = document.createElement("li");
    li.className = "side-item" + (STATE.filters.project_id == p.id ? " active" : "");
    li.dataset.projectId = String(p.id);
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
// Selects
// ---------------------------------------------------------------------------
function fillSelect(id, defaultLabel, values) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">${defaultLabel}</option>` +
    values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  if (cur) sel.value = cur;
}

function fillProjectFilterSelect() {
  const sel = $("#filterProject");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All Projects</option>` +
    STATE.projects.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
  if (cur) sel.value = cur;
}

function fillUserFilterSelect() {
  const sel = $("#filterAssignee");
  const cur = sel.value;
  sel.innerHTML = `<option value="">All Assignees</option>` +
    STATE.users.filter(u => u.is_active)
      .map(u => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join("");
  if (cur) sel.value = cur;
}

function fillActorSelect() {
  const sel = $("#actorSelect");
  if (!sel) return;
  sel.innerHTML = `<option value="">— select user —</option>` +
    STATE.users.filter(u => u.is_active)
      .map(u => `<option value="${u.id}" ${STATE.actorUserId == u.id ? "selected" : ""}>${escapeHtml(u.name)}</option>`).join("");
}

function fillAuditActorSelect() {
  const sel = $("#auditActorFilter");
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = `<option value="">All actors</option>` +
    STATE.users.map(u => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join("");
  if (cur) sel.value = cur;
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
    status:   { New: "#5a9fd4", "In Progress": "#d4a05a", Resolved: "#7ca860", Closed: "#8b8270", Reopened: "#a87fb8" },
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
  fillFormSelect(form.elements.reporter_id,
                 STATE.users.filter(u => u.is_active).map(u => [u.id, `${u.name} <${u.email}>`]),
                 bug ? (bug.reporter ? bug.reporter.id : "") : (STATE.actorUserId || ""));
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
  selEl.innerHTML = `<option value="">— select —</option>` +
    items.map(([v, lbl]) => `<option value="${v}">${escapeHtml(lbl)}</option>`).join("");
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
      payload.actor_user_id = STATE.actorUserId || payload.reporter_id;
      await api(`/bugs/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast(`Bug #${id} updated`, "success");
    } else {
      await api("/bugs", { method: "POST", body: JSON.stringify(payload) });
      toast("Bug created", "success");
    }
    closeModal("modalBug");
    await refreshAll();
  } catch (err) {
    toast(err.message, "error");
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
    toast(err.message, "error");
  }
}

function renderBugDetail(bug) {
  $("#detailTitle").textContent = `#${bug.id} — ${bug.title}`;
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
  if (ct.startsWith("image/")) {
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
  if (!STATE.actorUserId) {
    toast("Please pick 'Acting As' user in the sidebar first", "error");
    return;
  }
  const total = files.length;
  let done = 0;
  toast(`Uploading ${total} file(s)…`, "info");
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("uploader_user_id", String(STATE.actorUserId));
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
    toast(err.message, "error");
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
    form.elements.role.value = user.role || "";
    form.elements.is_active.checked = user.is_active;
  } else {
    form.elements.is_active.checked = true;
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
    role: form.elements.role.value.trim(),
    is_active: form.elements.is_active.checked,
  };
  try {
    let user;
    if (id) {
      user = await api(`/users/${id}`, { method: "PUT", body: JSON.stringify(payload) });
      toast("User updated", "success");
    } else {
      user = await api("/users", { method: "POST", body: JSON.stringify(payload) });
      toast("User created", "success");
      if (!STATE.actorUserId) {
        STATE.actorUserId = user.id;
        localStorage.setItem("actorUserId", user.id);
      }
    }
    closeModal("modalUser");
    await loadUsers();
    await refreshAll();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------------------------------------------------------------------------
// Action handlers
// ---------------------------------------------------------------------------
async function handleEditBug(bugId) {
  try {
    const bug = await api(`/bugs/${bugId}`);
    openBugForm(bug);
  } catch (err) { toast(err.message, "error"); }
}

async function handleDeleteBug(bugId) {
  const ok = await confirmDialog(`Delete bug #${bugId}? This will also delete its comments and attachments. Cannot be undone.`);
  if (!ok) return;
  try {
    await api(`/bugs/${bugId}`, { method: "DELETE" });
    toast(`Bug #${bugId} deleted`, "success");
    closeModal("modalDetail");
    await refreshAll();
  } catch (err) { toast(err.message, "error"); }
}

async function handleDeleteProject(id) {
  const project = STATE.projects.find(p => p.id === id);
  const name = project ? project.name : `#${id}`;
  const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs.`);
  if (!ok) return;
  try {
    await api(`/projects/${id}`, { method: "DELETE" });
    toast(`Project "${name}" deleted`, "success");
    if (STATE.filters.project_id == id) {
      STATE.filters.project_id = "";
      $("#filterProject").value = "";
    }
    await loadProjects();
    await refreshAll();
  } catch (err) { toast(err.message, "error"); }
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
    if (STATE.actorUserId === id) {
      STATE.actorUserId = null;
      localStorage.removeItem("actorUserId");
    }
    await loadUsers();
    await refreshAll();
  } catch (err) { toast(err.message, "error"); }
}

async function handleEditUser(id) {
  const u = STATE.users.find(x => x.id === id);
  if (u) openUserForm(u);
}

async function handleDeleteAttachment(attId) {
  const ok = await confirmDialog("Delete this attachment?");
  if (!ok) return;
  try {
    const q = STATE.actorUserId ? `?actor_user_id=${STATE.actorUserId}` : "";
    await api(`/bugs/${STATE.currentBugId}/attachments/${attId}${q}`, { method: "DELETE" });
    toast("Attachment deleted", "success");
    const bug = await api(`/bugs/${STATE.currentBugId}`);
    renderBugDetail(bug);
    await refreshBugs();
  } catch (err) { toast(err.message, "error"); }
}

async function postComment(form) {
  const body = form.elements.body.value.trim();
  if (!body) return;
  if (!STATE.actorUserId) {
    toast("Please pick 'Acting As' user in the sidebar first", "error");
    return;
  }
  try {
    const comment = await api(`/bugs/${STATE.currentBugId}/comments`, {
      method: "POST",
      body: JSON.stringify({ author_user_id: STATE.actorUserId, body }),
    });

    // Upload any attached files to this comment
    const files = form.elements.files?.files;
    if (files && files.length) {
      for (const f of files) {
        const fd = new FormData();
        fd.append("file", f);
        fd.append("uploader_user_id", String(STATE.actorUserId));
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
  } catch (err) { toast(err.message, "error"); }
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
    toast(err.message, "error");
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

  // Acting As selector
  $("#actorSelect").addEventListener("change", (e) => {
    const v = e.target.value;
    STATE.actorUserId = v ? parseInt(v, 10) : null;
    if (STATE.actorUserId) localStorage.setItem("actorUserId", STATE.actorUserId);
    else localStorage.removeItem("actorUserId");
    toast(STATE.actorUserId ? `Now acting as ${STATE.users.find(u => u.id === STATE.actorUserId)?.name}` : "Cleared actor", "info");
  });

  // Mobile hamburger
  $("#menuBtn").addEventListener("click", () => {
    $("#sidebar").classList.add("open");
    $("#sidebarBackdrop").hidden = false;
  });
  $("#sidebarBackdrop").addEventListener("click", closeSidebar);

  // Nav buttons
  $$(".nav-btn").forEach(b => b.addEventListener("click", () => { setView(b.dataset.view); closeSidebar(); }));

  // Filters
  $("#filterProject").addEventListener("change", (e) => { STATE.filters.project_id = e.target.value; STATE.page = 1; refreshBugs(); });
  $("#filterStatus").addEventListener("change", (e) => { STATE.filters.status = e.target.value; STATE.page = 1; refreshBugs(); });
  $("#filterPriority").addEventListener("change", (e) => { STATE.filters.priority = e.target.value; STATE.page = 1; refreshBugs(); });
  $("#filterEnvironment").addEventListener("change", (e) => { STATE.filters.environment = e.target.value; STATE.page = 1; refreshBugs(); });
  $("#filterAssignee").addEventListener("change", (e) => { STATE.filters.assignee_id = e.target.value; STATE.page = 1; refreshBugs(); });
  $("#clearFiltersBtn").addEventListener("click", () => {
    STATE.filters = { project_id: "", status: "", priority: "", environment: "", reporter_id: "", assignee_id: "", q: "" };
    $("#filterProject").value = ""; $("#filterStatus").value = "";
    $("#filterPriority").value = ""; $("#filterEnvironment").value = "";
    $("#filterAssignee").value = ""; $("#search").value = "";
    STATE.page = 1; refreshBugs();
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
      const pid = parseInt(li.dataset.projectId, 10);
      STATE.filters.project_id = STATE.filters.project_id == pid ? "" : String(pid);
      $("#filterProject").value = STATE.filters.project_id;
      STATE.page = 1; refreshBugs(); renderProjectList();
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
      const uid = parseInt(li.dataset.userId, 10);
      STATE.filters.assignee_id = STATE.filters.assignee_id == uid ? "" : String(uid);
      $("#filterAssignee").value = STATE.filters.assignee_id;
      STATE.page = 1; refreshBugs();
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
    // Click on backdrop (the .modal element itself, not children)
    if (e.target.classList && e.target.classList.contains("modal")) {
      e.target.hidden = true;
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
