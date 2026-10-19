/** AppContext: load failures, notification mutations failing, deep links and the notification url helper. */
import { afterEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createBackend, makeEvent, mountApp, reply } from "../test/backend";
import { notificationUrl } from "./AppContext";

afterEach(() => { history.replaceState(null, "", "/"); });

const notes = () => [
  { id: 1, kind: "assigned", title: "Assigned to you", body: "Login fails", bug_id: 101, event_id: null, actor_name: "Cy", read_at: null, created_at: "2026-01-05T10:00:00Z" },
];

describe("notificationUrl", () => {
  it("links to the bug, else the event, else home", () => {
    expect(notificationUrl({ bug_id: 5, event_id: 9 })).toBe("/#bug=5");
    expect(notificationUrl({ bug_id: null, event_id: 9 })).toBe("/#event=9");
    expect(notificationUrl({})).toBe("/");
  });
});

describe("load failures", () => {
  it.each([
    ["/users", "Users down"],
    ["/projects", "Projects down"],
    ["/stats", "Stats down"],
    ["/meta", "Meta down"],
    ["/notifications", "Notifications down"],
  ])("a failing %s is reported and the rest of the app still loads", async (path, detail) => {
    const backend = createBackend();
    backend.on("GET", path === "/stats" || path === "/notifications" ? new RegExp(`^\\${path}(\\?|$)`) : path, () => reply(500, { detail }));
    await mountApp({ backend });
    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  it("a failing bug list is reported", async () => {
    const backend = createBackend();
    backend.on("GET", /^\/bugs\?/, () => reply(500, { detail: "List down" }));
    await mountApp({ backend, waitFor: false });
    expect(await screen.findByText("List down")).toBeInTheDocument();
  });
});

describe("notification actions that fail", () => {
  async function openBell(user, backend) {
    backend.state.notifications = notes();
    await mountApp({ backend });
    await user.click(await screen.findByRole("button", { name: /Notifications, 1 unread/ }));
    return screen.findByRole("dialog", { name: "Notifications" });
  }

  it("marking one read, all read and dismissing report the error and reload the list", async () => {
    const user = userEvent.setup();
    const backend = createBackend();
    backend.on("POST", /^\/notifications\/\d+\/read$/, () => reply(500, { detail: "Cannot mark read" }));
    backend.on("POST", "/notifications/read-all", () => reply(500, { detail: "Cannot mark all" }));
    backend.on("DELETE", /^\/notifications\/\d+$/, () => reply(500, { detail: "Cannot dismiss" }));
    let dialog = await openBell(user, backend);
    const loads = () => backend.called("GET", /^\/notifications\?/).length;
    const reopen = async () => {
      if (!screen.queryByRole("dialog", { name: "Notifications" })) {
        await user.click(await screen.findByRole("button", { name: /^Notifications/ }));
      }
      dialog = await screen.findByRole("dialog", { name: "Notifications" });
    };
    let before = loads();
    await user.click(within(dialog).getByRole("button", { name: "Mark all read" }));
    expect(await screen.findByText("Cannot mark all")).toBeInTheDocument();
    await waitFor(() => expect(loads()).toBeGreaterThan(before));
    await reopen();
    before = loads();
    await user.click(within(dialog).getByLabelText("Dismiss notification"));
    expect(await screen.findByText("Cannot dismiss")).toBeInTheDocument();
    await waitFor(() => expect(loads()).toBeGreaterThan(before));
    await reopen();
    before = loads();
    await user.click(within(dialog).getByText("Assigned to you"));
    expect(await screen.findByText("Cannot mark read")).toBeInTheDocument();
    await waitFor(() => expect(loads()).toBeGreaterThan(before));
  });
});

describe("deep links", () => {
  it("a #bug= link opens that bug on load and clears the hash", async () => {
    history.replaceState(null, "", "/#bug=101");
    const backend = createBackend();
    await mountApp({ backend });
    await waitFor(() => expect(backend.called("GET", "/bugs/101")).toHaveLength(1));
    expect(await screen.findByDisplayValue("Login fails")).toBeInTheDocument();
    expect(location.hash).toBe("");
  });

  it("a #bug= link to a missing bug reports the problem", async () => {
    history.replaceState(null, "", "/#bug=9999");
    await mountApp({ backend: createBackend() });
    expect(await screen.findByText("Not found")).toBeInTheDocument();
  });

  it("an #event= link opens the Events view on that event", async () => {
    history.replaceState(null, "", "/#event=7");
    const backend = createBackend();
    backend.state.events = [makeEvent({ id: 7, name: "Launch" })];
    await mountApp({ backend });
    expect(await screen.findByText("Launch", { selector: "#eventDetailName" })).toBeInTheDocument();
  });

  it("following a link while the app is open works too (hash change)", async () => {
    const backend = createBackend();
    await mountApp({ backend });
    history.replaceState(null, "", "/#bug=102");
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(backend.called("GET", "/bugs/102")).toHaveLength(1));
  });

  it("an unrelated hash is left alone", async () => {
    history.replaceState(null, "", "/#section");
    const backend = createBackend();
    await mountApp({ backend });
    expect(location.hash).toBe("#section");
    expect(backend.called("GET", /^\/bugs\/\d+$/)).toHaveLength(0);
  });
});
