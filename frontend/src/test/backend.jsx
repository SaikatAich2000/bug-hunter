/** Test support: an in-memory fake of the Bug Hunter HTTP API plus a full-app mount.
 *
 * The app code under test is real (AppProvider, Shell, views, modals, lib/api); only the
 * network edge (`fetch`) is replaced. Tests register extra/overriding routes with
 * `backend.on(method, pattern, handler)` and assert on `backend.calls`.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { expect, vi } from "vitest";
import { AppProvider } from "../state/AppContext";
import App from "../App";

export const ADMIN = { id: 1, name: "Ada Admin", email: "ada@example.com", role: "admin", is_active: true };
const NOW = "2026-01-05T10:00:00Z";

export const reply = (status, body) => ({ __reply: true, status, body });
export const empty = () => reply(204, null);

export function makeBug(over = {}) {
  const id = over.id ?? 101;
  return {
    id,
    display_id: `PRJ-${id}`,
    project_id: 1,
    project_name: "Alpha",
    title: `Item ${id}`,
    description: "Something is broken",
    reporter: { id: 1, name: "Ada Admin", email: "ada@example.com" },
    assignees: [{ id: 2, name: "Bob Dev", email: "bob@example.com" }],
    item_type: "Bug",
    status: "New",
    priority: "High",
    environment: "DEV",
    due_date: null,
    event_id: null,
    event_name: null,
    created_at: NOW,
    updated_at: NOW,
    version: 1,
    attachment_count: 0,
    can_edit: true,
    epic_id: null,
    parent_id: null,
    story_points: null,
    owner_id: null,
    mandatory: false,
    blocked: false,
    blocked_reason: "",
    sprint_id: null,
    sprint_name: null,
    ...over,
  };
}

export function baseData() {
  return {
    users: [
      { ...ADMIN, created_at: NOW, updated_at: NOW, project_ids: [] },
      { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true, created_at: NOW, updated_at: NOW, project_ids: [1] },
      { id: 3, name: "Cy Manager", email: "cy@example.com", role: "manager", is_active: true, created_at: NOW, updated_at: NOW, project_ids: [] },
    ],
    projects: [
      { id: 1, name: "Alpha", description: "First", color: "#3366ff", created_at: NOW, updated_at: NOW, agile_enabled: false },
      { id: 2, name: "Beta", description: "Second", color: "#ff6633", created_at: NOW, updated_at: NOW, agile_enabled: false },
    ],
    bugs: [
      makeBug({ id: 101, title: "Login fails" }),
      makeBug({ id: 102, title: "Add export", item_type: "Requirement", status: "In Progress", priority: "Medium", project_id: 2, project_name: "Beta", assignees: [], attachment_count: 2 }),
      makeBug({ id: 103, title: "Write docs", item_type: "Task", status: "Resolved", priority: "Low", due_date: "2026-02-01", event_id: 7, event_name: "Launch" }),
    ],
    notifications: [],
    events: [],
  };
}

export function makeEvent(over = {}) {
  return {
    id: 7, name: "Launch", description: "Go live", scheduled_for: "2026-02-01", project_id: 1,
    project_name: "Alpha", created_by_name: "Ada Admin", item_count: 1, assignee_count: 1,
    managers: [], items: [], ...over,
  };
}

export const STATS = {
  bugs: 3, open: 2, resolved: 1, closed: 0, resolve_later: 0,
  by_status: { New: 1, "In Progress": 1, Resolved: 1 },
  by_priority: { High: 1, Medium: 1, Low: 1 },
  by_environment: { DEV: 3 },
  by_type: { Bug: 1, Requirement: 1, Task: 1 },
  by_project: [{ id: 1, name: "Alpha", color: "#3366ff", count: 1 }, { id: 2, name: "Beta", color: "#ff6633", count: 2 }],
  by_assignee: [{ id: 2, name: "Bob Dev", count: 1 }],
  timeline: [{ date: "2026-01-01", count: 1 }, { date: "2026-01-02", count: 2 }],
};

function jsonResponse(status, body) {
  const text = body === null || body === undefined ? "" : JSON.stringify(body);
  return new Response(status === 204 ? null : text, { status, headers: { "Content-Type": "application/json" } });
}

export function createBackend({ me = ADMIN, data = baseData() } = {}) {
  const calls = [];
  const custom = [];
  const state = { ...data, me };

  const builtin = [
    ["GET", "/auth/me", () => state.me],
    ["GET", "/health", () => ({ status: "ok", version: "9.9.9", asset_version: "v1" })],
    ["GET", "/meta", () => ({
      statuses: ["New", "In Progress", "Testing", "Resolved", "Closed"],
      statuses_by_type: {},
      priorities: ["Low", "Medium", "High", "Critical"],
      environments: ["DEV", "UAT", "PROD"],
      item_types: ["Bug", "Requirement", "Task"],
    })],
    ["GET", "/users", () => state.users],
    ["GET", "/projects", () => state.projects],
    ["GET", /^\/bugs\?/, ({ url }) => {
      const t = url.searchParams.getAll("item_type");
      const items = state.bugs.filter((b) => !t.length || t.includes(b.item_type));
      return { items, page: 1, page_size: 20, total: items.length, pages: 1 };
    }],
    ["GET", /^\/bugs\/(\d+)$/, ({ match }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      return bug ? { comments: [], activities: [], attachments: [], links: [], ...bug } : reply(404, { detail: "Not found" });
    }],
    ["POST", "/bugs", ({ body }) => {
      const id = 500 + state.bugs.length;
      const project = state.projects.find((x) => x.id === body.project_id);
      const bug = makeBug({ ...body, id, project_name: project?.name ?? "", assignees: (body.assignee_ids ?? []).map((i) => state.users.find((u) => u.id === i)).filter(Boolean) });
      state.bugs.push(bug);
      return bug;
    }],
    ["PUT", /^\/bugs\/(\d+)$/, ({ match, body }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      if (!bug) return reply(404, { detail: "Not found" });
      const { assignee_ids, ...rest } = body;
      delete rest.expected_version;
      Object.assign(bug, rest, { version: bug.version + 1 });
      if (assignee_ids) bug.assignees = assignee_ids.map((i) => state.users.find((u) => u.id === i)).filter(Boolean);
      return bug;
    }],
    ["DELETE", /^\/bugs\/(\d+)$/, ({ match }) => {
      state.bugs = state.bugs.filter((b) => b.id !== Number(match[1]));
      return empty();
    }],
    ["POST", /^\/bugs\/(\d+)\/comments$/, ({ match, body }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      const c = { id: 900 + (bug.comments?.length ?? 0), bug_id: bug.id, author_user_id: 1, author_name: "Ada Admin", body: body.body, created_at: NOW, attachments: [] };
      bug.comments = [...(bug.comments ?? []), c];
      return c;
    }],
    ["PUT", /^\/bugs\/(\d+)\/comments\/(\d+)$/, ({ match, body }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      const c = bug.comments.find((x) => x.id === Number(match[2]));
      c.body = body.body;
      return c;
    }],
    ["DELETE", /^\/bugs\/(\d+)\/comments\/(\d+)$/, ({ match }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      bug.comments = bug.comments.filter((x) => x.id !== Number(match[2]));
      return empty();
    }],
    ["POST", /^\/bugs\/(\d+)\/attachments$/, ({ match }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      const a = { id: 700 + (bug.attachments?.length ?? 0), filename: "up.png", content_type: "image/png", size_bytes: 10, uploader_user_id: 1, uploader_name: "Ada Admin", comment_id: null, created_at: NOW };
      bug.attachments = [...(bug.attachments ?? []), a];
      return a;
    }],
    ["DELETE", /^\/bugs\/(\d+)\/attachments\/(\d+)$/, ({ match }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      bug.attachments = bug.attachments.filter((x) => x.id !== Number(match[2]));
      return empty();
    }],
    ["POST", /^\/bugs\/(\d+)\/links$/, ({ match, body }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      const other = state.bugs.find((b) => b.id === body.target_bug_id);
      if (!other) return reply(404, { detail: "Target not found" });
      const l = { id: 800 + (bug.links?.length ?? 0), link_type: body.link_type, direction: "outgoing", label: body.link_type === "blocks" ? "blocks" : "relates to", other_bug_id: other.id, other_bug_title: other.title, other_bug_status: other.status, other_bug_item_type: other.item_type, created_at: NOW };
      bug.links = [...(bug.links ?? []), l];
      return l;
    }],
    ["DELETE", /^\/bugs\/(\d+)\/links\/(\d+)$/, ({ match }) => {
      const bug = state.bugs.find((b) => b.id === Number(match[1]));
      bug.links = bug.links.filter((x) => x.id !== Number(match[2]));
      return empty();
    }],
    ["GET", /^\/agile\/projects\/(\d+)\/settings$/, ({ match }) => state.agile?.[Number(match[1])] ?? { agile_enabled: false, board_id: null }],
    ["GET", /^\/git\/work-items\/(\d+)\/branches$/, () => ({ project_id: 1, branches: [], can_create_branch: false, reason: "" })],
    ["GET", /^\/stats/, () => STATS],
    ["GET", "/notifications/unread_count", () => ({ unread: state.notifications.filter((n) => !n.read_at).length })],
    ["GET", /^\/notifications\?/, () => state.notifications],
    ["POST", /^\/notifications\/(\d+)\/read$/, ({ match }) => {
      const n = state.notifications.find((x) => x.id === Number(match[1]));
      if (n) n.read_at = NOW;
      return empty();
    }],
    ["POST", "/notifications/read-all", () => { state.notifications.forEach((n) => { n.read_at = NOW; }); return empty(); }],
    ["DELETE", /^\/notifications\/(\d+)$/, ({ match }) => {
      state.notifications = state.notifications.filter((n) => n.id !== Number(match[1]));
      return empty();
    }],
    ["GET", "/events", () => state.events.map(({ items, ...e }) => ({ ...e, item_count: items.length }))],
    ["GET", /^\/events\/(\d+)$/, ({ match }) => state.events.find((e) => e.id === Number(match[1])) ?? reply(404, { detail: "No such event" })],
    ["POST", "/events", ({ body }) => {
      const ev = makeEvent({ id: 50 + state.events.length, ...body, managers: [], items: [], item_count: 0 });
      state.events.push(ev);
      return ev;
    }],
    ["PUT", /^\/events\/(\d+)$/, ({ match, body }) => {
      const ev = state.events.find((e) => e.id === Number(match[1]));
      Object.assign(ev, body);
      return ev;
    }],
    ["DELETE", /^\/events\/(\d+)$/, ({ match }) => {
      state.events = state.events.filter((e) => e.id !== Number(match[1]));
      return empty();
    }],
    ["GET", "/push/config", () => ({ enabled: false })],
    ["POST", "/auth/logout", () => empty()],
  ];

  const matches = (route, method, path) => {
    const [m, p] = route;
    if (m !== method) return null;
    if (typeof p === "string") return p === path.split("?")[0] ? [path] : null;
    return p.exec(path);
  };

  const handler = vi.fn(async (input, init = {}) => {
    const full = typeof input === "string" ? input : input.url;
    const path = full.replace(/^\/api/, "");
    const method = (init.method || "GET").toUpperCase();
    let body = init.body;
    if (typeof body === "string") {
      try { body = JSON.parse(body); } catch { /* raw text body */ }
    }
    const url = new URL(path, "https://test.local");
    const call = { method, path, body, url };
    calls.push(call);
    for (const route of [...custom, ...builtin]) {
      const match = matches(route, method, path);
      if (!match) continue;
      const out = await route[2]({ ...call, match, state });
      if (out?.__reply) return jsonResponse(out.status, out.body);
      return jsonResponse(out === undefined || out === null ? 204 : 200, out);
    }
    return jsonResponse(404, { detail: `no fake route: ${method} ${path}` });
  });

  return {
    state,
    calls,
    fetch: handler,
    /** Register a route that takes priority over defaults (and earlier custom routes). */
    on(method, pattern, fn) {
      custom.unshift([method, pattern, typeof fn === "function" ? fn : () => fn]);
      return this;
    },
    called(method, pattern) {
      return calls.filter((c) => c.method === method && (typeof pattern === "string" ? c.path.split("?")[0] === pattern : pattern.test(c.path)));
    },
  };
}

/** Install a backend as global fetch and mount the real app on it. */
export async function mountApp(opts = {}) {
  const backend = opts.backend ?? createBackend(opts);
  vi.stubGlobal("fetch", backend.fetch);
  const utils = render(
    <AppProvider me={backend.state.me}>
      <App />
    </AppProvider>,
  );
  // Boot is done once the list has loaded.
  if (opts.waitFor !== false) {
    await waitFor(() => expect(backend.called("GET", /^\/bugs\?/).length).toBeGreaterThan(0));
    await screen.findByText(opts.empty ? /showing 0 of 0/ : /showing [1-9]\d* of/);
  }
  return { backend, ...utils };
}

/** Click a button of the shared confirm dialog (Delete, Cancel, Remove, Log out ...). */
export async function confirm(user, label = "Delete") {
  const dlg = document.getElementById("modalConfirm");
  await user.click(await within(dlg).findByRole("button", { name: label }));
}

/** Record every message the single #toast element is given (later toasts overwrite earlier ones). */
export function recordToasts() {
  const el = document.getElementById("toast");
  const log = [];
  const real = Object.getOwnPropertyDescriptor(Node.prototype, "textContent");
  Object.defineProperty(el, "textContent", {
    configurable: true,
    get: () => real.get.call(el),
    set: (v) => { log.push(v); real.set.call(el, v); },
  });
  return log;
}
