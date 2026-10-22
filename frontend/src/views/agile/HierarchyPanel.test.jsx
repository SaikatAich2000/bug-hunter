/** Epics tab: the epic tree with progress, searching, re-linking issues, archiving and editing an epic. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, reply } from "../../test/backend";
import { hierarchyData, makeAgile, openSprints } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Epics", ...opts });

describe("epic tree", () => {
  it("shows each epic with status, progress, counts and dates; empty archived epics stay hidden", async () => {
    const user = userEvent.setup();
    await open(user);
    const onboarding = (await screen.findByText("Onboarding")).closest("li");
    expect(within(onboarding).getByRole("img", { name: "1 done, 1 in progress, 2 to do" })).toBeInTheDocument();
    expect(within(onboarding).getByText(/1\/4 issues · 3\/12 pts/)).toBeInTheDocument();
    expect(within(onboarding).getByText("2026-01-01 → 2026-03-01")).toBeInTheDocument();
    const billing = screen.getByText("Billing").closest("li");
    expect(within(billing).getByText("No issues yet")).toBeInTheDocument();
    expect(screen.getByText("Old stuff")).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(screen.queryByText("Dead epic")).not.toBeInTheDocument();
    expect(screen.getByText("1 issue")).toBeInTheDocument();
  });

  it("expands an epic to its issues, then an issue to its sub-tasks", async () => {
    const user = userEvent.setup();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Expand Onboarding" }));
    expect(screen.getByText("Signup form")).toBeInTheDocument();
    expect(screen.getByTitle("In Sprint 5")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show sub-tasks of ALP-310" }));
    expect(screen.getByText("Validate email")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide sub-tasks of ALP-310" }));
    expect(screen.queryByText("Validate email")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Collapse Onboarding" }));
    expect(screen.queryByText("Signup form")).not.toBeInTheDocument();
  });

  it("an epic with no issues says so and issues without an epic are listed on demand", async () => {
    const user = userEvent.setup();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "Expand Billing" }));
    expect(screen.getByText("No issues in this epic")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand issues without epic" }));
    expect(screen.getByText("Loose story")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Collapse issues without epic" }));
    expect(screen.queryByText("Loose story")).not.toBeInTheDocument();
  });

  it("says how many of a long tail of loose issues are listed", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.hierarchy = hierarchyData({ unparented_total: 40 });
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Expand issues without epic" }));
    expect(screen.getByText("Showing 1 of 40. Search to find the others")).toBeInTheDocument();
  });

  it("with no epics and no search, explains what epics are", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.hierarchy = hierarchyData({ epics: [], issues: [], subtasks: [], unparented_total: 0 });
    await open(user, { backend });
    expect(await screen.findByText(/No epics yet/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand issues without epic" }));
    expect(screen.getByText("None.")).toBeInTheDocument();
  });

  it("searches (debounced) and toggles done issues, both sent to the server", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.type(await screen.findByRole("searchbox", { name: "Search issues" }), "signup");
    await waitFor(() => expect(backend.calls.some((c) => c.path.includes("hierarchy") && c.path.includes("q=signup"))).toBe(true));
    await user.click(screen.getByRole("checkbox", { name: "Show done issues" }));
    await waitFor(() => expect(backend.calls.some((c) => c.path.includes("include_done=false"))).toBe(true));
  });
});

describe("epic actions", () => {
  async function menu(user, label) {
    await user.click(await screen.findByRole("button", { name: label }));
  }

  it("moves an issue to another epic or out of its epic", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Expand Onboarding" }));
    await menu(user, "Actions for ALP-310");
    expect(screen.queryByRole("menuitem", { name: "Move to epic: Old stuff" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Move to epic: Billing" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/work-items/310/hierarchy")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/work-items/310/hierarchy")[0].body).toEqual({ epic_id: 41, version: 1 });
    expect(await screen.findByText("ALP-310 moved to Billing")).toBeInTheDocument();
    await menu(user, "Actions for ALP-310");
    await user.click(screen.getByRole("menuitem", { name: "Remove from epic" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/work-items/310/hierarchy")).toHaveLength(2));
    expect(backend.called("PUT", "/agile/work-items/310/hierarchy")[1].body.epic_id).toBeNull();
    expect(await screen.findByText("ALP-310 removed from its epic")).toBeInTheDocument();
  });

  it("read-only issues offer no move actions; a failed move is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("PUT", /hierarchy$/, () => reply(409, { detail: "Epic is archived" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Expand Onboarding" }));
    await menu(user, "Actions for ALP-311");
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Open issue", "Create sub-task"]);
    await user.keyboard("{Escape}");
    await menu(user, "Actions for ALP-310");
    await user.click(screen.getByRole("menuitem", { name: "Move to epic: Billing" }));
    expect(await screen.findByText("Epic is archived")).toBeInTheDocument();
  });

  it("opens issues and starts new sub-tasks, issues and epics", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Expand Onboarding" }));
    await user.click(screen.getByRole("button", { name: "Welcome mail" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/311")).toHaveLength(1));
    await user.click(screen.getByRole("button", { name: "ALP-40" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/40")).toHaveLength(1));
    await menu(user, "Actions for ALP-310");
    await user.click(screen.getByRole("menuitem", { name: "Create sub-task" }));
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
    expect(document.getElementById("modalBugTitle")).toHaveTextContent("New Item");
  });

  it("opens sub-tasks and epics from titles and menus", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Onboarding" })); // the title toggles the epic
    await user.click(screen.getByRole("button", { name: "Show sub-tasks of ALP-310" }));
    await user.click(screen.getByRole("button", { name: "Validate email" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/320")).toHaveLength(1));
    await menu(user, "Actions for ALP-310");
    await user.click(screen.getByRole("menuitem", { name: "Open issue" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/310")).toHaveLength(1));
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Open epic" }));
    await waitFor(() => expect(backend.called("GET", "/bugs/40")).toHaveLength(1));
  });

  it("+ Create epic and Create issue in epic open the item form", async () => {
    const user = userEvent.setup();
    await open(user);
    await user.click(await screen.findByRole("button", { name: "+ Create epic" }));
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
  });

  it("archives an epic after confirming; cancelling or an error changes nothing", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    await open(user, { backend });
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Archive epic" }));
    await confirm(user, "Cancel");
    expect(backend.called("POST", /archive$/)).toHaveLength(0);
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Archive epic" }));
    await confirm(user, "Archive");
    await waitFor(() => expect(backend.called("POST", "/agile/epics/40/archive")).toHaveLength(1));
    expect(await screen.findByText("Epic archived")).toBeInTheDocument();
  });

  it("a failed archive is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /archive$/, () => reply(409, { detail: "Has open issues" }));
    await open(user, { backend });
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Archive epic" }));
    await confirm(user, "Archive");
    expect(await screen.findByText("Has open issues")).toBeInTheDocument();
  });

  it("archived epics cannot be archived again or take new issues", async () => {
    const user = userEvent.setup();
    await open(user);
    await menu(user, "Actions for ALP-42");
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Open epic", "Edit details (colour, dates)"]);
  });

  it("non-managers cannot edit or archive epics", async () => {
    const user = userEvent.setup();
    await open(user, { role: "user" });
    await menu(user, "Actions for ALP-40");
    expect(screen.getAllByRole("menuitem").map((m) => m.textContent)).toEqual(["Open epic", "Create issue in epic"]);
    await user.click(screen.getByRole("menuitem", { name: "Create issue in epic" }));
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
  });

  it("edits an epic's name, colour and dates with its version", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Edit details (colour, dates)" }));
    const dlg = await screen.findByRole("dialog", { name: "Epic details: ALP-40" });
    const name = within(dlg).getByRole("textbox", { name: /Epic name/ });
    expect(name).toHaveValue("Onboarding");
    await user.clear(name);
    await user.type(name, "Activation");
    await user.click(within(dlg).getByRole("button", { name: "Colour #2e7d32" }));
    expect(within(dlg).getByRole("button", { name: "Colour #2e7d32" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(dlg).getAllByRole("button", { name: "Clear date" })[1]);
    await user.click(within(dlg).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/epics/40")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/epics/40")[0].body).toEqual({
      title: "Activation", color: "#2e7d32", start_date: "2026-01-01", target_date: null, version: 2,
    });
    expect(await screen.findByText("Epic updated")).toBeInTheDocument();
  });

  it("a target date before the start blocks saving; a server error keeps the dialog", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("PUT", /^\/agile\/epics\/40$/, () => reply(409, { detail: "Epic changed" }));
    await open(user, { backend });
    await menu(user, "Actions for ALP-40");
    await user.click(screen.getByRole("menuitem", { name: "Edit details (colour, dates)" }));
    const dlg = await screen.findByRole("dialog", { name: "Epic details: ALP-40" });
    await user.click(within(dlg).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Epic changed")).toBeInTheDocument();
    await user.click(within(dlg).getByRole("button", { name: "Target date" }));
    for (let i = 0; i < 3; i += 1) await user.click(screen.getByRole("button", { name: "Previous month" }));
    await user.click(document.querySelector('.bh-date-cell[data-iso="2025-12-15"]'));
    expect(within(dlg).getByRole("alert")).toHaveTextContent("The target date cannot be before the start date");
    expect(within(dlg).getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(within(dlg).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(document.getElementById("epicDialog")).toBeNull());
  });
});
