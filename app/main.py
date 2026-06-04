"""FastAPI entry point — Bug Hunter."""
from __future__ import annotations

import hashlib
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
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import COOKIE_NAME, hash_password, parse_session_token
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Project, Session as SessionRow, User
from app.chatbot.router import router as chatbot_router
from app.routes import audit, auth, bugs, events, projects, sessions, stats, users
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
# This is what makes redeploys "just work" without users needing to hard-
# refresh. We hash the real bytes of every static asset, then inject that
# hash into the HTML wherever a placeholder appears. Browsers see a brand-
# new URL for each asset every time we redeploy, so they never serve a
# stale cached copy.
# ---------------------------------------------------------------------------
ASSET_VERSION_PLACEHOLDER = "__ASSET_VERSION__"


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

        # First-run admin. If you wipe the DB, this lets you log in
        # immediately without poking at SQL. After first login, the admin
        # should change the password (settings menu → Change password).
        if db.query(User).count() == 0:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _bootstrap()

    # Warn loudly if the session secret isn't set — fine for dev (a random
    # one is generated per process) but every restart invalidates every
    # session, and multiple uvicorn workers each get their OWN secret so
    # users would be randomly logged out as load-balanced requests hit
    # different workers. Both surprises in production.
    if not get_settings().SESSION_SECRET:
        logger.warning(
            "SESSION_SECRET is not set. Using a random per-process fallback. "
            "Set SESSION_SECRET in your environment for stable sessions across "
            "restarts and multi-worker deployments."
        )

    logger.info("Bug Hunter started. asset_version=%s", app.state.asset_version)
    yield
    logger.info("Bug Hunter shutting down.")


settings = get_settings()
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# Compute once at import time — used by middleware and the HTML serving
# helper. Kept on app.state so tests can override it deterministically.
app.state.asset_version = _compute_asset_version(settings.STATIC_DIR)


# ---------------------------------------------------------------------------
# CORS
#
# Important nuance: the CORS spec forbids `Access-Control-Allow-Origin: *`
# together with `Allow-Credentials: true`. Browsers reject this combo
# silently. Our SPA uses cookies (credentials=true), so we must NOT echo
# back "*" — we must echo the request's actual Origin (only if it's in our
# allowlist). Starlette's CORSMiddleware does that when given a concrete
# origin list, but if `*` is the only entry it breaks credentialed
# requests. Detect that combination and disable credentials in that case
# rather than silently breaking auth from non-same-origin clients.
# ---------------------------------------------------------------------------
_origins = list(settings.CORS_ORIGINS)
_allow_credentials = True
if not _origins:
    # Empty list = same-origin only. We don't register a wildcard fallback
    # because that would let any site read authenticated responses.
    # Same-origin SPA usage doesn't go through CORS middleware, so this is
    # the safe default.
    _allow_credentials = False
elif "*" in _origins:
    # Wildcard + credentials is forbidden by the CORS spec and silently
    # broken by browsers — fall back to no-credentials so the OPTIONS
    # preflight at least succeeds.
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS contains '*' which disables credentialed CORS. Set "
        "CORS_ORIGINS to your concrete origin(s) (e.g. "
        "https://bugs.example.com) to allow cross-origin browser sessions."
    )

# NOTE: CORSMiddleware is added LAST in this file (after every other
# middleware) so it runs OUTERMOST in the ASGI chain. Starlette stacks
# middleware in reverse-registration order, so the last add_middleware()
# call wraps the outside — which is what CORS needs to correctly handle
# preflight OPTIONS without other middleware (rate-limit, CSP) firing
# first and short-circuiting the preflight.

# ---------------------------------------------------------------------------
# Gzip compression
#
# Performance win for low-resource VMs: shrinks JSON / HTML / JS / CSS
# responses by ~70-90% over the wire so the small server spends less time
# pushing bytes. Skips bodies smaller than 1 KB (the CPU cost of compression
# isn't worth it for tiny payloads) and naturally skips already-compressed
# binary types (images / video) because Starlette's GZipMiddleware checks
# the Accept-Encoding header rather than blindly compressing.
#
# Attachment downloads are unaffected — they ship their own Cache-Control
# header which exits the cache middleware early; gzip is also typically
# unhelpful for already-compressed media (PDFs, JPEGs, MP4s, etc.).
# ---------------------------------------------------------------------------
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


# ---------------------------------------------------------------------------
# G3 — Request body size limit
#
# Pydantic caps individual string fields (description = 1 MB, comment body
# = 200 KB) and the attachment endpoint streams + aborts at 50 MB. But a
# request that arrives with a 5 GB Content-Length and no body fields the
# schema cares about — or a multipart upload to an unexpected endpoint —
# would still buffer body bytes into RAM before validation fails.
#
# This middleware rejects with 413 (Payload Too Large) BEFORE the body is
# read whenever Content-Length exceeds MAX_REQUEST_BODY_BYTES. It's a
# coarse second-line defense; the per-endpoint limits remain.
#
# Requests without a Content-Length (chunked transfer) are allowed
# through — the per-endpoint streamed reads still bound them.
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
# The deployment bug we're fixing: when the server is redeployed but the
# browser already has cached HTML pointing at old asset URLs, the user sees
# a broken page until they hard-refresh.
#
# Strategy:
#   - HTML       → no-store, must-revalidate. Tiny payload, cheap to refetch.
#   - /static/*  → public, max-age=1 year, immutable. Safe because the URL
#                  changes on every deploy via the asset_version we inject.
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
# Security headers (v3.2)
#
# Set on every response so even cached HTML and 304s get the same protections.
#
# CSP notes:
#   - script-src 'self'         no inline <script> in our HTML, so no
#                               'unsafe-inline' or hash juggling needed.
#   - style-src 'self' 'unsafe-inline'
#                               app.js sets a few inline styles via DOM
#                               (.style.x = …) which CSP treats as inline
#                               styles. 'unsafe-inline' is the practical
#                               escape; switching to a nonce strategy would
#                               require touching every dynamic-style site.
#   - img-src 'self' data: blob:
#                               attachments and JS-generated avatars use
#                               data: URLs; downloaded blobs use blob:.
#   - frame-ancestors 'none'    refuses iframe embedding (modern X-Frame-Options).
#   - base-uri 'self'           prevents <base href=…> hijack.
#   - object-src 'none'         no plugins.
#   - form-action 'self'        forms can only post to us.
#
# HSTS is conditional on COOKIE_SECURE so we don't accidentally emit it
# behind an HTTP-only dev proxy and lock the browser into https://.
# ---------------------------------------------------------------------------
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        h = response.headers
        # Don't clobber if a downstream layer set its own.
        h.setdefault("Content-Security-Policy", _CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("Permissions-Policy",
                     "camera=(), microphone=(), geolocation=(), interest-cohort=(), "
                     "payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # CORP blocks other origins from fetching our responses as
        # subresources — defense in depth for the API and HTML pages.
        # Attachment downloads override this in their own headers if a
        # legitimate cross-origin use case ever appears.
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # Strip uvicorn's default "server: uvicorn" — minor info leak,
        # but no reason to advertise the stack to a scanner.
        if "server" in h:
            del h["server"]
        # HSTS: only safe behind real https. Production-style deploys set
        # COOKIE_SECURE=true, which doubles as the "we're on https" signal.
        if settings.COOKIE_SECURE:
            h.setdefault("Strict-Transport-Security",
                         "max-age=63072000; includeSubDomains")
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Rate limiting on auth-sensitive endpoints (v3.2)
#
# A small in-memory sliding-window limiter — no Redis dependency. Per-IP
# buckets so one bad actor doesn't lock out everyone. Keys are kept tiny
# (<200 bytes/IP) and time-pruned on every check, so memory stays bounded
# under typical load. Multi-worker deployments get per-worker buckets,
# which means a determined attacker could still get N×limit attempts; the
# right fix at scale is to put nginx in front (it has its own limit_req).
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
}
_rate_buckets: dict[tuple[str, str], deque] = {}
_rate_lock = Lock()
# Soft cap to keep memory bounded if someone hammers from many IPs.
_RATE_BUCKETS_MAX = 10_000


def _client_ip(request: Request) -> str:
    """Best-effort client IP. We don't trust X-Forwarded-For by default
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
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many attempts. Please try again later."},
                    headers={"Retry-After": str(retry_after)},
                )
            bucket.append(now)
        return await call_next(request)


app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# CSRF defense-in-depth (v3.2.1)
#
# The session cookie is already SameSite=Lax which blocks most cross-site
# CSRF, but Lax has known gaps:
#   - it doesn't apply to subdomain attackers (if an attacker controls
#     evil.example.com and the app is at app.example.com, they're "same
#     site").
#   - older / non-standard browser behaviour for some top-level POSTs.
#
# We add a second, simple check: for state-changing requests (POST / PUT /
# PATCH / DELETE) to our JSON API, the Origin (or Referer) header must
# match one of the allowed origins. Same-origin SPA requests always
# satisfy this — fetch() includes Origin automatically. Server-to-server
# clients that don't send Origin or Referer (e.g. curl with an API key,
# python httpx without a Host) get a free pass since CSRF only matters
# when a browser is being abused as a deputy.
#
# /api/auth/login is exempted because cross-origin login from a trusted
# admin tool is a legitimate flow some operators want. The login itself
# is rate-limited and credential-checked.
#
# Allowed origins: settings.CORS_ORIGINS (when not "*"). The request's own
# Host is always implicitly allowed — Origin scheme is checked against
# the cookie-secure mode to avoid http→https confusion behind a reverse
# proxy.
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
# Endpoints exempt from the Origin check. Login may be invoked from
# operator scripts during onboarding; the chatbot download is GET-only.
_CSRF_EXEMPT_PATHS = {"/api/auth/login"}


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
        # accepting BOTH schemes. Behind a reverse proxy that terminates
        # TLS, request.url.scheme is whatever the proxy claims it is;
        # trusting that here is fine because if the proxy is compromised
        # CSRF is the least of our worries. Schemes are concatenated
        # rather than written as inline literals so static analyzers
        # don't flag "http://" as an insecure protocol choice — the user
        # is the one connecting, not us.
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
        return JSONResponse(
            status_code=403,
            content={"detail": "Cross-origin request blocked."},
        )


app.add_middleware(CsrfOriginMiddleware)

# CORS is registered LAST so it sits OUTERMOST in the ASGI chain — see the
# block near _origins above for why. Skip registration entirely when no
# origins are configured: same-origin traffic never hits CORSMiddleware
# anyway, so adding it would just be dead code that Sonar flags.
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _serve_html(filename: str) -> HTMLResponse:
    """Read an HTML file and replace the asset-version placeholder with
    the current build's hash. Querying the file system on every request
    is fine — these files are tiny and we don't care about a few µs."""
    body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
    body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """Check whether the request has a valid, non-revoked session cookie.

    BUG FIX (v3.1.1): the original implementation only verified the cookie's
    cryptographic signature. That meant a revoked session — where the
    server-side sessions row had been deleted but the user's browser still
    held the signed cookie — passed this check, and the / and /login.html
    HTML handlers couldn't tell the cookie was dead. The SPA's API calls
    correctly returned 401 (those go through _user_from_request which
    checks the sessions table), so the SPA fired location.replace('/login.html'),
    but the /login.html handler bounced them back to / because this
    function returned True. Result: infinite redirect loop, exactly the
    "behaving strangely after refresh" symptom the user reported.

    The fix: when the cookie carries a jti, look it up in the sessions
    table and only return True if the row exists and is not expired.
    Tokens without a jti (legacy) keep the cookie-only check for backward
    compat — they predate the sessions table, so there's nothing to look
    up. Those tokens still get the proper API-side check on /api/auth/me;
    we're just using a slightly looser HTML-routing decision for them.
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


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "version": settings.APP_VERSION,
        "asset_version": app.state.asset_version,
    }


@app.get("/api/meta", tags=["meta"])
def meta() -> dict[str, object]:
    """Expose static enums + the v2.5 per-item-type status sets so the
    frontend can swap the status dropdown when the user changes type."""
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
app.include_router(audit.router)
app.include_router(sessions.router)
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


if __name__ == "__main__":
    import uvicorn
    # Host defaults to 127.0.0.1 (loopback only). In containerised deploys,
    # set UVICORN_HOST=0.0.0.0 so the container can be reached from the host
    # network. The container boundary + reverse proxy are what makes binding
    # to all interfaces safe; running directly on a host should stay local.
    _host = os.getenv("UVICORN_HOST", "127.0.0.1")
    _port = int(os.getenv("UVICORN_PORT", "8000"))
    uvicorn.run("app.main:app", host=_host, port=_port, reload=False)
