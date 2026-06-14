# Web push notifications — design & scoping

Status: **IMPLEMENTED (Option A — FCM Web).** Browser push for the five
operations is built and default-off; enable it with the Firebase setup in
README → *Web push notifications*. This document is retained as the rationale
and the reference for the future Android client. Implementation: `app/config.py`
(settings), `app/models.py` (`PushSubscription`), `app/schemas.py`,
`app/fcm_transport.py` (FCM transport), `app/push_service.py` (orchestration),
`app/routes/push.py`, the SW served from `app/main.py`, the triggers in
`app/notification_service.notify`, and the frontend `lib/push.ts` +
`ProfileMenu` toggle + vendored SDK. Tests in `tests/test_push.py`.

## Context

- This repo (the FastAPI + React web app) has **no push infrastructure today**.
- The existing **Firebase/FCM push is in a separate Android app**, not this
  backend. So web push here is a brand-new integration — it can either *reuse*
  the same Firebase project or stand alone.

## Core principle: push is immediate, not digested

Push is the real-time channel. It fires **per-operation, immediately**, at the
same trigger points that already write the in-app notification row (see
`app/notification_service.py:notify`). It is **independent of the daily email
digest** — the digest only exists to reduce *email* volume.

End state for one operation:

| Channel | When | Controlled by |
|---|---|---|
| In-app bell row | always (synchronous) | — (always on) |
| **Web push** | **immediately, if the user granted permission** | this feature |
| Email | immediately, or in the daily digest | `EMAIL_DIGEST_ENABLED` |
| Android push | immediately (separate app/backend) | already exists |

A browser only receives push if the user clicked "Allow" — so it's inherently
per-user opt-in; no extra preference column is required for v1.

## Two approaches

### Option A — FCM Web (reuse your Firebase project)

The web client registers with Firebase Cloud Messaging and the backend sends
through FCM, exactly like the Android side — one Firebase project for both.

- **Frontend:** Firebase JS SDK (`firebase/messaging`), a
  `firebase-messaging-sw.js` service worker, the Firebase **web** config, and a
  Web-Push/VAPID key to call `getToken()`. The returned FCM registration token
  is POSTed to the backend.
- **Backend:** `firebase-admin` (new dependency) initialised from a
  **service-account JSON**; sends via FCM HTTP v1 (`messaging.send` /
  `send_each_for_multicast`) to each stored token. Handles `UNREGISTERED` /
  `NOT_FOUND` responses by pruning dead tokens.
- **You provide:**
  1. Firebase **web** config — `apiKey`, `authDomain`, `projectId`,
     `messagingSenderId`, `appId`.
  2. A **Web Push certificate / VAPID key pair** (Firebase console → Project
     settings → Cloud Messaging → Web configuration).
  3. A **service-account JSON** (Project settings → Service accounts) mounted
     into the backend container as a secret (path via the `FCM_CREDENTIALS_FILE`
     env var; the shipped `docker-compose.yml` mounts `secrets/firebase-admin.json`).
- **Pros:** unified with Android; one console; FCM handles delivery/retry.
- **Cons:** couples this backend to Firebase; secret-management for the
  service account; adds `firebase-admin`.

### Option B — Standard Web Push (VAPID + Push API, self-contained)

The W3C standard, no Firebase. The browser's own push service delivers the
message; the backend signs requests with a VAPID key.

- **Frontend:** a plain service worker (`/static/sw.js`) and
  `registration.pushManager.subscribe({ applicationServerKey })`. The resulting
  `PushSubscription` (endpoint + p256dh + auth keys) is POSTed to the backend.
- **Backend:** a **VAPID key pair** (I generate it — `py-vapid`/`cryptography`)
  and `pywebpush` to send. A `410 Gone` / `404` from the push service prunes the
  dead subscription.
- **You provide:** nothing external — keys are generated and stored as env
  vars/secrets. `VAPID_PUBLIC_KEY` ships to the browser; `VAPID_PRIVATE_KEY`
  stays server-side.
- **Browser support:** Chrome, Edge, Firefox, and **Safari 16.4+** (macOS 13+ /
  iOS 16.4+ as an installed PWA). Older Safari: no web push (degrades to in-app
  + email).
- **Pros:** fully self-hosted, no external account, no vendor coupling, smallest
  secret footprint.
- **Cons:** a second push system separate from your Android FCM (two things to
  reason about), though server-side it's a single extra module.

## Shared components (either approach)

1. **Data model (additive — honors the prod-DB rule).** New table
   `push_subscriptions`:
   - `id`, `user_id` (FK users, CASCADE), `endpoint`/`token` (unique),
     auth material (`p256dh`, `auth` for Option B; just the token for Option A),
     `user_agent`, `created_at`, `last_seen_at`, `failed_at` (for pruning).
   - Created by `init_db()` like every other table; no existing table changes.
2. **Endpoints** (all behind the existing session auth, scoped to the current
   user):
   - `POST /api/push/subscribe` — store a token/subscription for this user.
   - `DELETE /api/push/subscribe` — unsubscribe this browser.
   - `GET /api/push/vapid-public-key` (Option B only) — hand the browser the key.
3. **Send service** `app/push_service.py` — `push(user_ids, title, body, url)`,
   wired into the same five trigger points as `notification_service.notify`
   (created/updated/assigned/comment/event) so push, in-app, and email stay in
   lockstep. Best-effort and non-blocking (runs in the request `BackgroundTask`
   like email), with dead-token pruning.
4. **Frontend:** a service worker, a one-time permission prompt (gated behind a
   user gesture — e.g. a bell-menu "Enable push" toggle, never auto-prompted on
   load), token registration on login, and a `push`/`notificationclick` handler
   that deep-links to the item (`/#bug=<id>` or `/#event=<id>`).
5. **Config:** Option A → Firebase web config + `FCM_CREDENTIALS_FILE`;
   Option B → `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` / `VAPID_SUBJECT`. A
   global `WEB_PUSH_ENABLED` flag (default off) so it ships dark until ready.
6. **Tests:** subscribe/unsubscribe + per-user isolation, dead-token pruning,
   send-on-each-trigger (transport mocked — no real network), disabled no-op.

## Security / privacy notes

- Subscriptions are strictly per-user (same model as in-app notifications); no
  endpoint exposes another user's tokens.
- Service-worker scope limited to the app origin; HTTPS is required for push in
  all browsers (already the case in prod).
- Secrets (service-account JSON / VAPID private key) never reach the client and
  are injected via env/secret mounts, consistent with the existing SMTP secrets.
- No new third-party data sharing in Option B; Option A sends the notification
  title/body through Google (same as the existing Android path).

## Rough effort

| | Option A (FCM Web) | Option B (Standard) |
|---|---|---|
| Backend (model, endpoints, send svc, tests) | ~1 day | ~1 day |
| Frontend (SW, permission UX, registration) | ~0.5–1 day | ~0.5–1 day |
| External setup | Firebase web config + service account + VAPID key (you) | none (keys generated) |
| New dependency | `firebase-admin` | `pywebpush`, `py-vapid` |

## Recommendation

**Chosen direction: Option A (FCM Web).**

A native **Android app for Bug Hunter is planned**, and Android push effectively
*requires* FCM — so the backend will have `firebase-admin`, a Firebase
service-account, and the FCM HTTP v1 send path regardless. Given that, using FCM
for the **web** too means **one** push system across web + Android instead of
two: one dependency, one credential, one device-token table, one send call
(`messaging.send_each_for_multicast(tokens)`), one delivery console. Choosing
standard web push now would mean maintaining two parallel push stacks later for
no benefit.

Note that FCM Web is *not* less standard at the browser layer — it uses the same
W3C Push API + VAPID under the hood; FCM is the managed wrapper plus unified
Android delivery. The only real cost (Firebase creds on this backend) is one
Android forces anyway.

Option B (standard web push) would only win if Android were **never** on the
roadmap and zero Firebase coupling were the priority — not the case here.

### Build it platform-agnostic from day one

So the future Android app plugs in with **zero backend rework**:

- `push_subscriptions` is keyed on an **FCM registration token** plus a
  `platform` column (`web` / `android`). Android devices later register their
  tokens into the **same table**.
- The send service takes a list of tokens and calls FCM — it does **not** care
  whether a token is web or Android.
- The only web-specific artifact is the `firebase-messaging-sw.js` service
  worker on the frontend; the Android client brings its own native FCM SDK.

Either way the operation triggers, the additive table, and the immediate (not
digested) delivery model are the same — only the transport/registration differ.
