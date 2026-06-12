"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

# Load a local .env file (if present) BEFORE any setting is read, so all the
# values below can live in one git-ignored file for local dev / a single-VM
# deploy. `override=False` (the default) means real environment variables —
# e.g. those injected by docker-compose or the hosting platform — always win
# over the file, so production config is never shadowed by a stray .env.
# No-op if python-dotenv isn't installed or there's no .env file.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv is optional
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


class Settings:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STATIC_DIR: Path = BASE_DIR / "app" / "static"

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'bug_hunter.db'}",
    )

    # Default: empty (= same-origin only). Cross-origin clients must be
    # explicitly allow-listed via CORS_ORIGINS to satisfy Sonar's S5122
    # ("Permissive CORS policy") and to avoid the wildcard+credentials
    # combo that browsers silently reject. Same-origin SPA usage is
    # unaffected because the CORS middleware short-circuits for those.
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    APP_NAME: str = os.getenv("APP_NAME", "Bug Hunter")
    APP_VERSION: str = os.getenv("APP_VERSION", "2.10")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # G3: hard ceiling on request body size, in bytes. The 50 MB attachment
    # cap + multipart overhead + headers fit comfortably under 60 MB; raise
    # via env if you need bigger file uploads. Requests that exceed this are
    # rejected with 413 before any body is read into memory, so a hostile
    # client can't exhaust RAM by claiming a huge Content-Length.
    MAX_REQUEST_BODY_BYTES: int = int(
        os.getenv("MAX_REQUEST_BODY_BYTES", str(60 * 1024 * 1024))
        or str(60 * 1024 * 1024)
    )

    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8765")

    API_KEY: str = os.getenv("API_KEY", "")

    # --- Authentication ---
    # Used to sign session cookies. MUST be set to a long random string in
    # production (`openssl rand -hex 32`). If left blank, a process-local
    # random secret is generated at startup, which means every restart
    # invalidates every session — fine for dev, bad for prod.
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    # Session lifetime in seconds. Default = 1 day.
    SESSION_TTL_SECONDS: int = int(os.getenv("SESSION_TTL_SECONDS", "86400") or "86400")
    # Set to true behind HTTPS so cookie is only sent on TLS connections.
    COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", False)
    # When the app sits behind a trusted reverse proxy (nginx, traefik,
    # ALB, etc.) that sets X-Forwarded-For, set this to true so rate
    # limiting buckets per real client IP instead of per proxy IP.
    # Leave false in dev / direct-uvicorn deploys — spoofed XFF would
    # otherwise let attackers bypass the limiter.
    TRUST_PROXY_FORWARDED_FOR: bool = _env_bool("TRUST_PROXY_FORWARDED_FOR", False)
    # First-time bootstrap admin. Used only if zero users exist in the DB.
    BOOTSTRAP_ADMIN_EMAIL: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@bughunter.local")
    BOOTSTRAP_ADMIN_PASSWORD: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!")
    BOOTSTRAP_ADMIN_NAME: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Admin")

    # ------------------------------------------------------------------
    # Sleuth cloud LLM (optional Layer 4 — natural-language fallback).
    #
    # Everything here is OFF by default. With SLEUTH_CLOUD_ENABLED unset
    # the chatbot behaves exactly as before: pure rules + classifier +
    # the optional local llama.cpp layer, no outbound HTTP. Set the flag
    # and paste a key to switch on the Gemini-primary / OpenRouter-fallback
    # path for the ~5% of free-form questions the rules can't parse.
    #
    # The cloud layer NEVER performs writes and NEVER invents counts: data
    # questions are routed back through the deterministic SQL handlers; the
    # model only paraphrases or summarises retrieved context (see rag.py).
    # ------------------------------------------------------------------
    SLEUTH_CLOUD_ENABLED: bool = _env_bool("SLEUTH_CLOUD_ENABLED", False)
    # Primary provider — Google AI Studio (free tier). Paste the key you
    # create in AI Studio here via the env var; never hard-code it.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    # gemini-2.5-flash is the current free-tier chat model. (gemini-2.0-flash
    # returns HTTP 429 "limit: 0" on the free tier, and the 1.5 models are
    # retired — both 404/429 as of mid-2026.)
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_EMBED_MODEL: str = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")
    # Fallback provider — OpenRouter free models. Used only if Gemini errors
    # or is rate-limited. OpenAI-compatible chat completions API.
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct:free")
    # Per-call budget. 20 s is generous; on timeout we fall back then fail
    # closed to the normal "I didn't understand" reply.
    SLEUTH_CLOUD_TIMEOUT_S: float = float(os.getenv("SLEUTH_CLOUD_TIMEOUT_S", "20") or "20")
    SLEUTH_CLOUD_MAX_TOKENS: int = int(os.getenv("SLEUTH_CLOUD_MAX_TOKENS", "600") or "600")

    # RAG (retrieval over bugs / comments / docs). Independent flag so you
    # can run the cloud LLM with or without retrieval. Needs `chromadb`
    # installed; if the import fails the layer disables itself cleanly.
    SLEUTH_RAG_ENABLED: bool = _env_bool("SLEUTH_RAG_ENABLED", False)
    SLEUTH_RAG_DIR: str = os.getenv(
        "SLEUTH_RAG_DIR", str(BASE_DIR / ".sleuth_rag")
    )
    SLEUTH_RAG_TOP_K: int = int(os.getenv("SLEUTH_RAG_TOP_K", "5") or "5")
    # Folder of plain-text / markdown docs (FAQs, runbooks) to index
    # alongside the live bug/comment data. Optional.
    SLEUTH_DOCS_DIR: str = os.getenv("SLEUTH_DOCS_DIR", str(BASE_DIR / "docs"))

    # Persist Sleuth conversations to the additive chat_* tables. Additive
    # only — existing tables are never touched. Safe to leave on.
    SLEUTH_CHAT_MEMORY_ENABLED: bool = _env_bool("SLEUTH_CHAT_MEMORY_ENABLED", True)

    EMAIL_BACKEND: str = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "bughunter@localhost")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587") or "587")
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT: int = int(os.getenv("SMTP_TIMEOUT", "10") or "10")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
