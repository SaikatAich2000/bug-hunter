/** SPA entry: checks the session, restores theme/sidebar state, mounts the app or sends the user to sign in. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { createBackend } from "./test/backend";

let loc;
beforeEach(() => {
  vi.resetModules();
  loc = { replace: vi.fn(), search: "", hash: "", origin: "http://localhost", pathname: "/" };
  vi.stubGlobal("location", loc);
  document.body.innerHTML = '<div id="root"></div><div id="toast"></div>';
  document.body.className = "";
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("main entry", () => {
  it("mounts the app for a valid session with the stored theme and collapsed sidebar", async () => {
    const backend = createBackend();
    vi.stubGlobal("fetch", backend.fetch);
    localStorage.setItem("theme", "light");
    localStorage.setItem("sidebarCollapsed", "1");
    await import("./main.jsx");
    expect(await screen.findByText(/showing [1-9]\d* of/)).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.body).toHaveClass("sidebar-collapsed");
    expect(backend.called("GET", "/auth/me")).toHaveLength(1);
    expect(loc.replace).not.toHaveBeenCalled();
  });

  it("defaults to the dark theme and an expanded sidebar", async () => {
    vi.stubGlobal("fetch", createBackend().fetch);
    await import("./main.jsx");
    await screen.findByText(/showing [1-9]\d* of/);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(document.body).not.toHaveClass("sidebar-collapsed");
  });

  it("an expired session goes to the sign-in page and mounts nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "no" }), { status: 401 })));
    await import("./main.jsx");
    expect(loc.replace).toHaveBeenCalledWith("/login.html");
    expect(document.getElementById("root")).toBeEmptyDOMElement();
  });

  it("an unreachable server is treated like an expired session", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    await import("./main.jsx");
    expect(loc.replace).toHaveBeenCalledWith("/login.html");
  });

  it("blocked storage does not stop the app from booting", async () => {
    vi.stubGlobal("fetch", createBackend().fetch);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("denied", "SecurityError"); });
    await import("./main.jsx");
    expect(await screen.findByText(/showing [1-9]\d* of/)).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
