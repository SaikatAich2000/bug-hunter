/** Analytics, Audit Trail and Sessions views through the real app shell. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, mountApp, reply } from "../test/backend";

const go = (user, view) => user.click(document.querySelector(`.nav [data-view="${view}"]`));

describe("analytics", () => {
  it("charts the stats for every category and rescopes to the active type tab", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await go(user, "analytics");
    expect(await screen.findByText("Items over the last 14 days")).toBeInTheDocument();
    const status = document.getElementById("chartStatus");
    expect(within(status).getByText("In Progress")).toBeInTheDocument();
    expect(within(document.getElementById("chartProject")).getByText("Beta")).toBeInTheDocument();
    expect(within(document.getElementById("chartAssignee")).getByText("Bob Dev")).toBeInTheDocument();
    expect(document.querySelector("#chartTimeline circle title")).toHaveTextContent("2026-01-01: 1");
    await user.click(screen.getByRole("tab", { name: /Tasks/ }));
    expect(await screen.findByText("By Status (Tasks)")).toBeInTheDocument();
    expect(document.getElementById("chartEnvironmentCard")).toHaveAttribute("hidden");
    await waitFor(() => expect(backend.calls.some((c) => c.path.startsWith("/stats?item_type=Task"))).toBe(true));
  });

  it("shows 'No data' placeholders for empty stats", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/stats/, () => ({
      bugs: 0, open: 0, resolved: 0, closed: 0, resolve_later: 0,
      by_status: {}, by_priority: {}, by_environment: {}, by_type: {}, by_project: [], by_assignee: [], timeline: [],
    }));
    await mountApp({ backend });
    await go(user, "analytics");
    await waitFor(() => expect(screen.getAllByText("No data").length).toBe(5));
    expect(screen.getByText("No assignments yet")).toBeInTheDocument();
  });
});

describe("audit trail", () => {
  const entry = (id, over = {}) => ({
    id, actor_name: "Ada Admin", action: "created", entity_type: "bug", entity_id: id, detail: `Detail ${id}`,
    created_at: "2026-01-05T10:00:00Z", ...over,
  });

  it("lists entries, filters by entity/actor/search and clears", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/audit/, () => [entry(1), entry(2, { entity_type: "", entity_id: null, detail: "" })]);
    await mountApp({ backend });
    await go(user, "audit");
    expect(await screen.findByText("Detail 1")).toBeInTheDocument();
    expect(screen.getByText("bug#1")).toBeInTheDocument();
    expect(screen.getByText(/end of history \(2 entries\)/)).toBeInTheDocument();
    await user.selectOptions(document.getElementById("auditEntityFilter"), "user");
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.get("entity_type") === "user")).toBe(true));
    await user.selectOptions(document.getElementById("auditActorFilter"), "2");
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.get("actor_user_id") === "2")).toBe(true));
    await user.type(document.getElementById("auditSearch"), "login");
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.get("q") === "login")).toBe(true));
    await user.click(document.getElementById("auditClearBtn"));
    await waitFor(() => expect(document.getElementById("auditSearch")).toHaveValue(""));
    await user.click(document.getElementById("auditRefreshBtn"));
  });

  it("pages older entries in without duplicating rows", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    const page1 = Array.from({ length: 300 }, (_, i) => entry(i + 1));
    backend.on("GET", /^\/audit/, ({ url }) => (url.searchParams.get("offset") === "0" ? page1 : [entry(300), entry(301)]));
    await mountApp({ backend });
    await go(user, "audit");
    await user.click(await screen.findByRole("button", { name: "Load older entries" }));
    await waitFor(() => expect(screen.getByText(/end of history \(301 entries\)/)).toBeInTheDocument());
    expect(backend.calls.filter((c) => c.path.startsWith("/audit")).at(-1).url.searchParams.get("offset")).toBe("300");
  });

  it("an empty log shows guidance and a failing fetch is reported", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/audit/, () => reply(500, { detail: "audit down" }));
    await mountApp({ backend });
    await go(user, "audit");
    expect(await screen.findByText("audit down")).toBeInTheDocument();
    expect(await screen.findByText("No audit events match")).toBeInTheDocument();
  });

  it("plain users are bounced away from the audit view", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true } });
    await mountApp({ backend });
    expect(document.querySelector('.nav [data-view="audit"]')).toBeNull();
    expect(user).toBeDefined();
  });
});

describe("sessions", () => {
  const session = (id, over = {}) => ({
    id, user_name: "Bob Dev", user_email: "bob@example.com", user_role: "user", is_current: false,
    user_agent: "Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537", ip_address: "10.0.0.1",
    created_at: "2026-01-05T10:00:00Z", last_seen_at: "2026-01-05T10:00:00Z", expires_at: "2026-01-06T10:00:00Z", ...over,
  });

  it("lists sessions, flags the current one and revokes another after confirming", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    let rows = [session(1, { is_current: true, user_name: "Ada Admin", user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X) Firefox/120" }), session(2), session(3, { user_name: "", user_email: "", user_role: "", user_agent: "", ip_address: "" })];
    backend.on("GET", "/sessions", () => rows);
    backend.on("DELETE", /^\/sessions\/2$/, () => { rows = rows.filter((r) => r.id !== 2); return null; });
    await mountApp({ backend });
    await go(user, "sessions");
    expect(await screen.findByText("This is you")).toBeInTheDocument();
    expect(screen.getByText("Firefox on macOS · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Chrome on Windows · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("(deleted user)")).toBeInTheDocument();
    expect(screen.getByText("Unknown · (unknown IP)")).toBeInTheDocument();
    expect(document.querySelector('[data-id="1"][data-act="revoke-session"]')).toBeDisabled();
    await user.click(document.querySelector('[data-id="2"][data-act="revoke-session"]'));
    await confirm(user, "Revoke");
    await waitFor(() => expect(backend.called("DELETE", "/sessions/2")).toHaveLength(1));
    expect(await screen.findByText("Session revoked")).toBeInTheDocument();
    await waitFor(() => expect(document.querySelector('[data-session-id="2"]')).toBeNull());
  });

  it("reports a failed revoke, refreshes on demand and shows the empty state", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    let rows = [session(2)];
    backend.on("GET", "/sessions", () => rows);
    backend.on("DELETE", /^\/sessions\/2$/, () => reply(500, { detail: "cannot revoke" }));
    await mountApp({ backend });
    await go(user, "sessions");
    await user.click(await screen.findByRole("button", { name: "Revoke" }));
    await confirm(user, "Revoke");
    expect(await screen.findByText("cannot revoke")).toBeInTheDocument();
    rows = [];
    await user.click(document.getElementById("sessionsRefreshBtn"));
    expect(await screen.findByText(/No active sessions/)).toBeInTheDocument();
  });

  it("covers the remaining browser hints", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    const agents = ["Mozilla/5.0 (iPhone) Safari/604", "Edg/120 Android", "curl/8", "python-requests/2 Linux", "PostmanRuntime/7"];
    backend.on("GET", "/sessions", () => agents.map((ua, i) => session(i + 10, { user_agent: ua })));
    await mountApp({ backend });
    await go(user, "sessions");
    expect(await screen.findByText("Safari on iOS · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Edge on Android · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("curl · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Python on Linux · 10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("Postman · 10.0.0.1")).toBeInTheDocument();
  });
});
