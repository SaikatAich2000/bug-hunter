/** Reports view: catalog, filters, run, results table (summary cards, truncation) and XLSX download. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, mountApp, reply } from "../test/backend";

const CATALOG = {
  types: [
    { key: "bugs_by_status", label: "Bugs by status", icon: "📊", description: "Counts per status", default_window: "last_30_days" },
    { key: "weekly", label: "Weekly digest", icon: "", description: "Last week", default_window: "last_7_days" },
    { key: "everything", label: "Everything", icon: "🗂", description: "No window", default_window: "all_time" },
  ],
  vocab: { item_types: ["Bug", "Task"], statuses: ["New", "Resolved"], priorities: ["High", "Low"], environments: ["DEV", "PROD"] },
};

const RESULT = {
  report_label: "Bugs by status", total: 2, truncated: false, truncated_cap: 0,
  columns: [{ key: "title", label: "Title" }, { key: "points", label: "Points", align: "right" }, { key: "flag", label: "Flag" }, { key: "extra", label: "Extra" }],
  rows: [
    { title: "Login fails", points: 3, flag: true, extra: { a: 1 } },
    { title: "Add export", points: 5, flag: false, extra: null },
  ],
  summary: { total_rows: 2, by_type: { Bug: 1, Task: 1 }, empty_breakdown: {}, note: "ok" },
};

async function openReports(user, { backend = createBackend(), catalog = CATALOG, result = RESULT } = {}) {
  backend.on("GET", "/reports/types", () => catalog);
  backend.on("POST", "/reports/run", () => result);
  backend.on("POST", "/reports/export.xlsx", () => ({ ok: true }));
  await mountApp({ backend });
  await user.click(document.querySelector('.nav [data-view="reports"]'));
  await screen.findByRole("heading", { name: "Choose a report to begin" });
  return backend;
}

afterEach(() => vi.restoreAllMocks());

describe("catalog and defaults", () => {
  it("loads the report types and vocabularies and applies the first type's date window", async () => {
    const user = userEvent.setup();
    await openReports(user);
    expect(await screen.findByLabelText("Report type")).toHaveDisplayValue("📊  Bugs by status");
    expect(document.getElementById("reportTypeHelp")).toHaveTextContent("Counts per status");
    expect(within(document.getElementById("reportItemTypes")).getByRole("checkbox", { name: "Bug" })).toBeInTheDocument();
    expect(within(document.getElementById("reportProjects")).getByRole("checkbox", { name: "Alpha" })).toBeInTheDocument();
    expect(within(document.getElementById("reportAssignees")).getByRole("checkbox", { name: "Bob Dev" })).toBeInTheDocument();
    expect(document.getElementById("reportDateTo").value).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(document.getElementById("reportDateFrom").value).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("switching type resets the window; an all-time type clears both dates and a type without an icon gets the default", async () => {
    const user = userEvent.setup();
    await openReports(user);
    const select = await screen.findByLabelText("Report type");
    expect(screen.getByRole("option", { name: /Weekly digest/ })).toHaveTextContent("📈");
    await user.selectOptions(select, "everything");
    expect(document.getElementById("reportDateFrom")).toHaveValue("");
    expect(document.getElementById("reportDateTo")).toHaveValue("");
    await user.selectOptions(select, "weekly");
    const from = new Date(document.getElementById("reportDateFrom").value);
    const to = new Date(document.getElementById("reportDateTo").value);
    expect(Math.round((to - from) / 86_400_000)).toBe(7);
  });

  it("the Last 7/30 days presets set the range", async () => {
    const user = userEvent.setup();
    await openReports(user);
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportPresetThisWeekBtn"));
    const span = () => Math.round((new Date(document.getElementById("reportDateTo").value) - new Date(document.getElementById("reportDateFrom").value)) / 86_400_000);
    expect(span()).toBe(7);
    await user.click(document.getElementById("reportPresetThisMonthBtn"));
    expect(span()).toBe(30);
  });

  it("a failed catalog load is reported and Run does nothing", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", "/reports/types", () => reply(500, { detail: "Catalog down" }));
    await mountApp({ backend });
    await user.click(document.querySelector('.nav [data-view="reports"]'));
    expect(await screen.findByText("Catalog down")).toBeInTheDocument();
    await user.click(document.getElementById("reportRunBtn"));
    expect(backend.called("POST", "/reports/run")).toHaveLength(0);
  });

  it("an empty catalog leaves nothing to run", async () => {
    const user = userEvent.setup();
    const backend = await openReports(user, { catalog: { types: [], vocab: null } });
    await user.click(document.getElementById("reportRunBtn"));
    expect(backend.called("POST", "/reports/run")).toHaveLength(0);
  });

  it("the banner jumps to the Sprints reports", async () => {
    const user = userEvent.setup();
    const backend = await openReports(user);
    backend.state.projects[0].agile_enabled = true;
    backend.state.agile = { 1: { agile_enabled: true, board_id: 9 } };
    backend.on("GET", /^\/agile\/boards\/9$/, () => ({ id: 9, project_id: 1, name: "B", estimation_mode: "story_points", columns: [], timezone: "UTC", working_weekdays: [1] }));
    await user.click(screen.getByRole("button", { name: "View Sprint Reports →" }));
    expect(await screen.findByRole("tab", { name: /Reports/, selected: true })).toBeInTheDocument();
  });
});

describe("running", () => {
  it("sends the chosen filters, then shows summary cards, rows and the result meta", async () => {
    const user = userEvent.setup();
    const backend = await openReports(user);
    await screen.findByLabelText("Report type");
    await user.click(within(document.getElementById("reportItemTypes")).getByRole("checkbox", { name: "Bug" }));
    await user.click(within(document.getElementById("reportStatuses")).getByRole("checkbox", { name: "New" }));
    await user.click(within(document.getElementById("reportPriorities")).getByRole("checkbox", { name: "High" }));
    await user.click(within(document.getElementById("reportEnvironments")).getByRole("checkbox", { name: "DEV" }));
    await user.click(within(document.getElementById("reportProjects")).getByRole("checkbox", { name: "Alpha" }));
    await user.click(within(document.getElementById("reportAssignees")).getByRole("checkbox", { name: "Bob Dev" }));
    await user.click(within(document.getElementById("reportReporters")).getByRole("checkbox", { name: "Ada Admin" }));
    await user.type(document.getElementById("reportTextSearch"), "  login ");
    await user.type(document.getElementById("reportRunLabel"), " Q2 ");
    await user.click(document.getElementById("reportIncludeNotABug"));
    await user.click(within(document.getElementById("reportItemTypes")).getByRole("checkbox", { name: "Bug" })); // toggled back off
    await user.click(document.getElementById("reportRunBtn"));
    await waitFor(() => expect(backend.called("POST", "/reports/run")).toHaveLength(1));
    const body = backend.called("POST", "/reports/run")[0].body;
    expect(body.report_key).toBe("bugs_by_status");
    expect(body.filters).toMatchObject({
      item_types: [], statuses: ["New"], priorities: ["High"], environments: ["DEV"], project_ids: [1],
      assignee_ids: [2], reporter_ids: [1], include_not_a_bug: true, text_search: "login", label: "Q2",
    });
    expect(await screen.findByRole("heading", { name: "Bugs by status" })).toBeInTheDocument();
    expect(document.getElementById("reportResultMeta")).toHaveTextContent(/2 rows · from \d{4}-\d{2}-\d{2} · to \d{4}-\d{2}-\d{2}/);
    const table = screen.getByRole("table", { name: "Report results" });
    expect(within(table).getByText("Login fails")).toBeInTheDocument();
    expect(within(table).getByText("Yes")).toBeInTheDocument();
    expect(within(table).getByText("No")).toBeInTheDocument();
    expect(within(table).getByText('{"a":1}')).toBeInTheDocument();
    expect(within(table).getByText("5")).toHaveStyle({ textAlign: "right" });
    const summary = document.getElementById("reportSummary");
    expect(summary).not.toHaveAttribute("hidden");
    expect(within(summary).getByText("total rows")).toBeInTheDocument();
    expect(within(summary).getByText("Bug").nextSibling).toHaveTextContent("1");
    expect(within(summary).getByText("—")).toBeInTheDocument();
    expect(document.getElementById("reportEmpty")).toHaveAttribute("hidden");
  });

  it("Reset filters clears chips, text and restores the type's window", async () => {
    const user = userEvent.setup();
    await openReports(user);
    await screen.findByLabelText("Report type");
    const bug = within(document.getElementById("reportItemTypes")).getByRole("checkbox", { name: "Bug" });
    await user.click(bug);
    await user.type(document.getElementById("reportTextSearch"), "x");
    await user.type(document.getElementById("reportRunLabel"), "y");
    await user.click(document.getElementById("reportPresetThisWeekBtn"));
    await user.click(document.getElementById("reportClearBtn"));
    expect(bug).not.toBeChecked();
    expect(document.getElementById("reportTextSearch")).toHaveValue("");
    expect(document.getElementById("reportRunLabel")).toHaveValue("");
    const span = Math.round((new Date(document.getElementById("reportDateTo").value) - new Date(document.getElementById("reportDateFrom").value)) / 86_400_000);
    expect(span).toBe(30);
  });

  it("no matching rows shows the empty hint; a truncated result mentions the cap", async () => {
    const user = userEvent.setup();
    await openReports(user, { result: { ...RESULT, total: 0, rows: [], summary: undefined } });
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportRunBtn"));
    await waitFor(() => expect(document.getElementById("reportEmpty")).not.toHaveAttribute("hidden"));
    expect(document.getElementById("reportSummary")).toHaveAttribute("hidden");
    expect(document.getElementById("reportResultMeta")).toHaveTextContent("0 rows");
  });

  it("truncated results say how many rows are shown", async () => {
    const user = userEvent.setup();
    await openReports(user, { result: { ...RESULT, total: 1, rows: [RESULT.rows[0]], truncated: true, truncated_cap: 1000 } });
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportRunBtn"));
    await waitFor(() => expect(document.getElementById("reportTruncated")).not.toHaveAttribute("hidden"));
    expect(document.getElementById("reportTruncatedCount")).toHaveTextContent("1000");
    expect(document.getElementById("reportResultMeta")).toHaveTextContent("1 row ·");
  });

  it("long cell values are cut at 200 characters", async () => {
    const user = userEvent.setup();
    const long = "x".repeat(250);
    await openReports(user, { result: { ...RESULT, total: 1, rows: [{ title: long, points: 1, flag: false, extra: null }] } });
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportRunBtn"));
    expect(await screen.findByText(`${"x".repeat(197)}…`)).toBeInTheDocument();
  });

  it("a failed run is reported and the loader goes away", async () => {
    const user = userEvent.setup();
    const backend = await openReports(user);
    backend.on("POST", "/reports/run", () => reply(422, { detail: "Bad filter" }));
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportRunBtn"));
    expect(await screen.findByText("Bad filter")).toBeInTheDocument();
    expect(document.getElementById("globalLoader")).toHaveAttribute("hidden");
  });
});

describe("download", () => {
  it("is disabled until a run and then exports the last run's filters", async () => {
    const user = userEvent.setup();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const backend = await openReports(user);
    await screen.findByLabelText("Report type");
    expect(document.getElementById("reportDownloadBtn")).toBeDisabled();
    await user.click(document.getElementById("reportRunBtn"));
    await waitFor(() => expect(document.getElementById("reportDownloadBtn")).toBeEnabled());
    await user.type(document.getElementById("reportTextSearch"), "changed after run");
    await user.click(document.getElementById("reportDownloadBtn"));
    await waitFor(() => expect(backend.called("POST", "/reports/export.xlsx")).toHaveLength(1));
    expect(backend.called("POST", "/reports/export.xlsx")[0].body.filters.text_search).toBeNull();
    expect(click).toHaveBeenCalledTimes(1);
  });

  it("reports an export failure", async () => {
    const user = userEvent.setup();
    const backend = await openReports(user);
    backend.on("POST", "/reports/export.xlsx", () => reply(500, { detail: "Export broke" }));
    await screen.findByLabelText("Report type");
    await user.click(document.getElementById("reportRunBtn"));
    await waitFor(() => expect(document.getElementById("reportDownloadBtn")).toBeEnabled());
    await user.click(document.getElementById("reportDownloadBtn"));
    expect(await screen.findByText("Export broke")).toBeInTheDocument();
  });
});
