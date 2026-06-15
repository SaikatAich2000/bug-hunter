/**
 * Sessions admin view — port of the vanilla sessions section (index.html
 * L486-501), shortenUserAgent (app.js L3810-3831) and refreshSessions /
 * renderSessions / handleRevokeSession (app.js L4089-4162).
 *
 * Lists every active session row with user, role, IP, browser, when it was
 * created, when it was last seen, when it expires. Admin-only — the nav
 * button is role-gated and the API enforces 403 for non-admins. Your own
 * current session shows "This is you" and its Revoke button is disabled.
 */
import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { withLoader } from "../lib/loader";
import { formatDate, initials } from "../lib/format";
import { confirmDialog } from "../components/ConfirmHost";
import { DATA_POLL_MS } from "../state/AppContext";
import type { SessionOut } from "../types";

/**
 * Short browser/OS hint from a full UA string — exact port of
 * shortenUserAgent (app.js L3810-3831).
 */
function shortenUserAgent(ua: string): string {
  if (!ua) return "Unknown";
  const lower = ua.toLowerCase();
  let browser = "Unknown";
  if (lower.includes("edg/")) browser = "Edge";
  else if (lower.includes("chrome/")) browser = "Chrome";
  else if (lower.includes("firefox/")) browser = "Firefox";
  else if (lower.includes("safari/") && !lower.includes("chrome/")) browser = "Safari";
  else if (lower.includes("curl/")) browser = "curl";
  else if (lower.includes("python-")) browser = "Python";
  else if (lower.includes("postman")) browser = "Postman";
  let os = "";
  if (lower.includes("windows")) os = "Windows";
  else if (lower.includes("mac os") || lower.includes("macintosh")) os = "macOS";
  else if (lower.includes("linux")) os = "Linux";
  else if (lower.includes("android")) os = "Android";
  else if (lower.includes("iphone") || lower.includes("ios")) os = "iOS";
  return os ? `${browser} on ${os}` : browser;
}

export default function SessionsView() {
  const [sessions, setSessions] = useState<SessionOut[]>([]);
  const [loaded, setLoaded] = useState(false);

  const refreshSessions = useCallback(async (quiet = false) => {
    try {
      setSessions(await api<SessionOut[]>("/sessions"));
      setLoaded(true);
    } catch (err) {
      if (!quiet) toastError(err);
    }
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  // Live poll: refetch the session list on the shared cadence so a login /
  // revoke on another device shows up without a manual reload. Quiet (no error
  // toast every tick) and paused while the tab is hidden; fires on refocus.
  useEffect(() => {
    const tick = () => { if (!document.hidden) void refreshSessions(true); };
    const id = setInterval(tick, DATA_POLL_MS);
    document.addEventListener("visibilitychange", tick);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", tick); };
  }, [refreshSessions]);

  // Port of handleRevokeSession (app.js L4141-4162).
  const handleRevoke = useCallback(
    async (s: SessionOut) => {
      const who = s.user_name
        ? `${s.user_name} <${s.user_email ?? ""}>`
        : `session #${s.id}`;
      const ok = await confirmDialog(
        `Revoke this session for ${who}?\n\n` +
          `That device will be immediately logged out. Other sessions for the ` +
          `same user are not affected`,
        { title: "Revoke session", okLabel: "Revoke", danger: true },
      );
      if (!ok) return;
      try {
        await withLoader(async () => {
          await api(`/sessions/${s.id}`, { method: "DELETE" });
          await refreshSessions();
        }, "Revoking session…");
        toast("Session revoked", "success");
      } catch (err) {
        toastError(err);
      }
    },
    [refreshSessions],
  );

  return (
    <section className="view" id="viewSessions">
      <div className="page-intro sessions-intro">
        <div className="page-intro-icon" aria-hidden="true">🔐</div>
        <div className="page-intro-text">
          <h2>Active Sessions</h2>
          <p>Every device currently logged in to Bug Hunter. Revoke a session to log that device out without affecting anything else</p>
        </div>
      </div>
      <div className="sessions-controls">
        <p className="sessions-help">
          Click <strong>Revoke</strong> to log a specific device out without affecting any other session for that user. Your own current session can't be revoked from here — use the Log out button in the sidebar instead
        </p>
        <button
          type="button"
          className="btn ghost"
          id="sessionsRefreshBtn"
          onClick={() => void refreshSessions()}
        >
          Refresh
        </button>
      </div>
      <div className="sessions-list" id="sessionsList">
        {loaded && sessions.length === 0 ? (
          <div className="sessions-empty">No active sessions</div>
        ) : null}
        {sessions.map((s) => (
          <div
            className={`session-row${s.is_current ? " is-current" : ""}`}
            data-session-id={s.id}
            key={s.id}
          >
            <span className="session-avatar">{initials(s.user_name || "?")}</span>
            <div className="session-main">
              <div className="session-line1">
                <span className="session-name">{s.user_name || "(deleted user)"}</span>
                <span className="muted small">{s.user_email || ""}</span>
                {s.user_role ? (
                  <span className="session-role-pill">{s.user_role}</span>
                ) : null}
                {s.is_current ? (
                  <span
                    className="session-current-flag"
                    title="The session you're using right now — can't be revoked from here"
                  >
                    This is you
                  </span>
                ) : null}
              </div>
              <div className="session-line2">
                {shortenUserAgent(s.user_agent)} · {s.ip_address || "(unknown IP)"}
              </div>
              <div className="session-line3">
                Started {formatDate(s.created_at)} ·{" "}
                Last seen {formatDate(s.last_seen_at)} ·{" "}
                Expires {formatDate(s.expires_at)}
              </div>
            </div>
            <div className="session-actions">
              <button
                type="button"
                className="btn danger"
                data-act="revoke-session"
                data-id={s.id}
                disabled={s.is_current}
                title={
                  s.is_current
                    ? "Use Log out from the sidebar to end your own session"
                    : undefined
                }
                onClick={() => void handleRevoke(s)}
              >
                Revoke
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
