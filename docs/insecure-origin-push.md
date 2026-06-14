# Web push over plain HTTP (private VPN)

## The problem (and why it isn't a code bug)

Service Workers and the Push API — the machinery FCM web push runs on — are only
available in a **secure context**. A browser treats these as secure contexts:

- `https://…` (any HTTPS origin, **even with a self-signed / internal-CA cert**), and
- `http://localhost`, `http://127.0.0.1`, `http://*.localhost`.

A plain-HTTP origin on a LAN/VPN IP (e.g. `http://your-vpn-host.example:8765`) is **not**
a secure context. On such an origin the browser makes `navigator.serviceWorker`
unavailable, so `firebase.messaging().getToken()` can never succeed. **No server
header or JavaScript can opt out of this** — it is enforced by the browser
engine itself. So there is nothing to "fix" in the app; the app code is already
correct and will work the instant the origin is treated as secure.

There are exactly two real ways to get push working on your HTTP VPN.

---

## Option 1 (recommended) — give the VPN origin a certificate (real HTTPS)

This is the proper fix and needs no per-machine browser changes. You do **not**
need a public domain or a paid certificate — an **internal CA / self-signed
cert** is enough, because browsers accept HTTPS as a secure context once the
cert is trusted on the client machines.

Sketch:

1. Generate a cert for the VPN host (its IP or an internal DNS name) with an
   internal CA (e.g. `mkcert`, or your org's AD Certificate Services).
2. Terminate TLS in front of the app (nginx/Caddy reverse proxy, or the
   container) and serve `https://<vpn-host>:8765`.
3. Trust the internal CA root on each client (one GPO push for a managed fleet).
4. In `.env` set `COOKIE_SECURE=true` and point `APP_BASE_URL` at the `https://`
   URL.

Once the origin is HTTPS, push works with zero browser flags.

---

## Option 2 — tell the browsers to trust the HTTP origin (no cert)

Chrome and Edge can be told to treat a specific insecure origin as secure. This
is the "somehow make it work over HTTP" path. Two ways to apply it:

### 2a. Fleet-wide via policy (set once, applies to every user) — preferred

Use the bundled registry file: **`scripts/web-push-insecure-origin.reg`**.

1. Open it in a text editor and replace the example origin
   `http://your-vpn-host.example:8765` with **your exact VPN origin(s)** — scheme + host
   + port, no trailing slash. Add more numbered lines for more origins.
2. Apply it on each client (or convert to a GPO — see below):
   - double-click the `.reg`, **or**
   - `reg import scripts\web-push-insecure-origin.reg` from an elevated prompt,
     **or**
   - deploy the same keys as a Group Policy Preference across the domain.
3. Fully restart Chrome/Edge on the client. Verify at `chrome://policy` /
   `edge://policy` that **OverrideSecurityRestrictionsOnInsecureOrigin** lists
   your origin.

The policy keys it sets:

```
HKLM\SOFTWARE\Policies\Google\Chrome\OverrideSecurityRestrictionsOnInsecureOrigin\1  = "http://your-vpn-host.example:8765"
HKLM\SOFTWARE\Policies\Microsoft\Edge\OverrideSecurityRestrictionsOnInsecureOrigin\1 = "http://your-vpn-host.example:8765"
```

### 2b. Per machine, no admin policy (quick test / a few users)

Each user, once, in their browser:

1. Open `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
   (Edge: `edge://flags/#unsafely-treat-insecure-origin-as-secure`).
2. Set it to **Enabled** and type your origin (e.g. `http://your-vpn-host.example:8765`)
   in the box. Multiple origins are comma-separated.
3. Click **Relaunch**.

> Firefox has no equivalent allow-list flag, so on Firefox the HTTP origin must
> use Option 1 (HTTPS). Chrome and Edge are covered by either option.

---

## Caveats

- This only affects the machines/browsers where the policy or flag is applied.
  A device that hasn't been configured will simply get no web push (the in-app
  bell and email still work — push degrades silently, by design).
- `OverrideSecurityRestrictionsOnInsecureOrigin` is a deliberate security
  relaxation. It is acceptable here **only because the origin is reachable solely
  over your private, trusted VPN**. Do not point it at a public origin.
- The app's `WEB_PUSH_ENABLED`, Firebase config, and the per-user "Allow"
  browser prompt are all still required — this step only removes the
  secure-context blocker so that machinery can run over HTTP.
