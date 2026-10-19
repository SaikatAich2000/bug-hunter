/** Capacity tab: sprint picker, readiness checks, per-person capacity editing. */
import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, reply } from "../../test/backend";
import { BOARD, makeAgile, openSprints, sprintPlanningData } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Capacity", ...opts });

describe("planning summary", () => {
  it("shows totals, the readiness checks and the sprints that can still be planned", async () => {
    const user = userEvent.setup();
    await open(user);
    expect(await screen.findByText("Not ready")).toBeInTheDocument();
    expect(screen.getByText("Items").previousSibling).toHaveTextContent("4");
    expect(screen.getByText("Total points").previousSibling).toHaveTextContent("12");
    expect(screen.getByText("Unestimated").previousSibling).toHaveTextContent("1");
    expect(screen.getByRole("option", { name: "Sprint 6 (future)" })).toBeInTheDocument();
  });

  it("marks passed, warning and failed checks differently", async () => {
    const user = userEvent.setup();
    await open(user);
    const items = (await screen.findAllByRole("listitem")).filter((li) => li.className.includes("planning-readiness-item"));
    expect(items.map((li) => li.textContent)).toEqual(["✓ Sprint has a goal", "⚠ Dates are not set", "✕ No issues in the sprint"]);
  });

  it("a ready sprint says Ready; no checks says so; a missing total is a dash", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.sprintPlanning = sprintPlanningData({ ready_to_start: true, readiness: [], total_estimate: null });
    await open(user, { backend });
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByText("No checks configured.")).toBeInTheDocument();
    expect(screen.getByText("Total points").previousSibling).toHaveTextContent("—");
  });

  it("switches sprint and loads that sprint's plan", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await screen.findByText("Not ready");
    await user.selectOptions(screen.getByLabelText("Sprint:"), "6");
    await waitFor(() => expect(backend.called("GET", "/agile/sprints/6/planning")).toHaveLength(1));
  });

  it("with no open sprint there is nothing to plan", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.planning.sprints = [];
    await open(user, { backend });
    expect(await screen.findByText("Select a sprint to plan.")).toBeInTheDocument();
    expect(screen.getByLabelText("Sprint:")).toHaveDisplayValue("No open sprints");
  });

  it("a failed plan load is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /sprints\/\d+\/planning$/, () => reply(500, { detail: "Planning unavailable" }));
    await open(user, { backend });
    expect(await screen.findByText("Planning unavailable")).toBeInTheDocument();
  });
});

describe("capacity", () => {
  it("lists the project's people with saved capacity and saves the edited entries", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    const bob = await screen.findByRole("spinbutton", { name: "Capacity of Bob Dev (points)" });
    expect(bob).toHaveValue(8);
    expect(screen.getByRole("textbox", { name: "Notes for Bob Dev" })).toHaveValue("part time");
    expect(screen.queryByRole("spinbutton", { name: /Cy Manager/ })).not.toBeInTheDocument();
    await user.clear(bob);
    await user.type(bob, "5");
    await user.type(screen.getByRole("textbox", { name: "Notes for Bob Dev" }), " (until Fri)");
    await user.type(screen.getByRole("spinbutton", { name: "Capacity of Ada Admin (points)" }), "13");
    await user.click(screen.getByRole("button", { name: "Save capacity" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/sprints/5/capacity")).toHaveLength(1));
    expect(backend.called("PUT", "/agile/sprints/5/capacity")[0].body).toEqual({
      entries: [
        { user_id: 1, capacity_value: 13, capacity_unit: "points", notes: "", days_off: [] },
        { user_id: 2, capacity_value: 5, capacity_unit: "points", notes: "part time (until Fri)", days_off: [] },
      ],
    });
    expect(await screen.findByText("Capacity saved")).toBeInTheDocument();
  });

  it("a failed save is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("PUT", /capacity$/, () => reply(403, { detail: "Not allowed" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Save capacity" }));
    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
  });

  it("without manage rights the fields are read-only and there is no save", async () => {
    const user = userEvent.setup();
    await open(user, { role: "user" });
    expect(await screen.findByRole("spinbutton", { name: "Capacity of Bob Dev (points)" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save capacity" })).not.toBeInTheDocument();
  });

  it("time boards count capacity in minutes", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend(), { board: { ...BOARD, estimation_mode: "time" } });
    backend.state.sprintPlanning = sprintPlanningData({ capacity: [] });
    await open(user, { backend });
    await user.type(await screen.findByRole("spinbutton", { name: "Capacity of Bob Dev (minutes)" }), "480");
    await user.click(screen.getByRole("button", { name: "Save capacity" }));
    await waitFor(() => expect(backend.called("PUT", "/agile/sprints/5/capacity")[0].body.entries[0]).toMatchObject({ capacity_unit: "minutes", capacity_value: 480 }));
    expect(screen.getByText("Total minutes")).toBeInTheDocument();
  });

  it("item-count boards do not track per-person capacity", async () => {
    const user = userEvent.setup();
    await open(user, { backend: makeAgile(createBackend(), { board: { ...BOARD, estimation_mode: "item_count" } }) });
    expect(await screen.findByText(/estimates by item count/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save capacity" })).not.toBeInTheDocument();
  });

  it("when Scrum turns out to be off for the project the panel says so", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let calls = 0;
    // the Sprints view sees it enabled (settings + board), the panel's own re-check does not
    backend.on("GET", /^\/agile\/projects\/1\/settings$/, () => {
      calls += 1;
      return calls <= 1 ? { agile_enabled: true, board_id: 9 } : { agile_enabled: false, board_id: null };
    });
    await open(user, { backend });
    expect(await screen.findByText("Scrum is not set up for this project yet.")).toBeInTheDocument();
  });

  it("a failed settings check is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let calls = 0;
    backend.on("GET", /^\/agile\/projects\/1\/settings$/, () => {
      calls += 1;
      return calls <= 1 ? { agile_enabled: true, board_id: 9 } : reply(500, { detail: "Settings down" });
    });
    await open(user, { backend });
    expect(await screen.findByText("Settings down")).toBeInTheDocument();
  });
});
