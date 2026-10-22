/** Full-app behaviour of the shell: list, filters, sidebar, profile menu and notifications,
 * driven through the real AppProvider against a fake HTTP backend. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, empty, makeEvent, mountApp, reply } from "../test/backend";

const confirmOk = async (user) => user.click(await screen.findByRole("button", { name: "Delete", hidden: false }));

describe("list view", () => {
  it("shows every work item with its type, status and assignee", async () => {
    await mountApp();
    const table = screen.getByRole("table", { name: "Work items" });
    expect(within(table).getByText(/Login fails/)).toBeInTheDocument();
    expect(within(table).getByText(/Add export/)).toBeInTheDocument();
    expect(within(table).getByText("In Progress")).toBeInTheDocument();
    expect(screen.getByText("showing 3 of 3")).toBeInTheDocument();
    expect(screen.getAllByText("Bob Dev").length).toBeGreaterThan(0);
  });

  it("switching to the Task tab asks the API for tasks only", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.click(screen.getByRole("tab", { name: /Tasks/ }));
    await waitFor(() => expect(backend.calls.some((c) => c.path.includes("item_type=Task"))).toBe(true));
    expect(screen.queryByText("Login fails")).not.toBeInTheDocument();
    expect(screen.getByText("Write docs")).toBeInTheDocument();
    // Task tab shows due date column
    expect(screen.getByText("2026-02-01")).toBeInTheDocument();
  });

  it("search is debounced and sent as q", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.type(screen.getByRole("searchbox"), "login");
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.get("q") === "login")).toBe(true));
  });

  it("bulk-sets status for the selected rows and sends the row versions", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/bulk", () => ({ message: "2 updated", updated: 2 }));
    await mountApp({ backend });
    await user.click(screen.getByLabelText("Select item #101"));
    await user.click(screen.getByLabelText("Select item #102"));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Set status for selected"), "Resolved");
    await waitFor(() => expect(backend.called("POST", "/bugs/bulk")).toHaveLength(1));
    expect(backend.called("POST", "/bugs/bulk")[0].body).toMatchObject({
      action: "set_status", value: "Resolved", ids: [101, 102], expected_versions: { 101: 1, 102: 1 },
    });
    await waitFor(() => expect(screen.queryByText("2 selected")).not.toBeInTheDocument());
  });

  it("select-all then clear toggles every row", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByLabelText("Select all on this page"));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Select all on this page"));
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Select item #103"));
    await user.click(document.querySelector(".bulk-clear"));
    expect(screen.getByLabelText("Select item #103")).not.toBeChecked();
  });

  it("admin deletes a bug after confirming; a failed delete shows the server error", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    let first = true;
    backend.on("DELETE", /^\/bugs\/101$/, () => {
      if (first) { first = false; return reply(409, { detail: "Has links" }); }
      return empty();
    });
    await mountApp({ backend });
    const row = document.querySelector('tr[data-bug-id="101"]');
    await user.click(within(row).getByTitle("Delete"));
    await confirmOk(user);
    expect(await screen.findByText("Has links")).toBeInTheDocument();
    await user.click(within(document.querySelector('tr[data-bug-id="101"]')).getByTitle("Delete"));
    await confirmOk(user);
    await waitFor(() => expect(screen.getByText("Bug #101 deleted")).toBeInTheDocument());
  });
});

describe("sidebar", () => {
  it("clicking a project swatch filters the list by that project", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.click(screen.getByRole("button", { name: "Toggle filter: Beta" }));
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.getAll("project_id").includes("2"))).toBe(true));
    await user.click(screen.getByRole("button", { name: "Toggle filter: Beta" }));
    await waitFor(() => expect(backend.calls.at(-1).url.searchParams.getAll("project_id")).toEqual([]));
  });

  it("filters by assignee and clears all filters", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.click(screen.getByRole("button", { name: "Toggle filter: Bob Dev" }));
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.getAll("assignee_id").includes("2"))).toBe(true));
    await user.click(document.getElementById("clearFiltersBtn"));
    await waitFor(() => expect(backend.calls.at(-1).url.searchParams.getAll("assignee_id")).toEqual([]));
  });

  it("keyboard activation works on the project label (opens the project editor for managers)", async () => {
    const user = userEvent.setup();
    await mountApp();
    const label = screen.getAllByTitle("Alpha — click to edit")[0];
    label.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByDisplayValue("Alpha")).toBeInTheDocument();
  });

  it("admin deletes a project and a user", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("DELETE", /^\/projects\/2$/, () => empty());
    backend.on("DELETE", /^\/users\/2$/, () => empty());
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="delete-project"][data-id="2"]'));
    await confirmOk(user);
    await waitFor(() => expect(screen.getByText('Project "Beta" deleted')).toBeInTheDocument());
    await user.click(document.querySelector('[data-act="delete-user"][data-id="2"]'));
    await confirmOk(user);
    await waitFor(() => expect(screen.getByText('User "Bob Dev" deleted')).toBeInTheDocument());
  });

  it("collapse toggle persists to localStorage and flips body class", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByTitle("Collapse sidebar"));
    expect(document.body.classList.contains("sidebar-collapsed")).toBe(true);
    expect(localStorage.getItem("sidebarCollapsed")).toBe("1");
    await user.click(screen.getByTitle("Expand sidebar"));
    expect(document.body.classList.contains("sidebar-collapsed")).toBe(false);
  });

  it("mobile menu opens the drawer and the close button hides it again", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    expect(document.getElementById("sidebar")).toHaveClass("open");
    await user.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(document.getElementById("sidebar")).not.toHaveClass("open");
  });
});

describe("role gating", () => {
  it("a plain user sees neither Reports nor Sessions nav entries", async () => {
    await mountApp({ me: { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true } });
    expect(document.querySelector('.nav [data-view="reports"]')).toBeNull();
    expect(document.querySelector('.nav [data-view="sessions"]')).toBeNull();
    expect(document.getElementById("newProjectBtn")).toBeNull();
  });
});

describe("profile menu", () => {
  it("toggles theme, opens change-password and help", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
    const before = document.documentElement.dataset.theme;
    await user.click(screen.getByRole("menuitem", { name: /theme/ }));
    expect(document.documentElement.dataset.theme).not.toBe(before);
    expect(localStorage.getItem("theme")).toBe(document.documentElement.dataset.theme);
    await user.click(screen.getByRole("menuitem", { name: /Change password/ }));
    expect(await screen.findByLabelText(/Current password/i)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(screen.getByRole("menuitem", { name: /Help center/ }));
    expect(await screen.findByRole("heading", { name: /Help/i })).toBeInTheDocument();
  });

  it("Escape and outside click close the menu", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(document.body);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("log out asks for confirmation then calls the API", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(screen.getByRole("menuitem", { name: /Log out/ }));
    await user.click(await screen.findByRole("button", { name: "Log out", hidden: false }));
    await waitFor(() => expect(backend.called("POST", "/auth/logout")).toHaveLength(1));
  });

  it("declining the log-out confirmation keeps the session", async () => {
    const user = userEvent.setup();
    const { backend } = await mountApp();
    await user.click(screen.getByRole("button", { name: "Account menu" }));
    await user.click(screen.getByRole("menuitem", { name: /Log out/ }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(backend.called("POST", "/auth/logout")).toHaveLength(0);
  });
});

describe("notifications", () => {
  const notes = () => [
    { id: 1, kind: "assigned", title: "Assigned to you", body: "Login fails", bug_id: 101, event_id: null, actor_name: "Cy", read_at: null, created_at: "2026-01-05T10:00:00Z" },
    { id: 2, kind: "event", title: "Event soon", body: "", bug_id: null, event_id: 7, actor_name: "Cy", read_at: null, created_at: "2026-01-05T09:00:00Z" },
  ];

  it("shows the unread badge, lists items, opens a bug notification and marks it read", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.notifications = notes();
    await mountApp({ backend });
    const bell = await screen.findByRole("button", { name: "Notifications, 2 unread" });
    await user.click(bell);
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    expect(within(dialog).getByText("Assigned to you")).toBeInTheDocument();
    await user.click(within(dialog).getByText("Assigned to you"));
    await waitFor(() => expect(backend.called("POST", "/notifications/1/read")).toHaveLength(1));
    expect(await screen.findByDisplayValue("Login fails")).toBeInTheDocument();
  });

  it("mark all read, dismiss, and an empty panel", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.notifications = notes();
    await mountApp({ backend });
    await user.click(await screen.findByRole("button", { name: /Notifications, 2 unread/ }));
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    await user.click(within(dialog).getAllByLabelText("Dismiss notification")[0]);
    await waitFor(() => expect(backend.called("DELETE", "/notifications/1")).toHaveLength(1));
    await user.click(within(dialog).getByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(backend.called("POST", "/notifications/read-all")).toHaveLength(1));
    await user.click(within(dialog).getByLabelText("Dismiss notification"));
    expect(await within(dialog).findByText("You're all caught up")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Notifications" })).not.toBeInTheDocument();
  });

  it("an event notification routes to the Events view", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.notifications = notes();
    backend.state.events = [makeEvent({ id: 7, name: "Launch" })];
    await mountApp({ backend });
    await user.click(await screen.findByRole("button", { name: /Notifications, 2 unread/ }));
    const dialog = await screen.findByRole("dialog", { name: "Notifications" });
    const row = within(dialog).getByText("Event soon");
    row.closest("[role=button]").focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Launch", { selector: "#eventDetailName" })).toBeInTheDocument();
    expect(backend.called("GET", "/events/7")).toHaveLength(1);
  });
});
