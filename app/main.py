"""FastAPI entry point — Bug Hunter."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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
    logger.info("Bug Hunter started.")
    yield
    logger.info("Bug Hunter shutting down.")


settings = get_settings()
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS or ["*"],
    allow_credentials=True,   # cookies
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


def _serve_html(filename: str) -> HTMLResponse:
    return HTMLResponse((settings.STATIC_DIR / filename).read_text(encoding="utf-8"))


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
    return {"status": "ok", "version": settings.APP_VERSION}


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
