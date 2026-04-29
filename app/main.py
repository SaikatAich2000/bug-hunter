"""FastAPI entry point — Bug Hunter."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import Project, User
from app.routes import audit, bugs, projects, stats, users
from app.schemas import (
    ALLOWED_ENVIRONMENTS,
    ALLOWED_PRIORITIES,
    ALLOWED_STATUSES,
)

logger = logging.getLogger("bug_hunter")
logging.basicConfig(level=get_settings().LOG_LEVEL)


def _seed_defaults() -> None:
    with SessionLocal() as db:
        if db.query(Project).count() == 0:
            db.add(Project(
                name="General",
                description="Default project for uncategorized bugs",
                color="#c9764f",
            ))
        if db.query(User).count() == 0:
            db.add(User(
                name="System",
                email="system@example.com",
                role="System",
                is_active=True,
            ))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_defaults()
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
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def optional_api_key(request: Request, call_next):
    if settings.API_KEY and request.method in ("POST", "PUT", "DELETE"):
        provided = request.headers.get("X-API-Key", "")
        if provided != settings.API_KEY:
            return JSONResponse(status_code=401, content={"detail": "Invalid or missing X-API-Key"})
    return await call_next(request)


app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    return HTMLResponse((settings.STATIC_DIR / "index.html").read_text(encoding="utf-8"))


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
