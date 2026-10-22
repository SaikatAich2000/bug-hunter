/** Backlog: sprints above the ranked backlog, filters, selection, menu/drag moves and the sprint dialogs. */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, reply } from "../../test/backend";
import { card, makeAgile, openSprints, planningData } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Backlog", ...opts });
const sprint = (name) => screen.getByRole("region", { name });

describe("backlog layout", () => {
  it("lists sprints with their issues, totals and the backlog below", async () => {
    const user = userEvent.setup();
    await open(user);
    expect(await within(sprint("Sprint 5")).findByText("Wire login")).toBeInTheDocument();
    expect(within(sprint("Sprint 5")).getByText("Active")).toBeInTheDocument();
    expect(within(sprint("Sprint 5")).getByText(/🎯 Ship it/)).toBeInTheDocument();
    expect(within(sprint("Sprint 5")).getByText(/days? remaining/)).toBeInTheDocument();
    expect(within(sprint("Sprint 5")).getByText("2 issues")).toBeInTheDocument();
    expect(within(sprint("Sprint 6")).getByText("1 issue")).toBeInTheDocument();
    expect(within(sprint("Backlog")).getByText("Backlog idea")).toBeInTheDocument();
    expect(within(sprint("Backlog")).getByText("2 issues")).toBeInTheDocument();
    expect(screen.getByText(/15 planned in total/)).toBeInTheDocument();
  });

  it("says when only the top of a long backlog is shown", async () => {
    const user = userEvent.setup();
    await open(user, { backend: makeAgile(createBackend(), { planning: planningData({ backlog_total: 240 }) }) });
    expect(await screen.findByText(/Showing the top 2 issues by rank/)).toBeInTheDocument();
  });

  it("empty lists explain what to do", async () => {
    const user = userEvent.setup();
    const planning = planningData({ backlog: [], backlog_total: 0 });
    planning.sprints[1].issues = [];
    await open(user, { backend: makeAgile(createBackend(), { planning }) });
    expect(await screen.findByText("Your backlog is empty")).toBeInTheDocument();
    expect(screen.getByText(/Plan this sprint by dragging issues here/)).toBeInTheDocument();
    expect(within(sprint("Backlog")).getByText("0 issues")).toBeInTheDocument();
  });

  it("collapses and expands a sprint", async () => {
    const user = userEvent.setup();
    await open(user);
    await screen.findByText("Plan retro");
    await user.click(screen.getByRole("button", { name: "Collapse Sprint 6" }));
    expect(screen.queryByText("Plan retro")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand Sprint 6" }));
    expect(screen.getByText("Plan retro")).toBeInTheDocument();
  });

  it("read-only users see no drag handles, sprint buttons or planning actions", async () => {
    const user = userEvent.setup();
    await open(user, { role: "user" });
    await screen.findByText("Wire login");
    expect(screen.queryByRole("button", { name: /^Move ALP/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create sprint" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start sprint" })).not.toBeInTheDocument();
    expect(screen.getByText("Only admins and managers can re-rank or plan issues")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Actions for ALP-301" }));
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
  });

  it("opens an issue from its key or title", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "ALP-305" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/305")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "Another idea" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/306")).toHaveLength(1));
  });

  it("+ Create issue opens the item form", async () => {
    const user = userEvent.setup();
    await open(user);
    await screen.findByText("Wire login");
    await user.click(within(sprint("Sprint 6")).getByRole("button", { name: "+ Create issue" }));
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
    expect(document.getElementById("modalBugTitle")).toHaveTextContent("New Item");
  });
});

describe("filters", () => {
  it("filters by text, epic, assignee and type and clears them", async () => {
    const user = userEvent.setup();
    const planning = planningData();
    planning.backlog[1].assignees = [{ id: 2, name: "Bob Dev" }];
    await open(user, { backend: makeAgile(createBackend(), { planning }) });
    await screen.findByText("Backlog idea");
    await user.type(screen.getByRole("searchbox", { name: "Search issues" }), "another");
    expect(screen.queryByText("Backlog idea")).not.toBeInTheDocument();
    expect(screen.getAllByText("No issues match the filter").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("Backlog idea")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Filter by epic"), "40");
    expect(screen.queryByText("Another idea")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Filter by epic"), "none");
    expect(screen.queryByText("Backlog idea")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Filter by epic"), "");

    await user.selectOptions(screen.getByLabelText("Filter by assignee"), "2");
    expect(screen.queryByText("Wire login")).not.toBeInTheDocument();
    expect(screen.getByText("Another idea")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Filter by assignee"), "unassigned");
    expect(screen.queryByText("Another idea")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Filter by assignee"), "");

    await user.selectOptions(screen.getByLabelText("Filter by type"), "Bug");
    expect(screen.getByText("Another idea")).toBeInTheDocument();
    expect(screen.queryByText("Wire login")).not.toBeInTheDocument();
  });
});

describe("moving issues", () => {
  it("moves an issue to another sprint, the backlog, or the top/bottom of its list from its menu", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await screen.findByText("Backlog idea");
    await user.click(screen.getByRole("button", { name: "Actions for ALP-305" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to Sprint 6" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(1));
    expect(backend.called("POST", "/agile/boards/9/move")[0].body).toMatchObject({ item_ids: [305], sprint_id: 6, after_id: null });

    await user.click(screen.getByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to backlog" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(2));
    expect(backend.called("POST", "/agile/boards/9/move")[1].body).toMatchObject({ item_ids: [301], sprint_id: null });

    await user.click(screen.getByRole("button", { name: "Actions for ALP-306" }));
    await user.click(await screen.findByRole("menuitem", { name: "Top of this list" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(3));
    await user.click(screen.getByRole("button", { name: "Actions for ALP-306" }));
    await user.click(await screen.findByRole("menuitem", { name: "Bottom of this list" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(4));
    expect(backend.called("POST", "/agile/boards/9/move")[3].body.before_id).not.toBeNull();
  });

  it("selected issues move together and the selection is then cleared", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await screen.findByText("Backlog idea");
    await user.click(screen.getByRole("checkbox", { name: "Select ALP-305" }));
    await user.click(screen.getByRole("checkbox", { name: "Select ALP-306" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Actions for ALP-305" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to Sprint 6" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(1));
    expect(backend.called("POST", "/agile/boards/9/move")[0].body.item_ids).toEqual([305, 306]);
    await waitFor(() => expect(screen.queryByText("2 selected")).not.toBeInTheDocument());
  });

  it("Clear drops the selection without moving anything", async () => {
    const user = userEvent.setup();
    await open(user);
    await user.click(await screen.findByRole("checkbox", { name: "Select ALP-305" }));
    await user.click(within(screen.getByText("1 selected")).getByRole("button", { name: "Clear" }));
    expect(screen.queryByText("1 selected")).not.toBeInTheDocument();
  });

  it("a failed move is reported and the lists reload", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /\/move$/, () => reply(409, { detail: "Rank conflict" }));
    await open(user, { backend });
    await screen.findByText("Backlog idea");
    const before = backend.called("GET", /planning/).length;
    await user.click(screen.getByRole("button", { name: "Actions for ALP-305" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to Sprint 6" }));
    expect(await screen.findByText("Rank conflict")).toBeInTheDocument();
    await waitFor(() => expect(backend.called("GET", /planning/).length).toBeGreaterThan(before));
  });
});

/** jsdom has no layout: stack the rows so a dragged row's centre can land on another. */
function layoutRows() {
  const real = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function box() {
    if (this.classList?.contains("ag-row")) {
      const at = [...document.querySelectorAll(".ag-row:not(.ag-row-overlay)")].indexOf(this);
      const top = at * 100;
      return { x: 0, y: top, left: 0, top, width: 300, height: 50, right: 300, bottom: top + 50, toJSON() {} };
    }
    return real.call(this);
  };
  return () => { Element.prototype.getBoundingClientRect = real; };
}

describe("dragging", () => {
  async function drag(from, dy, { release = true } = {}) {
    fireEvent.mouseDown(from, { clientX: 5, clientY: 5, button: 0 });
    fireEvent.mouseMove(document, { clientX: 5, clientY: 15 });
    fireEvent.mouseMove(document, { clientX: 5, clientY: 5 + dy });
    await Promise.resolve();
    if (release) fireEvent.mouseUp(document, { clientX: 5, clientY: 5 + dy });
    await Promise.resolve();
  }

  it("dragging an issue onto another sprint's rows sends the move", async () => {
    const restore = layoutRows();
    try {
      const user = userEvent.setup();
      const backend = await open(user);
      const row = await screen.findByRole("group", { name: "ALP-301 Wire login" });
      // rows: 301,302 (Sprint 5), 304 (Sprint 6), 305,306 (backlog): 301 is dragged three rows down
      await drag(row, 300);
      await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(1));
      const body = backend.called("POST", "/agile/boards/9/move")[0].body;
      expect(body.item_ids).toEqual([301]);
      expect(body.sprint_id).not.toBe(5);
    } finally { restore(); }
  });

  it("a cancelled drag sends nothing", async () => {
    const restore = layoutRows();
    try {
      const user = userEvent.setup();
      const backend = await open(user);
      await drag(await screen.findByRole("group", { name: "ALP-305 Backlog idea" }), 100, { release: false });
      fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
      await Promise.resolve();
      expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(0);
      expect(screen.getByRole("group", { name: "ALP-305 Backlog idea" })).toBeInTheDocument();
    } finally { restore(); }
  });

  it("dropping a backlog issue on a row sends exactly one move for it", async () => {
    const restore = layoutRows();
    try {
      const user = userEvent.setup();
      const backend = await open(user);
      await drag(await screen.findByRole("group", { name: "ALP-305 Backlog idea" }), 100);
      await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(1));
      expect(backend.called("POST", "/agile/boards/9/move")[0].body).toMatchObject({ item_ids: [305] });
    } finally { restore(); }
  });

  it("a dragged selection moves together", async () => {
    const restore = layoutRows();
    try {
      const user = userEvent.setup();
      const backend = await open(user);
      await user.click(await screen.findByRole("checkbox", { name: "Select ALP-305" }));
      await user.click(screen.getByRole("checkbox", { name: "Select ALP-306" }));
      await drag(screen.getByRole("group", { name: "ALP-305 Backlog idea" }), -100);
      await waitFor(() => expect(backend.called("POST", "/agile/boards/9/move")).toHaveLength(1));
      expect(backend.called("POST", "/agile/boards/9/move")[0].body.item_ids).toEqual([305, 306]);
    } finally { restore(); }
  });
});

describe("sprint dialogs", () => {
  it("creates a sprint with a name, duration and goal", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Create sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Create sprint" });
    const name = within(dlg).getByRole("textbox", { name: /Sprint name/ });
    expect(name).toHaveValue("Alpha Sprint 3");
    await user.clear(name);
    await user.type(name, "Q1 push");
    await user.type(within(dlg).getByRole("textbox", { name: "Sprint goal" }), "Ship v2");
    await user.click(within(dlg).getByRole("button", { name: "Start date" }));
    await user.click(document.querySelector('.bh-date-cell:not(.muted)[data-iso$="-10"]'));
    await user.selectOptions(within(dlg).getByLabelText("Duration"), "3");
    await user.click(within(dlg).getByRole("button", { name: "Create" }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints")).toHaveLength(1));
    const body = backend.called("POST", "/agile/sprints")[0].body;
    expect(body).toMatchObject({ name: "Q1 push", goal: "Ship v2" });
    expect(body.start_date).toMatch(/-10$/);
    expect(body.end_date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(await screen.findByText("Sprint created")).toBeInTheDocument();
  });

  it("an end date before the start disables the form and says why", async () => {
    const user = userEvent.setup();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Create sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Create sprint" });
    await user.click(within(dlg).getByRole("button", { name: "Start date" }));
    await user.click(document.querySelector('.bh-date-cell:not(.muted)[data-iso$="-20"]'));
    await user.click(within(dlg).getByRole("button", { name: "End date" }));
    await user.click(document.querySelector('.bh-date-cell:not(.muted)[data-iso$="-05"]'));
    expect(within(dlg).getByRole("alert")).toHaveTextContent("The end date cannot be before the start date");
    expect(within(dlg).getByRole("button", { name: "Create" })).toBeDisabled();
    expect(within(dlg).getByLabelText("Duration")).toHaveValue("custom");
    await user.click(within(dlg).getAllByRole("button", { name: "Clear date" })[1]);
    expect(within(dlg).queryByRole("alert")).not.toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(document.getElementById("sprintFormDialog")).toHaveAttribute("hidden"));
  });

  it("a server error keeps the dialog open and is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /^\/agile\/sprints/, () => reply(400, { detail: "Name already used" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Create sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Create sprint" });
    await user.click(within(dlg).getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Name already used")).toBeInTheDocument();
    expect(within(dlg).getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("edits a sprint (with its version); an active sprint must keep its dates", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Sprint actions for Sprint 6" }));
    await user.click(await screen.findByRole("menuitem", { name: "Edit sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Edit sprint: Sprint 6" });
    const name = within(dlg).getByRole("textbox", { name: /Sprint name/ });
    await user.clear(name);
    await user.type(name, "Sprint Six");
    await user.click(within(dlg).getByRole("button", { name: "Update" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/sprints/6")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/sprints/6")[0].body).toMatchObject({ name: "Sprint Six", version: 1, start_date: null });

    await user.click(screen.getByRole("button", { name: "Sprint actions for Sprint 5" }));
    await user.click(await screen.findByRole("menuitem", { name: "Edit sprint" }));
    const active = await screen.findByRole("dialog", { name: "Edit sprint: Sprint 5" });
    await user.click(within(active).getAllByRole("button", { name: "Clear date" })[0]);
    expect(within(active).getByRole("alert")).toHaveTextContent("An active sprint needs a start and an end date");
  });

  it("starts a future sprint from the planning view", async () => {
    const user = userEvent.setup();
    const planning = planningData();
    planning.sprints = [planning.sprints[1]];
    planning.sprints[0].issues = [card({ id: 304, story_points: null }), card({ id: 307, story_points: 2 })];
    const backend = await open(user, { backend: makeAgile(createBackend(), { planning }) });
    await user.click(await screen.findByRole("button", { name: "Start sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Start sprint" });
    expect(within(dlg).getByText(/will be included in this sprint/)).toHaveTextContent("2 issues will be included in this sprint (2 pts)");
    expect(within(dlg).getByText("1 not estimated")).toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Start" }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints/6/start")).toHaveLength(1));
    const body = backend.called("POST", "/agile/sprints/6/start")[0].body;
    expect(body).toMatchObject({ name: "Sprint 6", version: 1 });
    expect(body.start_date).toMatch(/^\d{4}-/);
    expect(await screen.findByText("Sprint 6 started")).toBeInTheDocument();
  });

  it("Start is disabled while another sprint is active", async () => {
    const user = userEvent.setup();
    await open(user);
    const btn = await within(sprint("Sprint 6")).findByRole("button", { name: "Start sprint" });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", "Complete the active sprint first");
  });

  it("an empty future sprint cannot be started yet", async () => {
    const user = userEvent.setup();
    const planning = planningData();
    planning.sprints = [planning.sprints[1]];
    planning.sprints[0].issues = [];
    await open(user, { backend: makeAgile(createBackend(), { planning }) });
    expect(await screen.findByRole("button", { name: "Start sprint" })).toHaveAttribute("title", "Add issues first");
  });

  it("Start with a missing date is blocked", async () => {
    const user = userEvent.setup();
    const planning = planningData();
    planning.sprints = [planning.sprints[1]];
    await open(user, { backend: makeAgile(createBackend(), { planning }) });
    await user.click(await screen.findByRole("button", { name: "Start sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Start sprint" });
    await user.click(within(dlg).getAllByRole("button", { name: "Clear date" })[1]);
    expect(within(dlg).getByRole("alert")).toHaveTextContent("Pick the sprint's dates");
    expect(within(dlg).getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("completes the active sprint with the default destination and per-issue overrides", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await within(sprint("Sprint 5")).findByRole("button", { name: "Complete sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Complete Sprint 5" });
    expect(within(dlg).getByText("open issues")).toBeInTheDocument();
    await user.selectOptions(within(dlg).getByLabelText("Move open issues to"), "sprint:6");
    await user.selectOptions(within(dlg).getByLabelText("Destination for Fix export"), "new");
    await user.type(within(dlg).getByPlaceholderText("Next default sprint name"), "Next up");
    await user.click(within(dlg).getByRole("button", { name: "Complete sprint" }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints/5/complete")).toHaveLength(1));
    expect(backend.called("POST", "/agile/sprints/5/complete")[0].body).toMatchObject({
      default_destination: "sprint", default_target_sprint_id: 6, new_sprint_name: "Next up",
      dispositions: [{ item_id: 302, destination: "new_sprint", target_sprint_id: null }],
    });
    expect(await screen.findByText("Sprint 5 completed")).toBeInTheDocument();
  });

  it("a completion error is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /complete$/, () => reply(409, { detail: "Sprint changed" }));
    await open(user, { backend });
    await user.click(await within(sprint("Sprint 5")).findByRole("button", { name: "Complete sprint" }));
    const dlg = await screen.findByRole("dialog", { name: "Complete Sprint 5" });
    await user.click(within(dlg).getByRole("button", { name: "Complete sprint" }));
    expect(await screen.findByText("Sprint changed")).toBeInTheDocument();
  });

  it("deleting a sprint asks first and cancelling leaves it alone", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Sprint actions for Sprint 6" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete sprint" }));
    const dlg = document.getElementById("modalConfirm");
    expect(dlg).toHaveTextContent("Delete Sprint 6?");
    await user.click(within(dlg).getByRole("button", { name: "Cancel" }));
    expect(backend.called("POST", /cancel$/)).toHaveLength(0);
  });

  it("confirming deletion cancels the sprint and says so", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Sprint actions for Sprint 6" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete sprint" }));
    await user.click(within(document.getElementById("modalConfirm")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints/6/cancel")).toHaveLength(1));
    expect(await screen.findByText("Sprint 6 deleted")).toBeInTheDocument();
  });
});
