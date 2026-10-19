/** Board settings: general form, column editor (with its validation) and custom quick filters. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, reply } from "../../test/backend";
import { makeAgile, openSprints } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Board settings", ...opts });

describe("general settings", () => {
  it("saves the name, statistic, swimlanes, working days and timezone with the board version", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const form = await screen.findByRole("form", { name: "General" });
    const name = within(form).getByRole("textbox", { name: "Board name" });
    await user.clear(name);
    await user.type(name, "Platform");
    await user.selectOptions(within(form).getByLabelText("Estimation statistic"), "time");
    await user.selectOptions(within(form).getByLabelText("Default swimlanes"), "assignee");
    await user.click(within(form).getByRole("checkbox", { name: "Sat" }));
    await user.click(within(form).getByRole("checkbox", { name: "Mon" }));
    const tz = within(form).getByRole("combobox", { name: /Timezone/ });
    await user.clear(tz);
    await user.type(tz, "Asia/Kolkata");
    await user.click(within(form).getByRole("button", { name: "Save general settings" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/boards/9")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/boards/9")[0].body).toEqual({
      name: "Platform", estimation_mode: "time", swimlane_mode: "assignee", timezone: "Asia/Kolkata",
      working_weekdays: [2, 3, 4, 5, 6], version: 1,
    });
    expect(await screen.findByText("Board settings saved")).toBeInTheDocument();
  });

  it("refuses to save without a working day", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const form = await screen.findByRole("form", { name: "General" });
    for (const day of ["Mon", "Tue", "Wed", "Thu", "Fri"]) await user.click(within(form).getByRole("checkbox", { name: day }));
    await user.click(within(form).getByRole("button", { name: "Save general settings" }));
    expect(await screen.findByText("Pick at least one working day")).toBeInTheDocument();
    expect(backend.called("PUT", "/agile/boards/9")).toHaveLength(0);
  });

  it("reports a rejected save", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("PUT", /^\/agile\/boards\/9$/, () => reply(409, { detail: "Board changed" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Save general settings" }));
    expect(await screen.findByText("Board changed")).toBeInTheDocument();
  });
});

describe("columns editor", () => {
  it("lists the columns, their statuses and the unmapped statuses", async () => {
    const user = userEvent.setup();
    await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    expect(within(region).getByRole("textbox", { name: "Column 2 name" })).toHaveValue("In Progress");
    expect(within(region).getByRole("list", { name: "Statuses in Done" })).toHaveTextContent("Resolved");
    expect(within(region).getByRole("button", { name: "Unmap New" })).toBeInTheDocument();
    expect(within(region).getByLabelText("Add Testing to column")).toBeInTheDocument();
    expect(within(region).getByLabelText("Add Closed to column")).toBeInTheDocument();
    expect(within(region).getByLabelText("In Progress limit enforcement")).toBeInTheDocument();
  });

  it("edits names, limits and categories, moves statuses between columns and saves", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    const name = within(region).getByRole("textbox", { name: "Column 1 name" });
    await user.clear(name);
    await user.type(name, "Backlog");
    await user.type(within(region).getByRole("spinbutton", { name: "Backlog minimum" }), "1");
    await user.type(within(region).getByRole("spinbutton", { name: "Backlog maximum" }), "5");
    await user.selectOptions(within(region).getByLabelText("Backlog limit enforcement"), "block");
    await user.selectOptions(within(region).getByLabelText("Backlog category"), "in_progress");
    await user.selectOptions(within(region).getByLabelText("Add Testing to column"), "1");
    await user.selectOptions(within(region).getByLabelText("Move New to column"), "1");
    await user.click(within(region).getByRole("button", { name: "Unmap Resolved" }));
    await user.selectOptions(within(region).getByLabelText("Add Closed to column"), "2");
    await user.click(within(region).getByRole("button", { name: "Save columns" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/boards/9/columns")).toHaveLength(1));
    const sent = backend.called("PUT", "/agile/boards/9/columns")[0].body;
    expect(sent.version).toBe(1);
    expect(sent.columns[0]).toMatchObject({ id: 1, name: "Backlog", category: "in_progress", statuses: [], min_cards: 1, wip_limit: 5, wip_enforcement: "block" });
    expect(sent.columns[1].statuses).toEqual(["In Progress", "Testing", "New"]);
    expect(sent.columns[2].statuses).toEqual(["Closed"]);
    expect(await screen.findByText("Columns saved")).toBeInTheDocument();
  });

  it("clearing a limit or typing a non-number leaves it unlimited", async () => {
    const user = userEvent.setup();
    await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    const max = within(region).getByRole("spinbutton", { name: "In Progress maximum" });
    expect(max).toHaveValue(1);
    await user.clear(max);
    expect(max).toHaveValue(null);
    expect(within(region).queryByLabelText("In Progress limit enforcement")).not.toBeInTheDocument();
    await user.type(max, "0");
    expect(max).toHaveValue(null);
  });

  it("reorders columns with the arrows and the end buttons are disabled", async () => {
    const user = userEvent.setup();
    await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    expect(within(region).getByRole("button", { name: "Move To Do left" })).toBeDisabled();
    expect(within(region).getByRole("button", { name: "Move Done right" })).toBeDisabled();
    await user.click(within(region).getByRole("button", { name: "Move To Do right" }));
    expect(within(region).getByRole("textbox", { name: "Column 1 name" })).toHaveValue("In Progress");
    await user.click(within(region).getByRole("button", { name: "Move To Do left" }));
    expect(within(region).getByRole("textbox", { name: "Column 1 name" })).toHaveValue("To Do");
  });

  it("explains every problem and blocks saving until it is fixed", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    await user.click(within(region).getByRole("button", { name: "+ Add column" }));
    const added = within(region).getByRole("textbox", { name: "Column 3 name" });
    expect(added).toHaveValue("New column");
    await user.clear(added);
    expect(within(region).getByRole("alert")).toHaveTextContent("Every column needs a name.");
    await user.type(added, "to do");
    expect(within(region).getByRole("alert")).toHaveTextContent('Two columns are named "to do".');
    await user.type(within(region).getByRole("spinbutton", { name: "to do minimum" }), "9");
    await user.type(within(region).getByRole("spinbutton", { name: "to do maximum" }), "2");
    expect(within(region).getByRole("alert")).toHaveTextContent('"to do": the minimum is above the maximum.');
    expect(within(region).getByRole("button", { name: "Save columns" })).toBeDisabled();
    await user.selectOptions(within(region).getByLabelText("Done category"), "todo");
    expect(within(region).getByRole("alert")).toHaveTextContent("The right-most column must have the Done category");
    await user.click(within(region).getByRole("button", { name: "Unmap Resolved" }));
    expect(within(region).getByRole("alert")).toHaveTextContent("The right-most column needs at least one status.");
    expect(within(region).getAllByText("No statuses: cards cannot enter this column.")).toHaveLength(2);
    expect(backend.called("PUT", /columns$/)).toHaveLength(0);
  });

  it("removing a column asks first, and the last column cannot be removed", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    await user.click(within(region).getByRole("button", { name: "Remove In Progress" }));
    await confirm(user, "Cancel");
    expect(within(region).getByRole("textbox", { name: "Column 2 name" })).toHaveValue("In Progress");
    await user.click(within(region).getByRole("button", { name: "Remove In Progress" }));
    await confirm(user, "Remove");
    await waitFor(() => expect(within(region).getByRole("textbox", { name: "Column 2 name" })).toHaveValue("Done"));
    await user.click(within(region).getByRole("button", { name: "Remove To Do" }));
    await confirm(user, "Remove");
    await waitFor(() => expect(within(region).queryByRole("textbox", { name: "Column 2 name" })).not.toBeInTheDocument());
    expect(within(region).getByRole("button", { name: "Remove Done" })).toBeDisabled();
    expect(backend.called("PUT", /columns$/)).toHaveLength(0);
  });

  it("a rejected column save is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("PUT", /columns$/, () => reply(400, { detail: "Statuses in use" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Save columns" }));
    expect(await screen.findByText("Statuses in use")).toBeInTheDocument();
  });

  it("every status being mapped is said so", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const region = await screen.findByRole("region", { name: "Columns and statuses" });
    await user.selectOptions(within(region).getByLabelText("Add Testing to column"), "1");
    await user.selectOptions(within(region).getByLabelText("Add Closed to column"), "2");
    expect(within(region).getByText("Every status is on the board.")).toBeInTheDocument();
    expect(backend.called("PUT", /columns$/)).toHaveLength(0);
  });
});

describe("quick filters", () => {
  const existing = [
    { id: 70, name: "Urgent bugs", version: 4, filter_json: { item_types: ["Bug"], priorities: ["High"], assignee_ids: [2], epic_ids: [40], label_ids: [5], flagged: true, text: "login" } },
    { id: 71, name: "All", version: 1, filter_json: {} },
  ];

  it("describes each saved filter's criteria", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.quickFilters = existing;
    backend.on("GET", /^\/agile\/labels/, () => [{ id: 5, display_name: "backend" }]);
    await open(user, { backend });
    const region = await screen.findByRole("region", { name: "Quick filters" });
    await waitFor(() => expect(region).toHaveTextContent(/type: Bug · priority: High · assignee: Bob Dev · epic: Onboarding · label: backend · flagged · text: "login"/));
    expect(region).toHaveTextContent("everything");
  });

  it("with none saved says so; adds a filter with every kind of criterion", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/labels/, () => [{ id: 5, display_name: "backend" }]);
    await open(user, { backend });
    const region = await screen.findByRole("region", { name: "Quick filters" });
    expect(within(region).getByText("No custom quick filters yet.")).toBeInTheDocument();
    await user.click(within(region).getByRole("button", { name: "+ Add quick filter" }));
    expect(within(region).getByRole("button", { name: "Save quick filter" })).toBeDisabled();
    await user.type(within(region).getByRole("textbox", { name: /Name/ }), "  My bugs ");
    await user.click(await within(region).findByRole("button", { name: "Bug" }));
    await user.click(within(region).getByRole("button", { name: "High" }));
    await user.click(within(region).getByRole("button", { name: "Bob Dev" }));
    await user.click(await within(region).findByRole("button", { name: "Onboarding" }));
    await user.click(within(region).getByRole("button", { name: "backend" }));
    await user.click(within(region).getByRole("button", { name: "Bug" })); // toggled off again
    await user.click(within(region).getByRole("checkbox", { name: "Flagged only" }));
    await user.type(within(region).getByRole("textbox", { name: "Text in key or summary" }), "export");
    await user.click(within(region).getByRole("button", { name: "Save quick filter" }));
    await waitFor(() => expect(backend.called("POST", "/agile/boards/9/quick-filters")).toHaveLength(1));
    expect(backend.called("POST", "/agile/boards/9/quick-filters")[0].body).toEqual({
      name: "My bugs", is_shared: true,
      filter_json: { item_types: [], priorities: ["High"], assignee_ids: [2], epic_ids: [40], label_ids: [5], flagged: true, text: "export" },
    });
    expect(await screen.findByText("Quick filter saved")).toBeInTheDocument();
  });

  it("edits a saved filter (sending its version), can cancel and delete after confirming", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.quickFilters = existing;
    await open(user, { backend });
    const region = await screen.findByRole("region", { name: "Quick filters" });
    await user.click((await within(region).findAllByRole("button", { name: "Edit" }))[0]);
    const name = within(region).getByRole("textbox", { name: /Name/ });
    expect(name).toHaveValue("Urgent bugs");
    await user.click(within(region).getByRole("button", { name: "Cancel" }));
    expect(within(region).queryByRole("textbox", { name: /Name/ })).not.toBeInTheDocument();
    await user.click(within(region).getAllByRole("button", { name: "Edit" })[0]);
    const again = within(region).getByRole("textbox", { name: /Name/ });
    await user.clear(again);
    await user.type(again, "Urgent");
    await user.click(within(region).getByRole("button", { name: "Save quick filter" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/quick-filters/70")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/quick-filters/70")[0].body).toMatchObject({ name: "Urgent", version: 4, filter_json: { item_types: ["Bug"], flagged: true } });

    await user.click(within(region).getAllByRole("button", { name: "Delete" })[1]);
    await confirm(user, "Cancel");
    expect(backend.called("DELETE", /quick-filters/)).toHaveLength(0);
    await user.click(within(region).getAllByRole("button", { name: "Delete" })[1]);
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/agile/quick-filters/71")).toHaveLength(1));
  });

  it("reports a failed save and a failed delete", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.quickFilters = existing;
    backend.on("POST", /quick-filters$/, () => reply(400, { detail: "Name taken" }));
    backend.on("DELETE", /quick-filters/, () => reply(403, { detail: "Not yours" }));
    await open(user, { backend });
    const region = await screen.findByRole("region", { name: "Quick filters" });
    await user.click(await within(region).findByRole("button", { name: "+ Add quick filter" }));
    await user.type(within(region).getByRole("textbox", { name: /Name/ }), "Dup");
    await user.click(within(region).getByRole("button", { name: "Save quick filter" }));
    expect(await screen.findByText("Name taken")).toBeInTheDocument();
    await user.click(within(region).getAllByRole("button", { name: "Delete" })[0]);
    await confirm(user, "Delete");
    expect(await screen.findByText("Not yours")).toBeInTheDocument();
  });
});
