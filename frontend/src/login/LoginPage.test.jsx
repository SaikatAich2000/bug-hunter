/** Sign-in and forgot-password forms, plus the reset-password page and the three page entry points. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import LoginPage from "./LoginPage";
import ResetPage from "../reset/ResetPage";

const json = (status, body) => new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let loc;

beforeEach(() => {
  loc = { search: "", hash: "", origin: "http://localhost", replace: vi.fn() };
  vi.stubGlobal("location", loc);
});
afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
});

describe("sign in", () => {
  it("posts the trimmed email and password, then goes to the app", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => json(200, { id: 1 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginPage />);
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Email/, { selector: "#loginForm input" }), "  ada@example.com ");
    await user.type(document.getElementById("passwordInput"), "s3cret-pass");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loc.replace).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/auth/login");
    expect(JSON.parse(init.body)).toEqual({ email: "ada@example.com", password: "s3cret-pass" });
    expect(init.credentials).toBe("include");
    expect(loc.replace.mock.calls[0][0]).toMatch(/^\//);
  });

  it("follows a same-origin redirect target but never another origin", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => json(200, {})));
    loc.search = "?next=/#/bugs/5";
    loc.hash = "#/bugs/5";
    render(<LoginPage />);
    await user.type(screen.getByLabelText(/Email/, { selector: "#loginForm input" }), "a@b.co");
    await user.type(document.getElementById("passwordInput"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(loc.replace).toHaveBeenCalled());
    expect(String(loc.replace.mock.calls[0][0]).startsWith("http")).toBe(false);
  });

  it.each([
    [401, { detail: "Bad credentials" }, "Bad credentials"],
    [422, { detail: [{ msg: "Email invalid" }, { msg: "Password short" }] }, "Email invalid, Password short"],
    [500, { other: true }, "Sign in failed"],
  ])("a %s response shows its message", async (status, body, shown) => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => json(status, body)));
    render(<LoginPage />);
    await user.type(screen.getByLabelText(/Email/, { selector: "#loginForm input" }), "a@b.co");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(shown)).toBeVisible();
    expect(document.getElementById("loginAlert")).toHaveClass("error");
    expect(loc.replace).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("a non-JSON error body keeps the generic message", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>502</html>", { status: 502 })));
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Sign in failed")).toBeVisible();
  });

  it("a network failure says so", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Network error, try again")).toBeVisible();
  });

  it("the button shows progress while signing in", async () => {
    const user = userEvent.setup();
    let release;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { release = () => resolve(json(401, { detail: "no" })); })));
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDisabled();
    release();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("the theme button flips and remembers the theme", async () => {
    const user = userEvent.setup();
    document.documentElement.dataset.theme = "dark";
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: /Theme/ }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("theme")).toBe("light");
    await user.click(screen.getByRole("button", { name: /Theme/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("forgot password", () => {
  async function openForgot(user) {
    render(<LoginPage />);
    await user.click(screen.getByRole("button", { name: "Forgot your password?" }));
    await waitFor(() => expect(document.getElementById("forgotForm")).not.toHaveAttribute("hidden"));
  }

  it("switches forms, clears an old alert, sends the email and confirms", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => json(204));
    vi.stubGlobal("fetch", fetchMock);
    await openForgot(user);
    expect(document.getElementById("loginForm")).toHaveAttribute("hidden");
    const email = document.querySelector("#forgotForm input[name=email]");
    await waitFor(() => expect(email).toHaveFocus());
    await user.type(email, " ada@example.com ");
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText(/Reset link sent/)).toBeVisible();
    expect(document.getElementById("forgotAlert")).toHaveClass("success");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ email: "ada@example.com" });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/forgot-password");
  });

  it("shows a server error and a network error; Back returns to sign in", async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async () => json(429, { detail: "Too many requests" }));
    vi.stubGlobal("fetch", fetchMock);
    await openForgot(user);
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText("Too many requests")).toBeVisible();
    fetchMock.mockImplementationOnce(async () => new Response("oops", { status: 500 }));
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText("Request failed")).toBeVisible();
    fetchMock.mockImplementationOnce(async () => { throw new TypeError("offline"); });
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByText("Network error, try again")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "← Back to sign in" }));
    expect(document.getElementById("loginForm")).not.toHaveAttribute("hidden");
    expect(document.getElementById("forgotAlert")).toHaveAttribute("hidden");
    await waitFor(() => expect(document.querySelector("#loginForm input[name=email]")).toHaveFocus());
  });

  it("shows Sending… while the request is in flight", async () => {
    const user = userEvent.setup();
    let release;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { release = () => resolve(json(204)); })));
    await openForgot(user);
    await user.click(screen.getByRole("button", { name: "Send reset link" }));
    expect(await screen.findByRole("button", { name: "Sending…" })).toBeDisabled();
    release();
    expect(await screen.findByRole("button", { name: "Send reset link" })).toBeEnabled();
  });
});

describe("reset password page", () => {
  const fill = async (user, a, b) => {
    await user.type(document.querySelector("input[name=new_password]"), a);
    await user.type(document.getElementById("resetConfirmPassword"), b);
  };

  it("without a token the form is disabled and the problem is stated", () => {
    render(<ResetPage />);
    expect(screen.getByText(/Reset link is missing or malformed/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Set new password" })).toBeDisabled();
    expect(document.querySelector("input[name=new_password]")).toBeDisabled();
  });

  it("rejects mismatching and weak passwords before calling the server", async () => {
    const user = userEvent.setup();
    loc.search = "?token=abc";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<ResetPage />);
    await fill(user, "Longpass1!", "Different1!");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByText("Passwords don't match")).toBeVisible();
    await user.clear(document.querySelector("input[name=new_password]"));
    await user.clear(document.getElementById("resetConfirmPassword"));
    await fill(user, "short", "short");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    await waitFor(() => expect(document.getElementById("resetAlert")).not.toHaveTextContent("match"));
    expect(document.getElementById("resetAlert")).toHaveClass("error");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("sets the new password with the emailed token and goes back to sign in", async () => {
    const user = userEvent.setup();
    loc.search = "?token=abc123";
    const fetchMock = vi.fn(async () => json(204));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ResetPage />);
      await fill(user, "Longpass1!", "Longpass1!");
      await user.click(screen.getByRole("button", { name: "Set new password" }));
      expect(await screen.findByText("Password updated, redirecting to sign in…")).toBeVisible();
      expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ token: "abc123", new_password: "Longpass1!" });
      vi.advanceTimersByTime(1600);
      expect(loc.replace).toHaveBeenCalledWith("/login.html");
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    [400, { detail: "Token expired" }, "Token expired"],
    [422, { detail: [{ msg: "too common" }, "plain", { code: 7 }] }, 'too common, plain, {"code":7}'],
    [422, { detail: [] }, "Reset failed"],
    [500, { detail: "" }, "Reset failed"],
  ])("a %s answer is shown as %j", async (status, body, shown) => {
    const user = userEvent.setup();
    loc.search = "?token=abc";
    vi.stubGlobal("fetch", vi.fn(async () => json(status, body)));
    render(<ResetPage />);
    await fill(user, "Longpass1!", "Longpass1!");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByText(shown)).toBeVisible();
  });

  it("a non-JSON failure and a network failure are reported", async () => {
    const user = userEvent.setup();
    loc.search = "?token=abc";
    const fetchMock = vi.fn(async () => new Response("bad gateway", { status: 502 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ResetPage />);
    await fill(user, "Longpass1!", "Longpass1!");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByText("Reset failed")).toBeVisible();
    fetchMock.mockImplementationOnce(async () => { throw new TypeError("offline"); });
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByText("Network error, try again")).toBeVisible();
  });

  it("shows Saving… while the request is in flight", async () => {
    const user = userEvent.setup();
    loc.search = "?token=abc";
    let release;
    vi.stubGlobal("fetch", vi.fn(() => new Promise((resolve) => { release = () => resolve(json(400, { detail: "no" })); })));
    render(<ResetPage />);
    await fill(user, "Longpass1!", "Longpass1!");
    await user.click(screen.getByRole("button", { name: "Set new password" }));
    expect(await screen.findByRole("button", { name: "Saving…" })).toBeDisabled();
    release();
    expect(await screen.findByRole("button", { name: "Set new password" })).toBeEnabled();
  });
});

describe("page entry points", () => {
  beforeEach(() => {
    vi.resetModules();
    document.body.innerHTML = '<div id="root"></div>';
  });

  it("login.html mounts the sign-in page with the saved theme", async () => {
    localStorage.setItem("theme", "light");
    await import("./main.jsx");
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(document.getElementById("root").style.display).toBe("contents");
  });

  it("reset.html mounts the reset page and defaults to the dark theme", async () => {
    await import("../reset/main.jsx");
    expect(await screen.findByRole("heading", { name: "Reset your password" })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("both entries fail loudly without a #root container", async () => {
    document.body.innerHTML = "";
    await expect(import("./main.jsx")).rejects.toThrow("login: #root container not found");
    vi.resetModules();
    await expect(import("../reset/main.jsx")).rejects.toThrow("reset: #root container not found");
  });
});
