/** Poll intervals served by GET /api/meta (DATA_/SESSION_/NOTIFICATION_POLL_SECONDS); defaults match the server's. */
const intervals = { data: 30_000, session: 30_000, notifications: 15_000 };

/** Adopt the server's values; anything missing or not a positive number keeps the current one. */
export function applyPollConfig(meta) {
  const take = (key, seconds) => {
    if (Number.isFinite(seconds) && seconds > 0) intervals[key] = seconds * 1000;
  };
  take("data", meta?.data_poll_seconds);
  take("session", meta?.session_poll_seconds);
  take("notifications", meta?.notification_poll_seconds);
}

export const dataPollMs = () => intervals.data;
export const sessionPollMs = () => intervals.session;
export const notificationPollMs = () => intervals.notifications;
