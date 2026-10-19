/** Events view: card grid, filters, detail mode, create/edit/delete, add-existing-task. */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, makeBug, makeEvent, mountApp } from "../test/backend";

const item = (over = {}) => makeBug({ item_type: "Task", event_id: 7, ...over });

function seeded() {
  const backend = createBackend();
  backend.state.events = [
    makeEvent({
      id: 7, name: "Launch", managers: [{ id: 1, name: "Ada Admin", email: "ada@example.com", role: "admin" }],
      items: [item({ id: 201, title: "Pack boxes", status: "New", priority: "High" }),
              item({ id: 202, title: "Send invites", status: "Resolved", priority: "Low", assignees: [{ id: 3, name: "Cy Manager", email: "cy@example.com" }], due_date: "2026-03-03", attachment_count: 1 })],
      assignee_count: 2,
    }),
    makeEvent({ id: 8, name: "Retro", description: "Look back", scheduled_for: null, project_name: null, created_by_name: "", items: [], assignee_count: 0 }),
  ];
  return backend;
}

async function openEvents(user, backend = seeded()) {
  await mountApp({ backend });
  await user.click(document.querySelector('.nav [data-view="events"]'));
  await screen.findByRole("button", { name: "Open event Launch" });
  return backend;
}

describe("events list mode", () => {
  it("lists events as cards with a summary line", async () => {
    const user = userEvent.setup();
    await openEvents(user);
    expect(screen.getByRole("button", { name: "Open event Retro" })).toBeInTheDocument();
    expect(screen.getByText("No date")).toBeInTheDocument();
    expect(screen.getByText("📅 2026-02-01")).toBeInTheDocument();
    expect(document.getElementById("eventsSummary")).toHaveTextContent("2 events · 2 items · ~2 people");
  });

  it("filters by search text and date and can be cleared", async () => {
    const user = userEvent.setup();
    await openEvents(user);
    await user.type(document.getElementById("eventsSearch"), "retro");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open event Launch" })).not.toBeInTheDocument());
    expect(document.getElementById("eventsSummary")).toHaveTextContent("showing 1 event");
    await user.type(document.getElementById("eventsSearch"), "zzz");
    expect(await screen.findByText(/No events match the current filter/)).toBeInTheDocument();
    expect(document.getElementById("eventsSummary")).toHaveTextContent("0 matching events");
    await user.click(document.getElementById("eventsClearFiltersBtn"));
    expect(await screen.findByRole("button", { name: "Open event Launch" })).toBeInTheDocument();
    await user.type(document.getElementById("eventsDateFilter"), "2026-02-01");
    await waitFor(() => expect(screen.queryByRole("button", { name: "Open event Retro" })).not.toBeInTheDocument());
  });

  it("shows the empty banner when there are no events and Refresh refetches", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    await mountApp({ backend });
    await user.click(document.querySelector('.nav [data-view="events"]'));
    await waitFor(() => expect(document.getElementById("eventsEmpty")).not.toHaveAttribute("hidden"));
    const before = backend.called("GET", "/events").length;
    await user.click(document.getElementById("eventsRefreshBtn"));
    await waitFor(() => expect(backend.called("GET", "/events").length).toBe(before + 1));
  });

  it("surfaces a failed load as an error toast", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", "/events", () => ({ __reply: true, status: 500, body: { detail: "db down" } }));
    await mountApp({ backend });
    await user.click(document.querySelector('.nav [data-view="events"]'));
    expect(await screen.findByText("db down")).toBeInTheDocument();
  });
});

describe("events detail mode", () => {
  it("opens an event with metadata, managers and its tasks", async () => {
    const user = userEvent.setup();
    await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    expect(await screen.findByText("Pack boxes")).toBeInTheDocument();
    expect(document.getElementById("eventDetailName")).toHaveTextContent("Launch");
    expect(document.getElementById("eventDetailMeta")).toHaveTextContent("📁 Alpha · 📅 2026-02-01 · by Ada Admin · 1 item · 2 people");
    expect(screen.getByText("Managers:")).toBeInTheDocument();
    expect(screen.getByText("2026-03-03")).toBeInTheDocument();
  });

  it("filters tasks by search, #id, status and priority", async () => {
    const user = userEvent.setup();
    await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    const search = document.getElementById("eventDetailSearch");
    await user.type(search, "invites");
    await waitFor(() => expect(screen.queryByText("Pack boxes")).not.toBeInTheDocument());
    await user.clear(search);
    await user.type(search, "#201");
    await waitFor(() => expect(screen.queryByText("Send invites")).not.toBeInTheDocument());
    await user.clear(search);
    await user.type(search, "202");
    await waitFor(() => expect(screen.queryByText("Pack boxes")).not.toBeInTheDocument());
    await user.type(search, "99999");
    expect(await screen.findByText(/No tasks in this event match/)).toBeInTheDocument();
    await user.click(document.getElementById("eventDetailClearFiltersBtn"));
    expect(await screen.findByText("Pack boxes")).toBeInTheDocument();

    const bar = document.getElementById("eventDetailFilterBar");
    await user.click(within(bar).getByText("All Statuses"));
    await user.click(within(bar).getByRole("menuitemcheckbox", { name: /Resolved/ }));
    await waitFor(() => expect(screen.queryByText("Pack boxes")).not.toBeInTheDocument());
    await user.click(within(bar).getByRole("menuitemcheckbox", { name: /Resolved/ }));
    await user.click(within(bar).getByText("All Priorities"));
    await user.click(within(bar).getByRole("menuitemcheckbox", { name: /High/ }));
    await waitFor(() => expect(screen.queryByText("Send invites")).not.toBeInTheDocument());
    await user.click(within(bar).getByRole("menuitemcheckbox", { name: /High/ }));
    await user.click(within(bar).getByText("All Assignees"));
    await user.click(within(bar).getByRole("menuitemcheckbox", { name: "Cy Manager" }));
    await waitFor(() => expect(screen.queryByText("Pack boxes")).not.toBeInTheDocument());
  });

  it("opening a task row (click and Enter) opens the item modal", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    backend.state.bugs.push(item({ id: 201, title: "Pack boxes" }));
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    const row = (await screen.findByText("Pack boxes")).closest("tr");
    row.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(backend.called("GET", "/bugs/201")).toHaveLength(1));
  });

  it("empty event explains how to add items; Back returns to the grid", async () => {
    const user = userEvent.setup();
    await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Retro" }));
    expect(await screen.findByText(/No items yet/)).toBeInTheDocument();
    await user.click(document.getElementById("eventBackBtn"));
    expect(await screen.findByRole("button", { name: "Open event Launch" })).toBeVisible();
  });

  it("unknown event reports the error and falls back to the list", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    backend.state.events = backend.state.events.filter((e) => e.id !== 8);
    await user.click(screen.getByRole("button", { name: "Open event Retro" }));
    expect(await screen.findByText("No such event")).toBeInTheDocument();
  });

  it("edits an event (name saved via PUT and detail refreshed)", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("editEventBtn"));
    const nameBox = await screen.findByDisplayValue("Launch");
    await user.clear(nameBox);
    await user.type(nameBox, "Launch v2");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("PUT", "/events/7")).toHaveLength(1));
    expect(backend.called("PUT", "/events/7")[0].body).toMatchObject({ name: "Launch v2", project_id: 1, manager_ids: [1] });
    expect(await screen.findByText("Event updated")).toBeInTheDocument();
  });

  it("creates an event from the list, validating the name first", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    await user.click(document.getElementById("newEventBtn"));
    const form = document.getElementById("formEvent");
    const nameBox = within(form).getByPlaceholderText(/Morning standup/);
    await user.type(nameBox, "   ");
    form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    expect(await screen.findByText("Event name is required")).toBeInTheDocument();
    await user.clear(nameBox);
    await user.type(nameBox, "Planning");
    await user.type(within(form).getByPlaceholderText(/Agenda/), "Agenda");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("POST", "/events")).toHaveLength(1));
    expect(backend.called("POST", "/events")[0].body).toMatchObject({ name: "Planning", description: "Agenda", project_id: 1 });
    expect(await screen.findByText("Event created")).toBeInTheDocument();
  });

  it("admin deletes an event after confirming", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Retro" }));
    await screen.findByText(/No items yet/);
    await user.click(document.getElementById("deleteEventBtn"));
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(backend.called("DELETE", "/events/8")).toHaveLength(1));
    expect(await screen.findByText('Event "Retro" deleted')).toBeInTheDocument();
  });

  it("Add New Task opens the item form pre-bound to this event", async () => {
    const user = userEvent.setup();
    const backend = await openEvents(user);
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("addItemToEventBtn"));
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
    expect(document.getElementById("modalBugTitle")).toHaveTextContent("New Item");
    await user.type(screen.getByPlaceholderText("Short summary…"), "Book venue");
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("POST", "/bugs")).toHaveLength(1));
    expect(backend.called("POST", "/bugs")[0].body).toMatchObject({ title: "Book venue", item_type: "Task", event_id: 7 });
  });

  it("adds existing tasks to the event through the picker", async () => {
    const user = userEvent.setup();
    const backend = seeded();
    backend.state.bugs.push(item({ id: 301, title: "Loose task", event_id: null }));
    await openEvents(user, backend);
    await user.click(screen.getByRole("button", { name: "Open event Launch" }));
    await screen.findByText("Pack boxes");
    await user.click(document.getElementById("addExistingTaskBtn"));
    await user.click(screen.getByText(/Search existing tasks/));
    await user.click(await screen.findByText("Loose task"));
    await user.click(screen.getByRole("button", { name: "Add 1 task" }));
    await waitFor(() => expect(backend.called("PUT", "/bugs/301")).toHaveLength(1));
    expect(backend.called("PUT", "/bugs/301")[0].body).toEqual({ event_id: 7 });
    expect(await screen.findByText("Added 1 existing task")).toBeInTheDocument();
  });
});
