/**
 * Web push (Firebase Cloud Messaging) — foreground glue.
 *
 * The Firebase **compat** SDK is self-hosted under /static/vendor (so the CSP
 * keeps script-src 'self' — no gstatic CDN) and loaded on demand the first time
 * the user turns push on. The background service worker lives at
 * /firebase-messaging-sw.js (served by the backend with the config injected).
 *
 * Push must never break the app, so every path is guarded and degrades to
 * "no push" (the in-app bell and email still work).
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
      // instead of the querySelector above short-circuiting to a resolved()
      // for a script that never actually loaded (push silently dead otherwise).
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
    // Foreground messages don't pop a system notification — surface a toast.
    messaging.onMessage((payload) => {
      const n = payload.notification;
      if (n?.title) toast(`${n.title}${n.body ? ": " + n.body : ""}`, "info");
    });
  }
  return { messaging, reg };
}

async function subscribeToken(cfg: PushConfig): Promise<boolean> {
  const m = await ensureMessaging(cfg);
  if (!m) return false;
  const token = await m.messaging.getToken({
    vapidKey: cfg.vapid_key,
    serviceWorkerRegistration: m.reg,
  });
  if (!token) return false;
  await api("/push/subscribe", {
    method: "POST",
    json: { token, platform: "web", user_agent: navigator.userAgent.slice(0, 400) },
  });
  return true;
}

/**
 * On boot: web push is MANDATORY whenever the server has it enabled — there is
 * no in-app opt-out. If the browser permission is still "default" we prompt for
 * it; once granted we register/refresh this browser's FCM token (FCM tokens
 * rotate, so we re-subscribe every boot). The one thing the app cannot override
 * is a browser/OS-level "denied" — that is the user's own browser setting, and
 * no web API can force-grant notification permission. Never throws; never
 * blocks boot. No-op when web push is disabled or the browser can't support it
 * (e.g. an insecure-context origin — browsers only allow push over HTTPS or on
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
