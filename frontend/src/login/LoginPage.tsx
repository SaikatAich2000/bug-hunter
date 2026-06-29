// Playwright selects inputs by name="email" / name="password" and the submit
// button, so those attributes are load-bearing — don't rename them.
//
// "__APP_VERSION__" is substituted server-side by _serve_html() in app/main.py;
// keep it verbatim in any template that serves it.
//
// No inline scripts or styles: the CSP is strict (script-src 'self', no
// 'unsafe-inline'). Theme bootstrap lives in main.tsx.
import { useEffect, useRef, useState } from "react";
import type { FormEvent, MouseEvent as ReactMouseEvent } from "react";
import PasswordInput from "../components/PasswordInput";

type AlertKind = "error" | "success";

interface AlertState {
  msg: string;
  kind: AlertKind;
}

// detail can be a plain string or a Pydantic validation-error array.
function detailMessage(data: unknown, fallback: string): string {
  if (typeof data === "object" && data !== null && "detail" in data) {
    const detail: unknown = (data as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return detail.map((e: { msg?: string }) => e.msg).join(", ");
    }
  }
  return fallback;
}

export default function LoginPage() {
  const [view, setView] = useState<"login" | "forgot">("login");
  const [loginAlert, setLoginAlert] = useState<AlertState | null>(null);
  const [forgotAlert, setForgotAlert] = useState<AlertState | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [forgotBusy, setForgotBusy] = useState(false);
  const loginEmailRef = useRef<HTMLInputElement>(null);
  const forgotEmailRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(false);

  // Re-focus the email field on view switch; initial focus is handled by autoFocus.
  useEffect(() => {
    if (!mountedRef.current) {
      mountedRef.current = true;
      return;
    }
    (view === "forgot" ? forgotEmailRef : loginEmailRef).current?.focus();
  }, [view]);

  function hideAlerts() {
    setLoginAlert(null);
    setForgotAlert(null);
  }

  function onShowForgot(e: ReactMouseEvent<HTMLAnchorElement>) {
    e.preventDefault();
    hideAlerts();
    setView("forgot");
  }

  function onBackToLogin(e: ReactMouseEvent<HTMLAnchorElement>) {
    e.preventDefault();
    hideAlerts();
    setView("login");
  }

  function onToggleTheme() {
    const cur = document.documentElement.dataset.theme;
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("theme", next);
  }

  async function onLoginSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    hideAlerts();
    const f = e.currentTarget;
    const email = (f.elements.namedItem("email") as HTMLInputElement).value.trim();
    const password = (f.elements.namedItem("password") as HTMLInputElement).value;
    setLoginBusy(true);
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        let msg = "Sign in failed";
        try {
          msg = detailMessage(await res.json(), msg);
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setLoginAlert({ msg, kind: "error" });
        return;
      }
      // Redirect to `next` if given, but only accept a relative path to
      // prevent open-redirect via absolute or protocol-relative URLs.
      const params = new URLSearchParams(location.search);
      const requested = params.get("next") || "/";
      const next = /^\/(?![/\\])/.test(requested) ? requested : "/";
      location.replace(next);
    } catch {
      setLoginAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setLoginBusy(false);
    }
  }

  async function onForgotSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    hideAlerts();
    const f = e.currentTarget;
    const email = (f.elements.namedItem("email") as HTMLInputElement).value.trim();
    setForgotBusy(true);
    try {
      const res = await fetch("/api/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (res.status === 204) {
        // 204 means the server queued a reset email.
        setForgotAlert({
          msg: "Reset link sent. Check your inbox — the link expires in 30 minutes",
          kind: "success",
        });
      } else {
        let msg = "Request failed";
        try {
          msg = detailMessage(await res.json(), msg);
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setForgotAlert({ msg, kind: "error" });
      }
    } catch {
      setForgotAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setForgotBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-logo">
            <img src="/static/icon.png?v=__ASSET_VERSION__" alt="Bug Hunter" />
          </div>
          <h1>Bug Hunter</h1>
          <p className="auth-tagline">Sign in to continue</p>
        </div>

        {/* Login form */}
        <form
          id="loginForm"
          className="auth-form"
          noValidate
          hidden={view !== "login"}
          onSubmit={onLoginSubmit}
        >
          <div
            id="loginAlert"
            className={loginAlert ? `auth-alert ${loginAlert.kind}` : "auth-alert"}
            hidden={loginAlert === null}
          >
            {loginAlert?.msg}
          </div>
          <label className="field">
            <span>
              Email <em>*</em>
            </span>
            <input
              ref={loginEmailRef}
              name="email"
              type="email"
              required
              autoComplete="username"
              autoFocus
            />
          </label>
          <label className="field">
            <span>
              Password <em>*</em>
            </span>
            <PasswordInput
              name="password"
              required
              autoComplete="current-password"
            />
          </label>
          <button
            type="submit"
            className="btn primary auth-submit"
            id="loginSubmit"
            disabled={loginBusy}
          >
            {loginBusy ? "Signing in…" : "Sign in"}
          </button>
          <div className="auth-links">
            <a href="#" id="showForgot" onClick={onShowForgot}>
              Forgot your password?
            </a>
            <button
              type="button"
              className="link-btn"
              id="loginThemeBtn"
              onClick={onToggleTheme}
            >
              <svg
                className="theme-icon"
                viewBox="0 0 16 16"
                xmlns="http://www.w3.org/2000/svg"
                aria-hidden="true"
              >
                <path d="M 8 2 A 6 6 0 0 0 8 14 Z" fill="#0b1020" />
                <path d="M 8 2 A 6 6 0 0 1 8 14 Z" fill="#ffffff" />
                <circle
                  cx="8"
                  cy="8"
                  r="6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1"
                  strokeOpacity="0.55"
                />
                <line
                  x1="8"
                  y1="2"
                  x2="8"
                  y2="14"
                  stroke="currentColor"
                  strokeWidth="0.6"
                  strokeOpacity="0.4"
                />
              </svg>{" "}
              Theme
            </button>
          </div>
        </form>

        {/* Forgot password form (toggled) */}
        <form
          id="forgotForm"
          className="auth-form"
          noValidate
          hidden={view !== "forgot"}
          onSubmit={onForgotSubmit}
        >
          <div
            id="forgotAlert"
            className={forgotAlert ? `auth-alert ${forgotAlert.kind}` : "auth-alert"}
            hidden={forgotAlert === null}
          >
            {forgotAlert?.msg}
          </div>
          <p className="auth-help">
            Enter the email tied to your Bug Hunter account and we'll send a reset link
          </p>
          <label className="field">
            <span>
              Email <em>*</em>
            </span>
            <input
              ref={forgotEmailRef}
              name="email"
              type="email"
              required
              autoComplete="username"
            />
          </label>
          <button type="submit" className="btn primary auth-submit" disabled={forgotBusy}>
            {forgotBusy ? "Sending…" : "Send reset link"}
          </button>
          <div className="auth-links">
            <a href="#" id="backToLogin" onClick={onBackToLogin}>
              ← Back to sign in
            </a>
          </div>
        </form>
      </div>
      {/* Version string lives in login.html, not here — __APP_VERSION__ is
          replaced server-side by _serve_html() and would be emitted verbatim
          from this bundle. */}
    </main>
  );
}
