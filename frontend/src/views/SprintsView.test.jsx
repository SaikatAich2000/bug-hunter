/** Sprints view shell: project picker, enabling Scrum, tabs, deep links and empty states. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, mountApp, reply } from "../test/backend";
import { makeAgile, openSprints } from "../test/agile";

const goSprints = (user) => user.click(document.querySelector('.nav [data-view="sprints"]'));

describe("project without Scrum", () => {
  it("a manager can set the board up; success reloads into the tabs", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    makeAgile(backend);
    backend.state.projects[0].agile_enabled = false;
    backend.state.agile = { 1: { agile_enabled: false, board_id: null } };
    backend.on("POST", "/agile/projects/1/enable", () => {
      backend.state.agile = { 1: { agile_enabled: true, board_id: 9 } };
      return {};
    });
    await mountApp({ backend });
    await goSprints(user);
    expect(await screen.findByRole("heading", { name: /Scrum isn.t set up for Alpha yet/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Set up Scrum board" }));
    await waitFor(() => expect(backend.called("POST", "/agile/projects/1/enable")).toHaveLength(1));
    expect(backend.called("POST", "/agile/projects/1/enable")[0].body).toEqual({ feature_flags: {} });
    expect(await screen.findByText("Scrum board created")).toBeInTheDocument();
    expect(await screen.findByRole("tablist", { name: "Sprint views" })).toBeInTheDocument();
  });

  it("a failed set-up is reported and the button comes back", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    makeAgile(backend);
    backend.state.agile = { 1: { agile_enabled: false, board_id: null } };
    backend.on("POST", "/agile/projects/1/enable", () => reply(409, { detail: "Already being enabled" }));
    await mountApp({ backend });
    await goSprints(user);
    await user.click(await screen.findByRole("button", { name: "Set up Scrum board" }));
    expect(await screen.findByText("Already being enabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up Scrum board" })).toBeEnabled();
  });

  it("people who cannot manage are told to ask an admin", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    makeAgile(backend);
    backend.state.agile = { 1: { agile_enabled: false, board_id: null } };
    backend.state.me = { ...backend.state.me, role: "user" };
    await mountApp({ backend });
    await goSprints(user);
    expect(await screen.findByText("Ask an admin or manager to set it up")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set up Scrum board" })).not.toBeInTheDocument();
  });
});

describe("empty and switching", () => {
  it("with no projects it asks to create one", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.projects = [];
    await mountApp({ backend, waitFor: false });
    await goSprints(user);
    expect(await screen.findByText(/Create a project first to use Sprints/)).toBeInTheDocument();
    await user.click(within(document.getElementById("viewSprints")).getByRole("button", { name: "New project" }));
    await waitFor(() => expect(document.getElementById("modalProject")).not.toHaveAttribute("hidden"));
  });

  it("without manage rights there is no New project shortcut", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.projects = [];
    backend.state.me = { ...backend.state.me, role: "user" };
    await mountApp({ backend, waitFor: false });
    await goSprints(user);
    expect(await screen.findByText(/Create a project first to use Sprints/)).toBeInTheDocument();
    expect(within(document.getElementById("viewSprints")).queryByRole("button", { name: "New project" })).not.toBeInTheDocument();
  });

  it("switching project loads that project's settings", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    await openSprints(user, { backend });
    await user.selectOptions(screen.getByLabelText("Project:"), "2");
    await waitFor(() => expect(backend.called("GET", "/agile/projects/2/settings")).toHaveLength(1));
    expect(await screen.findByRole("button", { name: "Set up Scrum board" })).toBeInTheDocument();
  });

  it("a failed settings load is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/projects\/1\/settings$/, () => reply(500, { detail: "Settings unavailable" }));
    await mountApp({ backend });
    await goSprints(user);
    expect(await screen.findByText("Settings unavailable")).toBeInTheDocument();
  });

  it("tabs switch the panel; non-managers have no Board settings tab", async () => {
    const user = userEvent.setup();
    await openSprints(user, { role: "user" });
    expect(screen.queryByRole("tab", { name: /Board settings/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Epics/ }));
    expect(screen.getByRole("tab", { name: /Epics/, selected: true })).toBeInTheDocument();
    expect(await screen.findByText(/Epic → Story/)).toBeInTheDocument();
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).queryByText("Active sprint")).not.toBeInTheDocument();
  });
});
