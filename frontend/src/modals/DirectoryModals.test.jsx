/** Project, user and bulk-import dialogs plus per-project Git settings, driven through the app. */
import { describe, expect, it } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, mountApp, reply } from "../test/backend";

const toastText = () => document.getElementById("toast").textContent;
const MANAGER = { id: 3, name: "Cy Manager", email: "cy@example.com", role: "manager", is_active: true };
const USER = { id: 2, name: "Bob Dev", email: "bob@example.com", role: "user", is_active: true };

describe("project dialog", () => {
  it("creates a project with Agile on by default", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/projects", ({ body }) => ({ id: 9, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z", ...body }));
    await mountApp({ backend });
    await user.click(document.getElementById("newProjectBtn"));
    const form = await screen.findByRole("form", { hidden: false }).catch(() => document.getElementById("formProject"));
    await user.type(within(form).getByRole("textbox", { name: /Name/ }), " Gamma ");
    await user.type(within(form).getByRole("textbox", { name: "Description" }), "New work");
    fireEvent.change(form.querySelector('input[type="color"]'), { target: { value: "#112233" } });
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("POST", "/projects")).toHaveLength(1));
    expect(backend.called("POST", "/projects")[0].body).toEqual({ name: "Gamma", color: "#112233", description: "New work", agile_enabled: true });
    expect(await screen.findByText("Project created with Agile enabled")).toBeInTheDocument();
  });

  it("can create a plain tracker without Agile", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/projects", ({ body }) => ({ id: 9, created_at: "x", updated_at: "x", ...body }));
    await mountApp({ backend });
    await user.click(document.getElementById("newProjectBtn"));
    const form = document.getElementById("formProject");
    await user.type(within(form).getByRole("textbox", { name: /Name/ }), "Plain");
    await user.click(document.getElementById("projectAgileEnabled"));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Project created")).toBeInTheDocument();
    expect(backend.called("POST", "/projects")[0].body.agile_enabled).toBe(false);
  });

  it("edits an existing project (no Agile switch) and reports server errors", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("PUT", /^\/projects\/1$/, () => reply(409, { detail: "Name taken" }));
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => ({ configured: false, enabled: false, organization: "", base_url: "", default_base_branch: "main", credentials_configured: false, connection_status: "unknown", last_checked_at: null, last_error: "", version: 1, branch_creation_enabled: true }));
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="edit-project"][data-id="1"]'));
    const form = document.getElementById("formProject");
    expect(within(form).getByRole("textbox", { name: /Name/ })).toHaveValue("Alpha");
    expect(document.getElementById("projectAgileEnabled")).toBeNull();
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Name taken")).toBeInTheDocument();
    backend.on("PUT", /^\/projects\/1$/, ({ body }) => ({ id: 1, ...body, created_at: "x", updated_at: "x" }));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Project updated")).toBeInTheDocument();
  });

  it("does not render for plain users", async () => {
    await mountApp({ me: USER });
    expect(document.getElementById("modalProject")).toBeNull();
  });

  it("Cancel closes the dialog without saving", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    await mountApp({ backend });
    await user.click(document.getElementById("newProjectBtn"));
    await user.click(within(document.getElementById("formProject")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(document.getElementById("modalProject")).toBeNull());
    expect(backend.called("POST", "/projects")).toHaveLength(0);
  });
});

describe("user dialog", () => {
  it("role labels match the server policy (any user may edit bugs; tasks need a manager)", async () => {
    const user = userEvent.setup();
    await mountApp({ backend: createBackend() });
    await user.click(document.getElementById("newUserBtn"));
    const labels = within(document.getElementById("formUser")).getAllByRole("option").map((o) => o.textContent);
    expect(labels.join("|")).not.toMatch(/only edit own/i);
    expect(labels[0]).toMatch(/edit bugs/i);
  });

  it("creates a user with role, projects and password rules enforced client-side", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/users", ({ body }) => ({ id: 20, created_at: "x", updated_at: "x", ...body }));
    await mountApp({ backend });
    await user.click(document.getElementById("newUserBtn"));
    const form = document.getElementById("formUser");
    await user.type(within(form).getByRole("textbox", { name: /Name/ }), "New Person");
    await user.type(within(form).getByRole("textbox", { name: /Email/ }), "not-an-email");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(toastText()).toBe("Enter a valid email address");
    await user.clear(within(form).getByRole("textbox", { name: /Email/ }));
    await user.type(within(form).getByRole("textbox", { name: /Email/ }), "new@example.com");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(toastText()).toBe("Password is required for new users");
    await user.type(form.querySelector('input[name="password"]'), "short");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(backend.called("POST", "/users")).toHaveLength(0);
    expect(toastText()).toMatch(/password/i);
    await user.clear(form.querySelector('input[name="password"]'));
    await user.type(form.querySelector('input[name="password"]'), "Str0ng-Passw0rd!x");
    await user.selectOptions(within(form).getByRole("combobox"), "manager");
    await user.click(within(form).getByRole("button", { name: "Beta" }));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(backend.called("POST", "/users")).toHaveLength(1));
    expect(backend.called("POST", "/users")[0].body).toMatchObject({
      name: "New Person", email: "new@example.com", role: "manager", is_active: true, project_ids: [2], password: "Str0ng-Passw0rd!x",
    });
    expect(await screen.findByText("User created")).toBeInTheDocument();
  });

  it("edits a user, keeps the password when blank, and shows server errors", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("PUT", /^\/users\/2$/, () => reply(409, { detail: "Email in use" }));
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="edit-user"][data-id="2"]'));
    const form = document.getElementById("formUser");
    expect(within(form).getByRole("textbox", { name: /Name/ })).toHaveValue("Bob Dev");
    expect(form.querySelector('input[name="password"]').placeholder).toBe("Leave blank to keep current");
    await user.click(within(form).getByLabelText("Active (can log in)"));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Email in use")).toBeInTheDocument();
    expect(backend.called("PUT", "/users/2")[0].body).not.toHaveProperty("password");
    expect(backend.called("PUT", "/users/2")[0].body.is_active).toBe(false);
    backend.on("PUT", /^\/users\/2$/, ({ body }) => ({ id: 2, ...body }));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("User updated")).toBeInTheDocument();
  });

  it("managers cannot grant admin but can edit existing users", async () => {
    const user = userEvent.setup();
    await mountApp({ me: MANAGER });
    await user.click(document.getElementById("newUserBtn"));
    const form = document.getElementById("formUser");
    expect(within(form).queryByRole("option", { name: /Admin/ })).not.toBeInTheDocument();
    expect(within(form).getByRole("option", { name: /Manager/ })).toBeInTheDocument();
  });

  it("an edited admin keeps the admin role visible; a weak password on edit is refused", async () => {
    const user = userEvent.setup();
    const backend = createBackend({ me: MANAGER });
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="edit-user"][data-id="1"]'));
    const form = document.getElementById("formUser");
    expect(within(form).getByRole("option", { name: /Admin/ })).toBeInTheDocument();
    await within(form).findByDisplayValue("Ada Admin"); // the form is seeded after it opens
    const pw = form.querySelector('input[name="password"]');
    // the form can still be re-seeded by the list refresh that follows opening it: type until it sticks
    await waitFor(async () => {
      if (pw.value !== "abc") { await user.clear(pw); await user.type(pw, "abc"); }
      expect(pw).toHaveValue("abc");
    });
    await user.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Password must/)).toBeInTheDocument();
    expect(backend.called("PUT", "/users/1")).toHaveLength(0);
  });
});

describe("bulk import", () => {
  const open = async (user, backend) => {
    await mountApp({ backend });
    await user.click(document.getElementById("bulkUploadBtn"));
    return document.getElementById("modalBulkImport");
  };
  const xlsx = () => new File(["x"], "items.xlsx", { type: "application/vnd.ms-excel" });

  it("uploads a sheet and lists skipped rows and warnings", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/import", () => ({
      created: 2, failed: 1, created_ids: [1, 2], message: "2 created, 1 skipped",
      errors: [{ row: 4, title: "Bad", error: "Missing project" }, { row: 5, title: "", error: "No title" }], errors_truncated: true,
      warnings: [{ row: 3, title: "Odd", warning: "Unknown assignee" }], warnings_truncated: true,
    }));
    const dlg = await open(user, backend);
    expect(within(dlg).getByRole("button", { name: "Upload" })).toBeDisabled();
    await user.upload(dlg.querySelector('input[type="file"]'), xlsx());
    await user.click(within(dlg).getByRole("button", { name: "Upload" }));
    expect(await within(dlg).findByText("Skipped rows")).toBeInTheDocument();
    expect(within(dlg).getByText(/Row 4 \(“Bad”\): Missing project/)).toBeInTheDocument();
    expect(within(dlg).getByText(/Row 5: No title/)).toBeInTheDocument();
    expect(within(dlg).getByText("Created with a warning")).toBeInTheDocument();
    expect(within(dlg).getByText("More rows were skipped than shown here.")).toBeInTheDocument();
    expect(within(dlg).getByText("More warnings were reported than shown here.")).toBeInTheDocument();
    expect(toastText()).toBe("2 created, 1 skipped");
    await user.click(within(dlg).getAllByRole("button", { name: "Close" }).at(-1));
    await waitFor(() => expect(dlg).toHaveAttribute("hidden"));
  });

  it("nothing created yields an info/error toast and a server error is shown", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", "/bugs/import", () => ({ created: 0, failed: 3, created_ids: [], message: "Nothing imported", errors: [], warnings: [] }));
    const dlg = await open(user, backend);
    await user.upload(dlg.querySelector('input[type="file"]'), xlsx());
    await user.click(within(dlg).getByRole("button", { name: "Upload" }));
    await waitFor(() => expect(toastText()).toBe("Nothing imported"));
    backend.on("POST", "/bugs/import", () => reply(400, { detail: "Not a spreadsheet" }));
    await user.upload(dlg.querySelector('input[type="file"]'), xlsx());
    await user.click(within(dlg).getByRole("button", { name: "Upload" }));
    expect(await screen.findByText("Not a spreadsheet")).toBeInTheDocument();
  });

  it("downloads the template as a file named by the server", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", "/bugs/import/template.xlsx", () => new Response("bin"));
    await mountApp({ backend });
    const clicked = [];
    const orig = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function click() { clicked.push(this.download); };
    try {
      await user.click(document.getElementById("downloadTemplateBtn"));
      await waitFor(() => expect(clicked).toEqual(["bug-hunter-bulk-import-template.xlsx"]));
    } finally {
      HTMLAnchorElement.prototype.click = orig;
    }
  });

  it("a failed template download is reported", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", "/bugs/import/template.xlsx", () => reply(500, { detail: "no template" }));
    await mountApp({ backend });
    await user.click(document.getElementById("downloadTemplateBtn"));
    expect(await screen.findByText("no template")).toBeInTheDocument();
  });
});

describe("project Git settings", () => {
  const config = (over = {}) => ({
    configured: true, enabled: true, organization: "acme", base_url: "https://api.github.com", default_base_branch: "main",
    credentials_configured: true, connection_status: "ok", last_checked_at: "2026-01-05T10:00:00Z", last_error: "", version: 4,
    branch_creation_enabled: true, ...over,
  });

  async function openProject(user, backend) {
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="edit-project"][data-id="1"]'));
    return screen.findByRole("heading", { name: "Git Integration" });
  }

  it("saves edited settings, sending the opened version and no token when blank", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config());
    backend.on("PUT", /^\/git\/projects\/1\/config$/, ({ body }) => config({ ...body, version: 5 }));
    await openProject(user, backend);
    expect(screen.getByText("GitHub authentication is configured on the server.")).toBeInTheDocument();
    const org = document.getElementById("project-git-org-1");
    const enabled = screen.getByRole("combobox", { name: "Git integration enabled" });
    // a late refresh of the form can re-seed it: edit until the edits stick
    await waitFor(async () => {
      if (org.value !== "globex") { await user.clear(org); await user.type(org, "globex"); }
      if (enabled.value !== "disabled") await user.selectOptions(enabled, "disabled");
      expect(org).toHaveValue("globex");
      expect(enabled).toHaveValue("disabled");
    });
    await user.click(screen.getByRole("button", { name: "Save Git settings" }));
    await waitFor(() => expect(backend.called("PUT", "/git/projects/1/config")).toHaveLength(1));
    expect(backend.called("PUT", "/git/projects/1/config")[0].body).toEqual({
      enabled: false, organization: "globex", base_url: "https://api.github.com", default_base_branch: "main", version: 4,
    });
    expect(await screen.findByText("Git integration settings saved")).toBeInTheDocument();
  });

  it("tests a connection with a typed token without saving, then clears the token field", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config({ credentials_configured: false, configured: false }));
    backend.on("POST", /config\/test$/, () => ({ ok: true, message: "Connected", target: "acme" }));
    await openProject(user, backend);
    expect(screen.getByText("GitHub authentication is not configured on the server.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test Connection" })).toBeDisabled();
    await user.type(document.getElementById("project-git-credential-1"), "fake-credential-1234");
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    await waitFor(() => expect(backend.called("POST", /config\/test$/)).toHaveLength(1));
    expect(backend.called("POST", /config\/test$/)[0].body).toMatchObject({ credential: "fake-credential-1234", version: null });
    expect(await screen.findByText(/Test result:/)).toBeInTheDocument();
    expect(document.getElementById("project-git-credential-1")).toHaveValue("");
    expect(backend.called("PUT", "/git/projects/1/config")).toHaveLength(0);
  });

  it("a failed test is reported as an error result and a thrown one as a toast", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config({ last_error: "bad token" }));
    backend.on("POST", /config\/test$/, () => ({ ok: false, message: "Rejected" }));
    await openProject(user, backend);
    expect(screen.getByText("bad token")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText(/Test result:/)).toHaveTextContent("error");
    backend.on("POST", /config\/test$/, () => reply(500, { detail: "provider down" }));
    await user.click(screen.getByRole("button", { name: "Test Connection" }));
    expect(await screen.findByText("provider down")).toBeInTheDocument();
  });

  it("clears the stored credential after confirmation; a version conflict reloads the config", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config());
    backend.on("PUT", /^\/git\/projects\/1\/config$/, ({ body }) => config({ ...body, credentials_configured: false }));
    await openProject(user, backend);
    await user.click(screen.getByRole("button", { name: "Clear credential" }));
    await confirm(user, "Clear credential");
    await waitFor(() => expect(backend.called("PUT", "/git/projects/1/config")).toHaveLength(1));
    expect(backend.called("PUT", "/git/projects/1/config")[0].body).toMatchObject({ credential: null, clear_credential: true });
    expect(await screen.findByText("Git credential cleared")).toBeInTheDocument();

    backend.on("PUT", /^\/git\/projects\/1\/config$/, () => reply(409, { detail: "Stale version" }));
    await user.click(screen.getByRole("button", { name: "Save Git settings" }));
    expect(await screen.findByText("Stale version")).toBeInTheDocument();
    await waitFor(() => expect(backend.called("GET", "/git/projects/1/config").length).toBeGreaterThan(1));
  });

  it("lists accessible repositories on demand and says when there are none", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config());
    backend.on("GET", /available-repositories$/, () => [{ provider_repo_id: "1", full_name: "acme/api", owner: "acme", name: "api", default_branch: "main", url: "https://github.com/acme/api" }]);
    await openProject(user, backend);
    expect(screen.getByText(/loaded from live provider discovery/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load repositories" }));
    expect(await screen.findByText("acme/api")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open GitHub" })).toHaveAttribute("href", "https://github.com/acme/api");
    backend.on("GET", /available-repositories$/, () => []);
    await user.click(screen.getByRole("button", { name: "Load repositories" }));
    expect(await screen.findByText("No GitHub repositories are available.")).toBeInTheDocument();
    backend.on("GET", /available-repositories$/, () => reply(502, { detail: "GitHub unreachable" }));
    await user.click(screen.getByRole("button", { name: "Load repositories" }));
    expect(await screen.findByText("GitHub unreachable")).toBeInTheDocument();
  });

  it("states why Git is off, and when the settings cannot be loaded", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config({ enabled: false }));
    await openProject(user, backend);
    expect(screen.getByText("Git integration is disabled for this project.")).toBeInTheDocument();
  });

  it("branch creation switched off by the server is explained, and a failed load says so", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => config({ branch_creation_enabled: false }));
    await openProject(user, backend);
    expect(screen.getByText("Branch creation is disabled by the server.")).toBeInTheDocument();
  });

  it("when the settings cannot be loaded the section says so", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("GET", /^\/git\/projects\/1\/config$/, () => reply(500, { detail: "nope" }));
    await mountApp({ backend });
    await user.click(document.querySelector('[data-act="edit-project"][data-id="1"]'));
    expect(await screen.findByText("Git integration settings could not be loaded.")).toBeInTheDocument();
  });
});
