/** push.js: script loading, foreground messages, permission prompt, retry with backoff and reconnect handling. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let apiMock;
let toastMock;
let push;
let warnSpy;

const CONFIG = { enabled: true, api_key: "k", auth_domain: "a", project_id: "p", messaging_sender_id: "s", app_id: "app", vapid_key: "v" };

function notifications(permission = "granted", request) {
  const Stub = vi.fn();
  Stub.permission = permission;
  Stub.requestPermission = request ?? vi.fn(async () => permission);
  vi.stubGlobal("Notification", Stub);
  Object.defineProperty(window, "Notification", { value: Stub, configurable: true, writable: true });
  return Stub;
}

function pushSupport(reg = { showNotification: vi.fn(async () => undefined) }) {
  Object.defineProperty(navigator, "serviceWorker", {
    value: { register: vi.fn(async () => reg), getRegistration: vi.fn(async () => reg) },
    configurable: true, writable: true,
  });
  Object.defineProperty(window, "PushManager", { value: function PushManager() {}, configurable: true, writable: true });
  return reg;
}

/** Make loadScript see the SDK tags as already present. */
function sdkOnPage() {
  for (const name of ["app", "messaging"]) {
    const el = document.createElement("script");
    el.src = `/static/vendor/firebase-${name}-compat.js`;
    document.head.appendChild(el);
  }
}

function firebase({ token = "tok", onMessage = vi.fn(), apps = [] } = {}) {
  const getToken = vi.fn(async () => token);
  vi.stubGlobal("firebase", { apps, initializeApp: vi.fn(), messaging: () => ({ getToken, onMessage }) });
  return { getToken, onMessage };
}

beforeEach(async () => {
  vi.resetModules();
  localStorage.clear();
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.spyOn(console, "debug").mockImplementation(() => {});
  apiMock = vi.fn(async () => CONFIG);
  toastMock = vi.fn();
  vi.doMock("./api", () => ({ api: apiMock, ApiError: class ApiError extends Error {} }));
  vi.doMock("./toast", () => ({ toast: toastMock, toastError: vi.fn() }));
  push = await import("./push.js");
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.doUnmock("./api");
  vi.doUnmock("./toast");
  vi.restoreAllMocks();
  document.querySelectorAll("script").forEach((el) => el.remove());
});

describe("loading the SDK", () => {
  it("adds both compat scripts to the page and waits for them", async () => {
    pushSupport();
    notifications();
    const done = push.initPushOnBoot();
    await vi.waitFor(() => expect(document.querySelectorAll("script").length).toBe(1));
    const first = document.querySelector("script");
    expect(first.src).toContain("/static/vendor/firebase-app-compat.js");
    expect(first.async).toBe(true);
    firebase();
    first.onload();
    await vi.waitFor(() => expect(document.querySelectorAll("script").length).toBe(2));
    document.querySelectorAll("script")[1].onload();
    await done;
    expect(apiMock).toHaveBeenCalledWith("/push/subscribe", expect.anything());
  });

  it("a script that fails to load is removed again and the boot does not throw", async () => {
    pushSupport();
    notifications();
    vi.useFakeTimers();
    const done = push.initPushOnBoot();
    await vi.advanceTimersByTimeAsync(0);
    document.querySelector("script").onerror();
    await vi.advanceTimersByTimeAsync(0);
    expect(document.querySelectorAll("script").length).toBe(0);
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("subscribe attempt 1 failed"), expect.any(Error));
    await vi.advanceTimersByTimeAsync(5000);
    document.querySelector("script").onerror();
    await vi.advanceTimersByTimeAsync(0);
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("subscribe attempt 2 failed"), expect.any(Error));
    vi.useRealTimers();
    void done;
  });
});

describe("foreground messages", () => {
  async function boot() {
    const reg = pushSupport();
    notifications();
    sdkOnPage();
    const fb = firebase();
    await push.initPushOnBoot();
    return { reg, handler: fb.onMessage.mock.calls[0][0] };
  }

  it("shows a system notification through the service worker, carrying the click url", async () => {
    const { reg, handler } = await boot();
    document.head.insertAdjacentHTML("beforeend", '<meta name="application-asset-version" content="v7">');
    handler({ notification: { title: "Bug assigned", body: "PRJ-1" }, data: { url: "/#bug=1" } });
    expect(reg.showNotification).toHaveBeenCalledWith("Bug assigned", expect.objectContaining({
      body: "PRJ-1", data: { url: "/#bug=1" }, tag: "/#bug=1", icon: "/static/icon.png?v=v7",
    }));
    document.querySelector("meta[name=application-asset-version]").remove();
  });

  it("defaults the url and body, and ignores messages without a title", async () => {
    const { reg, handler } = await boot();
    handler({ notification: { title: "Hello" } });
    expect(reg.showNotification).toHaveBeenCalledWith("Hello", expect.objectContaining({ body: "", tag: "/" }));
    handler({ notification: {} });
    handler({});
    expect(reg.showNotification).toHaveBeenCalledTimes(1);
  });

  it("falls back to an in-app toast when the system notification fails", async () => {
    const { reg, handler } = await boot();
    reg.showNotification.mockRejectedValueOnce(new Error("blocked"));
    handler({ notification: { title: "Ping", body: "details" } });
    await vi.waitFor(() => expect(toastMock).toHaveBeenCalledWith("Ping: details", "info"));
    reg.showNotification.mockRejectedValueOnce(new Error("blocked"));
    handler({ notification: { title: "Solo" } });
    await vi.waitFor(() => expect(toastMock).toHaveBeenCalledWith("Solo", "info"));
  });
});

describe("permission and token", () => {
  it("asks for permission when undecided and subscribes once granted", async () => {
    pushSupport();
    const Stub = notifications("default", vi.fn(async () => "granted"));
    sdkOnPage();
    firebase();
    await push.initPushOnBoot();
    expect(Stub.requestPermission).toHaveBeenCalledTimes(1);
    expect(apiMock).toHaveBeenCalledWith("/push/subscribe", expect.anything());
  });

  it("a failing permission prompt is reported and nothing is subscribed", async () => {
    pushSupport();
    notifications("default", vi.fn(async () => { throw new Error("prompt blocked"); }));
    await push.initPushOnBoot();
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("permission prompt failed"), expect.any(Error));
    expect(apiMock).not.toHaveBeenCalledWith("/push/subscribe", expect.anything());
  });

  it("no token from FCM is retried with a delay and then stops when it arrives", async () => {
    pushSupport();
    notifications();
    sdkOnPage();
    vi.useFakeTimers();
    const getToken = vi.fn().mockResolvedValueOnce("").mockResolvedValue("late-token");
    vi.stubGlobal("firebase", { apps: [{}], initializeApp: vi.fn(), messaging: () => ({ getToken, onMessage: vi.fn() }) });
    const done = push.initPushOnBoot();
    await vi.advanceTimersByTimeAsync(0);
    expect(warnSpy).toHaveBeenCalledWith("[push] FCM returned no token", "");
    await vi.advanceTimersByTimeAsync(5000);
    await done;
    expect(localStorage.getItem("bh_push_token")).toBe("late-token");
    expect(getToken).toHaveBeenCalledTimes(2);
  });

  it("gives up after every retry delay has been used", async () => {
    pushSupport();
    notifications();
    sdkOnPage();
    vi.useFakeTimers();
    const getToken = vi.fn(async () => { throw new Error("network"); });
    vi.stubGlobal("firebase", { apps: [{}], initializeApp: vi.fn(), messaging: () => ({ getToken, onMessage: vi.fn() }) });
    const done = push.initPushOnBoot();
    for (const delay of [5_000, 15_000, 60_000, 300_000]) await vi.advanceTimersByTimeAsync(delay);
    await done;
    expect(getToken).toHaveBeenCalledTimes(5);
    expect(warnSpy).toHaveBeenCalledWith("[push] gave up subscribing to push after all retries", "");
  });

  it("a second boot while retrying does not start another loop", async () => {
    pushSupport();
    notifications();
    sdkOnPage();
    vi.useFakeTimers();
    const getToken = vi.fn(async () => "");
    vi.stubGlobal("firebase", { apps: [{}], initializeApp: vi.fn(), messaging: () => ({ getToken, onMessage: vi.fn() }) });
    const first = push.initPushOnBoot();
    await vi.advanceTimersByTimeAsync(0);
    await push.initPushOnBoot();
    expect(getToken).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
    void first;
  });

  it("a stored-token failure (storage blocked) does not stop the subscription", async () => {
    pushSupport();
    notifications();
    sdkOnPage();
    firebase();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("quota"); });
    await push.initPushOnBoot();
    expect(apiMock).toHaveBeenCalledWith("/push/subscribe", expect.anything());
  });
});

describe("coming back online", () => {
  it("re-subscribes when there is no stored token and push is enabled and allowed", async () => {
    pushSupport();
    notifications();
    sdkOnPage();
    firebase();
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith("/push/subscribe", expect.anything()));
  });

  it("does nothing when a token is already stored", async () => {
    pushSupport();
    notifications();
    localStorage.setItem("bh_push_token", "have");
    window.dispatchEvent(new Event("online"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock).not.toHaveBeenCalled();
  });

  it("does nothing when push is disabled or the permission is not granted", async () => {
    pushSupport();
    notifications("denied");
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith("/push/config"));
    await new Promise((r) => setTimeout(r, 20));
    expect(apiMock).not.toHaveBeenCalledWith("/push/subscribe", expect.anything());
  });

  it("treats unreadable storage as no token", async () => {
    pushSupport();
    notifications("denied");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(apiMock).toHaveBeenCalledWith("/push/config"));
  });
});

describe("local notifications and logout edge cases", () => {
  it("shows a local notification with the asset version, via a plain Notification when there is no worker", async () => {
    const Stub = notifications("granted");
    Object.defineProperty(navigator, "serviceWorker", { value: { getRegistration: vi.fn(async () => undefined) }, configurable: true, writable: true });
    expect(await push.showLocalNotification("T", "B", "/x")).toBe(true);
    expect(Stub).toHaveBeenCalledWith("T", expect.objectContaining({ body: "B", tag: "/x" }));
  });

  it("works without service worker support at all", async () => {
    const Stub = notifications("granted");
    const had = Object.getOwnPropertyDescriptor(navigator, "serviceWorker");
    delete navigator.serviceWorker;
    try {
      expect(await push.showLocalNotification("T", "B", "/x")).toBe(true);
      expect(Stub).toHaveBeenCalled();
    } finally {
      if (had) Object.defineProperty(navigator, "serviceWorker", had);
    }
  });

  it("logout survives unreadable and unwritable storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    await expect(push.unsubscribeOnLogout()).resolves.toBeUndefined();
    expect(apiMock).not.toHaveBeenCalled();
  });

  it("/push/config is fetched once and cached across boots", async () => {
    pushSupport();
    notifications("denied");
    await push.initPushOnBoot();
    await push.initPushOnBoot();
    expect(apiMock.mock.calls.filter(([path]) => path === "/push/config")).toHaveLength(1);
  });
});
