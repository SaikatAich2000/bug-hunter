// Reset-password page.
//
// No inline scripts/styles (strict CSP); the theme bootstrap lives in main.tsx.
import { useState } from "react";
import type { FormEvent } from "react";

type AlertKind = "error" | "success";

interface AlertState {
  msg: string;
  kind: AlertKind;
}

export default function ResetPage() {
  // The token arrives in the query string of the emailed link; it never
  // changes during the page's lifetime, so read it once.
  const [token] = useState(
    () => new URLSearchParams(location.search).get("token") || "",
  );
  // No token → show the error immediately and disable the whole form.
  const [resetAlert, setResetAlert] = useState<AlertState | null>(
    token
      ? null
      : {
          msg: "Reset link is missing or malformed. Request a new one from the sign-in page",
          kind: "error",
        },
  );
  const [busy, setBusy] = useState(false);
  const noToken = token === "";

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = e.currentTarget;
    const newPw = (f.elements.namedItem("new_password") as HTMLInputElement).value;
    const confirmPw = (f.elements.namedItem("confirm_password") as HTMLInputElement).value;
    if (newPw !== confirmPw) {
      setResetAlert({ msg: "Passwords don't match", kind: "error" });
      return;
    }
    if (newPw.length < 8) {
      setResetAlert({ msg: "Password must be at least 8 characters", kind: "error" });
      return;
    }

    setBusy(true);
    try {
      const res = await fetch("/api/auth/reset-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: newPw }),
      });
      if (res.status === 204) {
        setResetAlert({ msg: "Password updated. Redirecting to sign in…", kind: "success" });
        setTimeout(() => {
          location.replace("/login.html");
        }, 1500);
      } else {
        // Take {detail} as-is, falling back on parse failure or a falsy detail.
        let msg = "Reset failed";
        try {
          const detail: unknown = ((await res.json()) as { detail?: unknown }).detail;
          if (detail) {
            msg = typeof detail === "string" ? detail : String(detail);
          }
        } catch {
          /* non-JSON error body — keep the fallback message */
        }
        setResetAlert({ msg, kind: "error" });
      }
    } catch {
      setResetAlert({ msg: "Network error. Try again", kind: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">
          <h1>Reset your password</h1>
          <p className="auth-tagline">Pick a new password to finish the reset</p>
        </div>

        <form id="resetForm" className="auth-form" noValidate onSubmit={onSubmit}>
          <div
            id="resetAlert"
            className={resetAlert ? `auth-alert ${resetAlert.kind}` : "auth-alert"}
            hidden={resetAlert === null}
          >
            {resetAlert?.msg}
          </div>
          <label className="field">
            <span>
              New password <em>*</em>
            </span>
            <input
              name="new_password"
              type="password"
              required
              minLength={8}
              maxLength={200}
              autoComplete="new-password"
              autoFocus
              disabled={noToken}
            />
            <small className="hint">At least 8 characters</small>
          </label>
          <label className="field">
            <span>
              Confirm new password <em>*</em>
            </span>
            <input
              name="confirm_password"
              type="password"
              required
              minLength={8}
              maxLength={200}
              autoComplete="new-password"
              disabled={noToken}
            />
          </label>
          <button
            type="submit"
            className="btn primary auth-submit"
            id="resetSubmit"
            disabled={noToken || busy}
          >
            {busy ? "Saving…" : "Set new password"}
          </button>
          <div className="auth-links">
            <a href="/login.html">← Back to sign in</a>
          </div>
        </form>
      </div>
    </main>
  );
}
