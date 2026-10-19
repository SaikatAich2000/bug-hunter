/** Active-sprint board: header totals, quick filters, swimlanes, card menu moves, WIP confirm, complete. */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, confirm, reply } from "../../test/backend";
import { boardView, card, makeAgile, openSprints } from "../../test/agile";

const stamp = (hoursAgo) => new Date(Date.now() - hoursAgo * 3600_000).toISOString();

describe("board header and columns", () => {
  it("shows the sprint, goal, totals and a card per column with WIP counts", async () => {
    const user = userEvent.setup();
    await openSprints(user);
    expect(await screen.findByRole("heading", { name: "Sprint 5" })).toBeInTheDocument();
    expect(screen.getByText(/🎯 Ship it/)).toBeInTheDocument();
    expect(await screen.findByText(/3 of 6 pts done/)).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "ALP-301 Wire login, New" })).toBeInTheDocument();
    expect(screen.getByText(/· max 1/)).toBeInTheDocument();
    expect(screen.getByText(/· min 1/)).toBeInTheDocument();
    expect(screen.getByTitle("Not estimated")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Flagged" })).toBeInTheDocument();
  });

  it("opens an issue from its key or title", async () => {
    const user = userEvent.setup();
    const backend = await openSprints(user);
    await user.click(await screen.findByRole("button", { name: "Wire login" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/301")).toHaveLength(1));
  });

  it("explains an empty board and links to the backlog", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend(), { view: boardView({ sprint: null, columns: [] }) });
    await openSprints(user, { backend });
    expect(await screen.findByText("No active sprint")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Go to backlog" }));
    expect(await screen.findByRole("heading", { name: "Backlog" })).toBeInTheDocument();
  });

  it("warns about issues whose status is on no column and a manager can map them", async () => {
    const user = userEvent.setup();
    const view = boardView({ unmapped: [card({ id: 399, status: "Testing" }), card({ id: 398, status: "Closed" })] });
    await openSprints(user, { backend: makeAgile(createBackend(), { view }) });
    expect(await screen.findByText(/2 issues are not shown because/)).toHaveTextContent("Testing, Closed");
    await user.click(screen.getByRole("button", { name: "Map it in Board settings" }));
    expect(await screen.findByRole("tab", { name: /Board settings/, selected: true })).toBeInTheDocument();
  });

  it("a single unmapped issue reads in the singular and non-managers get no link", async () => {
    const user = userEvent.setup();
    const view = boardView({ unmapped: [card({ id: 399, status: "Testing" })] });
    await openSprints(user, { backend: makeAgile(createBackend(), { view }), role: "user" });
    expect(await screen.findByText(/1 issue is not shown because its status/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Map it in Board settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Complete sprint" })).not.toBeInTheDocument();
  });

  it("switches between parallel active sprints", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend(), { view: boardView({ active_sprints: [{ id: 5, name: "Sprint 5" }, { id: 8, name: "Sprint 8" }] }) });
    await openSprints(user, { backend });
    await user.selectOptions(await screen.findByLabelText("Active sprint"), "8");
    await waitFor(() => expect(backend.calls.some((c) => c.path.includes("/view?sprint_id=8"))).toBe(true));
  });
});

describe("quick filters and swimlanes", () => {
  it("filters to my issues and recently updated ones and reports hidden cards", async () => {
    const user = userEvent.setup();
    const view = boardView();
    view.columns[0].cards[0].updated_at = stamp(1);
    view.columns[1].cards[0].updated_at = stamp(72);
    await openSprints(user, { backend: makeAgile(createBackend(), { view }) });
    await user.click(await screen.findByRole("button", { name: "Only my issues" }));
    expect(screen.getByText("2 cards hidden by quick filters.")).toBeInTheDocument();
    expect(screen.queryByText("Fix export")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Recently updated" }));
    expect(screen.getByText("Wire login")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByText("Fix export")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Recently updated" }));
    expect(screen.getByText("1 card hidden by quick filters.")).toBeInTheDocument();
  });

  it("applies custom quick filters saved on the board", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.quickFilters = [{ id: 77, name: "Flagged only", filter_json: { flagged: true } }];
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Flagged only" }));
    expect(screen.queryByText("Wire login")).not.toBeInTheDocument();
    expect(screen.getByText("Ship docs")).toBeInTheDocument();
  });

  it("groups cards into collapsible swimlanes", async () => {
    const user = userEvent.setup();
    await openSprints(user);
    await screen.findByText("Wire login");
    await user.selectOptions(screen.getByLabelText("Group by"), "assignee");
    const lane = await screen.findByRole("region", { name: "Ada Admin" });
    expect(within(lane).getByText("Wire login")).toBeInTheDocument();
    await user.click(within(lane).getByRole("button", { name: /Ada Admin/ }));
    expect(screen.queryByText("Wire login")).not.toBeInTheDocument();
    await user.click(within(lane).getByRole("button", { name: /Ada Admin/ }));
    expect(screen.getByText("Wire login")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Group by"), "priority");
    expect(await screen.findByRole("region", { name: "High" })).toBeInTheDocument();
  });
});

describe("moving and flagging cards", () => {
  it("moves a card to another column from its actions menu", async () => {
    const user = userEvent.setup();
    const backend = await openSprints(user);
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to Resolved (Done)" }));
    await waitFor(() => expect(backend.called("POST", "/agile/work-items/301/transition")).toHaveLength(1));
    expect(backend.called("POST", "/agile/work-items/301/transition")[0].body).toMatchObject({ to_status: "Resolved", version: 1, acknowledged: false });
  });

  it("flags and unflags an issue", async () => {
    const user = userEvent.setup();
    const backend = await openSprints(user);
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Add flag" }));
    await waitFor(() => expect(backend.called("POST", "/agile/work-items/301/flag")[0].body).toEqual({ flagged: true, version: 1 }));
    await user.click(screen.getByRole("button", { name: "Actions for ALP-303" }));
    await user.click(await screen.findByRole("menuitem", { name: "Remove flag" }));
    await waitFor(() => expect(backend.called("POST", "/agile/work-items/303/flag")[0].body).toEqual({ flagged: false, version: 1 }));
  });

  it("a failed flag is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /flag$/, () => reply(409, { detail: "Version conflict" }));
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Add flag" }));
    expect(await screen.findByText("Version conflict")).toBeInTheDocument();
  });

  it("opens the issue from the menu; read-only cards only offer that", async () => {
    const user = userEvent.setup();
    const view = boardView();
    view.columns[0].cards[0].can_edit = false;
    const backend = await openSprints(user, { backend: makeAgile(createBackend(), { view }) });
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
    await user.click(screen.getByRole("menuitem", { name: "Open issue" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/301")).toHaveLength(1));
  });

  it("a rejected move rolls the card back and reports the error", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /transition$/, () => reply(400, { detail: "Not allowed" }));
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to Resolved (Done)" }));
    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "ALP-301 Wire login, New" })).toBeInTheDocument();
  });

  it("a WIP-limit warning asks to move anyway and retries acknowledged", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let first = true;
    backend.on("POST", /transition$/, () => {
      if (first) { first = false; return reply(400, { detail: "In Progress is at its limit; acknowledge to proceed." }); }
      return {};
    });
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to In Progress (In Progress)" }));
    await confirm(user, "Move anyway");
    await waitFor(() => expect(backend.called("POST", "/agile/work-items/301/transition")).toHaveLength(2));
    expect(backend.called("POST", "/agile/work-items/301/transition")[1].body.acknowledged).toBe(true);
  });

  it("declining the WIP warning puts the card back", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /transition$/, () => reply(400, { detail: "In Progress is at its limit; acknowledge to proceed." }));
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Actions for ALP-301" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to In Progress (In Progress)" }));
    await confirm(user, "Cancel");
    await waitFor(() => expect(screen.getByRole("group", { name: "ALP-301 Wire login, New" })).toBeInTheDocument());
    expect(backend.called("POST", "/agile/work-items/301/transition")).toHaveLength(1);
  });
});

describe("completing the sprint", () => {
  it("opens the dialog with the future sprints and sends the completion", async () => {
    const user = userEvent.setup();
    const backend = await openSprints(user);
    await user.click(await screen.findByRole("button", { name: "Complete sprint" }));
    expect(await screen.findByRole("dialog", { name: /Complete/ })).toBeInTheDocument();
    expect(backend.calls.some((c) => c.path.startsWith("/agile/sprints?board_id=9"))).toBe(true);
    await user.click(within(screen.getByRole("dialog", { name: /Complete/ })).getByRole("button", { name: /^Complete sprint$/ }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints/5/complete")).toHaveLength(1));
  });

  it("still opens when the sprint list cannot be loaded", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/sprints\?/, () => reply(500, { detail: "no sprints" }));
    await openSprints(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Complete sprint" }));
    expect(await screen.findByText("no sprints")).toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: /Complete/ })).toBeInTheDocument();
  });
});

/** jsdom has no layout: give the columns' cells fixed boxes so the pointer can land on one. */
function layoutCells(xOf) {
  const real = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function box() {
    const label = this.getAttribute?.("aria-label") ?? "";
    if (this.classList?.contains("ag-cell")) {
      const x = xOf(label);
      return { x, y: 0, left: x, top: 0, width: 100, height: 400, right: x + 100, bottom: 400, toJSON() {} };
    }
    return real.call(this);
  };
  return () => { Element.prototype.getBoundingClientRect = real; };
}

describe("dragging cards", () => {
  const X = { "To Do column": 0, "In Progress column": 200, "Done column": 400 };

  async function drag(from, toX, { release = true } = {}) {
    fireEvent.mouseDown(from, { clientX: 5, clientY: 5, button: 0 });
    fireEvent.mouseMove(document, { clientX: 40, clientY: 20 });
    fireEvent.mouseMove(document, { clientX: toX, clientY: 20 });
    if (release) fireEvent.mouseUp(document, { clientX: toX, clientY: 20 });
    await Promise.resolve();
  }

  it("dropping a card on another column transitions it to that column's first status", async () => {
    const restore = layoutCells((l) => X[l]);
    try {
      const user = userEvent.setup();
      const backend = await openSprints(user);
      const dragged = await screen.findByRole("group", { name: "ALP-301 Wire login, New" });
      await drag(dragged, 450);
      await waitFor(() => expect(backend.called("POST", "/agile/work-items/301/transition")).toHaveLength(1));
      expect(backend.called("POST", "/agile/work-items/301/transition")[0].body.to_status).toBe("Resolved");
    } finally { restore(); }
  });

  it("dropping on the card's own column does nothing; cancelling a drag does nothing", async () => {
    const restore = layoutCells((l) => X[l]);
    try {
      const user = userEvent.setup();
      const backend = await openSprints(user);
      const dragged = await screen.findByRole("group", { name: "ALP-301 Wire login, New" });
      await drag(dragged, 50);
      await drag(dragged, 450, { release: false });
      expect(await screen.findAllByText("ALP-301")).not.toHaveLength(0);
      fireEvent.keyDown(document, { key: "Escape", code: "Escape" });
      await Promise.resolve();
      expect(backend.called("POST", /transition/)).toHaveLength(0);
    } finally { restore(); }
  });

  it("dropping on a column with no mapped status explains how to fix it", async () => {
    const restore = layoutCells((l) => X[l]);
    try {
      const user = userEvent.setup();
      const view = boardView();
      view.columns[2].statuses = [];
      const backend = await openSprints(user, { backend: makeAgile(createBackend(), { view }) });
      const dragged = await screen.findByRole("group", { name: "ALP-301 Wire login, New" });
      await drag(dragged, 450);
      expect(await screen.findByText("Done has no status mapped to it. Map one in Board settings.")).toBeInTheDocument();
      expect(backend.called("POST", /transition/)).toHaveLength(0);
    } finally { restore(); }
  });

  it("dropping outside every column is ignored", async () => {
    const restore = layoutCells(() => 1000);
    try {
      const user = userEvent.setup();
      const backend = await openSprints(user);
      const dragged = await screen.findByRole("group", { name: "ALP-301 Wire login, New" });
      await drag(dragged, 5);
      expect(backend.called("POST", /transition/)).toHaveLength(0);
    } finally { restore(); }
  });
});
