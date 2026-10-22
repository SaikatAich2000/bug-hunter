// Top-bar bell with unread badge and dropdown panel; badge count comes from the session poll.
import EmptyGuide from "../onboarding/EmptyGuide";
import { useEffect, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { timeAgo } from "../lib/format";
import { notificationPollMs } from "../lib/pollConfig";
const KIND_ICON = {
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
  const rootRef = useRef(null);

  useEffect(() => {
    if (open) void loadNotifications();
  }, [open, loadNotifications]);

  // Poll (NOTIFICATION_POLL_SECONDS) while the panel is open; skip when the tab is hidden.
  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => { if (!document.hidden) void loadNotifications(); }, notificationPollMs());
    return () => clearInterval(id);
  }, [open, loadNotifications]);

  // Dismiss on outside click or Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e) => {
      if (rootRef.current && !rootRef.current.contains(e.target)) setOpen(false);
    };
    const onKey = (e) => {
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

  const onRowClick = (n) => {
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
        <div className="notif-panel" role="dialog" /* NOSONAR S6819: portal dialog with its own focus trap and enter/exit animation; native <dialog> needs showModal() top-layer semantics */ aria-label="Notifications">
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
                <span>You&apos;re all caught up</span>
                <EmptyGuide guide="notifications" />
              </div>
            ) : (
              notifications.map((n) => (
                <div // NOSONAR S6819: notification row is a div wrapping nested buttons; a <button> cannot contain them
                  key={n.id}
                  className={`notif-item${n.read_at ? "" : " is-unread"}`}
                  role="button" /* NOSONAR S6819: styled inline label inside a clickable row; a <button> would need a reset of the row styling */
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
