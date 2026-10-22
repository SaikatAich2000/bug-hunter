/** Reports tab: every report with its inputs, empty states, charts' text alternatives and exports. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, reply } from "../../test/backend";
import { makeAgile, openSprints } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Reports", ...opts });
const pick = (user, name) => user.click(screen.getByRole("button", { name: new RegExp(`^${name}`) }));
const issue = (id, over = {}) => ({
  id, display_id: `ALP-${id}`, title: `Issue ${id}`, item_type: "Task", status: "Resolved", status_category: "done",
  estimate: 3, estimate_at_start: 3, added_during_sprint: false, ...over,
});

const day = (n, working = true) => ({
  date: `2026-01-0${n}`, start: `2026-01-0${n}T00:00:00Z`, end: `2026-01-0${n + 1}T00:00:00Z`, working,
});
const burn = (over = {}) => ({
  state: "active", start: "2026-01-01T00:00:00Z", end: "2099-01-01T00:00:00Z", planned_end: "2099-01-02T00:00:00Z",
  committed_estimate: 10, remaining_estimate: 4, completed_estimate: 6, scope_estimate: 12,
  points: [
    { at: "2026-01-01T00:00:00Z", remaining: 10, scope: 10, completed: 0 },
    { at: "2026-01-03T00:00:00Z", remaining: 6, scope: 12, completed: 6 },
  ],
  guideline: [{ at: "2026-01-01T00:00:00Z", value: 10 }, { at: "2099-01-01T00:00:00Z", value: 0 }],
  days: [day(1), day(2), day(3, false)],
  events: [
    { at: "2026-01-02T10:00:00Z", work_item_id: 301, event: "completed", display_id: "ALP-301", title: "Wire login", change: -4, remaining: 6, scope_change: false },
    { at: "2026-01-03T10:00:00Z", work_item_id: 302, event: "added", display_id: "ALP-302", title: "Late story", change: 2, remaining: 8, scope_change: true },
  ],
  ...over,
});

const sprintReport = (over = {}) => ({
  estimation_mode: "story_points", committed_estimate: 10, completed_estimate: 6, incomplete_estimate: 4,
  added_estimate: 2, removed_estimate: 1, history_complete: true,
  completed: [issue(301, { estimate_at_start: 2, added_during_sprint: true })],
  not_completed: [issue(302, { status: "In Progress", status_category: "in_progress" })],
  removed: [], completed_outside: [issue(303)], ...over,
});

function withReports(routes) {
  const backend = makeAgile(createBackend());
  backend.state.planning.sprints.push({ id: 4, name: "Sprint 4", state: "closed", sequence_number: 4, start_date: "2025-12-01", end_date: "2025-12-14", version: 2, issues: [] });
  backend.state.planning.sprints[0].sequence_number = 5;
  for (const [pattern, body] of Object.entries(routes)) backend.on("GET", new RegExp(pattern), () => (typeof body === "function" ? body() : body));
  return backend;
}

afterEach(() => vi.restoreAllMocks());

describe("burndown and burnup", () => {
  it("shows the KPIs, an accessible chart summary and the change table", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/burndown": burn() }) });
    expect(await screen.findByRole("img", { name: "Burndown chart: 4 remaining of 10 committed" })).toBeInTheDocument();
    expect(screen.getByText("Committed").previousSibling).toHaveTextContent("10");
    expect(screen.getByText("Total scope").previousSibling).toHaveTextContent("12");
    expect(await screen.findByText("Today")).toBeInTheDocument();
    expect(screen.getByText("scope change")).toBeInTheDocument();
    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByText("ALP-301 Wire login")).toBeInTheDocument();
    expect(screen.getByText(/Sprint 5 · .*· active/)).toBeInTheDocument();
  });

  it("the burnup chart plots completed against scope", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/burndown": burn() }) });
    await pick(user, "Burnup chart");
    expect(await screen.findByRole("img", { name: "Burnup chart: 6 completed of 12 in scope" })).toBeInTheDocument();
    expect(screen.getByText("Total scope", { selector: "li" })).toBeInTheDocument();
  });

  it("falls back to calendar ticks and listed non-working days without board days", async () => {
    const user = userEvent.setup();
    const data = burn({ days: undefined, non_working_days: ["2026-01-03"], events: [], guideline: [], state: "closed" });
    await open(user, { backend: withReports({ "reports/burndown": data }) });
    expect(await screen.findByRole("img", { name: /Burndown chart/ })).toBeInTheDocument();
    expect(screen.getByText("No changes since the sprint started")).toBeInTheDocument();
    expect(screen.queryByText("Today")).not.toBeInTheDocument();
  });

  it("an unstarted sprint has nothing to chart", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/burndown": burn({ start: null, points: [] }) }) });
    expect(await screen.findByText("This sprint has not started, so there is nothing to chart yet")).toBeInTheDocument();
  });

  it("a different sprint can be picked", async () => {
    const user = userEvent.setup();
    const backend = withReports({ "reports/burndown": burn() });
    await open(user, { backend });
    await screen.findByRole("img", { name: /Burndown/ });
    await user.selectOptions(screen.getByLabelText("Sprint"), "4");
    await waitFor(() => expect(backend.calls.some((c) => c.path.includes("burndown?sprint_id=4"))).toBe(true));
    expect(screen.getByRole("option", { name: "Sprint 5 (active)" })).toBeInTheDocument();
  });
});

describe("sprint report", () => {
  it("shows the KPIs and each issue group, flagging late additions and estimate changes", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/sprint/5": sprintReport() }) });
    await pick(user, "Sprint report");
    expect(await screen.findByText("Committed at start")).toBeInTheDocument();
    expect(screen.getByText("Completed vs committed").previousSibling).toHaveTextContent("60%");
    expect(screen.getByRole("heading", { name: /Completed issues/ })).toHaveTextContent("(1)");
    expect(screen.getByText("2 → 3")).toBeInTheDocument();
    expect(screen.getByTitle("Added after the sprint started")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Issues removed from sprint/ })).toHaveTextContent("(0)");
    expect(screen.getByRole("heading", { name: /completed outside of this sprint/ })).toBeInTheDocument();
    expect(screen.queryByText(/before full change history/)).not.toBeInTheDocument();
  });

  it("explains incomplete history and skips the estimate columns on item-count boards", async () => {
    const user = userEvent.setup();
    const data = sprintReport({ history_complete: false, estimation_mode: "item_count", committed_estimate: 0, completed_outside: [] });
    await open(user, { backend: withReports({ "reports/sprint/5": data }) });
    await pick(user, "Sprint report");
    expect(await screen.findByText(/started before full change history was recorded/)).toBeInTheDocument();
    expect(screen.queryByText("Completed vs committed")).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Estimate" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /completed outside/ })).not.toBeInTheDocument();
  });

  it("opens an issue from its key and exports to Excel", async () => {
    const user = userEvent.setup();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const backend = withReports({ "reports/sprint/5$": sprintReport(), "export.xlsx": { ok: true } });
    await open(user, { backend });
    await pick(user, "Sprint report");
    await user.click(await screen.findByRole("button", { name: "ALP-302" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/302")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "⬇ Excel" }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(backend.called("GET", "/agile/reports/sprint/5/export.xlsx")).toHaveLength(1);
  });
});

describe("board reports", () => {
  it("velocity lists recent sprints with the average", async () => {
    const user = userEvent.setup();
    const data = {
      average_completed: 8, points: [
        { sprint_id: 4, sprint_name: "Sprint 4", start_date: "2025-12-01", end_date: "2025-12-14", committed_estimate: 10, completed_estimate: 8 },
        { sprint_id: 3, sprint_name: "A really long sprint name here", start_date: null, end_date: null, committed_estimate: 6, completed_estimate: 8 },
      ],
    };
    await open(user, { backend: withReports({ "reports/velocity": data }) });
    await pick(user, "Velocity chart");
    expect(await screen.findByRole("img", { name: "Velocity chart, average 8 completed per sprint" })).toBeInTheDocument();
    expect(screen.getAllByText("Sprint 4").length).toBeGreaterThan(0);
    expect(screen.getAllByText("A really long s…").length).toBeGreaterThan(0);
  });

  it("velocity without sprints says so", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/velocity": { average_completed: 0, points: [] } }) });
    await pick(user, "Velocity chart");
    expect(await screen.findByText("No completed sprints yet")).toBeInTheDocument();
  });

  it("cumulative flow stacks the columns over the date range", async () => {
    const user = userEvent.setup();
    const data = {
      columns: [{ id: 1, name: "To Do", category: "todo" }, { id: 2, name: "Doing", category: "in_progress" }, { id: 3, name: "Done", category: "done" }],
      points: [{ date: "2026-01-01", counts: { 1: 4, 2: 1 } }, { date: "2026-01-02", counts: { 1: 2, 2: 2, 3: 1 } }],
    };
    const backend = withReports({ "cumulative-flow": data });
    await open(user, { backend });
    await pick(user, "Cumulative flow diagram");
    expect(await screen.findByRole("img", { name: "Cumulative flow diagram" })).toBeInTheDocument();
    expect(screen.getAllByText("Doing").length).toBeGreaterThan(0);
    expect(backend.calls.some((c) => c.path.includes("cumulative-flow") && c.path.includes("include_subtasks=true"))).toBe(true);
  });

  it("cumulative flow with no data says so", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "cumulative-flow": { columns: [], points: [] } }) });
    await pick(user, "Cumulative flow diagram");
    expect(await screen.findByText("No data for this range")).toBeInTheDocument();
  });

  it("the control chart summarises cycle times; none completed says so", async () => {
    const user = userEvent.setup();
    const data = {
      count: 2, average_days: 3.5, median_days: 3.5, min_days: 2, max_days: 5, work_columns: ["Doing", "Testing"],
      entries: [
        { work_item_id: 1, display_id: "ALP-1", title: "One", completed_at: "2026-01-02T00:00:00Z", cycle_time_days: 2 },
        { work_item_id: 2, display_id: "ALP-2", title: "Two", completed_at: "2026-01-09T00:00:00Z", cycle_time_days: 5 },
      ],
      rolling_average: [{ completed_at: "2026-01-02T00:00:00Z", value: 2 }, { completed_at: "2026-01-09T00:00:00Z", value: 3.5 }],
    };
    const backend = withReports({ "control-chart": data });
    await open(user, { backend });
    await pick(user, "Control chart");
    expect(await screen.findByRole("img", { name: "Control chart: average cycle time 3.5 days over 2 issues" })).toBeInTheDocument();
    expect(screen.getByText("Min–max (days)").previousSibling).toHaveTextContent("2–5");
    expect(screen.getByText(/spent in Doing, Testing/)).toBeInTheDocument();
    backend.on("GET", /control-chart/, () => ({ ...data, entries: [], work_columns: [] }));
    await user.click(screen.getByRole("button", { name: /^Cumulative flow/ }));
    await pick(user, "Control chart");
    expect(await screen.findByText("No issues were completed in this range")).toBeInTheDocument();
    expect(screen.getByText(/spent in the working columns/)).toBeInTheDocument();
  });

  it("an inverted date range asks for a valid one and sends nothing", async () => {
    const user = userEvent.setup();
    const backend = withReports({ "cumulative-flow": { columns: [], points: [] } });
    await open(user, { backend });
    await pick(user, "Cumulative flow diagram");
    await screen.findByText("No data for this range");
    const before = backend.called("GET", /cumulative-flow/).length;
    await user.click(document.querySelectorAll(".ag-report-head .bh-date-btn")[1]);
    for (let i = 0; i < 3; i += 1) await user.click(screen.getByRole("button", { name: "Previous month" }));
    await user.click(document.querySelector(".bh-date-cell:not(.muted)"));
    expect(await screen.findByText("Pick a start date on or before the end date")).toBeInTheDocument();
    expect(backend.called("GET", /cumulative-flow/)).toHaveLength(before);
  });
});

describe("epic, daily and workload", () => {
  it("the epic report lists progress, sprints and the issue groups", async () => {
    const user = userEvent.setup();
    const data = {
      estimation_mode: "story_points", progress: { child_count: 4, completed_child_count: 2, total_estimate: 12, completed_estimate: 6, unestimated_count: 1 },
      sprints: [
        { sprint_id: 4, sprint_name: "Sprint 4", state: "closed", completed_estimate: 3, completed_count: 1, scope_estimate: 12, remaining_estimate: 9 },
        { sprint_id: 5, sprint_name: "Sprint 5", state: "active", completed_estimate: 3, completed_count: 1, scope_estimate: 12, remaining_estimate: 6 },
      ],
      done: [issue(1)], in_progress: [issue(2, { status: "In Progress", status_category: "in_progress" })], todo: [],
    };
    const backend = withReports({ "reports/epic/40": data });
    await open(user, { backend });
    await pick(user, "Epic report");
    expect(await screen.findByText("2/4")).toBeInTheDocument();
    expect(screen.getByText("6/12")).toBeInTheDocument();
    expect(screen.getByText("Sprint 5 (active)")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^Done/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "ALP-2" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/2")).toHaveLength(1));
  });

  it("an epic nobody has worked on yet says so; with no epics it asks to create one", async () => {
    const user = userEvent.setup();
    const data = { estimation_mode: "item_count", progress: { child_count: 0, completed_child_count: 0, total_estimate: 0, completed_estimate: 0, unestimated_count: 0 }, sprints: [], done: [], in_progress: [], todo: [] };
    const backend = withReports({ "reports/epic/40": data });
    await open(user, { backend });
    await pick(user, "Epic report");
    expect(await screen.findByText("No sprint has worked on this epic yet")).toBeInTheDocument();
    expect(screen.queryByText("Estimate done")).not.toBeInTheDocument();
  });

  it("without epics the epic report asks to create one", async () => {
    const user = userEvent.setup();
    const backend = withReports({});
    backend.state.planning.epics = [];
    await open(user, { backend });
    await pick(user, "Epic report");
    expect(await screen.findByText("Create an epic to see this report")).toBeInTheDocument();
  });

  it("the daily summary shows counts, the day's items and what moved", async () => {
    const user = userEvent.setup();
    const data = {
      todo_count: 2, in_progress_count: 1, testing_count: 0, done_count: 3, blocked_count: 1,
      completed_estimate: 6, remaining_estimate: 4, completed_estimate_today: 2, scope_added_today: 1, scope_removed_today: 0,
      items: [{ work_item_id: 301, display_id: "ALP-301", title: "Wire login", status: "Resolved", column: "Done", story_points: 3 }],
      transitions: [{ occurred_at: "2026-01-02T10:00:00Z", work_item_id: 301, title: "Wire login", event_type: "status_changed", detail: "In Progress -> Resolved" }],
      blockers: [{ work_item_id: 302, title: "Fix export", blocked_reason: "Waiting on API" }],
    };
    const backend = withReports({ "reports/daily": data });
    await open(user, { backend });
    await pick(user, "Daily summary");
    expect(await screen.findByText("Blocked or flagged")).toBeInTheDocument();
    expect(screen.getByText("status changed")).toBeInTheDocument();
    expect(screen.getByText("Fix export — Waiting on API")).toBeInTheDocument();
    expect(screen.getByText(/Completed this day:/)).toHaveTextContent("Completed this day: 2");
    expect(backend.calls.some((c) => c.path.includes("daily?sprint_id=5&report_date="))).toBe(true);
  });

  it("a quiet day says nothing changed", async () => {
    const user = userEvent.setup();
    const data = {
      todo_count: 0, in_progress_count: 0, testing_count: 0, done_count: 0, blocked_count: 0, completed_estimate: 0, remaining_estimate: 0,
      completed_estimate_today: 0, scope_added_today: 0, scope_removed_today: 0, items: [], transitions: [], blockers: [],
    };
    await open(user, { backend: withReports({ "reports/daily": data }) });
    await pick(user, "Daily summary");
    expect(await screen.findByText("Nothing changed")).toBeInTheDocument();
    expect(screen.queryByText("Blocked or flagged now")).not.toBeInTheDocument();
  });

  it("workload shows load per person, flags overload and counts the unassigned", async () => {
    const user = userEvent.setup();
    const data = {
      entries: [
        { user_id: 2, user_name: "Bob Dev", assigned_count: 3, assigned_estimate: 12, capacity_value: 8, utilization_pct: 150 },
        { user_id: 1, user_name: "Ada Admin", assigned_count: 1, assigned_estimate: 2, capacity_value: null, utilization_pct: null },
      ],
      unassigned_count: 2, unassigned_estimate: 5,
    };
    await open(user, { backend: withReports({ "reports/workload": data }) });
    await pick(user, "Workload");
    const body = await screen.findByRole("region", { name: "Workload" });
    const bob = within(body).getByText("Bob Dev").closest("tr");
    expect(bob).toHaveClass("ag-over");
    expect(within(bob).getByText("150%")).toBeInTheDocument();
    expect(within(within(body).getByText("Ada Admin").closest("tr")).getAllByText("–")).toHaveLength(2);
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("an empty workload says nobody has work", async () => {
    const user = userEvent.setup();
    await open(user, { backend: withReports({ "reports/workload": { entries: [], unassigned_count: 0, unassigned_estimate: 0 } }) });
    await pick(user, "Workload");
    expect(await screen.findByText("Nobody has work in this sprint yet")).toBeInTheDocument();
  });
});

describe("empty states, exports and failures", () => {
  it("sprint reports without a started sprint say so", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.planning.sprints = backend.state.planning.sprints.filter((s) => s.state === "future");
    await open(user, { backend });
    expect(await screen.findByText("Start a sprint to see this report")).toBeInTheDocument();
  });

  it("exports the current report as CSV through the server", async () => {
    const user = userEvent.setup();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const backend = withReports({ "reports/burndown": burn() });
    backend.on("POST", /reports\/export/, () => ({ ok: true }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "⬇ CSV" }));
    await waitFor(() => expect(backend.called("POST", "/agile/reports/export")).toHaveLength(1));
    expect(backend.called("POST", "/agile/reports/export")[0].path).toContain("report_key=burndown&sprint_id=5");
    await waitFor(() => expect(click).toHaveBeenCalled());
  });

  it("a failed export is reported", async () => {
    const user = userEvent.setup();
    const backend = withReports({ "reports/burndown": burn() });
    backend.on("POST", /reports\/export/, () => reply(500, { detail: "Export failed" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "⬇ CSV" }));
    expect(await screen.findByText("Export failed")).toBeInTheDocument();
  });

  it("the daily summary has no CSV export; the board-scoped ones do", async () => {
    const user = userEvent.setup();
    const backend = withReports({ "reports/velocity": { average_completed: 1, points: [] }, "reports/daily": { todo_count: 0, in_progress_count: 0, testing_count: 0, done_count: 0, blocked_count: 0, completed_estimate: 0, remaining_estimate: 0, completed_estimate_today: 0, scope_added_today: 0, scope_removed_today: 0, items: [], transitions: [], blockers: [] } });
    backend.on("POST", /reports\/export/, () => ({ ok: true }));
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await open(user, { backend });
    await pick(user, "Velocity chart");
    await user.click(await screen.findByRole("button", { name: "⬇ CSV" }));
    await waitFor(() => expect(backend.called("POST", "/agile/reports/export")[0].path).toContain("report_key=velocity&board_id=9"));
    await pick(user, "Daily summary");
    await screen.findByText("Nothing changed");
    expect(screen.queryByRole("button", { name: "⬇ CSV" })).not.toBeInTheDocument();
  });

  it("a failing report load is reported", async () => {
    const user = userEvent.setup();
    const backend = withReports({});
    backend.on("GET", /reports\/burndown/, () => reply(500, { detail: "Report unavailable" }));
    await open(user, { backend });
    expect(await screen.findByText("Report unavailable")).toBeInTheDocument();
  });
});
