/**
 * Web push (Firebase Cloud Messaging), foreground handling.
 *
 * The Firebase compat SDK is self-hosted under /static/vendor (so the CSP keeps
 * script-src 'self', no gstatic CDN) and loaded on demand the first time the
 * user turns push on. The background service worker lives at
 * /firebase-messaging-sw.js, served by the backend with the config injected.
 *
 * Every path is guarded so a push failure degrades to no push (the in-app bell
 * and email still work) rather than breaking the app.
 */
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

// Minimal shape of the firebase compat globals we use (compat ships no types).
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
      // Remove the failed node so a later attempt re-adds and retries it,
      // instead of the querySelector above short-circuiting to resolved() for a
      // script that never actually loaded.
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
    // Foreground messages don't pop a system notification, so surface a toast.
    messaging.onMessage((payload) => {
      const n = payload.notification;
      if (n?.title) toast(`${n.title}${n.body ? ": " + n.body : ""}`, "info");
    });
  }
  return { messaging, reg };
}

// Remember the last subscribed token so logout can deregister it even after the
// messaging SDK state is gone (e.g. a fresh tab). Cleared on unsubscribe.
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
    /* storage may be unavailable (private mode); non-fatal */
  }
  await api("/push/subscribe", {
    method: "POST",
    json: { token, platform: "web", user_agent: navigator.userAgent.slice(0, 400) },
  });
  return true;
}

/**
 * Deregister this device's push token on logout so the next user on a shared
 * browser stops receiving the previous user's notifications. Must be called
 * before /auth/logout, since the unsubscribe endpoint needs the session.
 * Best-effort and never throws so a failure can't block logout.
 */
export async function unsubscribeOnLogout(): Promise<void> {
  let token = "";
  try {
    token = localStorage.getItem(_PUSH_TOKEN_KEY) || "";
  } catch {
    /* ignore */
  }
  // Best-effort: drop the FCM token client-side so the SDK stops refreshing it.
  try {
    if (supported() && messagingCache) {
      await messagingCache.deleteToken();
    }
  } catch {
    /* ignore; still tell the server to forget it below */
  }
  if (token) {
    try {
      await api("/push/unsubscribe", { method: "POST", json: { token } });
    } catch {
      /* ignore; logout proceeds regardless */
    }
  }
  try {
    localStorage.removeItem(_PUSH_TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/**
 * On boot, web push is required whenever the server has it enabled; there is no
 * in-app opt-out. If the browser permission is still "default" we prompt for it;
 * once granted we register/refresh this browser's FCM token (FCM tokens rotate,
 * so we re-subscribe every boot). A browser/OS-level "denied" cannot be
 * overridden, since no web API can force-grant notification permission. Never
 * throws and never blocks boot. No-op when web push is disabled or the browser
 * can't support it (e.g. an insecure-context origin; push requires HTTPS or
 * localhost).
 */
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
