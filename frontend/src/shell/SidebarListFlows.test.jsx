/** Sidebar filters (click and keyboard, delete failures) and the list's remaining bulk actions and pager. */
import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, makeBug, mountApp, reply } from "../test/backend";

const lastBugsQuery = (backend) => backend.called("GET", /^\/bugs\?/).at(-1).url.searchParams;

describe("sidebar filters", () => {
  it("clicking or pressing Enter on a swatch filters by that project, and again removes the filter", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    await mountApp({ backend });
    const swatch = screen.getByRole("button", { name: "Toggle filter: Beta" });
    await user.click(swatch);
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["2"]));
    expect(document.querySelector('[data-project-id="2"]')).toHaveClass("active");
    await user.click(screen.getByRole("button", { name: "Toggle filter: Alpha" }));
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["2", "1"]));
    swatch.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["1"]));
  });

  it("assignee avatars filter the same way, by click and keyboard", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    await mountApp({ backend });
    const avatar = screen.getByRole("button", { name: "Toggle filter: Bob Dev" });
    await user.click(avatar);
    await waitFor(() => expect(lastBugsQuery(backend).getAll("assignee_id")).toEqual(["2"]));
    avatar.focus();
    await user.keyboard(" ");
    await waitFor(() => expect(lastBugsQuery(backend).getAll("assignee_id")).toEqual([]));
  });

  it("people who cannot manage filter by clicking a project name", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.me = { ...backend.state.me, role: "user" };
    await mountApp({ backend });
    await user.click(document.querySelector('[data-project-id="2"] [data-act="open-project"]'));
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["2"]));
    document.querySelector('[data-project-id="1"] [data-act="open-project"]').focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["2", "1"]));
  });

  it("managers open the project and user editors from the names, by click or keyboard", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(document.querySelector('[data-project-id="2"] [data-act="open-project"]'));
    await waitFor(() => expect(document.getElementById("modalProject")).not.toHaveAttribute("hidden"));
    await user.click(document.querySelector("#modalProject .modal-close"));
    document.querySelector('[data-user-id="2"] [data-act="open-user"]').focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(document.getElementById("modalUser")).not.toHaveAttribute("hidden"));
  });

  it("an admin deleting a user or project that the server refuses sees the reason", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("DELETE", /^\/projects\/2$/, () => reply(409, { detail: "Project has bugs" }));
    backend.on("DELETE", /^\/users\/3$/, () => reply(409, { detail: "User owns items" }));
    await mountApp({ backend });
    await user.click(document.querySelector('[data-project-id="2"] [data-act="delete-project"]'));
    await confirm(user, "Delete");
    expect(await screen.findByText("Project has bugs")).toBeInTheDocument();
    await user.click(document.querySelector('[data-user-id="3"] [data-act="delete-user"]'));
    await confirm(user, "Delete");
    expect(await screen.findByText("User owns items")).toBeInTheDocument();
  });

  it("deleting a filtered project drops it from the filter", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("DELETE", /^\/projects\/2$/, () => ({}));
    await mountApp({ backend });
    await user.click(screen.getByRole("button", { name: "Toggle filter: Beta" }));
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual(["2"]));
    await user.click(document.querySelector('[data-project-id="2"] [data-act="delete-project"]'));
    await confirm(user, "Delete");
    await waitFor(() => expect(lastBugsQuery(backend).getAll("project_id")).toEqual([]));
    expect(await screen.findByText('Project "Beta" deleted')).toBeInTheDocument();
  });

  it("deleting a filtered assignee drops them from the filter", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("DELETE", /^\/users\/2$/, () => ({}));
    await mountApp({ backend });
    await user.click(screen.getByRole("button", { name: "Toggle filter: Bob Dev" }));
    await waitFor(() => expect(lastBugsQuery(backend).getAll("assignee_id")).toEqual(["2"]));
    await user.click(document.querySelector('[data-user-id="2"] [data-act="delete-user"]'));
    await confirm(user, "Delete");
    await waitFor(() => expect(lastBugsQuery(backend).getAll("assignee_id")).toEqual([]));
  });
});

describe("list bulk actions and pager", () => {
  it("bulk-sets priority and environment", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/bulk", () => ({ message: "2 updated", updated: 2 }));
    await mountApp({ backend });
    await user.click(screen.getByLabelText("Select item #101"));
    await user.selectOptions(screen.getByLabelText("Set priority for selected"), "Low");
    await waitFor(() => expect(backend.called("POST", "/bugs/bulk")).toHaveLength(1));
    expect(backend.called("POST", "/bugs/bulk")[0].body).toMatchObject({ action: "set_priority", value: "Low", ids: [101] });
    await user.click(screen.getByLabelText("Select item #102"));
    await user.selectOptions(screen.getByLabelText("Set environment for selected"), "UAT");
    await waitFor(() => expect(backend.called("POST", "/bugs/bulk")).toHaveLength(2));
    expect(backend.called("POST", "/bugs/bulk")[1].body).toMatchObject({ action: "set_environment", value: "UAT" });
  });

  it("an admin bulk-deletes after confirming; cancelling keeps everything", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/bulk", () => ({ message: "2 deleted", updated: 2 }));
    await mountApp({ backend });
    await user.click(screen.getByLabelText("Select item #101"));
    await user.click(screen.getByLabelText("Select item #102"));
    await user.click(document.querySelector(".bulk-delete"));
    expect(document.getElementById("modalConfirm")).toHaveTextContent("Delete 2 selected items?");
    await confirm(user, "Cancel");
    expect(backend.called("POST", "/bugs/bulk")).toHaveLength(0);
    await user.click(document.querySelector(".bulk-delete"));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("POST", "/bugs/bulk")).toHaveLength(1));
    expect(backend.called("POST", "/bugs/bulk")[0].body).toMatchObject({ action: "delete", ids: [101, 102] });
    expect(await screen.findByText("2 deleted")).toBeInTheDocument();
  });

  it("a single selected item reads in the singular", async () => {
    const user = userEvent.setup();
    await mountApp();
    await user.click(screen.getByLabelText("Select item #101"));
    await user.click(document.querySelector(".bulk-delete"));
    expect(document.getElementById("modalConfirm")).toHaveTextContent("Delete 1 selected item?");
  });

  it("a rejected bulk action is reported and keeps the selection", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/bulk", () => reply(409, { detail: "Version conflict" }));
    await mountApp({ backend });
    await user.click(screen.getByLabelText("Select item #101"));
    await user.selectOptions(screen.getByLabelText("Set status for selected"), "Resolved");
    expect(await screen.findByText("Version conflict")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
  });

  it("pages through results with Prev and Next", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/bugs\?/, ({ url }) => {
      const page = Number(url.searchParams.get("page") || 1);
      return { items: [makeBug({ id: 200 + page, title: `Page ${page} item` })], page, page_size: 1, total: 3, pages: 3 };
    });
    await mountApp({ backend, waitFor: false });
    expect(await screen.findByText("Page 1 item")).toBeInTheDocument();
    expect(document.getElementById("pgPrev")).toBeDisabled();
    await user.click(document.getElementById("pgNext"));
    expect(await screen.findByText("Page 2 item")).toBeInTheDocument();
    await user.click(document.getElementById("pgNext"));
    expect(await screen.findByText("Page 3 item")).toBeInTheDocument();
    expect(document.getElementById("pgNext")).toBeDisabled();
    await user.click(document.getElementById("pgPrev"));
    expect(await screen.findByText("Page 2 item")).toBeInTheDocument();
  });

  it("an empty list offers a New Item action that opens the form", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.state.bugs = [];
    await mountApp({ backend, empty: true });
    const action = document.querySelector("#viewList .empty-guide button, #viewList .empty-state button");
    await user.click(action);
    await waitFor(() => expect(document.getElementById("modalBug")).not.toHaveAttribute("hidden"));
  });
});
