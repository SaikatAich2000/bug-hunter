/** The Development section of a Story: listing branches, previewing and creating one, removing one. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, reply } from "../test/backend";
import ConfirmHost from "./ConfirmHost";
import GitBranchesPanel from "./GitBranchesPanel";

const branch = (over = {}) => ({
  id: 1, branch_name: "feature/PRJ-1-login", branch_url: "https://git.example/b/1", status: "Active", version: 3,
  repository_full_name: "acme/app", base_commit_sha: "0123456789abcdef", ...over,
});
const listing = (over = {}) => ({
  project_id: 4, branches: [], can_create_branch: true, branch_deletion_enabled: true, can_remove_branch: true, reason: "", ...over,
});
const repos = [
  { provider_repo_id: "r1", full_name: "acme/app", default_branch: "main" },
  { provider_repo_id: "r2", name: "acme-docs", default_branch: "" },
];

let backend;
beforeEach(() => {
  backend = createBackend();
  vi.stubGlobal("fetch", backend.fetch);
  backend.on("GET", "/git/work-items/7/branches", () => listing());
  backend.on("GET", "/git/projects/4/available-repositories", () => repos);
});
afterEach(() => vi.unstubAllGlobals());

function mount(props = {}) {
  return render(<><div id="toast" /><ConfirmHost /><GitBranchesPanel targetId={7} itemType="Story" {...props} /></>);
}

describe("listing", () => {
  it("renders nothing while hidden or without a target", () => {
    const { container } = render(<GitBranchesPanel targetId={7} visible={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(backend.called("GET", /branches$/)).toHaveLength(0);
    const none = render(<GitBranchesPanel itemType="Story" />);
    expect(none.container).toBeEmptyDOMElement();
  });

  it("says when there are no branches yet", async () => {
    mount();
    expect(await screen.findByText("No Git branches yet")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Development" })).toBeInTheDocument();
  });

  it("lists active branches before deleted history, with repository, short sha and status", async () => {
    backend.on("GET", "/git/work-items/7/branches", () => listing({
      branches: [branch({ id: 2, branch_name: "feature/old", status: "Deleted" }), branch()],
    }));
    mount();
    const rows = await screen.findAllByRole("link");
    expect(rows.map((a) => a.textContent)).toEqual(["feature/PRJ-1-login", "feature/old"]);
    expect(screen.getAllByText(/acme\/app · 01234567/).length).toBe(2);
    expect(screen.getByText("Deleted")).toHaveAttribute("data-branch-status", "Deleted");
    expect(screen.getAllByRole("button", { name: /Remove feature branch/ })).toHaveLength(1);
  });

  it("a failed load is reported and nothing else renders", async () => {
    backend.on("GET", "/git/work-items/7/branches", () => reply(500, { detail: "Git offline" }));
    mount();
    expect(await screen.findByText("Git offline")).toBeInTheDocument();
    expect(screen.queryByText("No Git branches yet")).toBeInTheDocument();
  });

  it("explains why a branch cannot be created", async () => {
    backend.on("GET", "/git/work-items/7/branches", () => listing({ can_create_branch: false, reason: "Connect a repository first" }));
    mount();
    expect(await screen.findByText("Connect a repository first")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create Feature Branch" })).not.toBeInTheDocument();
  });

  it("shows nothing about creation when it is impossible without a reason", async () => {
    backend.on("GET", "/git/work-items/7/branches", () => listing({ can_create_branch: false }));
    mount();
    await screen.findByText("No Git branches yet");
    expect(document.querySelector(".git-branch-create, .git-branch-reason")).toBeNull();
  });
});

describe("creating a branch", () => {
  it("loads repositories on demand, previews, and creates from the previewed base", async () => {
    const user = userEvent.setup();
    backend.on("GET", /branches\/preview\?/, () => ({
      can_create: true, branch_name: "feature/PRJ-7-x", repository_full_name: "acme/app", base_branch: "develop", work_item_title: "A story",
    }));
    backend.on("POST", "/git/work-items/7/branches", () => ({ branch_name: "feature/PRJ-7-x" }));
    mount();
    expect(backend.called("GET", /available-repositories/)).toHaveLength(0);
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    const repo = await screen.findByLabelText("Repository for new Git branch");
    expect(repo).toHaveValue("r1");
    expect(screen.getByLabelText("Base branch")).toHaveValue("main");
    await user.selectOptions(repo, "r2");
    expect(screen.getByLabelText("Base branch")).toHaveValue("");
    expect(screen.getByRole("option", { name: "acme-docs" })).toBeInTheDocument();
    await user.selectOptions(repo, "r1");
    await user.type(screen.getByLabelText("Base branch"), "x");
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    const confirmBox = await screen.findByLabelText("Confirm Git branch creation");
    expect(confirmBox).toHaveTextContent("From acme/app:develop");
    await user.click(within(confirmBox).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Confirm Git branch creation")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    await user.click(within(await screen.findByLabelText("Confirm Git branch creation")).getByRole("button", { name: "Create Feature Branch" }));
    await waitFor(() => expect(backend.called("POST", "/git/work-items/7/branches")).toHaveLength(1));
    expect(backend.called("POST", "/git/work-items/7/branches")[0].body).toEqual({ provider_repo_id: "r1", base_branch: "develop" });
    expect(await screen.findByText("Branch feature/PRJ-7-x is ready")).toBeInTheDocument();
  });

  it("a preview that cannot proceed gives the reason instead of a button", async () => {
    const user = userEvent.setup();
    backend.on("GET", /branches\/preview\?/, () => ({ can_create: false, branch_name: "b", repository_full_name: "acme/app", base_branch: "main" }));
    mount();
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("This branch cannot be created.")).toBeInTheDocument();
    backend.on("GET", /branches\/preview\?/, () => ({ can_create: false, reason: "Branch already exists", branch_name: "b", repository_full_name: "r", base_branch: "m" }));
    await user.type(screen.getByLabelText("Base branch"), "z"); // editing the base discards the preview
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("Branch already exists")).toBeInTheDocument();
  });

  it("a failed preview and a failed create are reported", async () => {
    const user = userEvent.setup();
    backend.on("GET", /branches\/preview\?/, () => reply(422, { detail: "Bad base branch" }));
    mount();
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("Bad base branch")).toBeInTheDocument();
    backend.on("GET", /branches\/preview\?/, () => ({ can_create: true, branch_name: "b", repository_full_name: "acme/app", base_branch: "main" }));
    backend.on("POST", "/git/work-items/7/branches", () => reply(409, { detail: "Branch exists remotely" }));
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    await user.click(within(await screen.findByLabelText("Confirm Git branch creation")).getByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText("Branch exists remotely")).toBeInTheDocument();
  });

  it("repositories that cannot be loaded show why and can be retried; none accessible says so", async () => {
    const user = userEvent.setup();
    let fail = true;
    backend.on("GET", /available-repositories/, () => (fail ? reply(502, { detail: "Provider unreachable" }) : []));
    mount();
    await user.click(await screen.findByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Provider unreachable");
    fail = false;
    await user.click(screen.getByRole("button", { name: "Create Feature Branch" }));
    expect(await screen.findByText(/No repositories are accessible/)).toBeInTheDocument();
  });
});

describe("removing a branch", () => {
  it("asks first, sends the exact record and version, and reloads", async () => {
    const user = userEvent.setup();
    backend.on("GET", "/git/work-items/7/branches", () => listing({ branches: [branch()] }));
    backend.on("DELETE", /^\/git\/branches\/1/, () => ({}));
    mount();
    await user.click(await screen.findByRole("button", { name: "Remove feature branch feature/PRJ-1-login" }));
    expect(document.getElementById("modalConfirm")).toHaveTextContent("Repository: acme/app");
    await confirm(user, "Cancel");
    expect(backend.calls.some((c) => c.method !== "GET")).toBe(false);
    await user.click(screen.getByRole("button", { name: "Remove feature branch feature/PRJ-1-login" }));
    await confirm(user, "Remove");
    await waitFor(() => expect(backend.calls.some((c) => c.method !== "GET")).toBe(true));
    expect(await screen.findByText("Feature branch removed")).toBeInTheDocument();
  });

  it("a refused removal is reported", async () => {
    const user = userEvent.setup();
    backend.on("GET", "/git/work-items/7/branches", () => listing({ branches: [branch()] }));
    backend.on("DELETE", /^\/git\/branches\/1/, () => reply(403, { detail: "Not permitted" }));
    mount();
    await user.click(await screen.findByRole("button", { name: /Remove feature branch/ }));
    await confirm(user, "Remove");
    expect(await screen.findByText("Not permitted")).toBeInTheDocument();
  });
});
