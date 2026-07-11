// Top-right account dropdown (change-password, theme, log-out).
// Test contract: id="accountName" is the smoke test's SPA-ready signal; other ids are Playwright selectors.
import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { initials } from "../lib/format";
import { confirmDialog } from "../components/ConfirmHost";

function currentTheme(): "dark" | "light" {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export default function ProfileMenu() {
  const { currentUser, setChangePasswordOpen } = useApp();

  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">(currentTheme);
  const rootRef = useRef<HTMLDivElement>(null);

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const handleLogout = async () => {
    setOpen(false);
    const ok = await confirmDialog("Log out now?", {
      title: "Log out",
      okLabel: "Log out",
      danger: false,
    });
    if (!ok) return;
    // Deregister push token while the session is still valid (shared-browser safety).
    try {
      const { unsubscribeOnLogout } = await import("../lib/push");
      await unsubscribeOnLogout();
    } catch {
      /* push not configured or unavailable — proceed */
    }
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      /* log out locally even if the server call fails */
    }
    location.replace("/login.html");
  };

  const handleTheme = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    setTheme(next);
    try {
      localStorage.setItem("theme", next);
    } catch {
      /* private browsing — skip persistence */
    }
  };

  const handleChangePassword = () => {
    setOpen(false);
    setChangePasswordOpen(true);
  };

  const avatar = initials(currentUser.name) || "?";

  return (
    <div className="profile-wrap" ref={rootRef}>
      <button
        type="button"
        className="profile-btn"
        id="profileBtn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="profile-avatar" id="accountAvatar" aria-hidden="true">
          {avatar}
        </span>
        <span className="profile-name" id="accountName">
          {currentUser.name}
        </span>
        <span className="profile-caret" aria-hidden="true">
          ▾
        </span>
      </button>

      {open && (
        <div className="profile-menu" role="menu" aria-label="Account">
          <div className="profile-menu-head">
            <span className="profile-avatar lg" aria-hidden="true">
              {avatar}
            </span>
            <div className="profile-menu-id">
              <div className="profile-menu-name">{currentUser.name}</div>
              <div className="profile-menu-meta">
                <span id="accountRole">{currentUser.role}</span>
                {" · "}
                <span id="accountEmail">{currentUser.email}</span>
              </div>
            </div>
          </div>

          <div className="profile-menu-sep" />

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item"
            id="changePasswordBtn"
            onClick={handleChangePassword}
          >
            <span className="profile-menu-ic" aria-hidden="true">🔑</span>
            Change password
          </button>

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item"
            id="themeBtn"
            onClick={handleTheme}
          >
            <span className="profile-menu-ic" aria-hidden="true">{theme === "dark" ? "☀️" : "🌙"}</span>
            {theme === "dark" ? "Light theme" : "Dark theme"}
          </button>

          <div className="profile-menu-sep" />

          <button
            type="button"
            role="menuitem"
            className="profile-menu-item danger"
            id="logoutBtn"
            onClick={() => void handleLogout()}
          >
            <span className="profile-menu-ic" aria-hidden="true">🚪</span>
            Log out
          </button>
        </div>
      )}
    </div>
  );
}
