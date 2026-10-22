/** Releases & labels tab: releases, components and labels (create, release, archive, delete). */
import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { confirm, createBackend, reply } from "../../test/backend";
import { makeAgile, openSprints } from "../../test/agile";

const open = (user, opts = {}) => openSprints(user, { tab: "Releases", ...opts });
const subtab = (user, name) => user.click(screen.getByRole("tab", { name }));

describe("project setup", () => {
  it("says so when the project turns out not to have Scrum enabled", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let calls = 0;
    backend.on("GET", /^\/agile\/projects\/1\/settings$/, () => {
      calls += 1;
      return calls <= 1 ? { agile_enabled: true, board_id: 9 } : { agile_enabled: false, board_id: null };
    });
    await open(user, { backend });
    expect(await screen.findByText("Scrum is not set up for this project yet")).toBeInTheDocument();
  });

  it("reports a failed settings check", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let calls = 0;
    backend.on("GET", /^\/agile\/projects\/1\/settings$/, () => {
      calls += 1;
      return calls <= 1 ? { agile_enabled: true, board_id: 9 } : reply(500, { detail: "Settings down" });
    });
    await open(user, { backend });
    expect(await screen.findByText("Settings down")).toBeInTheDocument();
    expect(screen.getByText("Scrum is not set up for this project yet")).toBeInTheDocument();
  });
});

describe("releases", () => {
  it("lists releases with status and date; only unreleased ones can be released or archived", async () => {
    const user = userEvent.setup();
    await open(user);
    const row = (await screen.findByText("v1.0")).closest("tr");
    expect(within(row).getByText("unreleased")).toBeInTheDocument();
    expect(within(row).getByText("—")).toBeInTheDocument();
    const done = screen.getByText("v0.9").closest("tr");
    expect(within(done).getByText("2025-12-01")).toBeInTheDocument();
    expect(within(done).queryByRole("button")).not.toBeInTheDocument();
  });

  it("creates a release (name required) and clears the field", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Create" }));
    expect(await screen.findByText("Release name required")).toBeInTheDocument();
    const box = screen.getByPlaceholderText("e.g. v2.1");
    await user.type(box, "  v2.0 ");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(backend.called("POST", "/agile/releases")).toHaveLength(1));
    expect(backend.called("POST", "/agile/releases")[0].body).toEqual({ project_id: 1, name: "v2.0" });
    expect(await screen.findByText("Release created")).toBeInTheDocument();
    expect(box).toHaveValue("");
  });

  it("releases a version after confirming, sending its version number", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await user.click(await screen.findByRole("button", { name: "Release" }));
    await confirm(user, "Cancel");
    expect(backend.called("POST", /\/release$/)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Release" }));
    await confirm(user, "Release");
    await waitFor(() => expect(backend.called("POST", "/agile/releases/1/release")).toHaveLength(1));
    expect(backend.called("POST", "/agile/releases/1/release")[0].body).toEqual({ version: 2 });
    expect(await screen.findByText("Version released")).toBeInTheDocument();
  });

  it("archives a release after confirming; failures are reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    let fail = true;
    backend.on("POST", /^\/agile\/releases\/\d+\/archive$/, () => {
      if (fail) { fail = false; return reply(409, { detail: "Release in use" }); }
      return {};
    });
    backend.on("POST", /\/release$/, () => reply(409, { detail: "Already released" }));
    await open(user, { backend });
    await user.click(await screen.findByRole("button", { name: "Archive" }));
    await confirm(user, "Archive");
    expect(await screen.findByText("Release in use")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await confirm(user, "Archive");
    expect(await screen.findByText("Release archived")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Release" }));
    await confirm(user, "Release");
    expect(await screen.findByText("Already released")).toBeInTheDocument();
  });

  it("a create failure is reported; no releases shows the empty note", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.releases = [];
    backend.on("POST", /^\/agile\/releases$/, () => reply(400, { detail: "Duplicate name" }));
    await open(user, { backend });
    expect(await screen.findByText("No releases yet")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("e.g. v2.1"), "v1");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Duplicate name")).toBeInTheDocument();
  });

  it("a load failure is reported and non-managers get no create row", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/releases/, () => reply(500, { detail: "Releases down" }));
    await open(user, { backend, role: "user" });
    expect(await screen.findByText("Releases down")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("e.g. v2.1")).not.toBeInTheDocument();
  });
});

describe("components", () => {
  it("lists active components, creates and archives them", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await subtab(user, "Components");
    expect(await screen.findByText("Backend")).toBeInTheDocument();
    expect(screen.queryByText("Legacy")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Component name required")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("e.g. Backend"), "Mobile");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(backend.called("POST", "/agile/components")[0].body).toEqual({ project_id: 1, name: "Mobile" }));
    expect(await screen.findByText("Component created")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await confirm(user, "Archive");
    await waitFor(() => expect(backend.called("POST", "/agile/components/1/archive")).toHaveLength(1));
    expect(await screen.findByText("Component archived")).toBeInTheDocument();
  });

  it("reports failures and shows the empty note", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /^\/agile\/components/, () => reply(400, { detail: "Component clash" }));
    await open(user, { backend });
    await subtab(user, "Components");
    await user.type(await screen.findByPlaceholderText("e.g. Backend"), "X");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Component clash")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await confirm(user, "Cancel");
    await user.click(screen.getByRole("button", { name: "Archive" }));
    await confirm(user, "Archive");
    await waitFor(() => expect(screen.getAllByText("Component clash")).not.toHaveLength(0));
  });

  it("without any components says so", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.components = [];
    await open(user, { backend });
    await subtab(user, "Components");
    expect(await screen.findByText("No components yet")).toBeInTheDocument();
  });

  it("a load failure is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/components/, () => reply(500, { detail: "Components down" }));
    await open(user, { backend });
    await subtab(user, "Components");
    expect(await screen.findByText("Components down")).toBeInTheDocument();
  });
});

describe("labels", () => {
  it("shows labels with usage counts; creates and deletes (after confirming) a label", async () => {
    const user = userEvent.setup();
    const backend = await open(user);
    await subtab(user, "Labels");
    expect(await screen.findByText(/backend \(3\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Label name required")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("e.g. security"), "security");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(backend.called("POST", "/agile/labels")[0].body).toEqual({ project_id: 1, display_name: "security" }));
    expect(await screen.findByText("Label created")).toBeInTheDocument();
    await user.click(within(document.querySelector(".taxonomy-panel")).getByTitle("Delete"));
    expect(document.getElementById("modalConfirm")).toHaveTextContent('Delete label "backend"? Used on 3 item(s)');
    await confirm(user, "Cancel");
    expect(backend.called("DELETE", /labels/)).toHaveLength(0);
    await user.click(within(document.querySelector(".taxonomy-panel")).getByTitle("Delete"));
    await confirm(user, "Delete");
    await waitFor(() => expect(backend.called("DELETE", "/agile/labels/5")).toHaveLength(1));
    expect(await screen.findByText("Label deleted")).toBeInTheDocument();
  });

  it("reports failures; empty and read-only states", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("POST", /^\/agile\/labels/, () => reply(400, { detail: "Label clash" }));
    backend.on("DELETE", /^\/agile\/labels/, () => reply(409, { detail: "Label in use" }));
    await open(user, { backend });
    await subtab(user, "Labels");
    await user.type(await screen.findByPlaceholderText("e.g. security"), "x");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Label clash")).toBeInTheDocument();
    await user.click(within(document.querySelector(".taxonomy-panel")).getByTitle("Delete"));
    await confirm(user, "Delete");
    expect(await screen.findByText("Label in use")).toBeInTheDocument();
  });

  it("no labels says so and non-managers cannot create or delete", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.state.labels = [];
    await open(user, { backend, role: "user" });
    await subtab(user, "Labels");
    expect(await screen.findByText("No labels yet")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("e.g. security")).not.toBeInTheDocument();
  });

  it("a load failure is reported", async () => {
    const user = userEvent.setup();
    const backend = makeAgile(createBackend());
    backend.on("GET", /^\/agile\/labels/, () => reply(500, { detail: "Labels down" }));
    await open(user, { backend });
    await subtab(user, "Labels");
    expect(await screen.findByText("Labels down")).toBeInTheDocument();
  });
});
