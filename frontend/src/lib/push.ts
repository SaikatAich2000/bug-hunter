/** Web push via FCM (foreground side). SDK self-hosted under /static/vendor to keep CSP script-src 'self'. */
import { api } from "./api";
import { toast } from "./toast";

interface PushConfig {
  enabled: boolean;
  api_key: string;
  auth_domain: string;
  project_id: string;
  messaging_sender_id: string;
  app_id: string;
  vapid_key: string;
}

// minimal compat-global shapes (compat SDK ships no types)
interface FirebaseMessaging {
  getToken(opts: {
    vapidKey: string;
    serviceWorkerRegistration: ServiceWorkerRegistration;
  }): Promise<string>;
  deleteToken(): Promise<boolean>;
  onMessage(cb: (payload: { notification?: { title?: string; body?: string } }) => void): void;
}
interface FirebaseCompat {
  apps: unknown[];
  initializeApp(config: Record<string, string>): void;
  messaging(): FirebaseMessaging;
}
declare global {
  interface Window {
    firebase?: FirebaseCompat;
  }
}

let configCache: PushConfig | null = null;
let messagingCache: FirebaseMessaging | null = null;

function supported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "Notification" in window &&
    "PushManager" in window
  );
}

async function getConfig(): Promise<PushConfig | null> {
  if (configCache) return configCache;
  try {
    configCache = await api<PushConfig>("/push/config");
    return configCache;
  } catch {
    return null;
  }
}

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) {
      resolve();
      return;
    }
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => {
      // remove failed node so a retry isn't short-circuited by the querySelector guard
      el.remove();
      reject(new Error(`failed to load ${src}`));
    };
    document.head.appendChild(el);
  });
}

async function ensureMessaging(
  cfg: PushConfig,
): Promise<{ messaging: FirebaseMessaging; reg: ServiceWorkerRegistration } | null> {
  await loadScript("/static/vendor/firebase-app-compat.js");
  await loadScript("/static/vendor/firebase-messaging-compat.js");
  const fb = window.firebase;
  if (!fb) return null;
  if (fb.apps.length === 0) {
    fb.initializeApp({
      apiKey: cfg.api_key,
      authDomain: cfg.auth_domain,
      projectId: cfg.project_id,
      messagingSenderId: cfg.messaging_sender_id,
      appId: cfg.app_id,
    });
  }
  const reg = await navigator.serviceWorker.register("/firebase-messaging-sw.js");
  const messaging = fb.messaging();
  if (!messagingCache) {
    messagingCache = messaging;
    // foreground messages don't pop system notifications; show a toast
    messaging.onMessage((payload) => {
      const n = payload.notification;
      if (n?.title) toast(`${n.title}${n.body ? ": " + n.body : ""}`, "info");
    });
  }
  return { messaging, reg };
}

// last token persisted so logout can deregister even if SDK never initialised this tab
const _PUSH_TOKEN_KEY = "bh_push_token";

async function subscribeToken(cfg: PushConfig): Promise<boolean> {
  const m = await ensureMessaging(cfg);
  if (!m) return false;
  const token = await m.messaging.getToken({
    vapidKey: cfg.vapid_key,
    serviceWorkerRegistration: m.reg,
  });
  if (!token) return false;
  try {
    localStorage.setItem(_PUSH_TOKEN_KEY, token);
  } catch {
    /* storage unavailable; non-fatal */
  }
  await api("/push/subscribe", {
    method: "POST",
    json: { token, platform: "web", user_agent: navigator.userAgent.slice(0, 400) },
  });
  return true;
}

/** Deregister FCM token on logout; must run before /auth/logout (needs the session). Never throws. */
export async function unsubscribeOnLogout(): Promise<void> {
  let token = "";
  try {
    token = localStorage.getItem(_PUSH_TOKEN_KEY) || "";
  } catch {
    /* ignore */
  }
  // drop token client-side so the SDK stops auto-refreshing it
  try {
    if (supported() && messagingCache) {
      await messagingCache.deleteToken();
    }
  } catch {
    /* ignore; server unsubscribe below still runs */
  }
  if (token) {
    try {
      await api("/push/unsubscribe", { method: "POST", json: { token } });
    } catch {
      /* logout proceeds regardless */
    }
  }
  try {
    localStorage.removeItem(_PUSH_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** Re-register FCM token on every boot (tokens rotate). Never throws or blocks boot. */
export async function initPushOnBoot(): Promise<void> {
  try {
    if (!supported()) return;
    const cfg = await getConfig();
    if (!cfg?.enabled) return;
    let permission = Notification.permission;
    if (permission === "default") {
      try {
        permission = await Notification.requestPermission();
      } catch {
        return;
      }
    }
    if (permission !== "granted") return;
    await subscribeToken(cfg);
  } catch {
    /* never break boot */
  }
}
