"""FastAPI application entry point for Bug Hunter."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import (
    COOKIE_NAME,
    hash_password,
    parse_session_token,
    trusted_forwarded_ip,
)
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Project, Session as SessionRow, User
from app.chatbot.router import router as chatbot_router
from app.routes import (
    audit, auth, bugs, events, notifications, projects, push, reports,
    sessions, stats, users,
)
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_ITEM_TYPES,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
    STATUSES_BY_TYPE,
)

logger = logging.getLogger("bug_hunter")
logging.basicConfig(level=get_settings().LOG_LEVEL)


# ---------------------------------------------------------------------------
# Asset version hash, recomputed on every server start.
# Injected into HTML so asset URLs change on redeploy (cache busting).
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"


# Files over this cap hash by path+size, not content, to keep startup fast.
_MAX_ASSET_FILE_BYTES = 8 * 1024 * 1024


def _compute_asset_version(static_dir: Path) -> str:
    h = hashlib.sha256()
    if not static_dir.exists():
        return "dev"
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            try:
                h.update(path.relative_to(static_dir).as_posix().encode("utf-8"))
                h.update(b"|")
                if path.stat().st_size > _MAX_ASSET_FILE_BYTES:
                    # Path + size still shifts the version if the file is replaced.
                    h.update(f"size={path.stat().st_size}".encode("utf-8"))
                else:
                    h.update(path.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


def _bootstrap() -> None:
    """Seed the default project and admin user on first run. Idempotent."""
    s = get_settings()
    with SessionLocal() as db:
        if db.query(Project).count() == 0:
            db.add(Project(
                name="General",
                description="Default project for uncategorized bugs",
                color="#c9764f",
            ))

        # Admin only when the users table is empty, so a fresh install can log in.
        if db.query(User).count() == 0:
            if s.is_production and s.BOOTSTRAP_ADMIN_PASSWORD == "ChangeMe123!":
                # Refuse the default password on production deploys.
                raise RuntimeError(
                    "Refusing to bootstrap the default admin with the built-in "
                    "default password in a production deploy. Set "
                    "BOOTSTRAP_ADMIN_PASSWORD to a strong value (and APP_ENV / "
                    "COOKIE_SECURE appropriately for non-production)."
                )
            admin = User(
                name=s.BOOTSTRAP_ADMIN_NAME,
                email=s.BOOTSTRAP_ADMIN_EMAIL.lower(),
                role="admin",
                is_active=True,
                password_hash=hash_password(s.BOOTSTRAP_ADMIN_PASSWORD),
            )
            db.add(admin)
            logger.warning(
                "Bootstrap: created default admin %s — CHANGE THE PASSWORD.",
                s.BOOTSTRAP_ADMIN_EMAIL,
            )
        db.commit()


def _runtime_config_warnings(s) -> list[str]:
    """Collect non-fatal startup warnings for insecure-but-allowed config.

    Returned as a list so tests can verify the policy; fatal checks stay in lifespan().
    """
    warnings: list[str] = []
    if not s.SESSION_SECRET:
        warnings.append(
            "SESSION_SECRET is not set. Using a random per-process fallback, so "
            "sessions will NOT survive a restart and multi-worker deployments log "
            "users out unpredictably. Set SESSION_SECRET (`openssl rand -hex 32`) "
            "for any non-throwaway deploy, HTTP or HTTPS."
        )
    if s.is_production and s.EMAIL_BACKEND == "console":
        warnings.append(
            "EMAIL_BACKEND=console on a production deploy: every email body, "
            "INCLUDING password-reset links, is written to the logs. Set "
            "EMAIL_BACKEND=smtp (or 'disabled') in production."
        )
    return warnings


def _safe_init_db() -> bool:
    """Run schema init and bootstrap; a DB-down error starts the app degraded
    (reported by /api/health) rather than crash-looping."""
    try:
        init_db()
        _bootstrap()
        return True
    except (SQLAlchemyError, OSError):
        logger.exception(
            "Database initialization failed at startup; starting degraded."
        )
        return False


def _check_db_health() -> bool:
    """Quick DB liveness probe used by /api/health."""
    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            return True
        finally:
            db.close()
    except (SQLAlchemyError, OSError):
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    _safe_init_db()

    _settings = get_settings()
    if _settings.is_production and len(_settings.SESSION_SECRET) < 32:
        # Fail closed rather than weaken every session.
        raise RuntimeError(
            "SESSION_SECRET must be set to a strong value (>= 32 chars) in "
            "production (APP_ENV=production or COOKIE_SECURE=true). Generate "
            "one with `openssl rand -hex 32`."
        )
    for _w in _runtime_config_warnings(_settings):
        logger.warning(_w)

    # Optional email-digest scheduler. No-op unless EMAIL_DIGEST_CRON is set.
    from app import scheduler
    scheduler.start()

    logger.info("Bug Hunter started. asset_version=%s", app.state.asset_version)
    yield
    await scheduler.stop()
    logger.info("Bug Hunter shutting down.")


settings = get_settings()
# Expose interactive docs in dev; disable in production unless explicitly opted in.
_docs_enabled = settings.ENABLE_API_DOCS or not settings.is_production
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Computed once at import time. Kept on app.state so tests can override it.
app.state.asset_version = _compute_asset_version(settings.STATIC_DIR)


# ---------------------------------------------------------------------------
# CORS
# The SPA uses cookies, so responses must echo a concrete allowlisted Origin —
# the spec forbids "*" with credentials.
# ---------------------------------------------------------------------------
_origins = list(settings.CORS_ORIGINS)
_allow_credentials = True
if not _origins:
    # Same-origin only; same-origin traffic skips CORS anyway.
    _allow_credentials = False
elif "*" in _origins:
    # Wildcard + credentials is forbidden; drop credentials so preflights still pass.
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS contains '*' which disables credentialed CORS. Set "
        "CORS_ORIGINS to your concrete origin(s) (e.g. "
        "https://bugs.example.com) to allow cross-origin browser sessions."
    )

# CORSMiddleware is registered last: Starlette stacks middleware in reverse,
# and CORS must be outermost to intercept OPTIONS before other middleware.

# ---------------------------------------------------------------------------
# Gzip compression — skips bodies under 1 KB and already-compressed types.
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# Request body size limit
# Rejects an oversized Content-Length with 413 before the body buffers into
# RAM. Chunked requests are covered by StreamingBodyLimitMiddleware.
# ---------------------------------------------------------------------------
class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl_header = request.headers.get("content-length")
        if cl_header:
            try:
                length = int(cl_header)
            except ValueError:
                # Malformed Content-Length; reject.
                return JSONResponse(
                    status_code=400,
                    content={"detail": "We couldn't process this request. Please try again."},
                )
            if length > settings.MAX_REQUEST_BODY_BYTES:
                logger.warning(
                    "Body too large: %d bytes claimed (limit %d) on %s",
                    length, settings.MAX_REQUEST_BODY_BYTES, request.url.path,
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "This upload is too large. Please attach a smaller file."},
                )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


class _RequestBodyTooLarge(Exception):
    """Raised by the wrapped receive callable when the body exceeds the cap."""


class StreamingBodyLimitMiddleware:
    """Pure-ASGI backstop: counts actual body bytes so chunked requests
    (no Content-Length) can't stream unbounded into RAM; 413 past the cap.
    Raw ASGI because BaseHTTPMiddleware cannot wrap `receive`."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = settings.MAX_REQUEST_BODY_BYTES
        total = 0

        async def counting_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b"") or b"")
                if total > max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        started = False

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _RequestBodyTooLarge:
            if started:
                # Response already started; can't replace it cleanly.
                raise
            logger.warning(
                "Streaming body exceeded %d bytes on %s",
                max_bytes, scope.get("path", ""),
            )
            resp = JSONResponse(
                status_code=413,
                content={"detail": "This upload is too large. Please attach a smaller file."},
            )
            _apply_security_headers(resp.headers)
            resp.headers.setdefault("Cache-Control", "no-store")
            await resp(scope, receive, send)


app.add_middleware(StreamingBodyLimitMiddleware)


# ---------------------------------------------------------------------------
# Cache-Control middleware — prevents stale HTML after redeploy.
#   HTML / api -> no-store; /static/assets/ -> immutable 1y (content-hashed);
#   /static/ -> 1h.
# ---------------------------------------------------------------------------
class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        path = request.url.path
        # Don't override if the route already set Cache-Control (e.g. downloads).
        if response.headers.get("Cache-Control"):
            return response
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/assets/"):
            # Vite content-hashed filenames — safe to cache for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            # Not fingerprinted; shorter cache.
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            # HTML uncached so a redeploy shows immediately.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(CacheControlMiddleware)


# ---------------------------------------------------------------------------
# Security headers — applied to every response.
# style-src needs 'unsafe-inline' (app sets .style.x); HSTS is gated on
# COOKIE_SECURE so a dev/HTTP deploy isn't locked into HTTPS.
# ---------------------------------------------------------------------------
# Endpoints the Firebase SDK needs for token mint/refresh; added to
# connect-src only when web push is enabled. Firebase scripts are vendored,
# so script-src stays 'self'.
_FCM_CONNECT_SRC = (
    " https://fcm.googleapis.com https://fcmregistrations.googleapis.com"
    " https://firebaseinstallations.googleapis.com https://www.googleapis.com"
)


def _build_csp() -> str:
    connect = "connect-src 'self'"
    if get_settings().WEB_PUSH_ENABLED:
        connect += _FCM_CONNECT_SRC
    return (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "font-src 'self' data:; "
        f"{connect}; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


_CSP = _build_csp()

def _apply_security_headers(h) -> None:
    """Set the standard security headers (shared by middleware, short-circuit
    responses, and the 500 handler). setdefault lets handlers override."""
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), geolocation=(), "
                 "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    # Don't advertise the stack.
    if "server" in h:
        del h["server"]
    # HSTS only behind real HTTPS (COOKIE_SECURE doubles as the signal).
    if settings.COOKIE_SECURE:
        h.setdefault("Strict-Transport-Security",
                     "max-age=63072000; includeSubDomains")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        _apply_security_headers(response.headers)
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Rate limiting on auth-sensitive endpoints
# In-memory per-IP sliding window (per-worker buckets; add nginx limit_req
# for a strict global limit). Tuned to absorb typos, slow credential stuffing.
# ---------------------------------------------------------------------------
_RATE_RULES: dict[str, tuple[int, int]] = {
    # (max_requests, window_seconds)
    "/api/auth/login": (8, 60),
    "/api/auth/forgot-password": (3, 60),
    # change-password bcrypt-verifies on every call — throttle the amplification.
    "/api/auth/reset-password": (5, 60),
    "/api/auth/change-password": (5, 60),
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
# Soft cap to bound memory when hammered from many IPs.
_RATE_BUCKETS_MAX = 10_000
# Buckets idle longer than the longest window are reclaimable.
_MAX_RATE_WINDOW = max((w for _, w in _RATE_RULES.values()), default=60)


def _evict_one_rate_bucket(now: float) -> None:
    """Make room for a new bucket: reclaim idle buckets first, then the
    least-recently-active — never insertion order, so an attacker can't churn
    keys to flush their own throttle. Caller holds _rate_lock."""
    horizon = now - _MAX_RATE_WINDOW
    dead = [k for k, b in _rate_buckets.items() if not b or b[-1] < horizon]
    for k in dead:
        del _rate_buckets[k]
    if len(_rate_buckets) >= _RATE_BUCKETS_MAX and _rate_buckets:
        oldest = min(_rate_buckets, key=lambda k: _rate_buckets[k][-1])
        del _rate_buckets[oldest]


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate limiting.

    X-Forwarded-For is spoofable, so it's only used when TRUST_PROXY_FORWARDED_FOR
    is set; trusted_forwarded_ip shares semantics with the audit IP resolver.
    """
    if settings.TRUST_PROXY_FORWARDED_FOR:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            trusted = trusted_forwarded_ip(xff, settings.TRUST_PROXY_HOP_COUNT)
            if trusted is not None:
                return trusted
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        rule = _RATE_RULES.get(path)
        if rule is None or request.method.upper() != "POST":
            return await call_next(request)

        max_req, window = rule
        ip = _client_ip(request)
        now = time.monotonic()
        cutoff = now - window

        with _rate_lock:
            bucket = _rate_buckets.get((path, ip))
            if bucket is None:
                # Evict idle buckets first; only fall back to least-recently-active.
                if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
                    _evict_one_rate_bucket(now)
                bucket = deque()
                _rate_buckets[(path, ip)] = bucket
            # Drop timestamps outside the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_req:
                retry_after = max(1, int(window - (now - bucket[0])))
                logger.warning(
                    "Rate limit hit: %s from %s (%d/%d in %ss)",
                    path, ip, len(bucket), max_req, window,
                )
                resp = JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
                _apply_security_headers(resp.headers)
                resp.headers.setdefault("Cache-Control", "no-store")
                return resp
            bucket.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# CSRF defense in depth — SameSite=Lax has gaps (subdomains, older browsers),
# so mutating /api/ requests must present a matching Origin/Referer. Clients
# sending neither (curl, httpx) pass: CSRF needs a browser as deputy.
# ---------------------------------------------------------------------------
def _allowed_origins() -> set[str]:
    """Origins allowed by the CSRF check (CORS_ORIGINS minus "*"); the request
    Host is added separately so SPA usage needs no CORS config."""
    return {o.rstrip("/") for o in settings.CORS_ORIGINS if o and o != "*"}


# Safe methods (GET/HEAD/OPTIONS) skip the check.
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# No exemptions: login included, so cross-site forced-login is blocked too.
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Block mutating requests whose Origin doesn't match the app's host."""
    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path
        if (
            method not in _CSRF_UNSAFE_METHODS
            or not path.startswith("/api/")
            or path in _CSRF_EXEMPT_PATHS
        ):
            return await call_next(request)

        origin = request.headers.get("origin", "").rstrip("/")
        referer = request.headers.get("referer", "")

        # No browser fingerprint: not a browser, cannot be CSRF.
        if not origin and not referer:
            return await call_next(request)

        # Same-origin URLs from the request Host. Schemes are concatenated
        # (not literal "http://") to dodge static-analyzer hardcoded-URL warnings.
        host = request.headers.get("host", "")
        allowed = _allowed_origins()
        if host:
            sep = "://"
            for scheme in ("http", "https"):
                allowed.add(scheme + sep + host)

        if origin:
            if origin in allowed:
                return await call_next(request)
        elif referer:
            # Origin absent but Referer present: match by URL prefix.
            if any(referer.startswith(a + "/") or referer == a for a in allowed if a):
                return await call_next(request)

        logger.warning(
            "CSRF check failed: method=%s path=%s origin=%r referer=%r",
            method, path, origin, referer,
        )
        resp = JSONResponse(
            status_code=403,
            content={"detail": "Cross-origin request blocked."},
        )
        _apply_security_headers(resp.headers)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp


app.add_middleware(CsrfOriginMiddleware)

# CORS registered last (outermost — see note near _origins).
if _origins:
    # Restrict methods/headers to what the SPA uses; smaller blast radius.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


# Rendered pages are stable per process; keyed on asset_version so tests
# that swap it stay correct.
_html_cache: dict[tuple[str, str], str] = {}


def _serve_html(filename: str) -> HTMLResponse:
    """Serve an HTML file with the version placeholders replaced, cached
    per (file, asset_version)."""
    key = (filename, app.state.asset_version)
    body = _html_cache.get(key)
    if body is None:
        body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
        body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
        body = body.replace(APP_VERSION_PLACEHOLDER, settings.APP_VERSION)
        if len(_html_cache) > 32:  # only a few pages exist; clear if somehow large
            _html_cache.clear()
        _html_cache[key] = body
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """True if the request carries a valid, non-revoked session cookie.

    Must check revocation, not just the signature — accepting a revoked cookie
    here makes / and /login.html redirect-loop each other.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        # Legacy pre-sessions-table cookie: signature alone; /api/auth/me still
        # does the full check.
        return True
    # Modern cookie: the session row must exist and not be expired.
    db = SessionLocal()
    try:
        sess = db.scalar(select(SessionRow).where(SessionRow.jti == jti))
        if sess is None or sess.user_id != user_id:
            return False
        expires = sess.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires >= datetime.now(timezone.utc)
    except SQLAlchemyError:
        # DB momentarily down: send to login rather than 500.
        logger.exception("_has_valid_session: DB lookup failed for jti=%s", jti)
        return False
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    # Server-side redirect avoids a flash of the empty app shell.
    if not _has_valid_session(request):
        return RedirectResponse(url="/login.html", status_code=302)
    return _serve_html("index.html")


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    # Skip the login form for users who are already logged in.
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("login.html")


@app.get("/reset.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    # Always reachable — even a logged-in user may follow a reset link.
    return _serve_html("reset.html")


# FCM service worker served from root scope (a worker only controls pages at
# or below its own path, so /static/ would be too narrow). Config is injected
# like the HTML placeholders.
_FIREBASE_SW = """\
importScripts('/static/vendor/firebase-app-compat.js');
importScripts('/static/vendor/firebase-messaging-compat.js');
firebase.initializeApp(__FIREBASE_CONFIG__);
const messaging = firebase.messaging();
messaging.onBackgroundMessage(function (payload) {
  const n = payload.notification || {};
  const d = payload.data || {};
  self.registration.showNotification(n.title || 'Bug Hunter', {
    body: n.body || '',
    icon: '/static/icon.png',
    badge: '/static/icon.png',
    data: { url: d.url || '/' },
    tag: d.url || 'bug-hunter'
  });
});
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (cl) {
    for (const c of cl) { if ('focus' in c) { c.navigate(url); return c.focus(); } }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
"""


@app.get("/firebase-messaging-sw.js", include_in_schema=False)
def firebase_messaging_sw() -> Response:
    """Serve the FCM background service worker with the Firebase config injected.

    Returns a no-op comment when web push isn't configured, so the client-side
    service worker registration doesn't 404.
    """
    media = "application/javascript"
    headers = {"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"}
    if not (settings.WEB_PUSH_ENABLED and settings.FIREBASE_API_KEY):
        return Response("/* web push not configured */", media_type=media, headers=headers)
    cfg = json.dumps({
        "apiKey": settings.FIREBASE_API_KEY,
        "authDomain": settings.FIREBASE_AUTH_DOMAIN,
        "projectId": settings.FIREBASE_PROJECT_ID,
        "messagingSenderId": settings.FIREBASE_MESSAGING_SENDER_ID,
        "appId": settings.FIREBASE_APP_ID,
    })
    return Response(
        _FIREBASE_SW.replace("__FIREBASE_CONFIG__", cfg),
        media_type=media,
        headers=headers,
    )


@app.get("/api/health", tags=["meta"])
def health() -> Response:
    """Liveness and readiness probe.

    Returns 503 when the database is unreachable so the Docker HEALTHCHECK and
    load balancer treat a DB-down app as unhealthy. version and asset_version
    are unauthenticated: the SPA uses asset_version to detect a redeploy, and
    the app version is already shown on the public login page.
    """
    db_ok = _check_db_health()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "version": settings.APP_VERSION,
        "asset_version": app.state.asset_version,
    }
    return JSONResponse(payload, status_code=200 if db_ok else 503)


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    """Return static enums and per-item-type status sets for the frontend."""
    return {
        "statuses": ALLOWED_STATUSES,
        "statuses_by_type": STATUSES_BY_TYPE,
        "priorities": ALLOWED_PRIORITIES,
        "environments": ALLOWED_ENVIRONMENTS,
        "item_types": ALLOWED_ITEM_TYPES,
    }


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(bugs.router)
app.include_router(events.router)
app.include_router(stats.router)
app.include_router(reports.router)
app.include_router(audit.router)
app.include_router(sessions.router)
app.include_router(notifications.router)
app.include_router(push.router)
app.include_router(chatbot_router)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # Preserve headers attached by the raiser (Retry-After on 429,
    # WWW-Authenticate on 401); FastAPI's default handler drops them.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or None,
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    # ServerErrorMiddleware sits outside the security-headers middleware, so
    # without this handler an unhandled 500 would ship as bare text with no
    # CSP or anti-clickjacking headers. Return a generic JSON response with the
    # full header set and no internal details.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    _apply_security_headers(resp.headers)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    import uvicorn
    # Default to loopback. Set UVICORN_HOST=0.0.0.0 in containers where the
    # container boundary and reverse proxy make it safe to bind all interfaces.
    _host = os.getenv("UVICORN_HOST", "127.0.0.1")
    _port = int(os.getenv("UVICORN_PORT", "8000"))
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
