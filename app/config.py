"""Configuration loaded from environment variables."""
from __future__ import annotations

import logging
import math
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("bug_hunter.config")

# Load a local .env file (if present) before any setting is read, so the
# values below can live in one git-ignored file for local dev / a single-VM
# deploy. `override=False` (the default) means real environment variables
# (e.g. those injected by docker-compose or the hosting platform) win over the
# file, so production config is never shadowed by a stray .env. No-op if
# python-dotenv isn't installed or there's no .env file.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is an optional dependency
    load_dotenv = None  # type: ignore[assignment]


def _load_dotenv() -> None:
    """Load a local .env (if python-dotenv is installed and a file exists).

    A missing .env is fine (load_dotenv just no-ops), but a malformed or
    unreadable file should be surfaced — a security-relevant override silently
    failing to load is worse than a noisy log line. We log rather than crash so
    a broken .env doesn't take the whole app down.
    """
    if load_dotenv is None:  # pragma: no cover - dotenv is an optional dependency
        return
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except OSError as exc:
        logger.warning("Could not load .env file: %s", exc)


_load_dotenv()


# Recognized boolean spellings. Anything outside these sets must not silently
# flip a hardening switch — see _env_bool.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var against an explicit truthy/falsy vocabulary.

    An unrecognized value (e.g. SMTP_USE_TLS=enabled, COOKIE_SECURE=secure) is
    logged and falls back to the declared default rather than False, so a typo
    can't silently disable a security control whose default is True.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    logger.warning(
        "Env var %s=%r is not a recognized boolean (expected one of "
        "1/true/yes/on or 0/false/no/off); using default %s.",
        name, raw, default,
    )
    return default


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    """Parse an int env var, falling back to the default on blank/garbage and
    clamping to a minimum. A malformed value (e.g. DB_POOL_SIZE=ten) must not
    crash the whole app at import time, and a zero/negative for a knob like
    SESSION_TTL_SECONDS would be silently broken — so clamp rather than trust.
    Float-formatted values (e.g. "3600.0", common in Helm/templated env) are
    tolerated by parsing through float()."""
    raw = os.getenv(name)
    if raw in (None, ""):
        value = default
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            try:
                value = int(float(raw))
            except (TypeError, ValueError):
                logger.warning(
                    "Env var %s=%r is not an integer; using default %d.",
                    name, raw, default,
                )
                value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    """Float counterpart of _env_int — crash-safe and optionally clamped.

    Rejects non-finite values: float() happily parses 'inf'/'-inf'/'nan', and
    the `< minimum` clamp never catches them (NaN comparisons are always
    False, inf is never below a finite minimum), so an inf/nan would feed an
    infinite cloud-call budget or a NaN httpx timeout. Coerce those back to the
    default before clamping."""
    raw = os.getenv(name)
    if raw in (None, ""):
        value = default
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Env var %s=%r is not a number; using default %s.",
                name, raw, default,
            )
            value = default
    if not math.isfinite(value):
        logger.warning(
            "Env var %s=%r is not finite; using default %s.", name, raw, default,
        )
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


class Settings:
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    STATIC_DIR: Path = BASE_DIR / "app" / "static"

    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'bug_hunter.db'}",
    )

    # Postgres connection-pool sizing (ignored for SQLite dev/test). Defaults
    # suit a normal box; lower them on a very small VM (0.1 CPU / 512 MB) so a
    # traffic spike can't open pool_size + max_overflow connections at once.
    DB_POOL_SIZE: int = _env_int("DB_POOL_SIZE", 5, minimum=1)
    DB_MAX_OVERFLOW: int = _env_int("DB_MAX_OVERFLOW", 10, minimum=0)

    # Default: empty (same-origin only). Cross-origin clients must be
    # explicitly allow-listed via CORS_ORIGINS, which avoids a permissive CORS
    # policy and the wildcard+credentials combo that browsers reject.
    # Same-origin SPA usage is unaffected because the CORS middleware
    # short-circuits for those.
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    APP_NAME: str = os.getenv("APP_NAME", "Bug Hunter")
    APP_VERSION: str = os.getenv("APP_VERSION", "3.0")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Explicit deployment-environment signal. Set APP_ENV=production (also
    # accepts prod/staging) on any real deploy, including HTTP/intranet ones
    # behind a TLS-terminating proxy where COOKIE_SECURE may be false, to turn
    # on the fail-closed production hardening (refuse the default admin
    # password, require a strong SESSION_SECRET, refuse console email, hide the
    # API docs). When APP_ENV is unset, fall back to the COOKIE_SECURE (HTTPS)
    # signal so existing HTTPS deploys keep hardening with no config change. See
    # is_production below and the fail-closed checks in app/main.py.
    APP_ENV: str = os.getenv("APP_ENV", "").strip().lower()

    # Hard ceiling on request body size, in bytes. The 50 MB attachment cap +
    # multipart overhead + headers fit under 60 MB; raise via env if you need
    # bigger file uploads. Requests that exceed this are
    # rejected with 413 before any body is read into memory, so a hostile
    # client can't exhaust RAM by claiming a huge Content-Length.
    MAX_REQUEST_BODY_BYTES: int = _env_int(
        "MAX_REQUEST_BODY_BYTES", 60 * 1024 * 1024, minimum=1024
    )

    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8765")

    API_KEY: str = os.getenv("API_KEY", "")

    # --- Authentication ---
    # Used to sign session cookies. Set to a long random string in production
    # (`openssl rand -hex 32`). If left blank, a process-local random secret is
    # generated at startup, so every restart invalidates every session: fine
    # for dev, not for production.
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    # Session lifetime in seconds. Default = 1 day.
    SESSION_TTL_SECONDS: int = _env_int("SESSION_TTL_SECONDS", 86400, minimum=60)
    # Set to true behind HTTPS so cookie is only sent on TLS connections.
    COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", False)
    # Stop honouring jti-less session cookies (issued before the per-session
    # sessions table existed). Those cookies can't be revoked per-device and,
    # in their 1-part form, pin session_version to 0. Default False keeps them
    # working through a migration window; set True once all clients have
    # re-logged-in to close the revocation gap.
    SESSION_REQUIRE_JTI: bool = _env_bool("SESSION_REQUIRE_JTI", False)
    # Interactive API docs (/docs, /redoc, /openapi.json). Auto-enabled in dev
    # and disabled once COOKIE_SECURE is set (production) since the schema
    # publishes the full endpoint surface for recon. Set true to force them on.
    ENABLE_API_DOCS: bool = _env_bool("ENABLE_API_DOCS", False)
    # When the app sits behind a trusted reverse proxy (nginx, traefik,
    # ALB, etc.) that sets X-Forwarded-For, set this to true so rate
    # limiting buckets per real client IP instead of per proxy IP.
    # Leave false in dev / direct-uvicorn deploys — spoofed XFF would
    # otherwise let attackers bypass the limiter.
    TRUST_PROXY_FORWARDED_FOR: bool = _env_bool("TRUST_PROXY_FORWARDED_FOR", False)
    # Number of trusted reverse-proxy hops in front of the app. A proxy
    # appends the peer it saw to the right of X-Forwarded-For, so the
    # trustworthy client address is the Nth entry counting from the right,
    # where N = number of trusted proxies. The left-most entry is fully
    # client-controlled and must not be used for a security decision. With
    # the default single proxy (N=1) the right-most entry is used.
    TRUST_PROXY_HOP_COUNT: int = _env_int("TRUST_PROXY_HOP_COUNT", 1, minimum=1)
    # First-time bootstrap admin. Used only if zero users exist in the DB.
    BOOTSTRAP_ADMIN_EMAIL: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@bughunter.local")
    BOOTSTRAP_ADMIN_PASSWORD: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!")
    BOOTSTRAP_ADMIN_NAME: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Admin")

    # --- Password policy ----------------------------------------------------
    # Strength rules for new passwords, enforced in one place
    # (app/schemas._check_password_strength). The default "changeme" is always
    # accepted regardless of these so existing accounts keep working; that
    # exception lives above these checks by design.
    PASSWORD_MIN_LENGTH: int = _env_int("PASSWORD_MIN_LENGTH", 8, minimum=1)
    # Require at least one letter AND one digit. On by default (current rule).
    PASSWORD_REQUIRE_COMPLEXITY: bool = _env_bool("PASSWORD_REQUIRE_COMPLEXITY", True)
    # Account-enumeration resistance on POST /api/auth/forgot-password. When
    # True (default) the endpoint always returns 204 and never reveals whether
    # an email maps to an account ("if an account exists, we've sent a link").
    # Set False for the friendlier UX that 404s on an unknown address (a
    # lower-security trade-off). Either way a reset email is only ever sent to a
    # real, active account, and the server-side "no account" signal is audited.
    FORGOT_PASSWORD_ENUMERATION_SAFE: bool = _env_bool(
        "FORGOT_PASSWORD_ENUMERATION_SAFE", True
    )

    # --- Reports ------------------------------------------------------------
    # Hard ceiling on rows a single XLSX export (POST /api/reports/export.xlsx)
    # may materialize. The export buffers the entire workbook in memory, so an
    # unbounded export over the full work-item table could OOM a small worker.
    # Above this many matching rows the endpoint returns 413 and asks the caller
    # to narrow filters. Generous enough for real audit/compliance exports.
    MAX_REPORT_ROWS: int = _env_int("MAX_REPORT_ROWS", 50000, minimum=1)

    # ------------------------------------------------------------------
    # Sleuth cloud LLM (optional Layer 4 — natural-language fallback).
    #
    # Everything here is off by default. With SLEUTH_CLOUD_ENABLED unset the
    # chatbot uses rules + classifier + the optional local llama.cpp layer,
    # with no outbound HTTP. Set the flag and a key to enable the
    # Gemini-primary / OpenRouter-fallback path for free-form questions the
    # rules can't parse.
    #
    # The cloud layer never performs writes and never invents counts: data
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
    SLEUTH_CLOUD_TIMEOUT_S: float = _env_float("SLEUTH_CLOUD_TIMEOUT_S", 20, minimum=1)
    SLEUTH_CLOUD_MAX_TOKENS: int = _env_int("SLEUTH_CLOUD_MAX_TOKENS", 600, minimum=1)

    # RAG (retrieval over bugs / comments / docs). Independent flag so you
    # can run the cloud LLM with or without retrieval. Needs `chromadb`
    # installed; if the import fails the layer disables itself cleanly.
    SLEUTH_RAG_ENABLED: bool = _env_bool("SLEUTH_RAG_ENABLED", False)
    SLEUTH_RAG_DIR: str = os.getenv(
        "SLEUTH_RAG_DIR", str(BASE_DIR / ".sleuth_rag")
    )
    SLEUTH_RAG_TOP_K: int = _env_int("SLEUTH_RAG_TOP_K", 5, minimum=1)
    # Folder of plain-text / markdown docs (FAQs, runbooks) to index
    # alongside the live bug/comment data. Optional.
    SLEUTH_DOCS_DIR: str = os.getenv("SLEUTH_DOCS_DIR", str(BASE_DIR / "docs"))

    # Dependency-free keyword retrieval over bugs, used to ground free-form
    # answers without chromadb so grounding fits the low-memory target. Read
    # scope matches the REST API (every authenticated user can read every bug),
    # so it never surfaces more than the normal API would.
    SLEUTH_RETRIEVAL_ENABLED: bool = _env_bool("SLEUTH_RETRIEVAL_ENABLED", False)
    # After a free-form answer, deterministically check the bug numbers it
    # cites against the retrieved records and flag any that were not grounded.
    SLEUTH_VERIFY_ANSWERS: bool = _env_bool("SLEUTH_VERIFY_ANSWERS", False)

    # --- Sleuth agent (optional, read-only multi-step reasoning) -------------
    # When on, a free-form question is handled by a bounded ReAct-style loop:
    # the cloud model may call read-only tools (run a canonical SQL query, or
    # keyword-retrieve bugs) a few times, observe the results, then answer from
    # them. This improves accuracy on multi-hop questions ("who owns the project
    # with the most overdue critical bugs?"). Every tool re-parses through the
    # deterministic NLU and the same write firewall that drops any action
    # intent, so the agent can't write and numbers always come from SQL.
    # Off by default; needs SLEUTH_CLOUD_ENABLED + a key to do anything.
    SLEUTH_AGENT_ENABLED: bool = _env_bool("SLEUTH_AGENT_ENABLED", False)
    # Hard ceiling on model round-trips per question — caps cloud cost/latency
    # and prevents a runaway loop. Each step is one model call.
    SLEUTH_AGENT_MAX_STEPS: int = _env_int("SLEUTH_AGENT_MAX_STEPS", 4, minimum=1)

    # --- Sleuth answer evaluation (optional LLM-as-judge) -------------------
    # When on, every free-form answer is scored by a second, independent model
    # call for grounding and faithfulness before it is returned. A weak verdict
    # only appends a short "please verify" caveat: the judge can't rewrite an
    # answer, block a reply, or trigger a write, and a judge failure fails open
    # (the answer is returned unchanged). Complements the deterministic citation
    # check (SLEUTH_VERIFY_ANSWERS), which always runs.
    SLEUTH_EVAL_ENABLED: bool = _env_bool("SLEUTH_EVAL_ENABLED", False)
    # Below this confidence (0..1) the answer gets the verify-it-yourself caveat.
    SLEUTH_EVAL_MIN_SCORE: float = _env_float("SLEUTH_EVAL_MIN_SCORE", 0.5, minimum=0.0)

    # Persist Sleuth conversations to the additive chat_* tables. Additive
    # only; existing tables are never touched.
    SLEUTH_CHAT_MEMORY_ENABLED: bool = _env_bool("SLEUTH_CHAT_MEMORY_ENABLED", True)

    EMAIL_BACKEND: str = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "bughunter@localhost")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _env_int("SMTP_PORT", 587, minimum=1)
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
    SMTP_USE_TLS: bool = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT: int = _env_int("SMTP_TIMEOUT", 10, minimum=1)

    # --- Daily email digest -------------------------------------------------
    # When on, the per-operation work-item emails (new item / update /
    # assignment / comment / event) are not sent immediately. Instead each
    # operation is recorded as a notification row, and the once-a-day digest
    # job (`python -m app.jobs.email_digest`) batches every user's operations
    # into one grouped email. Security/transactional emails (password reset)
    # are never batched — they always send immediately, regardless of this flag.
    # Default off keeps the immediate-email behaviour until ops opts in.
    EMAIL_DIGEST_ENABLED: bool = _env_bool("EMAIL_DIGEST_ENABLED", False)
    # How far back the digest job looks for un-emailed operations. Slightly
    # more than 24h so a cron that drifts or runs late never drops a day, while
    # still bounding the very first run so it can't replay months of history.
    EMAIL_DIGEST_LOOKBACK_HOURS: int = _env_int(
        "EMAIL_DIGEST_LOOKBACK_HOURS", 26, minimum=1
    )
    # In-app scheduler (optional). Set to a standard 5-field cron expression
    # (e.g. "0 7 * * *" = every day at 07:00) and the app runs the digest job
    # itself on that schedule — no host cron / Task Scheduler needed. Empty
    # (default) leaves scheduling to an external runner, unchanged. The
    # scheduler only does anything when EMAIL_DIGEST_ENABLED is also true.
    EMAIL_DIGEST_CRON: str = os.getenv("EMAIL_DIGEST_CRON", "").strip()
    # IANA timezone the cron is evaluated in (e.g. "Asia/Kolkata", "UTC").
    # Empty = UTC.
    EMAIL_DIGEST_TIMEZONE: str = os.getenv("EMAIL_DIGEST_TIMEZONE", "").strip()

    # --- Web push (Firebase Cloud Messaging) --------------------------------
    # Browser push notifications, sent immediately when an operation happens
    # (independent of the email digest). Off unless enabled and a Firebase
    # service account is configured, so the app/tests run fine without it.
    #
    #   WEB_PUSH_ENABLED            master switch.
    #   FCM_CREDENTIALS_FILE        path to the Firebase service-account JSON
    #                               (Project settings → Service accounts). The
    #                               backend signs FCM sends with it.
    # The remaining FIREBASE_* values are the *web app* config (public — they
    # ship to the browser via GET /api/push/config) plus the Web-Push/VAPID
    # public key (Cloud Messaging → Web configuration → Generate key pair).
    WEB_PUSH_ENABLED: bool = _env_bool("WEB_PUSH_ENABLED", False)
    FCM_CREDENTIALS_FILE: str = os.getenv("FCM_CREDENTIALS_FILE", "")
    FIREBASE_API_KEY: str = os.getenv("FIREBASE_API_KEY", "")
    FIREBASE_AUTH_DOMAIN: str = os.getenv("FIREBASE_AUTH_DOMAIN", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_MESSAGING_SENDER_ID: str = os.getenv("FIREBASE_MESSAGING_SENDER_ID", "")
    FIREBASE_APP_ID: str = os.getenv("FIREBASE_APP_ID", "")
    FIREBASE_VAPID_KEY: str = os.getenv("FIREBASE_VAPID_KEY", "")

    # Production-deployment predicate used to drive the fail-closed startup
    # checks. An explicit APP_ENV wins; otherwise fall back to the HTTPS
    # (COOKIE_SECURE) signal so HTTPS deploys that never set APP_ENV still
    # harden. A property (not a stored attribute) so it always reflects the
    # current APP_ENV/COOKIE_SECURE, including tests that patch either one.
    @property
    def is_production(self) -> bool:
        env = self.APP_ENV
        if env in ("production", "prod", "staging"):
            return True
        if env in ("dev", "development", "test", "testing", "local", "ci"):
            return False
        return self.COOKIE_SECURE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
