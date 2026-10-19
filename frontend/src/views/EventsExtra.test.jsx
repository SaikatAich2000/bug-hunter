/** Events: keyboard access, creating the first event, event dialog rules, delete/add-existing failures; list filter bar toggles. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, makeBug, makeEvent, mountApp, reply } from "../test/backend";

const goEvents = (user) => user.click(document.querySelector('.nav [data-view="events"]'));

function seeded() {
  const backend = createBackend();
  backend.state.events = [makeEvent({
    id: 7, name: "Launch", managers: [{ id: 1, name: "Ada Admin", email: "ada@example.com", role: "admin" }],
    items: [makeBug({ id: 201, title: "Pack boxes", item_type: "Task", event_id: 7 })],
  })];
  return backend;
}

describe("events keyboard and empty state", () => {
  it("event cards open with Enter and Space", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    await mountApp({ backend });
    await goEvents(user);
    const card = await screen.findByRole("button", { name: "Open event Launch" });
    card.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Pack boxes")).toBeInTheDocument();
    await user.click(document.getElementById("eventBackBtn"));
    const again = await screen.findByRole("button", { name: "Open event Launch" });
    again.focus();
    await user.keyboard(" ");
    expect(await screen.findByText("Pack boxes")).toBeInTheDocument();
    again.focus();
    await user.keyboard("a");
    expect(backend.called("GET", "/events/7").length).toBeGreaterThan(0);
  });

  it("with no events a manager is offered New Event, which opens the dialog", async () => {
    const user = userEvent.setup();
    await mountApp();
    await goEvents(user);
    await waitFor(() => expect(document.getElementById("eventsEmpty")).not.toHaveAttribute("hidden"));
    await user.click(within(document.getElementById("eventsEmpty")).getByRole("button", { name: "+ New Event" }));
    await waitFor(() => expect(document.getElementById("modalEvent")).not.toHaveAttribute("hidden"));
  });

  it("people who cannot manage are not offered New Event", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.me = { ...backend.state.me, role: "user" };
    await mountApp({ backend });
    await goEvents(user);
    await waitFor(() => expect(document.getElementById("eventsEmpty")).not.toHaveAttribute("hidden"));
    expect(screen.queryByRole("button", { name: "+ New Event" })).not.toBeInTheDocument();
  });
});

describe("event failures", () => {
  it("a failed delete is reported and the event stays", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    backend.on("DELETE", /^\/events\/7$/, () => reply(409, { detail: "Event is locked" }));
    await mountApp({ backend });
    await goEvents(user);
    await user.click(await screen.findByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("deleteEventBtn"));
    await confirm(user, "Delete");
    expect(await screen.findByText("Event is locked")).toBeInTheDocument();
    expect(screen.getByText("Pack boxes")).toBeInTheDocument();
  });

  it("adding existing tasks that the server refuses is reported and the picker stays usable", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    backend.state.bugs.push(makeBug({ id: 301, title: "Loose task", item_type: "Task", event_id: null }));
    backend.on("PUT", /^\/bugs\/301$/, () => reply(409, { detail: "Version conflict" }));
    await mountApp({ backend });
    await goEvents(user);
    await user.click(await screen.findByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("addExistingTaskBtn"));
    await user.click(screen.getByText(/Search existing tasks/));
    await user.click(await screen.findByText("Loose task"));
    await user.click(screen.getByRole("button", { name: "Add 1 task" }));
    expect(await screen.findByText("Version conflict")).toBeInTheDocument();
  });
});

describe("event dialog rules", () => {
  it("without any project the event cannot be saved", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.projects = [];
    await mountApp({ backend, waitFor: false });
    await goEvents(user);
    await user.click(document.getElementById("newEventBtn"));
    const form = document.getElementById("formEvent");
    await user.type(within(form).getByPlaceholderText(/Morning standup/), "Planning");
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(await screen.findByText("Pick a project for this event")).toBeInTheDocument();
    expect(backend.called("POST", "/events")).toHaveLength(0);
  });

  it("managers can be picked and unpicked, and a stale manager is kept visible as inactive", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    backend.state.events[0].managers = [
      { id: 1, name: "Ada Admin", email: "ada@example.com", role: "admin" },
      { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user" },
    ];
    await mountApp({ backend });
    await goEvents(user);
    await user.click(await screen.findByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("editEventBtn"));
    const picker = await screen.findByRole("group", { name: /Managers/ }).catch(() => document.getElementById("eventManagerPicker"));
    expect(picker).toHaveTextContent("Bob Dev (inactive)");
    await user.click(within(picker).getByRole("button", { name: /Ada Admin/ }));
    await user.click(within(picker).getByRole("button", { name: /Ada Admin/ }));
    await user.click(within(picker).getByRole("button", { name: /Bob Dev/ }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("PUT", "/events/7")).toHaveLength(1));
    expect(backend.called("PUT", "/events/7")[0].body.manager_ids).toEqual([1]);
  });

  it("a failed save keeps the dialog open and reports the error", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    backend.on("PUT", /^\/events\/7$/, () => reply(422, { detail: "Name already used" }));
    await mountApp({ backend });
    await goEvents(user);
    await user.click(await screen.findByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("editEventBtn"));
    await user.click(await screen.findByRole("button", { name: "Save" }));
    expect(await screen.findByText("Name already used")).toBeInTheDocument();
    expect(document.getElementById("modalEvent")).not.toHaveAttribute("hidden");
  });
});

describe("list filter bar", () => {
  it("toggles project, environment and assignee filters through the API query", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    await mountApp({ backend });
    const bar = within(document.getElementById("filterBar"));
    const query = () => backend.called("GET", /^\/bugs\?/).at(-1).url.searchParams;
    await user.click(bar.getByText(/All Projects/));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /Beta/ }));
    await waitFor(() => expect(query().getAll("project_id")).toEqual(["2"]));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /Beta/ }));
    await waitFor(() => expect(query().getAll("project_id")).toEqual([]));
    await user.click(bar.getByText(/All Envs/));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /PROD/ }));
    await waitFor(() => expect(query().getAll("environment")).toEqual(["PROD"]));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /PROD/ }));
    await user.click(bar.getByText(/All Assignees/));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /Bob Dev/ }));
    await waitFor(() => expect(query().getAll("assignee_id")).toEqual(["2"]));
    await user.click(bar.getByRole("menuitemcheckbox", { name: /Bob Dev/ }));
    await waitFor(() => expect(query().getAll("assignee_id")).toEqual([]));
  });
});
