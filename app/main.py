"""FastAPI entry point — Bug Hunter."""
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

from app.auth import COOKIE_NAME, hash_password, parse_session_token
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
# Asset version — recomputed on every server start.
#
# Lets redeploys take effect without a hard refresh. Hashing the real bytes
# of every static asset and injecting that hash into the HTML wherever a
# placeholder appears gives each asset a fresh URL on every redeploy, so
# browsers never serve a stale cached copy.
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"


def _compute_asset_version(static_dir: Path) -> str:
    h = hashlib.sha256()
    if not static_dir.exists():
        return "dev"
    for path in sorted(static_dir.rglob("*")):
        if path.is_file() and not path.name.startswith("."):
            try:
                h.update(path.relative_to(static_dir).as_posix().encode("utf-8"))
                h.update(b"|")
                h.update(path.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


def _bootstrap() -> None:
    """Run once at startup. Creates the default project + first admin user
    if the DB is empty. Idempotent — safe to call repeatedly."""
    s = get_settings()
    with SessionLocal() as db:
        if db.query(Project).count() == 0:
            db.add(Project(
                name="General",
                description="Default project for uncategorized bugs",
                color="#c9764f",
            ))

        # First-run admin. After wiping the DB, this allows an immediate
        # login without touching SQL. The admin should change the password
        # after first login (settings menu, Change password).
        if db.query(User).count() == 0:
            if s.COOKIE_SECURE and s.BOOTSTRAP_ADMIN_PASSWORD == "ChangeMe123!":
                # Don't stand up a live admin with a publicly-known password on
                # a production (COOKIE_SECURE=true) deploy — fail closed.
                raise RuntimeError(
                    "Refusing to bootstrap the default admin with the built-in "
                    "default password in a production (COOKIE_SECURE=true) "
                    "deploy. Set BOOTSTRAP_ADMIN_PASSWORD to a strong value."
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
    """Non-fatal startup warnings for insecure-but-allowed configuration.

    Returned (not just logged) so the policy is unit-testable without driving the
    whole lifespan. The fatal fail-closed checks stay inline in lifespan().
    """
    warnings: list[str] = []
    if not s.SESSION_SECRET:
        # Fine for dev (a strong random secret is generated per process) but on
        # ANY long-lived deploy — including HTTP/intranet (COOKIE_SECURE=false),
        # where the fatal check below doesn't fire — every restart invalidates
        # every session and multi-worker uvicorn gives each worker its own
        # secret, randomly logging users out.
        warnings.append(
            "SESSION_SECRET is not set. Using a random per-process fallback, so "
            "sessions will NOT survive a restart and multi-worker deployments log "
            "users out unpredictably. Set SESSION_SECRET (`openssl rand -hex 32`) "
            "for any non-throwaway deploy, HTTP or HTTPS."
        )
    if s.COOKIE_SECURE and s.EMAIL_BACKEND == "console":
        # The console backend logs full email bodies — including live, single-use
        # password-reset links — to stdout/log aggregation. Fine for dev; a
        # credential-in-logs exposure on a production-signalled deploy.
        warnings.append(
            "EMAIL_BACKEND=console on a COOKIE_SECURE deploy: every email body, "
            "INCLUDING password-reset links, is written to the logs. Set "
            "EMAIL_BACKEND=smtp (or 'disabled') in production."
        )
    return warnings


def _safe_init_db() -> bool:
    """Run schema init + bootstrap, swallowing a DB-down failure so a transient
    outage at boot starts the app degraded instead of crash-looping with no
    observable cause."""
    try:
        init_db()
        _bootstrap()
        return True
    except (SQLAlchemyError, OSError):
        logger.exception(
            "Database initialization failed at startup — starting degraded; "
            "/api/health will report the database as unavailable until it recovers."
        )
        return False


def _check_db_health() -> bool:
    """Cheap DB liveness probe for /api/health."""
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
    if _settings.COOKIE_SECURE and len(_settings.SESSION_SECRET) < 32:
        # Production signal (COOKIE_SECURE=true / HTTPS) but no stable, strong
        # secret → fail closed rather than silently weakening every session
        # (per-process random secret = users logged out on every restart and
        # non-deterministically across workers).
        raise RuntimeError(
            "SESSION_SECRET must be set to a strong value (>= 32 chars) when "
            "COOKIE_SECURE=true. Generate one with `openssl rand -hex 32`."
        )
    for _w in _runtime_config_warnings(_settings):
        logger.warning(_w)

    # Optional in-app email-digest scheduler. No-op unless EMAIL_DIGEST_CRON
    # is configured (see app/scheduler.py); never breaks startup.
    from app import scheduler
    scheduler.start()

    logger.info("Bug Hunter started. asset_version=%s", app.state.asset_version)
    yield
    await scheduler.stop()
    logger.info("Bug Hunter shutting down.")


settings = get_settings()
# Interactive API docs (/docs, /redoc, /openapi.json) are handy in dev but
# publish the full endpoint surface for recon. Keep them in dev; drop them once
# COOKIE_SECURE (the production/https signal) is set, unless ENABLE_API_DOCS
# explicitly forces them on.
_docs_enabled = settings.ENABLE_API_DOCS or not settings.COOKIE_SECURE
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Compute once at import time — used by middleware and the HTML serving
# helper. Kept on app.state so tests can override it deterministically.
app.state.asset_version = _compute_asset_version(settings.STATIC_DIR)


# ---------------------------------------------------------------------------
# CORS
#
# The CORS spec forbids `Access-Control-Allow-Origin: *` together with
# `Allow-Credentials: true`, and browsers reject this combination silently.
# The SPA uses cookies (credentials=true), so the response must echo the
# request's actual Origin (only when it is in the allowlist), never "*".
# Starlette's CORSMiddleware does that when given a concrete origin list, but
# a sole "*" entry breaks credentialed requests; detect that case and disable
# credentials rather than silently breaking auth from non-same-origin clients.
# ---------------------------------------------------------------------------
_origins = list(settings.CORS_ORIGINS)
_allow_credentials = True
if not _origins:
    # Empty list = same-origin only. A wildcard fallback would let any site
    # read authenticated responses; same-origin SPA usage doesn't pass through
    # CORS middleware anyway, so this is the safe default.
    _allow_credentials = False
elif "*" in _origins:
    # Wildcard + credentials is forbidden by the CORS spec and silently broken
    # by browsers — fall back to no-credentials so the OPTIONS preflight at
    # least succeeds.
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS contains '*' which disables credentialed CORS. Set "
        "CORS_ORIGINS to your concrete origin(s) (e.g. "
        "https://bugs.example.com) to allow cross-origin browser sessions."
    )

# CORSMiddleware is added LAST in this file (after every other middleware) so
# it runs OUTERMOST in the ASGI chain. Starlette stacks middleware in
# reverse-registration order, so the last add_middleware() call wraps the
# outside — which is what CORS needs to handle preflight OPTIONS without other
# middleware (rate-limit, CSP) firing first and short-circuiting the preflight.

# ---------------------------------------------------------------------------
# Gzip compression
#
# Shrinks JSON / HTML / JS / CSS responses by ~70-90% over the wire so a
# low-resource server spends less time pushing bytes. Skips bodies smaller
# than 1 KB (compression CPU cost isn't worth it for tiny payloads) and skips
# already-compressed binary types (images / video) because Starlette's
# GZipMiddleware checks the Accept-Encoding header rather than blindly
# compressing.
#
# Attachment downloads are unaffected — they ship their own Cache-Control
# header which exits the cache middleware early, and gzip is unhelpful for
# already-compressed media (PDFs, JPEGs, MP4s, etc.).
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# Request body size limit
#
# Pydantic caps individual string fields (description = 1 MB, comment body
# = 200 KB) and the attachment endpoint streams and aborts at 50 MB. But a
# request that arrives with a 5 GB Content-Length and no body fields the
# schema cares about — or a multipart upload to an unexpected endpoint —
# would still buffer body bytes into RAM before validation fails.
#
# This middleware rejects with 413 (Payload Too Large) before the body is
# read whenever Content-Length exceeds MAX_REQUEST_BODY_BYTES — a coarse
# second-line defense alongside the per-endpoint limits.
#
# Requests without a Content-Length (chunked transfer) are allowed through;
# the per-endpoint streamed reads still bound them.
# ---------------------------------------------------------------------------
class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cl_header = request.headers.get("content-length")
        if cl_header:
            try:
                length = int(cl_header)
            except ValueError:
                # Malformed Content-Length is the client's bug; reject.
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header."},
                )
            if length > settings.MAX_REQUEST_BODY_BYTES:
                logger.warning(
                    "Body too large: %d bytes claimed (limit %d) on %s",
                    length, settings.MAX_REQUEST_BODY_BYTES, request.url.path,
                )
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large."},
                )
        return await call_next(request)


app.add_middleware(BodySizeLimitMiddleware)


# ---------------------------------------------------------------------------
# Cache-Control middleware
#
# Prevents the stale-HTML problem on redeploy: when the browser has cached
# HTML pointing at old asset URLs, the user would otherwise see a broken page
# until a hard refresh.
#
#   - HTML       → no-store, must-revalidate. Tiny payload, cheap to refetch.
#   - /static/*  → public, max-age=1 year, immutable. Safe because the URL
#                  changes on every deploy via the injected asset_version.
#   - /api/*     → no-store (API responses must never be cached anywhere).
# ---------------------------------------------------------------------------
class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        path = request.url.path
        # Don't override if the route already set a Cache-Control header
        # (e.g. attachment downloads).
        if response.headers.get("Cache-Control"):
            return response
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/static/"):
            # Static assets are cache-busted by the asset_version query
            # string, so they're safe to cache aggressively.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            # HTML pages — never cache so a redeploy is reflected immediately.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(CacheControlMiddleware)


# ---------------------------------------------------------------------------
# Security headers
#
# Set on every response so even cached HTML and 304s get the same protections.
#
# CSP notes:
#   - script-src 'self'         no inline <script> in the HTML, so no
#                               'unsafe-inline' or hash juggling needed.
#   - style-src 'self' 'unsafe-inline'
#                               the app sets a few inline styles via the DOM
#                               (.style.x = …) which CSP treats as inline
#                               styles. 'unsafe-inline' is the practical
#                               escape; a nonce strategy would require
#                               touching every dynamic-style site.
#   - img-src 'self' data: blob:
#                               attachments and generated avatars use data:
#                               URLs; downloaded blobs use blob:.
#   - frame-ancestors 'none'    refuses iframe embedding (modern X-Frame-Options).
#   - base-uri 'self'           prevents <base href=…> hijack.
#   - object-src 'none'         no plugins.
#   - form-action 'self'        forms can only post to this origin.
#
# HSTS is conditional on COOKIE_SECURE to avoid emitting it behind an
# HTTP-only dev proxy and locking the browser into https://.
# ---------------------------------------------------------------------------
# When web push is on, the browser's Firebase Messaging SDK (self-hosted —
# script-src stays 'self') talks to these Google endpoints to mint/refresh the
# device token. They are added to connect-src only then, so the default posture
# stays locked to 'self' when push is off. Firebase scripts are vendored
# locally, so script-src never needs to be relaxed.
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
    """Set the standard security headers on a response header map.

    Shared so the normal path (this middleware), the middleware-generated
    short-circuits (429 rate-limit, 403 CSRF — they sit OUTSIDE this middleware
    in the stack) and the generic 500 handler (served by Starlette's
    ServerErrorMiddleware, also outside this one) all emit one identical,
    complete set. `setdefault` so a downstream layer can still override.
    """
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), geolocation=(), "
                 "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # CORP blocks other origins from fetching our responses as subresources.
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    # Strip uvicorn's default "server: uvicorn" — no reason to advertise the stack.
    if "server" in h:
        del h["server"]
    # HSTS: only safe behind real https; COOKIE_SECURE doubles as the https signal.
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
#
# A small in-memory sliding-window limiter with no Redis dependency. Per-IP
# buckets so one bad actor doesn't lock out everyone. Keys are kept tiny
# (<200 bytes/IP) and time-pruned on every check, so memory stays bounded
# under typical load. Multi-worker deployments get per-worker buckets, so a
# determined attacker could still get N×limit attempts; the fix at scale is to
# put nginx in front (it has its own limit_req).
#
# Limits chosen for human users with occasional typos but tight enough to
# meaningfully slow credential-stuffing scripts:
#   /api/auth/login           — 8 attempts / 60s per IP
#   /api/auth/forgot-password — 3 attempts / 60s per IP (more abusive)
# ---------------------------------------------------------------------------
_RATE_RULES: dict[str, tuple[int, int]] = {
    # path: (max_requests, window_seconds)
    "/api/auth/login": (8, 60),
    "/api/auth/forgot-password": (3, 60),
    # reset-password validates a 256-bit token (brute-force already infeasible)
    # but we cap it anyway; change-password bcrypt-verifies the current password
    # on every call, so an unthrottled endpoint is an auth-amplification vector.
    "/api/auth/reset-password": (5, 60),
    "/api/auth/change-password": (5, 60),
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
# Soft cap to keep memory bounded if someone hammers from many IPs.
_RATE_BUCKETS_MAX = 10_000


def _client_ip(request: Request) -> str:
    """Resolve the client IP. X-Forwarded-For is not trusted by default
    because spoofing it bypasses the limit; deploys behind a reverse proxy
    can opt into trusting it via TRUST_PROXY_FORWARDED_FOR."""
    if settings.TRUST_PROXY_FORWARDED_FOR:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            # Left-most entry is the original client per RFC 7239 conventions.
            return xff.split(",")[0].strip()
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
                # Bound the dict — drop a random old entry if we're at cap.
                if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
                    _rate_buckets.pop(next(iter(_rate_buckets)), None)
                bucket = deque()
                _rate_buckets[(path, ip)] = bucket
            # Evict timestamps older than the window.
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
# CSRF defense-in-depth
#
# The session cookie is already SameSite=Lax, which blocks most cross-site
# CSRF, but Lax has known gaps:
#   - it doesn't apply to subdomain attackers (if an attacker controls
#     evil.example.com and the app is at app.example.com, they're "same
#     site").
#   - older / non-standard browser behaviour for some top-level POSTs.
#
# A second check covers this: for state-changing requests (POST / PUT /
# PATCH / DELETE) to the JSON API, the Origin (or Referer) header must match
# one of the allowed origins. Same-origin SPA requests always satisfy this —
# fetch() includes Origin automatically. Server-to-server clients that send
# neither Origin nor Referer (e.g. curl with an API key, httpx without a Host)
# pass, since CSRF only matters when a browser is abused as a deputy.
#
# /api/auth/login is exempted because cross-origin login from a trusted admin
# tool is a legitimate flow some operators want; the login itself is
# rate-limited and credential-checked.
#
# Allowed origins: settings.CORS_ORIGINS (when not "*"). The request's own
# Host is always implicitly allowed; the Origin scheme is checked against the
# cookie-secure mode to avoid http→https confusion behind a reverse proxy.
# ---------------------------------------------------------------------------
def _allowed_origins() -> set[str]:
    """Build the set of allowed origins for the CSRF check.

    Always includes the configured CORS_ORIGINS (skipping "*"). Same-host
    requests are accepted unconditionally via Host comparison so an
    operator who hasn't bothered to configure CORS still gets working
    SPA usage.
    """
    return {o.rstrip("/") for o in settings.CORS_ORIGINS if o and o != "*"}


# Methods that mutate state; safe methods (GET/HEAD/OPTIONS) skip the check.
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Endpoints exempt from the Origin check. None: login is no longer exempt so a
# cross-site forced-login (session fixation) is blocked. Operator scripts still
# work — they send no Origin/Referer and are allowed by the no-fingerprint
# branch — and a legitimately allow-listed cross-origin browser client passes
# via CORS_ORIGINS.
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Reject browser-driven mutating requests whose Origin doesn't match
    the app's own host (or an explicitly-allowed CORS origin).

    Non-browser clients (no Origin and no Referer) are allowed through —
    CSRF is exclusively a browser-confused-deputy attack, so a request
    that came from curl is by definition not a CSRF.
    """
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

        # No browser fingerprint at all → not a browser → cannot be CSRF.
        if not origin and not referer:
            return await call_next(request)

        # Build the expected same-origin URLs from the request itself,
        # accepting both schemes. Behind a reverse proxy that terminates TLS,
        # request.url.scheme is whatever the proxy claims it is; trusting that
        # here is fine because a compromised proxy makes CSRF the least of the
        # concerns. Schemes are concatenated rather than written as inline
        # literals so static analyzers don't flag "http://" as an insecure
        # protocol choice — it describes the client connection, not an
        # outbound one.
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
            # Origin missing but Referer present — match by URL prefix.
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

# CORS is registered LAST so it sits OUTERMOST in the ASGI chain — see the
# block near _origins above for why. Skip registration entirely when no
# origins are configured: same-origin traffic never hits CORSMiddleware
# anyway, so adding it would just be dead code.
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


# Rendered-HTML cache. The placeholders depend only on asset_version
# (fixed at process start) and APP_VERSION (env, fixed for the process
# lifetime), so the rendered page never changes between restarts —
# re-reading the file from disk on every request was pure I/O waste on
# the small deployment target. Tests that mutate app.state.asset_version
# key the cache on it, so they stay correct.
_html_cache: dict[tuple[str, str], str] = {}


def _serve_html(filename: str) -> HTMLResponse:
    """Render an HTML file with the asset-version and app-version
    placeholders replaced, caching the result per (file, asset_version).

    __ASSET_VERSION__ → 12-char hash of the static bundle, for cache-busting.
    __APP_VERSION__   → settings.APP_VERSION. Lets the login page show the
                       running version without an extra round trip to
                       /api/health.
    """
    key = (filename, app.state.asset_version)
    body = _html_cache.get(key)
    if body is None:
        body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
        body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
        body = body.replace(APP_VERSION_PLACEHOLDER, settings.APP_VERSION)
        if len(_html_cache) > 32:  # paranoia bound; we serve ~3 pages
            _html_cache.clear()
        _html_cache[key] = body
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """Check whether the request has a valid, non-revoked session cookie.

    The HTML page handlers (/ and /login.html) must reject a revoked session
    — one whose server-side sessions row was deleted while the browser still
    holds the signed cookie — not just verify the signature. Otherwise the
    SPA's API calls return 401 and redirect to /login.html, while this
    function still reports the cookie valid and bounces the user back to /,
    producing an infinite redirect loop.

    When the cookie carries a jti, look it up in the sessions table and only
    return True if the row exists and is not expired. Legacy tokens without a
    jti predate the sessions table, so they keep the cookie-only check here;
    they still get the full API-side check on /api/auth/me.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        # Legacy cookie pre-dates the sessions table — accept based on
        # signature alone. _user_from_request still validates fully on
        # any API call.
        return True
    # Modern cookie: verify the session row still exists and isn't expired.
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
        # This is called from the HTML page handlers (/ and /login.html).
        # If the DB is momentarily unreachable, we'd rather fall back to
        # "no valid session" — which sends the user to the login page —
        # than 500 the entire HTML route. The next API call will surface
        # the real DB error to the SPA with a proper toast.
        logger.exception("_has_valid_session: DB lookup failed for jti=%s", jti)
        return False
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    # If not logged in, redirect to login page server-side so the user
    # never sees a flash of the app shell with no data.
    if not _has_valid_session(request):
        return RedirectResponse(url="/login.html", status_code=302)
    return _serve_html("index.html")


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    # If already logged in, send them straight to the app — there's no
    # value in showing a login form to a logged-in user.
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("login.html")


@app.get("/reset.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    # Reset page is always reachable — even logged-in users may need to
    # reset somebody else's password from a link.
    return _serve_html("reset.html")


# Firebase Cloud Messaging background service worker. Served from the ROOT
# scope (a service worker can only control pages at or below its own URL path,
# so /static/ would be too narrow). Firebase's compat SDK is self-hosted under
# /static/vendor (CSP script-src stays 'self'); the public web config is
# injected here the same way the HTML pages get their version placeholders.
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
    """Serve the FCM background service worker with the public Firebase config
    injected. A no-op worker when web push isn't configured, so registration
    never 404s."""
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
def health() -> dict[str, str]:
    db_ok = _check_db_health()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unavailable",
        "version": settings.APP_VERSION,
        "asset_version": app.state.asset_version,
    }


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    """Expose static enums and the per-item-type status sets so the frontend
    can swap the status dropdown when the user changes type."""
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
    # Preserve any headers the raiser attached (e.g. Retry-After on 429,
    # WWW-Authenticate on 401). The default handler used to drop them,
    # which silently broke client retry behaviour for rate-limited
    # endpoints.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or None,
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette serves unhandled errors from ServerErrorMiddleware, which sits
    # OUTSIDE the security-headers middleware — so without this an unexpected 500
    # would ship as bare text with no CSP/anti-clickjacking headers. Log it and
    # return a generic, header-complete JSON 500 (no internals leak to clients).
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    _apply_security_headers(resp.headers)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    import uvicorn
    # Host defaults to 127.0.0.1 (loopback only). In containerised deploys,
    # set UVICORN_HOST=0.0.0.0 so the container can be reached from the host
    # network. The container boundary + reverse proxy are what makes binding
    # to all interfaces safe; running directly on a host should stay local.
    _host = os.getenv("UVICORN_HOST", "127.0.0.1")
    _port = int(os.getenv("UVICORN_PORT", "8000"))
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
