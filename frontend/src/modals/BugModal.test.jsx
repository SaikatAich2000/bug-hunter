/** Work-item modal (create / edit / read-only) driven through the whole app against a fake API. */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, empty, makeBug, mountApp, recordToasts, reply } from "../test/backend";

const modal = () => within(document.getElementById("modalBug"));
const select = (user, name, value) => user.selectOptions(modal().getByRole("combobox", { name }), value);
const toastText = () => document.getElementById("toast").textContent;
const file = (name, type = "image/png", size = 5) => {
  const f = new File(["x".repeat(size)], name, { type });
  return f;
};

// The modal focuses the title ~50ms after opening; wait for that so it cannot steal focus mid-interaction.
const titleFocused = () => waitFor(() => {
  const t = modal().getByPlaceholderText("Short summary…");
  if (!t.disabled) expect(t).toHaveFocus();
});

async function openNew(user, backend = createBackend()) {
  await mountApp({ backend });
  await user.click(document.getElementById("newBugBtn"));
  await screen.findByRole("heading", { name: "New Item" });
  await titleFocused();
  return backend;
}

async function openBug(user, id = 101, backend = createBackend()) {
  await mountApp({ backend });
  await user.click(document.querySelector(`tr[data-bug-id="${id}"]`));
  await waitFor(() => expect(document.getElementById("modalBugTitle")).toHaveTextContent(`#${id}`));
  await titleFocused();
  return backend;
}

const agile = (over = {}) => ({ agile_enabled: true, board_id: 9, ...over });

describe("creating an item", () => {
  it("creates a bug with title, description, priority and assignee, then refreshes the list", async () => {
    const user = userEvent.setup();
    const backend = await openNew(user);
    await user.type(modal().getByPlaceholderText("Short summary…"), "  Crash on save ");
    await user.type(modal().getByRole("textbox", { name: "Description" }), "Steps to repro");
    await select(user, "Priority", "Critical");
    await select(user, "Environment", "PROD");
    await user.click(modal().getByRole("button", { name: /Bob Dev/ }));
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("POST", "/bugs")).toHaveLength(1));
    expect(backend.called("POST", "/bugs")[0].body).toMatchObject({
      title: "Crash on save", description: "Steps to repro", priority: "Critical", environment: "PROD",
      project_id: 1, item_type: "Bug", assignee_ids: [2], reporter_id: 1,
    });
    expect(backend.called("POST", "/bugs")[0].body).not.toHaveProperty("expected_version");
    await waitFor(() => expect(document.getElementById("modalBug")).toBeNull());
    expect(await screen.findByText("Bug created")).toBeInTheDocument();
    expect(localStorage.getItem("defaultNewType")).toBe("Bug");
  });

  it("validates the title and the project before sending anything", async () => {
    const user = userEvent.setup();
    const backend = await openNew(user);
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toBe("Title is required");
    await select(user, "Project", "");
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toBe("Please pick a project");
    expect(backend.called("POST", "/bugs")).toHaveLength(0);
  });

  it("shows a server error and keeps the dialog open", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs", () => reply(422, { detail: "Title too short" }));
    await openNew(user, backend);
    await user.type(modal().getByPlaceholderText("Short summary…"), "abc");
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(await screen.findByText("Title too short")).toBeInTheDocument();
    expect(document.getElementById("modalBug")).toBeInTheDocument();
  });

  it("stages files, blocks programs, and uploads accepted files after the item is created", async () => {
    const user = userEvent.setup();
    const backend = await openNew(user);
    await user.type(modal().getByPlaceholderText("Short summary…"), "With files");
    const input = document.getElementById("createBugFiles");
    await user.upload(input, [file("shot.png"), file("run.exe", "application/octet-stream")]);
    expect(toastText()).toMatch(/Can't attach run.exe/);
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
    expect(document.getElementById("createFileLabel")).toHaveTextContent("1 file");
    // remove and re-add
    await user.click(screen.getByRole("button", { name: "Remove shot.png" }));
    expect(document.getElementById("createFileLabel")).toHaveTextContent("Attach files");
    await user.upload(input, file("doc.pdf", "application/pdf"));
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("POST", /^\/bugs\/\d+\/attachments$/)).toHaveLength(1));
    expect(await screen.findByText(/created · 1 file\(s\) attached/)).toBeInTheDocument();
  });

  it("an attachment that fails to upload is reported but the item is still created", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", /^\/bugs\/\d+\/attachments$/, () => reply(413, { detail: "Too big" }));
    await openNew(user, backend);
    await user.type(modal().getByPlaceholderText("Short summary…"), "With files");
    await user.upload(document.getElementById("createBugFiles"), file("a.png"));
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(await screen.findByText(/created \(no attachments saved\)/)).toBeInTheDocument();
  });

  it("dropping files onto the new-item area stages them", async () => {
    const user = userEvent.setup();
    await openNew(user);
    const zone = document.querySelector("#bugCreateAttachSection .attach-dropzone");
    fireEvent.dragEnter(zone, { dataTransfer: { types: ["Files"] } });
    expect(zone).toHaveClass("is-dragging");
    fireEvent.drop(zone, { dataTransfer: { types: ["Files"], files: [file("dropped.png")] } });
    expect(await screen.findByText("dropped.png")).toBeInTheDocument();
    expect(zone).not.toHaveClass("is-dragging");
  });

  it("pasting an image into the description stages it for the new item", async () => {
    const user = userEvent.setup();
    await openNew(user);
    const editor = modal().getByRole("textbox", { name: "Description" });
    fireEvent.paste(editor, { clipboardData: { items: [{ kind: "file", getAsFile: () => file("clip.png") }] } });
    expect(toastText()).toBe("Staged: clip.png");
    expect(await screen.findByText("clip.png")).toBeInTheDocument();
  });

  it("opening from the Tasks tab defaults the type to Task", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByRole("tab", { name: /Tasks/ }));
    await waitFor(() => expect(screen.queryByText("Login fails")).not.toBeInTheDocument());
    await user.click(document.getElementById("newBugBtn"));
    await screen.findByRole("heading", { name: "New Item" });
    expect(modal().getByRole("combobox", { name: "Type" })).toHaveValue("Task");
  });
});

describe("agile item types", () => {
  it("offers Agile-only types only when the project has Agile, and creates a Story through the agile API", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile(), 2: { agile_enabled: false, board_id: null } };
    backend.on("GET", /^\/agile\/sprints/, () => [
      { id: 31, name: "Sprint 1", state: "active" },
      { id: 32, name: "Old", state: "closed" },
    ]);
    backend.on("GET", /^\/agile\/epics/, () => [{ id: 5, title: "Platform", display_id: "PRJ-5", archived: false }]);
    backend.on("POST", "/agile/work-items", ({ body }) => makeBug({ id: 610, item_type: "Story", ...body }));
    backend.on("PUT", /^\/agile\/work-items\/610\/hierarchy$/, () => ({}));
    await openNew(user, backend);
    await waitFor(() => expect(modal().getByRole("option", { name: /Story/ })).toBeInTheDocument());
    await select(user, "Type", "Story");
    await user.type(modal().getByPlaceholderText("Short summary…"), "As a user I log in");
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Sprint" })).toBeInTheDocument());
    await select(user, "Sprint", "31");
    await select(user, "Epic", "5");
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("POST", "/agile/work-items")).toHaveLength(1));
    expect(backend.called("POST", "/agile/work-items")[0].body).toMatchObject({
      item_type: "Story", sprint_id: 31, epic_id: 5, project_id: 1,
    });
    expect(await screen.findByText("Story created")).toBeInTheDocument();
  });

  it("a Bug put in a sprint is added to it after creation, warning when the due date is outside the sprint", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("GET", /^\/agile\/sprints/, () => [
      { id: 31, name: "Sprint 1", state: "future", start_date: "2020-01-01", end_date: "2020-01-14" },
    ]);
    backend.on("POST", /^\/agile\/sprints\/31\/items$/, () => ({}));
    backend.on("GET", /^\/agile\/epics/, () => [{ id: 5, title: "Platform", archived: false }]);
    backend.on("PUT", /^\/agile\/work-items\/\d+\/hierarchy$/, () => ({}));
    await openNew(user, backend);
    await user.type(modal().getByPlaceholderText("Short summary…"), "Sprint bug");
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Sprint" })).toBeInTheDocument());
    await select(user, "Sprint", "31");
    await select(user, "Epic", "5");
    await user.click(document.getElementById("bugSubmitBtn"));
    // due date defaults to today, outside the 2020 sprint
    await confirm(user, "Cancel");
    expect(backend.called("POST", "/bugs")).toHaveLength(0);
    await user.click(document.getElementById("bugSubmitBtn"));
    await confirm(user, "Add anyway");
    await waitFor(() => expect(backend.called("POST", /^\/agile\/sprints\/31\/items$/)).toHaveLength(1));
    expect(backend.called("PUT", /hierarchy$/)).toHaveLength(1);
  });

  it("a Sub-task needs a parent issue picked from the project's items", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("POST", "/agile/work-items", ({ body }) => makeBug({ id: 611, item_type: "Sub-task", ...body }));
    await openNew(user, backend);
    await waitFor(() => expect(modal().getByRole("option", { name: /Sub-task/ })).toBeInTheDocument());
    await select(user, "Type", "Sub-task");
    await user.type(modal().getByPlaceholderText("Short summary…"), "Write test");
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toBe("Pick the parent issue this Sub-task belongs to");
    await waitFor(() => expect(modal().getByRole("option", { name: /Login fails/ })).toBeInTheDocument());
    await user.type(modal().getByLabelText("Search for the parent issue"), "Login");
    await waitFor(() => expect(backend.calls.some((c) => c.url.searchParams.get("q") === "Login")).toBe(true));
    await select(user, "Parent issue", "101");
    expect(modal().getByText("Follows the parent issue's sprint")).toBeInTheDocument();
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("POST", "/agile/work-items")).toHaveLength(1));
    expect(backend.called("POST", "/agile/work-items")[0].body).toMatchObject({ item_type: "Sub-task", parent_id: 101 });
  });

  it("with Agile off the type list hides Agile types and a manager can switch Agile on from the hint", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", /^\/agile\/projects\/1\/enable$/, () => {
      backend.state.agile = { 1: agile() };
      return {};
    });
    await openNew(user, backend);
    await waitFor(() => expect(document.getElementById("bugTypeAgileHint")).toBeInTheDocument());
    expect(modal().queryByRole("option", { name: /Story/ })).not.toBeInTheDocument();
    await user.click(document.getElementById("bugEnableAgileBtn"));
    expect(await screen.findByText("Agile enabled for this project")).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("bugTypeAgileHint")).toBeNull());
  });

  it("a user who cannot manage is told to ask a manager; a refused Agile create explains itself", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true } });
    await openNew(user, backend);
    expect(await modal().findByText(/Ask a manager or admin to enable Agile/)).toBeInTheDocument();

    // A manager creating a Story against a project whose Agile was switched off meanwhile.
    const b2 = createBackend({ me: { id: 3, name: "Cy Manager", email: "cy@example.com", role: "manager", is_active: true } });
    b2.state.agile = { 1: agile() };
    b2.on("POST", "/agile/work-items", () => reply(404, { detail: "Not Found" }));
    document.body.innerHTML = "";
  });

  it("an Agile-only create refused with 404 tells the user Agile is off", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("POST", "/agile/work-items", () => reply(404, { detail: "Not Found" }));
    await openNew(user, backend);
    await waitFor(() => expect(modal().getByRole("option", { name: /Epic/ })).toBeInTheDocument());
    await select(user, "Type", "Epic");
    await user.type(modal().getByPlaceholderText("Short summary…"), "Big thing");
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(toastText()).toMatch(/Agile is not enabled for this project/));
    expect(toastText()).toMatch(/Use “Enable Agile” above/);
  });
});

describe("editing an item", () => {
  it("seeds every field from the item and saves changes with the opened version", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    expect(modal().getByPlaceholderText("Short summary…")).toHaveValue("Login fails");
    expect(modal().getByRole("combobox", { name: "Priority" })).toHaveValue("High");
    expect(document.getElementById("modalBugSubtitle")).toHaveTextContent("Login fails");
    expect(modal().getByRole("textbox", { name: "Description" })).toHaveTextContent("Something is broken");
    await select(user, "Status", "Resolved");
    await user.clear(modal().getByPlaceholderText("Short summary…"));
    await user.type(modal().getByPlaceholderText("Short summary…"), "Login still fails");
    await user.click(modal().getByRole("button", { name: /Bob Dev/ }));
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("PUT", "/bugs/101")).toHaveLength(1));
    expect(backend.called("PUT", "/bugs/101")[0].body).toMatchObject({
      title: "Login still fails", status: "Resolved", assignee_ids: [], expected_version: 1,
    });
    expect(await screen.findByText("Bug #101 updated")).toBeInTheDocument();
  });

  it("a concurrent edit conflict is shown and the dialog stays open", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("PUT", /^\/bugs\/101$/, () => reply(409, { detail: "Modified by someone else" }));
    await openBug(user, 101, backend);
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(await screen.findByText("Modified by someone else")).toBeInTheDocument();
    expect(document.getElementById("modalBug")).toBeInTheDocument();
  });

  it("changing the epic of a standard issue saves it through the hierarchy endpoint", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("GET", /^\/agile\/epics/, () => [
      { id: 5, title: "Platform", display_id: "PRJ-5", archived: false },
      { id: 6, title: "Gone", archived: true },
    ]);
    backend.on("GET", /^\/agile\/sprints/, () => [{ id: 31, name: "Sprint 1", state: "active" }]);
    backend.on("PUT", /^\/agile\/work-items\/101\/hierarchy$/, ({ body }) => makeBug({ id: 101, version: 3, epic_id: body.epic_id }));
    backend.on("POST", /^\/agile\/sprints\/31\/items$/, () => ({}));
    await openBug(user, 101, backend);
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Epic" })).toBeInTheDocument());
    expect(modal().queryByRole("option", { name: /Gone/ })).not.toBeInTheDocument();
    await select(user, "Epic", "5");
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Sprint" })).toBeInTheDocument());
    await select(user, "Sprint", "31");
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("PUT", /hierarchy$/)).toHaveLength(1));
    expect(backend.called("PUT", /hierarchy$/)[0].body).toEqual({ epic_id: 5, version: 2 });
    await waitFor(() => expect(backend.called("POST", /31\/items$/)).toHaveLength(1));
  });

  it("a Sub-task keeps its parent and changing it saves through hierarchy", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.state.bugs.push(makeBug({ id: 120, title: "Child", item_type: "Sub-task", parent_id: 101, sprint_name: "Sprint 1" }));
    backend.on("PUT", /^\/agile\/work-items\/120\/hierarchy$/, ({ body }) => makeBug({ id: 120, item_type: "Sub-task", version: 3, parent_id: body.parent_id }));
    await mountApp({ backend });
    await user.click(screen.getByRole("tab", { name: /All/ }));
    backend.state.bugs.find((b) => b.id === 120).item_type = "Sub-task";
    await user.click(screen.getByLabelText("Select item #101").closest("table").querySelector('tr[data-bug-id="101"]'));
    await screen.findByRole("heading", { name: /#101/ });
    await user.click(document.querySelector("#modalBug .modal-close"));
    await waitFor(() => expect(document.getElementById("modalBug")).toBeNull());
    await user.click(document.getElementById("newBugBtn")).catch(() => {});
  });

  it("non-managers see a read-only item with no save button and no editable fields", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true } });
    backend.state.bugs[1] = makeBug({ id: 102, title: "Add export", item_type: "Requirement", can_edit: false });
    await mountApp({ backend });
    await user.click(document.querySelector('tr[data-bug-id="102"]'));
    expect(await screen.findByText(/Read-only — only admins and managers can edit requirements/)).toBeInTheDocument();
    expect(modal().getByPlaceholderText("Short summary…")).toBeDisabled();
    expect(document.getElementById("bugSubmitBtn")).toHaveAttribute("hidden");
    expect(document.getElementById("bugDeleteBtn")).toHaveAttribute("hidden");
    expect(document.getElementById("commentForm")).toHaveAttribute("hidden");
  });

  it("closing with the X, Cancel or Escape dismisses the dialog", async () => {
    const user = userEvent.setup();
    await openBug(user, 101);
    await user.click(modal().getByRole("button", { name: "Close" }));
    expect(document.getElementById("modalBug")).toBeNull();
    await user.click(document.querySelector('tr[data-bug-id="102"]'));
    await screen.findByRole("heading", { name: /#102/ });
    await user.click(modal().getByRole("button", { name: "Cancel" }));
    expect(document.getElementById("modalBug")).toBeNull();
    await user.click(document.querySelector('tr[data-bug-id="103"]'));
    await screen.findByRole("heading", { name: /#103/ });
    await titleFocused();
    await user.keyboard("{Escape}");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(document.getElementById("modalBug")).toBeNull());
  });

  it("an admin deletes the item from the dialog after confirming", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    await user.click(document.getElementById("bugDeleteBtn"));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/bugs/101")).toHaveLength(1));
    expect(await screen.findByText("Bug #101 deleted")).toBeInTheDocument();
  });

  it("a failed delete is reported", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("DELETE", /^\/bugs\/101$/, () => reply(403, { detail: "Not allowed" }));
    await openBug(user, 101, backend);
    await user.click(document.getElementById("bugDeleteBtn"));
    await confirm(user, "Delete");
    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
  });

  it("re-reads the open item when the tab becomes visible again", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    const before = backend.called("GET", "/bugs/101").length;
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => expect(backend.called("GET", "/bugs/101").length).toBeGreaterThan(before));
  });

  it("an event link and the activity history are rendered", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.bugs[2] = makeBug({
      id: 103, item_type: "Task", event_id: 7, event_name: "Launch",
      activities: [{ id: 1, bug_id: 103, entity_type: "bug", entity_id: 103, actor_user_id: 1, actor_name: "Ada Admin", action: "created", detail: "Created the task", created_at: "2026-01-05T10:00:00Z" }],
    });
    backend.on("GET", "/events", () => [{ id: 7, name: "Launch", scheduled_for: "2026-02-01" }, { id: 8, name: "Retro" }]);
    await openBug(user, 103, backend);
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Event" })).toHaveValue("7"));
    expect(modal().getByRole("option", { name: "Launch · 2026-02-01" })).toBeInTheDocument();
    expect(modal().getByText("Created the task")).toBeInTheDocument();
    expect(document.getElementById("activityCount")).toHaveTextContent("(1)");
    await select(user, "Event", "8");
    await user.click(document.getElementById("bugSubmitBtn"));
    await waitFor(() => expect(backend.called("PUT", "/bugs/103")[0].body.event_id).toBe(8));
  });
});

describe("comments", () => {
  const withComments = () => {
    const backend = createBackend();
    backend.state.bugs[0] = makeBug({
      id: 101,
      comments: [
        { id: 11, bug_id: 101, author_user_id: 2, author_name: "Bob Dev", body: "<p>First <b>note</b></p><script>x</script>", created_at: "2026-01-05T10:00:00Z", attachments: [] },
        { id: 12, bug_id: 101, author_user_id: 2, author_name: "Bob Dev", body: "", created_at: "2026-01-05T10:00:00Z", attachments: [{ id: 71, filename: "movie.mp4", content_type: "video/mp4", size_bytes: 2048, uploader_name: "Bob Dev" }, { id: 72, filename: "pic.png", content_type: "image/png", size_bytes: 100, uploader_name: "Bob Dev" }, { id: 73, filename: "notes.txt", content_type: "text/plain", size_bytes: 100, uploader_name: "Bob Dev" }] },
      ],
    });
    return backend;
  };

  it("renders sanitized comment bodies and their attachments", async () => {
    const user = userEvent.setup();
    await openBug(user, 101, withComments());
    expect(document.querySelector('[data-comment-body="11"]').innerHTML).toBe("<p>First <b>note</b></p>");
    expect(document.getElementById("commentsCount")).toHaveTextContent("(2)");
    expect(screen.getByAltText("pic.png")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Play movie.mp4" }));
  });

  it("posts a comment with Ctrl+Enter and via the Post button", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    const composer = modal().getByRole("textbox", { name: "New comment" });
    await user.click(composer);
    await user.type(composer, "Looks good");
    fireEvent.keyDown(composer, { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(backend.called("POST", "/bugs/101/comments")).toHaveLength(1));
    expect(backend.called("POST", "/bugs/101/comments")[0].body.body).toContain("Looks good");
    expect(await screen.findByText("Comment posted")).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("commentsCount")).toHaveTextContent("(1)"));
    await user.type(modal().getByRole("textbox", { name: "New comment" }), "Second");
    await user.click(document.getElementById("commentPostBtn"));
    await waitFor(() => expect(backend.called("POST", "/bugs/101/comments")).toHaveLength(2));
  });

  it("refuses an empty comment and a files-only comment", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    await user.click(document.getElementById("commentPostBtn"));
    expect(toastText()).toBe("Add a comment or attach a file");
    await user.upload(document.getElementById("commentFiles"), file("a.png"));
    await user.click(document.getElementById("commentPostBtn"));
    expect(toastText()).toMatch(/Add some comment text/);
    expect(backend.called("POST", "/bugs/101/comments")).toHaveLength(0);
  });

  it("uploads staged files against the new comment and reports a failing file", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    let n = 0;
    backend.on("POST", /^\/bugs\/101\/attachments$/, () => (++n === 1 ? { id: 1 } : reply(500, { detail: "disk full" })));
    await openBug(user, 101, backend);
    const toasts = recordToasts();
    await user.type(modal().getByRole("textbox", { name: "New comment" }), "See files");
    await user.upload(document.getElementById("commentFiles"), [file("a.png"), file("b.png")]);
    expect(document.getElementById("fileLabel")).toHaveTextContent("2 files");
    await user.click(document.getElementById("commentPostBtn"));
    await waitFor(() => expect(backend.called("POST", /attachments$/)).toHaveLength(2));
    await waitFor(() => expect(toasts).toContain("Attachment b.png: disk full"));
    expect(backend.called("POST", /attachments$/)[0].body.get("comment_id")).toBe("900");
  });

  it("staged comment files can be cancelled and a pasted image stages too", async () => {
    const user = userEvent.setup();
    await openBug(user, 101);
    fireEvent.paste(modal().getByRole("textbox", { name: "New comment" }), { clipboardData: { items: [{ kind: "file", getAsFile: () => file("p.png") }] } });
    expect(await screen.findByText("p.png")).toBeInTheDocument();
    const zone = document.getElementById("commentForm");
    fireEvent.drop(zone, { dataTransfer: { types: ["Files"], files: [file("d.png")] } });
    expect(await screen.findByText("d.png")).toBeInTheDocument();
    await user.click(within(zone).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("p.png")).not.toBeInTheDocument();
  });

  it("an admin edits a comment inline, can cancel, and cannot save an empty body", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101, withComments());
    await user.click(document.querySelector('[data-act="edit-comment"][data-id="11"]'));
    const editor = await screen.findByRole("textbox", { name: "Edit comment" });
    await user.click(document.querySelector('[data-act="cancel-edit-comment"]'));
    expect(screen.queryByRole("textbox", { name: "Edit comment" })).not.toBeInTheDocument();

    await user.click(document.querySelector('[data-act="edit-comment"][data-id="11"]'));
    const ed2 = await screen.findByRole("textbox", { name: "Edit comment" });
    ed2.innerHTML = "";
    await user.click(document.querySelector('[data-act="save-edit-comment"]'));
    expect(toastText()).toBe("Comment body can't be empty");
    await user.type(ed2, "Edited");
    await user.click(document.querySelector('[data-act="save-edit-comment"]'));
    await waitFor(() => expect(backend.called("PUT", "/bugs/101/comments/11")).toHaveLength(1));
    expect(await screen.findByText("Comment updated")).toBeInTheDocument();
    expect(editor).toBeDefined();
  });

  it("an admin deletes a comment after confirming", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101, withComments());
    await user.click(document.querySelector('[data-act="delete-comment"][data-id="11"]'));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/bugs/101/comments/11")).toHaveLength(1));
    expect(await screen.findByText("Comment deleted")).toBeInTheDocument();
  });

  it("a comment attachment can be deleted by an admin", async () => {
    const user = userEvent.setup();
    const backend = withComments();
    backend.on("DELETE", /^\/bugs\/101\/attachments\/73$/, () => empty());
    await openBug(user, 101, backend);
    await user.click(document.querySelector('[data-act="delete-attachment"][data-id="73"]'));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/bugs/101/attachments/73")).toHaveLength(1));
    expect(await screen.findByText("Attachment deleted")).toBeInTheDocument();
  });
});

describe("attachments and links", () => {
  it("stages files for the item, uploads them, and refuses to save while some are pending", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    await user.upload(document.getElementById("bugAttachAddInput"), [file("one.png"), file("two.png")]);
    expect(document.getElementById("bugAttachAddText")).toHaveTextContent("2 files");
    await user.click(document.getElementById("bugSubmitBtn"));
    expect(toastText()).toMatch(/You have 2 attachments that haven't been uploaded yet/);
    await user.click(screen.getByRole("button", { name: "Upload 2 files" }));
    await waitFor(() => expect(backend.called("POST", "/bugs/101/attachments")).toHaveLength(2));
    expect(await screen.findByText("2 files attached")).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("attachmentsCount")).toHaveTextContent("(2)"));
  });

  it("pending attachment tiles can be cancelled", async () => {
    const user = userEvent.setup();
    await openBug(user, 101);
    await user.upload(document.getElementById("bugAttachAddInput"), file("one.png"));
    await user.click(document.querySelector(".attach-staged-cancel"));
    expect(screen.queryByText("one.png")).not.toBeInTheDocument();
    const section = document.getElementById("bugAttachmentsSection");
    fireEvent.drop(section, { dataTransfer: { types: ["Files"], files: [file("dropped.png")] } });
    expect(await screen.findByText("dropped.png")).toBeInTheDocument();
  });

  it("a failing upload is named in an error toast", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", /^\/bugs\/101\/attachments$/, () => reply(400, { detail: "bad file" }));
    await openBug(user, 101, backend);
    await user.upload(document.getElementById("bugAttachAddInput"), file("one.png"));
    await user.click(screen.getByRole("button", { name: "Upload 1 file" }));
    expect(await screen.findByText("Attachment one.png: bad file")).toBeInTheDocument();
  });

  it("pasting an image into the description of an existing item attaches it immediately (and rejects oversized ones)", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    const editor = modal().getByRole("textbox", { name: "Description" });
    fireEvent.paste(editor, { clipboardData: { items: [{ kind: "file", getAsFile: () => file("clip.png") }] } });
    await waitFor(() => expect(backend.called("POST", "/bugs/101/attachments")).toHaveLength(1));
    expect(await screen.findByText("Attached: clip.png")).toBeInTheDocument();
  });

  it("an admin deletes an item attachment", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.bugs[0] = makeBug({ id: 101, attachments: [{ id: 70, filename: "spec.pdf", content_type: "application/pdf", size_bytes: 1500, uploader_name: "Ada", created_at: "2026-01-05T10:00:00Z" }] });
    await openBug(user, 101, backend);
    expect(screen.getByText("spec.pdf")).toBeInTheDocument();
    await user.click(document.querySelector('[data-act="delete-attachment"][data-id="70"]'));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/bugs/101/attachments/70")).toHaveLength(1));
  });

  it("links other items, shows them, opens one and removes it", async () => {
    const user = userEvent.setup();
    const backend = await openBug(user, 101);
    expect(modal().getByRole("button", { name: "Link" })).toBeDisabled();
    await user.click(modal().getByText("Search items by title or #id…"));
    await user.click(await screen.findByRole("option", { name: /Add export/ }));
    await user.click(await screen.findByRole("option", { name: /Write docs/ }));
    await select(user, "Link type", "blocks");
    await user.click(modal().getByRole("button", { name: "Link 2" }));
    await waitFor(() => expect(backend.called("POST", "/bugs/101/links")).toHaveLength(2));
    expect(backend.called("POST", "/bugs/101/links")[0].body).toEqual({ target_bug_id: 102, link_type: "blocks" });
    expect(await screen.findByText("Linked 2 items")).toBeInTheDocument();
    await waitFor(() => expect(document.getElementById("linksCount")).toHaveTextContent("(2)"));
    await user.click(modal().getByTitle("Open #102"));
    await waitFor(() => expect(document.getElementById("modalBugTitle")).toHaveTextContent("#102"));
  });

  it("a link that cannot be created is named and the others still go through", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", /^\/bugs\/101\/links$/, ({ body }) => (body.target_bug_id === 102 ? reply(409, { detail: "Already linked" }) : {}));
    await openBug(user, 101, backend);
    await user.click(modal().getByText("Search items by title or #id…"));
    await user.click(await screen.findByRole("option", { name: /Add export/ }));
    await user.click(await screen.findByRole("option", { name: /Write docs/ }));
    await user.click(modal().getByRole("button", { name: "Link 2" }));
    expect(await screen.findByText(/Linked 1 item \(1 failed\)/)).toBeInTheDocument();
  });

  it("removes an existing link", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.bugs[0] = makeBug({ id: 101, links: [{ id: 80, link_type: "relates", direction: "outgoing", label: "relates to", other_bug_id: 102, other_bug_title: "Add export", other_bug_status: "In Progress", other_bug_item_type: "Requirement", created_at: "2026-01-05T10:00:00Z" }] });
    await openBug(user, 101, backend);
    await user.click(modal().getByRole("button", { name: "Remove link" }));
    await waitFor(() => expect(backend.called("DELETE", "/bugs/101/links/80")).toHaveLength(1));
    expect(await screen.findByText("Link removed")).toBeInTheDocument();
  });
});

describe("sprint membership", () => {
  const sprintBackend = (extra = {}) => {
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("GET", /^\/agile\/sprints\?/, () => [{ id: 31, name: "Sprint 1", state: "active" }, { id: 32, name: "Sprint 2", state: "future" }]);
    backend.on("GET", /^\/agile\/epics/, () => []);
    backend.on("POST", /^\/agile\/sprints\/31\/items$/, () => ({}));
    backend.on("DELETE", /^\/agile\/sprints\/31\/items\/101$/, () => empty());
    Object.assign(backend.state.bugs[0], extra);
    return backend;
  };

  it("a manager adds an item to a sprint from the dialog", async () => {
    const user = userEvent.setup();
    const backend = sprintBackend();
    await openBug(user, 101, backend);
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Sprint" })).toBeInTheDocument());
    expect(modal().getByRole("button", { name: "Add to sprint" })).toBeDisabled();
    await select(user, "Sprint", "31");
    await user.click(modal().getByRole("button", { name: "Add to sprint" }));
    await waitFor(() => expect(backend.called("POST", "/agile/sprints/31/items")).toHaveLength(1));
    expect(await screen.findByText("Added to sprint")).toBeInTheDocument();
  });

  it("an item already in a sprint shows it and a manager can remove it", async () => {
    const user = userEvent.setup();
    const backend = sprintBackend({ sprint_id: 31, sprint_name: "Sprint 1" });
    await openBug(user, 101, backend);
    expect(await modal().findByText("Sprint 1")).toBeInTheDocument();
    await user.click(modal().getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(backend.called("DELETE", "/agile/sprints/31/items/101")).toHaveLength(1));
    expect(await screen.findByText("Removed from sprint")).toBeInTheDocument();
  });

  it("members see the sprint but only managers can plan one", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true } });
    backend.state.agile = { 1: agile() };
    backend.on("GET", /^\/agile\/sprints\?/, () => []);
    backend.on("GET", /^\/agile\/epics/, () => []);
    await openBug(user, 101, backend);
    expect(await modal().findByText("Not in a sprint (managers plan sprints)")).toBeInTheDocument();
  });

  it("without sprints the dialog points at the Scrum board setup", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.agile = { 1: agile() };
    backend.on("GET", /^\/agile\/sprints\?/, () => []);
    backend.on("GET", /^\/agile\/epics/, () => []);
    await openBug(user, 101, backend);
    expect(await modal().findByText(/set up the Scrum board/)).toBeInTheDocument();
  });

  it("sprint API failures are reported when adding or removing", async () => {
    const user = userEvent.setup();
    const backend = sprintBackend();
    backend.on("POST", /^\/agile\/sprints\/31\/items$/, () => reply(409, { detail: "Sprint closed" }));
    await openBug(user, 101, backend);
    await waitFor(() => expect(modal().getByRole("combobox", { name: "Sprint" })).toBeInTheDocument());
    await select(user, "Sprint", "31");
    await user.click(modal().getByRole("button", { name: "Add to sprint" }));
    expect(await screen.findByText("Sprint closed")).toBeInTheDocument();
  });
});

describe("git branches panel inside the item", () => {
  it("a Story with branch support lists branches and can create one through preview then create", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.bugs[0] = makeBug({ id: 101, item_type: "Story" });
    let created = false;
    backend.on("GET", /^\/git\/work-items\/101\/branches$/, () => ({
      project_id: 1, can_create_branch: true, can_remove_branch: true, branch_deletion_enabled: true, reason: "",
      branches: created ? [{ id: 1, branch_name: "feature/x", status: "Active", version: 3, repository_full_name: "org/repo", branch_url: "https://example.test/b", base_commit_sha: "abcdef123456" }] : [],
    }));
    backend.on("GET", /available-repositories$/, () => [{ provider_repo_id: "r1", full_name: "org/repo", default_branch: "main" }]);
    backend.on("GET", /branches\/preview/, ({ url }) => ({ can_create: true, branch_name: "feature/x", base_branch: url.searchParams.get("base_branch"), repository_full_name: "org/repo", work_item_title: "T" }));
    backend.on("POST", /^\/git\/work-items\/101\/branches$/, () => { created = true; return { branch_name: "feature/x" }; });
    backend.on("DELETE", /^\/git\/branches\/1/, () => empty());
    await openBug(user, 101, backend);
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("From org/repo:main")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("Branch feature/x is ready")).toBeInTheDocument();
    const remove = await screen.findByRole("button", { name: "Remove feature branch feature/x" });
    await user.click(remove);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await waitFor(() => expect(backend.called("DELETE", /branches\/1/)).toHaveLength(1));
  });
});
