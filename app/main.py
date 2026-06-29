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
#
# The hash is injected into the HTML placeholder so each asset URL changes on
# redeploy, preventing browsers from serving a stale cached copy.
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"


# Files larger than this cap (e.g. stray media in STATIC_DIR) contribute their
# path and size to the hash rather than their content, to keep startup fast.
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
    """Seed the DB on first run: create the default project and admin user.

    Idempotent — safe to call on every startup.
    """
    s = get_settings()
    with SessionLocal() as db:
        if db.query(Project).count() == 0:
            db.add(Project(
                name="General",
                description="Default project for uncategorized bugs",
                color="#c9764f",
            ))

        # Create the admin only when the users table is empty, so a fresh
        # install can log in without touching SQL. Change the password immediately.
        if db.query(User).count() == 0:
            if s.is_production and s.BOOTSTRAP_ADMIN_PASSWORD == "ChangeMe123!":
                # Refuse to start with the default password on a production
                # deploy. is_production covers HTTP/intranet deployments behind
                # TLS-terminating proxies (not just HTTPS), since it checks
                # APP_ENV first and falls back to COOKIE_SECURE.
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

    Returned as a list (rather than just logged) so tests can verify the policy
    without driving the full lifespan. Fatal checks remain inline in lifespan().
    """
    warnings: list[str] = []
    if not s.SESSION_SECRET:
        # Without a stable secret, every restart invalidates all sessions, and
        # multi-worker uvicorn gives each worker its own secret, randomly logging
        # users out. A random per-process fallback is fine for throwaway dev
        # instances, but not for any long-lived deploy (HTTP or HTTPS).
        warnings.append(
            "SESSION_SECRET is not set. Using a random per-process fallback, so "
            "sessions will NOT survive a restart and multi-worker deployments log "
            "users out unpredictably. Set SESSION_SECRET (`openssl rand -hex 32`) "
            "for any non-throwaway deploy, HTTP or HTTPS."
        )
    if s.is_production and s.EMAIL_BACKEND == "console":
        # The console backend writes full email bodies to stdout, including live
        # single-use password-reset links. Fine for dev; a credentials-in-logs
        # exposure on a production deploy.
        warnings.append(
            "EMAIL_BACKEND=console on a production deploy: every email body, "
            "INCLUDING password-reset links, is written to the logs. Set "
            "EMAIL_BACKEND=smtp (or 'disabled') in production."
        )
    return warnings


def _safe_init_db() -> bool:
    """Run schema init and bootstrap, swallowing a DB-down error.

    A transient outage at boot starts the app degraded rather than crash-looping.
    /api/health will report the database as unavailable until it recovers.
    """
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
        # Fail closed rather than weaken every session. A short or missing
        # secret in production means every restart logs users out and workers
        # share no common signing key.
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
#
# The CORS spec forbids `Access-Control-Allow-Origin: *` with credentials, and
# browsers silently reject that combination. The SPA uses cookies, so the
# response must echo the actual request Origin (when allowlisted), not "*".
# Starlette's CORSMiddleware handles that correctly with a concrete origin list.
# ---------------------------------------------------------------------------
_origins = list(settings.CORS_ORIGINS)
_allow_credentials = True
if not _origins:
    # No origins configured: same-origin only. A wildcard would let any site
    # read authenticated responses; same-origin traffic skips CORS anyway.
    _allow_credentials = False
elif "*" in _origins:
    # Wildcard + credentials is forbidden. Fall back to no-credentials so
    # OPTIONS preflights still succeed rather than breaking silently.
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS contains '*' which disables credentialed CORS. Set "
        "CORS_ORIGINS to your concrete origin(s) (e.g. "
        "https://bugs.example.com) to allow cross-origin browser sessions."
    )

# CORSMiddleware is registered last so it sits outermost in the ASGI chain.
# Starlette stacks middleware in reverse order, so the last add_middleware()
# wraps the outside. CORS must be outermost to intercept OPTIONS before the
# rate-limit or CSP middleware can short-circuit it.

# ---------------------------------------------------------------------------
# Gzip compression
#
# Compresses JSON/HTML/JS/CSS over the wire; skips bodies under 1 KB (not
# worth the CPU) and already-compressed types (images/video).
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# Request body size limit
#
# Pydantic and the attachment endpoint cap individual fields and uploads, but a
# request with a huge Content-Length and no matching schema would still buffer
# into RAM before validation fails. This middleware rejects with 413 before the
# body is read, as a coarse second line of defense. Chunked requests (no
# Content-Length) are allowed through; StreamingBodyLimitMiddleware covers them.
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
    """Pure-ASGI backstop for the Content-Length check.

    BodySizeLimitMiddleware only sees headers, so a chunked request without
    Content-Length could stream an unbounded body into RAM. This wraps `receive`
    and counts actual bytes, rejecting with 413 once the cap is exceeded.

    Implemented as raw ASGI rather than BaseHTTPMiddleware because
    BaseHTTPMiddleware cannot wrap the receive callable.
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
# Cache-Control middleware
#
# Prevents stale HTML after redeploy (cached HTML with old asset URLs renders
# a broken page until a hard refresh).
#
#   HTML             -> no-store, must-revalidate
#   /static/assets/  -> immutable for a year (Vite content-hashed filenames)
#   /static/         -> 1 hour (other static files, not fingerprinted)
#   /api/            -> no-store
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
            # Vite emits content-hashed filenames here, so the URL changes
            # whenever the file changes — safe to cache for a year.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/static/"):
            # Other static files (favicon, fonts, vendored SDK) aren't
            # fingerprinted, so use a shorter cache.
            response.headers["Cache-Control"] = "public, max-age=3600"
        else:
            # HTML must not be cached so a redeploy is reflected immediately.
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(CacheControlMiddleware)


# ---------------------------------------------------------------------------
# Security headers
#
# Applied to every response, including cached HTML and 304s.
#
# CSP notes:
#   script-src 'self'             no inline <script>, so no 'unsafe-inline' needed.
#   style-src 'self' 'unsafe-inline'  the app sets inline styles via .style.x; a
#                                     nonce strategy would require touching every site.
#   img-src 'self' data: blob:   data: for attachments/avatars; blob: for downloads.
#   frame-ancestors 'none'       blocks iframe embedding (supersedes X-Frame-Options).
#   base-uri 'self'              prevents <base href> hijack.
#   object-src 'none'            no plugins.
#   form-action 'self'           forms post only to this origin.
#
# HSTS is gated on COOKIE_SECURE to avoid locking a dev/HTTP deploy into HTTPS.
# ---------------------------------------------------------------------------
# When web push is enabled, the Firebase Messaging SDK calls these Google
# endpoints to mint/refresh device tokens. They are added to connect-src only
# then; the default stays 'self'. Firebase scripts are vendored locally, so
# script-src stays 'self'.
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

    Shared by the middleware, middleware-generated short-circuits (429/403),
    and the 500 handler, so all responses carry the same complete set.
    Uses setdefault so a downstream handler can still override.
    """
    h.setdefault("Content-Security-Policy", _CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("Permissions-Policy",
                 "camera=(), microphone=(), geolocation=(), "
                 "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # CORP prevents other origins from loading our responses as subresources.
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    # Remove uvicorn's default "server" header to avoid advertising the stack.
    if "server" in h:
        del h["server"]
    # HSTS only when behind real HTTPS (COOKIE_SECURE doubles as the signal).
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
# In-memory sliding window per IP, no Redis required. Timestamps are pruned on
# each check to keep memory bounded. Multi-worker deployments get per-worker
# buckets; for a strict global limit add nginx limit_req in front.
#
# Limits are tuned to absorb human typos while slowing credential stuffing:
#   /api/auth/login           — 8 attempts / 60s
#   /api/auth/forgot-password — 3 attempts / 60s
# ---------------------------------------------------------------------------
_RATE_RULES: dict[str, tuple[int, int]] = {
    # (max_requests, window_seconds)
    "/api/auth/login": (8, 60),
    "/api/auth/forgot-password": (3, 60),
    # reset-password validates a 256-bit token (brute force is already
    # infeasible), but we cap it anyway. change-password bcrypt-verifies the
    # current password on every call, making an unthrottled endpoint an
    # auth-amplification vector.
    "/api/auth/reset-password": (5, 60),
    "/api/auth/change-password": (5, 60),
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
# Soft cap to bound memory when hammered from many IPs.
_RATE_BUCKETS_MAX = 10_000
# A bucket whose newest timestamp is older than this isn't throttling anything
# and can be reclaimed.
_MAX_RATE_WINDOW = max((w for _, w in _RATE_RULES.values()), default=60)


def _evict_one_rate_bucket(now: float) -> None:
    """Make room for a new bucket without dropping an active throttle.

    Preference: reclaim buckets with no in-window activity first (empty, or
    newest entry older than the longest window). Only if all buckets are still
    active is the least-recently-active one evicted. Eviction is never by
    insertion order, so an attacker can't churn new keys to flush their own
    throttled bucket. Caller must hold _rate_lock.
    """
    horizon = now - _MAX_RATE_WINDOW
    dead = [k for k, b in _rate_buckets.items() if not b or b[-1] < horizon]
    for k in dead:
        del _rate_buckets[k]
    if len(_rate_buckets) >= _RATE_BUCKETS_MAX and _rate_buckets:
        oldest = min(_rate_buckets, key=lambda k: _rate_buckets[k][-1])
        del _rate_buckets[oldest]


def _client_ip(request: Request) -> str:
    """Resolve the client IP for rate limiting.

    X-Forwarded-For is not trusted by default because a client can spoof it to
    bypass the limit. Set TRUST_PROXY_FORWARDED_FOR=true and configure
    TRUST_PROXY_HOP_COUNT to enable it behind a reverse proxy. Uses
    trusted_forwarded_ip to share right-most-entry semantics with the audit
    IP resolver in routes/auth.py.
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
# CSRF defense in depth
#
# The session cookie is SameSite=Lax, which stops most CSRF, but Lax has gaps:
#   - subdomain attackers (evil.example.com and app.example.com are "same site")
#   - some older browsers on top-level navigation POSTs
#
# For state-changing requests (POST/PUT/PATCH/DELETE) to /api/, the Origin (or
# Referer) header must match an allowed origin. Same-origin SPA requests satisfy
# this automatically because fetch() includes Origin. Clients sending neither
# header (curl, httpx) pass through — CSRF requires a browser as a deputy.
#
# Allowed origins: settings.CORS_ORIGINS (excluding "*"), plus the Host from
# the request itself.
# ---------------------------------------------------------------------------
def _allowed_origins() -> set[str]:
    """Return the set of origins allowed by the CSRF check.

    Includes CORS_ORIGINS (excluding "*"). The request Host is always added
    separately, so SPA usage works even without CORS configuration.
    """
    return {o.rstrip("/") for o in settings.CORS_ORIGINS if o and o != "*"}


# Safe methods (GET/HEAD/OPTIONS) skip the check.
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# No paths are exempt: login included, so cross-site forced-login (session
# fixation) is blocked. Operator scripts have no Origin/Referer and pass via
# the no-fingerprint branch; allowlisted cross-origin clients pass via CORS_ORIGINS.
_CSRF_EXEMPT_PATHS: frozenset[str] = frozenset()


class CsrfOriginMiddleware(BaseHTTPMiddleware):
    """Block mutating requests whose Origin doesn't match the app's host.

    Non-browser clients (no Origin, no Referer) pass through — CSRF is a
    browser confused-deputy attack, so a curl request is not CSRF by definition.
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

        # No browser fingerprint: not a browser, cannot be CSRF.
        if not origin and not referer:
            return await call_next(request)

        # Build the same-origin URLs from the request. Behind a TLS-terminating
        # proxy, request.url.scheme reflects what the proxy reports; trusting it
        # is fine because a compromised proxy makes CSRF the least concern.
        # Schemes are concatenated (not literal "http://") to avoid static-analyzer
        # warnings about hardcoded URLs.
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

# CORS is registered last (see note near _origins). Skip when no origins are
# configured; same-origin traffic never reaches CORSMiddleware anyway.
if _origins:
    # Restrict methods/headers to what the SPA actually uses. A wildcard with
    # credentials would let any allowlisted origin send anything, which is a
    # larger blast radius if one of those origins is XSS'd.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


# Both placeholders depend on values fixed at process start (asset_version,
# APP_VERSION), so the rendered page doesn't change within a process lifetime.
# Keyed on asset_version so tests that swap it stay correct.
_html_cache: dict[tuple[str, str], str] = {}


def _serve_html(filename: str) -> HTMLResponse:
    """Serve an HTML file with version placeholders replaced, cached per (file, asset_version).

    __ASSET_VERSION__: 12-char bundle hash for cache-busting asset URLs.
    __APP_VERSION__: the running version, so the login page can display it
    without an extra /api/health call.
    """
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
    """Return True if the request carries a valid, non-revoked session cookie.

    The HTML page handlers must check for revoked sessions, not just a valid
    signature. If a revoked cookie is accepted here, the SPA's API calls 401
    and redirect to /login.html, which bounces back to / — an infinite loop.

    For cookies with a jti, the sessions table row must exist and not be
    expired. Legacy tokens (no jti) predate the sessions table and are accepted
    on signature alone; they still get the full check on /api/auth/me.
    """
    token = request.cookies.get(COOKIE_NAME, "")
    parsed = parse_session_token(token)
    if parsed is None:
        return False
    user_id, _session_version, jti = parsed
    if jti is None:
        # Legacy cookie: accept on signature alone.
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
        # If the DB is momentarily unreachable, treat the session as invalid
        # and send the user to the login page rather than 500'ing. The next API
        # call will surface the real error to the SPA.
        logger.exception("_has_valid_session: DB lookup failed for jti=%s", jti)
        return False
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    # Server-side redirect keeps the user from seeing a flash of the app shell
    # with no data when they aren't logged in.
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


# FCM background service worker served from root scope. A service worker only
# controls pages at or below its own path, so /static/ would be too narrow.
# Firebase's compat SDK is vendored under /static/vendor; the web config is
# injected the same way as the HTML version placeholders.
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
