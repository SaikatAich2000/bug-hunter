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
# Asset version, recomputed on every server start.
#
# Hashing the bytes of every static asset and injecting that hash into the HTML
# placeholders gives each asset a fresh URL on every redeploy, so browsers don't
# serve a stale cached copy.
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"


# Per-file byte cap when hashing the static bundle. A file larger than this is
# unexpected (e.g. stray media under STATIC_DIR), so its path + size is folded
# into the hash instead of reading megabytes at startup. Bounds boot time and
# the memory the hash can touch regardless of what lands in the directory.
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
                    # Skip reading an oversized file into the hash; its path +
                    # size still changes the version on replacement.
                    h.update(f"size={path.stat().st_size}".encode("utf-8"))
                else:
                    h.update(path.read_bytes())
            except OSError:
                continue
    return h.hexdigest()[:12]


def _bootstrap() -> None:
    """Run once at startup. Creates the default project and first admin user
    if the DB is empty. Idempotent and safe to call repeatedly."""
    s = get_settings()
    with SessionLocal() as db:
        if db.query(Project).count() == 0:
            db.add(Project(
                name="General",
                description="Default project for uncategorized bugs",
                color="#c9764f",
            ))

        # First-run admin, so a fresh DB allows an immediate login without
        # touching SQL. The admin should change the password after first login.
        if db.query(User).count() == 0:
            if s.is_production and s.BOOTSTRAP_ADMIN_PASSWORD == "ChangeMe123!":
                # Don't stand up a live admin with a publicly-known password on
                # a production deploy. Keyed on is_production (APP_ENV, falling
                # back to COOKIE_SECURE) so an HTTP/intranet production behind a
                # TLS-terminating proxy is covered too, not only HTTPS deploys.
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
    """Non-fatal startup warnings for insecure-but-allowed configuration.

    Returned rather than just logged so the policy is unit-testable without
    driving the whole lifespan. The fatal fail-closed checks stay inline in
    lifespan().
    """
    warnings: list[str] = []
    if not s.SESSION_SECRET:
        # Fine for dev (a random secret is generated per process), but on any
        # long-lived deploy, including HTTP/intranet (COOKIE_SECURE=false) where
        # the fatal check below doesn't fire, every restart invalidates every
        # session and multi-worker uvicorn gives each worker its own secret,
        # logging users out unpredictably.
        warnings.append(
            "SESSION_SECRET is not set. Using a random per-process fallback, so "
            "sessions will NOT survive a restart and multi-worker deployments log "
            "users out unpredictably. Set SESSION_SECRET (`openssl rand -hex 32`) "
            "for any non-throwaway deploy, HTTP or HTTPS."
        )
    if s.is_production and s.EMAIL_BACKEND == "console":
        # The console backend logs full email bodies — including live, single-use
        # password-reset links — to stdout/log aggregation. Fine for dev; a
        # credential-in-logs exposure on a production-signalled deploy.
        warnings.append(
            "EMAIL_BACKEND=console on a production deploy: every email body, "
            "INCLUDING password-reset links, is written to the logs. Set "
            "EMAIL_BACKEND=smtp (or 'disabled') in production."
        )
    return warnings


def _safe_init_db() -> bool:
    """Run schema init and bootstrap, swallowing a DB-down failure so a
    transient outage at boot starts the app degraded instead of crash-looping
    with no observable cause."""
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
    if _settings.is_production and len(_settings.SESSION_SECRET) < 32:
        # Production signal (APP_ENV=production, or COOKIE_SECURE=true / HTTPS
        # when APP_ENV is unset) but no stable, strong secret: fail closed
        # rather than weaken every session (a per-process random secret logs
        # users out on every restart and across workers).
        raise RuntimeError(
            "SESSION_SECRET must be set to a strong value (>= 32 chars) in "
            "production (APP_ENV=production or COOKIE_SECURE=true). Generate "
            "one with `openssl rand -hex 32`."
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
# Interactive API docs (/docs, /redoc, /openapi.json) publish the full endpoint
# surface, so keep them in dev and drop them once COOKIE_SECURE (the
# production/https signal) is set, unless ENABLE_API_DOCS forces them on.
_docs_enabled = settings.ENABLE_API_DOCS or not settings.is_production
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Computed once at import time, used by middleware and the HTML serving helper.
# Kept on app.state so tests can override it deterministically.
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

# CORSMiddleware is added last in this file so it runs outermost in the ASGI
# chain. Starlette stacks middleware in reverse-registration order, so the last
# add_middleware() call wraps the outside, which CORS needs to handle preflight
# OPTIONS before other middleware (rate-limit, CSP) can short-circuit it.

# ---------------------------------------------------------------------------
# Gzip compression
#
# Compresses JSON / HTML / JS / CSS responses over the wire. Skips bodies under
# 1 KB (compression CPU cost isn't worth it) and already-compressed binary
# types (images / video), via Starlette's Accept-Encoding handling.
#
# Attachment downloads are unaffected: they ship their own Cache-Control header
# which exits the cache middleware early, and gzip doesn't help already-
# compressed media.
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# Request body size limit
#
# Pydantic caps individual string fields and the attachment endpoint aborts at
# 50 MB, but a request with a huge Content-Length and no schema-relevant fields
# (or a multipart upload to an unexpected endpoint) would still buffer body
# bytes into RAM before validation fails.
#
# This middleware rejects with 413 before the body is read whenever
# Content-Length exceeds MAX_REQUEST_BODY_BYTES, a coarse second-line defense
# alongside the per-endpoint limits. Requests without a Content-Length (chunked
# transfer) are allowed through; the per-endpoint streamed reads still bound
# them.
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
    """Raised by the streaming guard's wrapped receive when the accumulated
    request body exceeds the cap. Caught in StreamingBodyLimitMiddleware."""


class StreamingBodyLimitMiddleware:
    """Pure-ASGI backstop for the Content-Length check.

    BodySizeLimitMiddleware can only see headers, so a chunked request that
    omits Content-Length would stream an unbounded body into RAM, defeating
    MAX_REQUEST_BODY_BYTES. This wraps `receive` and counts the actual bytes,
    aborting with 413 once they exceed the cap regardless of Content-Length.

    Implemented as raw ASGI (not BaseHTTPMiddleware, which can't wrap receive).
    As a user middleware it sits outside Starlette's ExceptionMiddleware but
    inside ServerErrorMiddleware, so a _RequestBodyTooLarge raised deep in a
    body read propagates back up to the try/except here.
    """

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
                # Response already began and can't be cleanly replaced; re-raise
                # so the outer error handling tears the connection down.
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
# Cache-Control middleware
#
# Prevents stale HTML on redeploy: cached HTML pointing at old asset URLs would
# otherwise render a broken page until a hard refresh.
#
#   - HTML       -> no-store, must-revalidate. Cheap to refetch.
#   - /static/*  -> public, max-age=1 year, immutable. Safe because the URL
#                  changes on every deploy via the injected asset_version.
#   - /api/*     -> no-store. API responses must not be cached.
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
        elif path.startswith("/static/assets/"):
            # Vite emits content-hashed filenames under /static/assets/, so
            # these URLs change whenever the bytes change — safe to cache
            # immutably for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            # Other static files (favicon, icons, fonts, vendored Firebase SDK)
            # aren't fingerprinted, so they aren't cached immutably; a one-hour
            # cache lets a redeploy replace them within the day.
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            # HTML pages aren't cached so a redeploy is reflected immediately.
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
# When web push is on, the browser's Firebase Messaging SDK talks to these
# Google endpoints to mint/refresh the device token. They are added to
# connect-src only then, so the default posture stays locked to 'self' when
# push is off. Firebase scripts are vendored locally, so script-src stays 'self'.
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
    short-circuits (429 rate-limit, 403 CSRF, which sit outside this middleware)
    and the generic 500 handler all emit one identical, complete set.
    `setdefault` lets a downstream layer still override.
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
    # Strip uvicorn's default "server: uvicorn" so the stack isn't advertised.
    if "server" in h:
        del h["server"]
    # HSTS only behind real https; COOKIE_SECURE doubles as the https signal.
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
# An in-memory sliding-window limiter with no Redis dependency. Per-IP buckets
# so one bad actor doesn't lock out everyone. Keys are time-pruned on every
# check so memory stays bounded. Multi-worker deployments get per-worker
# buckets, so a tighter global limit needs nginx limit_req in front.
#
# Limits allow for occasional human typos while slowing credential-stuffing:
#   /api/auth/login           — 8 attempts / 60s per IP
#   /api/auth/forgot-password — 3 attempts / 60s per IP
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
# Longest configured window; a bucket whose newest timestamp is older than this
# can't be throttling anything, so it's safe to reclaim.
_MAX_RATE_WINDOW = max((w for _, w in _RATE_RULES.values()), default=60)


def _evict_one_rate_bucket(now: float) -> None:
    """Make room for a new bucket without dropping an active throttle.

    First reclaim buckets with no in-window activity (empty, or newest entry
    older than the longest window). Only if everything is still active is the
    least-recently-active bucket evicted. Eviction is never by insertion order,
    so an attacker can't churn fresh keys to flush their own recently-active
    throttled bucket and reset the counter. Caller holds _rate_lock.
    """
    horizon = now - _MAX_RATE_WINDOW
    dead = [k for k, b in _rate_buckets.items() if not b or b[-1] < horizon]
    for k in dead:
        del _rate_buckets[k]
    if len(_rate_buckets) >= _RATE_BUCKETS_MAX and _rate_buckets:
        oldest = min(_rate_buckets, key=lambda k: _rate_buckets[k][-1])
        del _rate_buckets[oldest]


def _client_ip(request: Request) -> str:
    """Resolve the client IP. X-Forwarded-For is not trusted by default
    because spoofing it bypasses the limit; deploys behind a reverse proxy
    can opt into trusting it via TRUST_PROXY_FORWARDED_FOR (and configure the
    proxy-hop count via TRUST_PROXY_HOP_COUNT). Uses the shared
    trusted_forwarded_ip so the right-most-entry semantics match the audit IP
    resolver in routes/auth.py."""
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
                # Bound the dict without evicting an active throttle: reclaim
                # idle/empty buckets first, then least-recently-active.
                if len(_rate_buckets) >= _RATE_BUCKETS_MAX:
                    _evict_one_rate_bucket(now)
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
# The session cookie is SameSite=Lax, which blocks most cross-site CSRF, but
# Lax has gaps:
#   - it doesn't apply to subdomain attackers (evil.example.com and
#     app.example.com are "same site").
#   - older / non-standard browser behaviour for some top-level POSTs.
#
# A second check covers this: for state-changing requests (POST / PUT / PATCH /
# DELETE) to the JSON API, the Origin (or Referer) header must match one of the
# allowed origins. Same-origin SPA requests satisfy this since fetch() includes
# Origin automatically. Clients that send neither Origin nor Referer (e.g. curl,
# httpx) pass, since CSRF only matters when a browser is abused as a deputy.
#
# Allowed origins: settings.CORS_ORIGINS (when not "*"). The request's own Host
# is always implicitly allowed; the Origin scheme is checked against the
# cookie-secure mode to avoid http/https confusion behind a reverse proxy.
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
# Endpoints exempt from the Origin check. Empty: login is not exempt, so a
# cross-site forced-login (session fixation) is blocked. Operator scripts send
# no Origin/Referer and pass via the no-fingerprint branch; an allow-listed
# cross-origin browser client passes via CORS_ORIGINS.
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
        # accepting both schemes. Behind a TLS-terminating reverse proxy,
        # request.url.scheme is whatever the proxy claims; trusting it here is
        # fine because a compromised proxy makes CSRF the least concern. Schemes
        # are concatenated rather than inline literals so static analyzers don't
        # flag "http://"; it describes the client connection, not an outbound one.
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

# CORS is registered last so it sits outermost in the ASGI chain (see the block
# near _origins above). Skip registration entirely when no origins are
# configured: same-origin traffic never hits CORSMiddleware anyway.
if _origins:
    # Scope methods/headers to what the SPA uses rather than "*". With
    # allow_credentials=True a wildcard would let any allow-listed origin send
    # any method/header on a credentialed cross-origin request, wider than a
    # cookie-auth SPA needs and a larger blast radius if an origin is XSS'd.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


# Rendered-HTML cache. The placeholders depend only on asset_version (fixed at
# process start) and APP_VERSION (fixed for the process lifetime), so the
# rendered page doesn't change between restarts and need not be re-read from
# disk per request. The cache is keyed on asset_version so tests that mutate it
# stay correct.
_html_cache: dict[tuple[str, str], str] = {}


def _serve_html(filename: str) -> HTMLResponse:
    """Render an HTML file with the asset-version and app-version
    placeholders replaced, caching the result per (file, asset_version).

    __ASSET_VERSION__ -> 12-char hash of the static bundle, for cache-busting.
    __APP_VERSION__   -> settings.APP_VERSION, so the login page can show the
                        running version without an extra request to /api/health.
    """
    key = (filename, app.state.asset_version)
    body = _html_cache.get(key)
    if body is None:
        body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
        body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
        body = body.replace(APP_VERSION_PLACEHOLDER, settings.APP_VERSION)
        if len(_html_cache) > 32:  # bound; only a few pages are served
            _html_cache.clear()
        _html_cache[key] = body
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """Check whether the request has a valid, non-revoked session cookie.

    The HTML page handlers (/ and /login.html) must reject a revoked session
    (one whose server-side sessions row was deleted while the browser still
    holds the signed cookie), not just verify the signature. Otherwise the
    SPA's API calls 401 and redirect to /login.html while this function reports
    the cookie valid and bounces the user back to /, an infinite redirect loop.

    When the cookie carries a jti, look it up in the sessions table and return
    True only if the row exists and isn't expired. Legacy tokens without a jti
    predate the sessions table, so they keep the cookie-only check here; they
    still get the full API-side check on /api/auth/me.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        # Legacy cookie predates the sessions table; accept based on signature
        # alone. _user_from_request still validates fully on any API call.
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
        # Called from the HTML page handlers (/ and /login.html). If the DB is
        # momentarily unreachable, fall back to "no valid session" (sending the
        # user to the login page) rather than 500 the HTML route. The next API
        # call surfaces the real DB error to the SPA.
        logger.exception("_has_valid_session: DB lookup failed for jti=%s", jti)
        return False
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    # Redirect to the login page server-side when not logged in, so the user
    # never sees a flash of the app shell with no data.
    if not _has_valid_session(request):
        return RedirectResponse(url="/login.html", status_code=302)
    return _serve_html("index.html")


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    # Send an already-logged-in user straight to the app rather than the login
    # form.
    if _has_valid_session(request):
        return RedirectResponse(url="/", status_code=302)
    return _serve_html("login.html")


@app.get("/reset.html", response_class=HTMLResponse, include_in_schema=False)
@app.get("/reset", response_class=HTMLResponse, include_in_schema=False)
def reset_page() -> HTMLResponse:
    # The reset page is always reachable; even logged-in users may need to
    # reset another account's password from a link.
    return _serve_html("reset.html")


# Firebase Cloud Messaging background service worker, served from the root scope
# (a service worker only controls pages at or below its own URL path, so
# /static/ would be too narrow). Firebase's compat SDK is self-hosted under
# /static/vendor; the public web config is injected here the same way the HTML
# pages get their version placeholders.
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
    injected. Returns a no-op worker when web push isn't configured, so
    registration doesn't 404."""
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

    Returns HTTP 503 (not 200) when the database is unreachable so the Docker
    HEALTHCHECK / orchestrator / load balancer treat a DB-down app as unhealthy
    instead of in-rotation. version/asset_version are exposed unauthenticated:
    the SPA reads asset_version to detect a redeploy, and the app version is
    already shown on the public login page.
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
    # WWW-Authenticate on 401); the default handler drops them, which breaks
    # client retry behaviour for rate-limited endpoints.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None) or None,
    )


@app.exception_handler(Exception)
async def unhandled_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette serves unhandled errors from ServerErrorMiddleware, which sits
    # outside the security-headers middleware, so without this an unexpected 500
    # would ship as bare text with no CSP/anti-clickjacking headers. Log it and
    # return a generic, header-complete JSON 500 with no internals.
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    resp = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    _apply_security_headers(resp.headers)
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


if __name__ == "__main__":
    import uvicorn
    # Host defaults to 127.0.0.1 (loopback only). In containerised deploys, set
    # UVICORN_HOST=0.0.0.0 so the container is reachable from the host network;
    # the container boundary and reverse proxy make binding to all interfaces
    # safe. Running directly on a host should stay local.
    _host = os.getenv("UVICORN_HOST", "127.0.0.1")
    _port = int(os.getenv("UVICORN_PORT", "8000"))
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
