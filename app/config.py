"""App configuration, loaded from environment variables."""
from __future__ import annotations

import logging
import math
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("bug_hunter.config")

# Load .env before any setting is read; real env vars win over the file.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is an optional dependency
    load_dotenv = None  # type: ignore[assignment]


def _load_dotenv() -> None:
    """Load a local .env if available; a bad file is logged, never fatal."""
    if load_dotenv is None:  # pragma: no cover - dotenv is an optional dependency
        return
    try:
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except OSError as exc:
        logger.warning("Could not load .env file: %s", exc)


_load_dotenv()


# Recognized boolean spellings; anything else falls back to the default.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off", ""})


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var; unrecognized values fall back to the default
    (not False) so a typo can't silently disable a security control."""
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
    """Parse an int env var: default on garbage, clamp to minimum, accept
    float-formatted values like "3600.0"."""
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
    """Float counterpart of _env_int; inf/nan are rejected explicitly since the
    minimum clamp never catches them."""
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

    # Postgres pool sizing (ignored for SQLite); lower on small VMs.
    DB_POOL_SIZE: int = _env_int("DB_POOL_SIZE", 5, minimum=1)
    DB_MAX_OVERFLOW: int = _env_int("DB_MAX_OVERFLOW", 10, minimum=0)

    # Empty default = same-origin only; cross-origin clients must be allow-listed.
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    APP_NAME: str = os.getenv("APP_NAME", "Bug Hunter")
    APP_VERSION: str = os.getenv("APP_VERSION", "3.1")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Deployment-environment signal; production/prod/staging enables the
    # fail-closed hardening checks (see is_production and app/main.py).
    APP_ENV: str = os.getenv("APP_ENV", "").strip().lower()

    # Request-body ceiling; over-limit requests get 413 before the body is read,
    # so a huge Content-Length can't exhaust RAM.
    MAX_REQUEST_BODY_BYTES: int = _env_int(
        "MAX_REQUEST_BODY_BYTES", 60 * 1024 * 1024, minimum=1024
    )

    APP_BASE_URL: str = os.getenv("APP_BASE_URL", "http://localhost:8765")

    API_KEY: str = os.getenv("API_KEY", "")

    # --- Authentication ---
    # Signs session cookies; if blank a per-process random secret is used
    # (sessions die on restart — dev only).
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")
    # Session lifetime in seconds (default 1 day).
    SESSION_TTL_SECONDS: int = _env_int("SESSION_TTL_SECONDS", 86400, minimum=60)
    # Set true behind HTTPS so the cookie is TLS-only.
    COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", False)
    # Reject legacy jti-less cookies (not revocable per-device); flip on after
    # a migration window.
    SESSION_REQUIRE_JTI: bool = _env_bool("SESSION_REQUIRE_JTI", False)
    # /docs, /redoc, /openapi.json — off in production (endpoint recon surface).
    ENABLE_API_DOCS: bool = _env_bool("ENABLE_API_DOCS", False)
    # Only trust X-Forwarded-For behind a trusted proxy; otherwise a spoofed
    # header bypasses the rate limiter.
    TRUST_PROXY_FORWARDED_FOR: bool = _env_bool("TRUST_PROXY_FORWARDED_FOR", False)
    # Trusted proxy hops: the client IP is the Nth XFF entry from the right;
    # the left-most entry is client-controlled.
    TRUST_PROXY_HOP_COUNT: int = _env_int("TRUST_PROXY_HOP_COUNT", 1, minimum=1)
    # Bootstrap admin credentials, used only when the users table is empty.
    BOOTSTRAP_ADMIN_EMAIL: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@bughunter.local")
    BOOTSTRAP_ADMIN_PASSWORD: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "ChangeMe123!")
    BOOTSTRAP_ADMIN_NAME: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Admin")

    # --- Password policy ----------------------------------------------------
    # Enforced in app/schemas._check_password_strength; legacy "changeme" is
    # intentionally exempt.
    PASSWORD_MIN_LENGTH: int = _env_int("PASSWORD_MIN_LENGTH", 8, minimum=1)
    # Require at least one letter and one digit.
    PASSWORD_REQUIRE_COMPLEXITY: bool = _env_bool("PASSWORD_REQUIRE_COMPLEXITY", True)
    # When true, forgot-password always returns 204 so account existence never leaks.
    FORGOT_PASSWORD_ENUMERATION_SAFE: bool = _env_bool(
        "FORGOT_PASSWORD_ENUMERATION_SAFE", True
    )

    # --- Reports ------------------------------------------------------------
    # XLSX export row ceiling; the workbook is buffered in memory, so unbounded
    # exports could OOM a small worker (over-limit returns 413).
    MAX_REPORT_ROWS: int = _env_int("MAX_REPORT_ROWS", 50000, minimum=1)

    # --- Sleuth cloud LLM (optional, natural-language fallback) -------------
    # Everything here is off by default. Without SLEUTH_CLOUD_ENABLED the
    # chatbot uses rules + classifier + the optional local llama.cpp layer,
    # with no outbound HTTP. Set the flag and a key to enable the
    # Groq-primary / OpenRouter-fallback path for questions the rules can't
    # parse. The cloud layer never writes and never invents counts: data
    # questions are routed back through the deterministic SQL handlers; the
    # model only paraphrases or summarises retrieved context (see rag.py).
    SLEUTH_CLOUD_ENABLED: bool = _env_bool("SLEUTH_CLOUD_ENABLED", False)
    # Primary provider: Groq (free tier, OpenAI-compatible). Create a key at
    # https://console.groq.com/keys; never hard-code it.
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    # Override to pick a different model (e.g. llama-3.1-8b-instant for lower
    # latency / higher rate limits).
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    # Fallback provider: OpenRouter free models, used only if Groq errors or is
    # rate-limited. Leave the key blank to run Groq-only.
    OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "qwen/qwen-2.5-7b-instruct:free")
    # Used for RAG embeddings only (see SLEUTH_RAG_ENABLED). Groq has no
    # embeddings endpoint, so the local-RAG layer uses Google's free embedding
    # API instead. The chat assistant never calls Gemini; leave these blank
    # unless you enable RAG.
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_EMBED_MODEL: str = os.getenv("GEMINI_EMBED_MODEL", "text-embedding-004")
    # Per-call timeout. On timeout the app falls back then fails closed with the
    # normal "I didn't understand" reply.
    SLEUTH_CLOUD_TIMEOUT_S: float = _env_float("SLEUTH_CLOUD_TIMEOUT_S", 20, minimum=1)
    SLEUTH_CLOUD_MAX_TOKENS: int = _env_int("SLEUTH_CLOUD_MAX_TOKENS", 600, minimum=1)
    # Sampling temperature for the conversational answer path. Greedy decoding
    # (0.0) makes capable models sound flat and repetitive, working against the
    # "answer naturally" prompt instruction. A small temperature plus mild
    # repetition penalties help here. This is safe even though the same call
    # decides data-vs-answer: a data ask only picks a canonical query that the
    # deterministic NLU re-parses, so variation can't break the read-only
    # firewall. Tool/eval sub-calls (LLM-as-judge, agent loop, ingest) stay at
    # TEMPERATURE_TOOLS (0.0) for reproducible JSON and stable scoring.
    SLEUTH_CLOUD_TEMPERATURE: float = _env_float("SLEUTH_CLOUD_TEMPERATURE", 0.6, minimum=0.0)
    SLEUTH_CLOUD_TEMPERATURE_TOOLS: float = _env_float("SLEUTH_CLOUD_TEMPERATURE_TOOLS", 0.0, minimum=0.0)
    # Mild repetition penalties for the conversational path only. Kept low so
    # they never perturb the JSON envelope or the fixed canonical vocabulary the
    # data path depends on.
    SLEUTH_CLOUD_FREQUENCY_PENALTY: float = _env_float("SLEUTH_CLOUD_FREQUENCY_PENALTY", 0.3, minimum=0.0)
    SLEUTH_CLOUD_PRESENCE_PENALTY: float = _env_float("SLEUTH_CLOUD_PRESENCE_PENALTY", 0.2, minimum=0.0)

    # RAG retrieval over bugs/comments/docs. Independent of SLEUTH_CLOUD_ENABLED
    # so you can run the cloud LLM with or without it. Requires chromadb; the
    # layer disables itself cleanly if the import fails.
    SLEUTH_RAG_ENABLED: bool = _env_bool("SLEUTH_RAG_ENABLED", False)
    SLEUTH_RAG_DIR: str = os.getenv(
        "SLEUTH_RAG_DIR", str(BASE_DIR / ".sleuth_rag")
    )
    SLEUTH_RAG_TOP_K: int = _env_int("SLEUTH_RAG_TOP_K", 5, minimum=1)
    # Optional folder of plain-text/markdown docs (FAQs, runbooks) to index
    # alongside the live bug/comment data.
    SLEUTH_DOCS_DIR: str = os.getenv("SLEUTH_DOCS_DIR", str(BASE_DIR / "docs"))

    # Dependency-free keyword retrieval over bugs, used to ground free-form
    # answers without chromadb. Read scope matches the REST API (every
    # authenticated user can read every bug), so it never surfaces more than the
    # normal API would.
    SLEUTH_RETRIEVAL_ENABLED: bool = _env_bool("SLEUTH_RETRIEVAL_ENABLED", False)
    # After a free-form answer, check the bug numbers it cites against the
    # retrieved records and flag any that weren't grounded.
    SLEUTH_VERIFY_ANSWERS: bool = _env_bool("SLEUTH_VERIFY_ANSWERS", False)

    # --- Sleuth agent (optional, read-only multi-step reasoning) -------------
    # When enabled, a free-form question is handled by a bounded ReAct-style
    # loop: the model may call read-only tools (canonical SQL query or keyword
    # retrieval) a few times, observe results, then answer. This improves
    # accuracy on multi-hop questions like "who owns the project with the most
    # overdue critical bugs?". Every tool call re-parses through the
    # deterministic NLU and the same write firewall, so the agent can't write
    # and numbers always come from SQL. Requires SLEUTH_CLOUD_ENABLED + a key.
    SLEUTH_AGENT_ENABLED: bool = _env_bool("SLEUTH_AGENT_ENABLED", False)
    # Hard ceiling on model round-trips per question, to cap cost/latency and
    # prevent a runaway loop. Each step is one model call.
    SLEUTH_AGENT_MAX_STEPS: int = _env_int("SLEUTH_AGENT_MAX_STEPS", 4, minimum=1)

    # --- Sleuth answer evaluation (optional LLM-as-judge) -------------------
    # When enabled, every free-form answer is scored by a second independent
    # model call for grounding and faithfulness. A weak verdict appends a short
    # "please verify" caveat; the judge can't rewrite an answer, block a reply,
    # or trigger a write. A judge failure fails open (answer returned unchanged).
    # Complements the deterministic citation check (SLEUTH_VERIFY_ANSWERS).
    SLEUTH_EVAL_ENABLED: bool = _env_bool("SLEUTH_EVAL_ENABLED", False)
    # Below this confidence (0..1) the answer gets the verify-it-yourself caveat.
    SLEUTH_EVAL_MIN_SCORE: float = _env_float("SLEUTH_EVAL_MIN_SCORE", 0.5, minimum=0.0)
    # App-side character ceiling on a free-text cloud answer, independent of the
    # provider's max_tokens. This guards against a future config bump or a flaky
    # fallback provider pushing an unbounded blob into the chat transcript. Set
    # generously so normal answers are never truncated; truncation is the last
    # step so it never cuts an appended caveat.
    SLEUTH_ANSWER_MAX_CHARS: int = _env_int("SLEUTH_ANSWER_MAX_CHARS", 4000, minimum=1)

    # Persist Sleuth conversations to the chat_* tables (additive schema only).
    SLEUTH_CHAT_MEMORY_ENABLED: bool = _env_bool("SLEUTH_CHAT_MEMORY_ENABLED", True)

    EMAIL_BACKEND: str = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    EMAIL_FROM: str = os.getenv("EMAIL_FROM", "bughunter@localhost")
    SMTP_HOST: str = os.getenv("SMTP_HOST", "")
    SMTP_PORT: int = _env_int("SMTP_PORT", 587, minimum=1)
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
    # Gmail (and most providers) display app passwords as 4 space-separated
    # groups for on-screen readability, but the actual secret has no spaces.
    # Pasting the display form straight into .env sends the wrong credential
    # and SMTP AUTH fails silently from the caller's point of view — strip any
    # spaces so both the raw and display-formatted values work.
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "").replace(" ", "")
    SMTP_USE_TLS: bool = _env_bool("SMTP_USE_TLS", True)
    SMTP_USE_SSL: bool = _env_bool("SMTP_USE_SSL", False)
    SMTP_TIMEOUT: int = _env_int("SMTP_TIMEOUT", 10, minimum=1)

    # --- Daily email digest -------------------------------------------------
    # When enabled, per-operation work-item emails (new item, update, assignment,
    # comment, event) are not sent immediately. Instead each operation is
    # recorded as a notification row and the once-a-day digest job
    # (`python -m app.jobs.email_digest`) batches everything into one grouped
    # email per user. Security/transactional emails (password reset) are never
    # batched and always send immediately.
    EMAIL_DIGEST_ENABLED: bool = _env_bool("EMAIL_DIGEST_ENABLED", False)
    # How far back the digest job looks for un-emailed operations. Two days-ish
    # (twice the daily gap) so one missed or failed run — a restart at fire
    # time, an SMTP outage whose rows were released for retry — is fully caught
    # up by the next run, while still bounding the very first run so it can't
    # replay months of history. A wider window can never double-send: rows are
    # claimed on emailed_at IS NULL.
    EMAIL_DIGEST_LOOKBACK_HOURS: int = _env_int(
        "EMAIL_DIGEST_LOOKBACK_HOURS", 50, minimum=1
    )
    # Optional in-app scheduler. Set to a standard 5-field cron expression
    # (e.g. "0 7 * * *") and the app runs the digest itself on that schedule,
    # no host cron needed. Empty (default) leaves scheduling to an external
    # runner. Has no effect unless EMAIL_DIGEST_ENABLED is also true.
    EMAIL_DIGEST_CRON: str = os.getenv("EMAIL_DIGEST_CRON", "").strip()
    # IANA timezone for the cron expression (e.g. "Asia/Kolkata"). Empty = UTC.
    EMAIL_DIGEST_TIMEZONE: str = os.getenv("EMAIL_DIGEST_TIMEZONE", "").strip()

    # --- Web push (Firebase Cloud Messaging) --------------------------------
    # Browser push notifications sent immediately on each operation, independent
    # of the email digest. Off by default; the app and tests run fine without it.
    #
    #   WEB_PUSH_ENABLED      master switch
    #   FCM_CREDENTIALS_FILE  path to the Firebase service-account JSON
    #                         (Project settings -> Service accounts); the
    #                         backend signs FCM requests with it
    #
    # The remaining FIREBASE_* values are the web app config (public; shipped to
    # the browser via GET /api/push/config) plus the VAPID public key
    # (Cloud Messaging -> Web configuration -> Generate key pair).
    WEB_PUSH_ENABLED: bool = _env_bool("WEB_PUSH_ENABLED", False)
    FCM_CREDENTIALS_FILE: str = os.getenv("FCM_CREDENTIALS_FILE", "")
    FIREBASE_API_KEY: str = os.getenv("FIREBASE_API_KEY", "")
    FIREBASE_AUTH_DOMAIN: str = os.getenv("FIREBASE_AUTH_DOMAIN", "")
    FIREBASE_PROJECT_ID: str = os.getenv("FIREBASE_PROJECT_ID", "")
    FIREBASE_MESSAGING_SENDER_ID: str = os.getenv("FIREBASE_MESSAGING_SENDER_ID", "")
    FIREBASE_APP_ID: str = os.getenv("FIREBASE_APP_ID", "")
    FIREBASE_VAPID_KEY: str = os.getenv("FIREBASE_VAPID_KEY", "")

    # Predicate used by the fail-closed startup checks. An explicit APP_ENV wins;
    # otherwise falls back to the COOKIE_SECURE signal so HTTPS deploys that
    # never set APP_ENV still harden. Implemented as a property (not a stored
    # attribute) so it always reflects the current values of APP_ENV and
    # COOKIE_SECURE, including in tests that patch either one.
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
