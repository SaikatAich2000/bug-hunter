"""FastAPI entry point — Bug Hunter."""
from __future__ import annotations

import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import COOKIE_NAME, hash_password, parse_session_token
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Project, User
from app.routes import audit, auth, bugs, projects, stats, users
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
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
_origins = settings.CORS_ORIGINS or ["*"]
_allow_credentials = True
if _origins == ["*"]:
    _allow_credentials = False
    logger.warning(
        "CORS_ORIGINS='*' is incompatible with credentials. Set CORS_ORIGINS to "
        "your concrete origin(s) (e.g. https://bugs.example.com) to allow cross-"
        "origin browser sessions. Same-origin SPA usage is unaffected."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _serve_html(filename: str) -> HTMLResponse:
    """Read an HTML file and replace the asset-version placeholder with
    the current build's hash. Querying the file system on every request
    is fine — these files are tiny and we don't care about a few µs."""
    body = (settings.STATIC_DIR / filename).read_text(encoding="utf-8")
    body = body.replace(ASSET_VERSION_PLACEHOLDER, app.state.asset_version)
    return HTMLResponse(body)


def _has_valid_session(request: Request) -> bool:
    """Quick check (no DB hit) of whether the request has a non-expired
    session cookie. Used only to gate which HTML page to serve — the API
    routes still verify the user against the DB, which catches deleted /
    deactivated accounts."""
    token = request.cookies.get(COOKIE_NAME, "")
    return parse_session_token(token) is not None


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
def meta() -> dict[str, list[str]]:
    return {
        "statuses": ALLOWED_STATUSES,
        "priorities": ALLOWED_PRIORITIES,
        "environments": ALLOWED_ENVIRONMENTS,
    }


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(bugs.router)
app.include_router(stats.router)
app.include_router(audit.router)


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
