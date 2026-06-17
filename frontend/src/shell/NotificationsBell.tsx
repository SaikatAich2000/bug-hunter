/**
 * NotificationsBell — the top-bar bell + unread badge + dropdown panel.
 *
 * Per-user only: every row comes from GET /api/notifications, which is scoped
 * to the current session's user server-side (no cross-user leak). The badge
 * count rides on the AppContext session poll; opening the panel pulls a fresh
 * list. Clicking a row marks it read and deep-links to the linked bug/event.
 */
import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { timeAgo } from "../lib/format";
import type { NotificationKind, NotificationOut } from "../types";

/** Emoji glyph per notification kind (chrome-only; not a brand mark). */
const KIND_ICON: Record<NotificationKind, string> = {
  assigned: "🎯",
  reported: "📌",
  updated: "✏️",
  comment: "💬",
  event: "📅",
};

export default function NotificationsBell() {
  const {
    notifications,
    unreadCount,
    loadNotifications,
    markAllNotificationsRead,
    deleteNotification,
    openNotification,
  } = useApp();

  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // Pull a fresh list each time the panel opens.
  useEffect(() => {
    if (open) void loadNotifications();
  }, [open, loadNotifications]);

  // While the panel is open, re-fetch the list every 15s (matching the badge
  // cadence) so new notifications appear without reopening. Only runs while
  // open; the open-load effect already does the first fetch. Skips when the
  // tab is hidden.
  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => { if (!document.hidden) void loadNotifications(); }, 15_000);
    return () => clearInterval(id);
  }, [open, loadNotifications]);

  // Close on outside click or Escape.
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

  const badge = unreadCount > 99 ? "99+" : String(unreadCount);

  const onRowClick = (n: NotificationOut) => {
    setOpen(false);
    void openNotification(n);
  };

  return (
    <div className="notif-wrap" ref={rootRef}>
      <button
        type="button"
        className="icon-btn notif-btn"
        id="notifBtn"
        aria-label={unreadCount ? `Notifications, ${unreadCount} unread` : "Notifications"}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="notif-bell" aria-hidden="true">🔔</span>
        {unreadCount > 0 && (
          <span className="notif-badge" aria-hidden="true">{badge}</span>
        )}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="Notifications">
          <header className="notif-panel-head">
            <span className="notif-panel-title">Notifications</span>
            <button
              type="button"
              className="notif-readall"
              disabled={unreadCount === 0}
              onClick={() => void markAllNotificationsRead()}
            >
              Mark all read
            </button>
          </header>

          <div className="notif-list">
            {notifications.length === 0 ? (
              <div className="notif-empty">
                <span className="notif-empty-glyph" aria-hidden="true">🔕</span>
                <span>You're all caught up.</span>
              </div>
            ) : (
              notifications.map((n) => (
                <div
                  key={n.id}
                  className={`notif-item${n.read_at ? "" : " is-unread"}`}
                  role="button"
                  tabIndex={0}
                  onClick={() => onRowClick(n)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onRowClick(n);
                    }
                  }}
                >
                  <span className="notif-icon" aria-hidden="true">{KIND_ICON[n.kind] ?? "🔔"}</span>
                  <div className="notif-body">
                    <div className="notif-title">{n.title}</div>
                    {n.body && <div className="notif-text">{n.body}</div>}
                    <div className="notif-time">{timeAgo(n.created_at)}</div>
                  </div>
                  {!n.read_at && <span className="notif-dot" aria-hidden="true" />}
                  <button
                    type="button"
                    className="notif-del"
                    aria-label="Dismiss notification"
                    onClick={(e) => {
                      e.stopPropagation();
                      void deleteNotification(n.id);
                    }}
                  >
                    ✕
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
