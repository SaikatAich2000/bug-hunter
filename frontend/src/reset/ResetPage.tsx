// Reset-password page. No inline scripts/styles (strict CSP); theme bootstrap is in main.tsx.
import { useState } from "react";
import type { FormEvent } from "react";
import PasswordInput from "../components/PasswordInput";
import { PASSWORD_HINT, validatePassword } from "../lib/constants";

type AlertKind = "error" | "success";

interface AlertState {
  msg: string;
  kind: AlertKind;
}

export default function ResetPage() {
  // Token comes from the emailed link's query string; read once.
  const [token] = useState(
    () => new URLSearchParams(location.search).get("token") || "",
  );
  // Missing token: surface the error immediately and disable the form.
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
    const pwErr = validatePassword(newPw);
    if (pwErr) {
      setResetAlert({ msg: pwErr, kind: "error" });
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
        // 422 `detail` may be a Pydantic error array — stringify items to avoid "[object Object]".
        let msg = "Reset failed";
        try {
          const detail: unknown = ((await res.json()) as { detail?: unknown }).detail;
          const asText = (v: unknown): string =>
            typeof v === "string" ? v : JSON.stringify(v);
          if (Array.isArray(detail)) {
            const parts = detail
              .map((e) =>
                e && typeof e === "object" && "msg" in e
                  ? asText((e as { msg: unknown }).msg)
                  : asText(e),
              )
              .filter(Boolean);
            if (parts.length) msg = parts.join(", ");
          } else if (typeof detail === "string" && detail) {
            msg = detail;
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
            <PasswordInput
              name="new_password"
              required
              minLength={8}
              maxLength={200}
              autoComplete="new-password"
              autoFocus
              disabled={noToken}
            />
            <small className="hint">{PASSWORD_HINT}</small>
          </label>
          <label className="field">
            <span>
              Confirm new password <em>*</em>
            </span>
            <PasswordInput
              name="confirm_password"
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
