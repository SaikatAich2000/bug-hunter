"""Regenerate audit/ENV_REFERENCE.md from the code, docker-compose.yml and .env.example.

Usage (repo root): python scripts/gen_env_reference.py
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
cfg = (ROOT / "app/config.py").read_text(encoding="utf-8")
tel = (ROOT / "app/telemetry.py").read_text(encoding="utf-8")
compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
example = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()

rx = re.compile(r'(?:os\.getenv|os\.environ\.get|_env_bool|_env_int|_env_float|_env_list)\(\s*"([A-Z0-9_]+)"(?:\s*,\s*([^\n]*))?')
found = {}
extra = [p.read_text(encoding="utf-8") for p in (ROOT / "app").rglob("*.py")
         if p.name not in ("config.py", "telemetry.py", "main.py") and "static" not in p.parts]
for src in (cfg, tel, *extra):
    for m in rx.finditer(src):
        name, raw = m.group(1), (m.group(2) or "").strip()
        q = re.match(r'("[^"]*")', raw)
        if q:
            default = q.group(1)
        elif raw.startswith("f\""):
            default = "sqlite:///<repo>/bug_hunter.db"
        elif raw.startswith("str(Path(__file__"):
            default = "<repo>/models/sleuth.gguf"
        elif raw.startswith("str(BASE_DIR"):
            default = "<repo>/" + re.search(r'"([^"]+)"', raw).group(1)
        else:
            default = re.split(r"[,)]", raw)[0].strip() or '""'
        found.setdefault(name, default)
for m in re.finditer(r"\b(POSTGRES_[A-Z]+|BASE_IMAGE)\b", compose):
    found.setdefault(m.group(1), "(compose)")

# Comment block directly above each variable in .env.example.
docs = {}
for i, line in enumerate(example):
    m = re.match(r"#?\s*([A-Z][A-Z0-9_]+)=(.*)$", line)
    if not m:
        continue
    j, block = i - 1, []
    while j >= 0 and example[j].startswith("#") and not re.match(r"#\s*[A-Z][A-Z0-9_]+=", example[j]) \
            and "═" not in example[j]:
        block.insert(0, example[j].lstrip("# ").strip())
        j -= 1
    docs.setdefault(m.group(1), " ".join(b for b in block if b))


PURPOSE = {
    "LOGIN_FAIL_WINDOW_SECONDS": "Window for counting failed sign-ins (see LOGIN_FAIL_LIMIT).",
    "LOGIN_LOCKOUT_SECONDS": "How long an account stays locked after too many failures.",
    "SLEUTH_LLM_CTX_LEN": "Local LLM context window (tokens).",
    "SLEUTH_LLM_MAX_TOKENS": "Local LLM answer length cap.",
    "SLEUTH_LLM_RAM_HEADROOM": "Load the local model only when free RAM >= model size x this factor.",
    "SLEUTH_LLM_THREADS": "CPU threads for local inference.",
    "SLEUTH_LLM_TIMEOUT_S": "Local inference timeout in seconds.",
    "APP_ENV": "`production` (or `staging`) turns on the strict start-up checks; blank or `development` otherwise.",
    "APP_NAME": "Product name in page titles, API docs, emails, reports and notifications.",
    "LOG_LEVEL": "Root log level (DEBUG, INFO, WARNING, ERROR).",
    "BOOTSTRAP_ADMIN_NAME": "Display name of the first admin created on an empty database.",
    "BOOTSTRAP_ADMIN_PASSWORD": "Password of the first admin (empty database only). Production refuses blank, `ChangeMe123!` and `replace_with...` placeholders.",
    "DB_LOCK_TIMEOUT_MS": "PostgreSQL `lock_timeout` per session, so a request never waits forever on a row lock.",
    "DB_STATEMENT_TIMEOUT_MS": "PostgreSQL `statement_timeout` per session.",
    "DB_MAX_OVERFLOW": "Extra PostgreSQL connections above DB_POOL_SIZE. Requests are admitted to (pool + overflow - 2) at once; the rest queue without blocking a worker thread.",
    "EMAIL_FROM": "From header for outgoing email.",
    "FIREBASE_APP_ID": "Firebase web app config (console > Project settings > General).",
    "FIREBASE_AUTH_DOMAIN": "Firebase web app config.",
    "FIREBASE_MESSAGING_SENDER_ID": "Firebase web app config.",
    "FIREBASE_PROJECT_ID": "Firebase web app config.",
    "FIREBASE_VAPID_KEY": "Public Web Push certificate key (Cloud Messaging > Web configuration).",
    "FORGOT_PASSWORD_ENUMERATION_SAFE": "true = forgot-password always answers the same way, so it can't reveal which emails have accounts.",
    "GEMINI_API_KEY": "Gemini embeddings key, used only by the optional RAG index (SLEUTH_RAG_ENABLED).",
    "GEMINI_EMBED_MODEL": "Embeddings model for RAG.",
    "GITHUB_BRANCH_NAME_MAX_LENGTH": "Maximum generated branch name length.",
    "GITHUB_CONNECT_TIMEOUT_SECONDS": "TCP connect timeout for GitHub API calls.",
    "GITHUB_READ_TIMEOUT_SECONDS": "Read timeout for GitHub API calls.",
    "GITHUB_MAX_RETRIES": "Bounded retries for transient GitHub failures.",
    "GITHUB_DEFAULT_BASE_BRANCH": "Deployment default base branch; a project's own setting wins.",
    "GITHUB_ORGANIZATION": "Deployment default organization; a project's own setting wins.",
    "GROQ_MODEL": "Groq chat model for Sleuth's cloud layer.",
    "OPENROUTER_MODEL": "OpenRouter fallback model.",
    "MAX_REPORT_ROWS": "Row cap for one Reports Excel export (larger requests get 413).",
    "OTEL_EXPORTER_OTLP_INSECURE": "Override TLS detection for the OTLP endpoint (derived from the URL scheme by default).",
    "OTEL_INSTRUMENT_HTTPX": "Trace outbound HTTP calls (LLM, GitHub, webhooks).",
    "OTEL_LOGS_ENABLED": "Export logs when the OTLP endpoint is set.",
    "OTEL_METRICS_ENABLED": "Export metrics when the OTLP endpoint is set.",
    "OTEL_LOG_EXCLUDE_LOGGERS": "Loggers kept out of the export (access logs arrive as traces instead).",
    "OTEL_LOG_REDACT_SENSITIVE": "Redact attribute keys that look like passwords, tokens, secrets or cookies before export.",
    "OTEL_RESOURCE_ATTRIBUTES": "Extra resource attributes, comma-separated key=value.",
    "OTEL_SERVICE_NAME": "service.name reported to the collector.",
    "OTEL_SERVICE_VERSION": "service.version; defaults to APP_VERSION.",
    "OTEL_TRACES_SAMPLER_ARG": "Ratio for the *traceidratio samplers.",
    "PASSWORD_REQUIRE_COMPLEXITY": "Require at least one letter and one number in passwords.",
    "POSTGRES_DB": "Database name for the Compose PostgreSQL container (default bugtracker).",
    "POSTGRES_PASSWORD": "Password for the Compose PostgreSQL container. Set before first boot; changing it later does not change the existing volume.",
    "POSTGRES_USER": "User for the Compose PostgreSQL container (default bugtracker).",
    "SLEUTH_AGENT_MAX_STEPS": "Lookups the read-only agent may run per question.",
    "SLEUTH_ANSWER_MAX_CHARS": "Cap on a cloud answer's length.",
    "SLEUTH_CHAT_MEMORY_ENABLED": "Remember the last few turns per user (pronouns like 'close it').",
    "SLEUTH_CLOUD_FREQUENCY_PENALTY": "LLM sampling setting.",
    "SLEUTH_CLOUD_MAX_TOKENS": "LLM response token cap.",
    "SLEUTH_CLOUD_PRESENCE_PENALTY": "LLM sampling setting.",
    "SLEUTH_CLOUD_TEMPERATURE": "LLM temperature for free-form answers.",
    "SLEUTH_CLOUD_TEMPERATURE_TOOLS": "LLM temperature for tool calling (0 = deterministic).",
    "SLEUTH_DOCS_DIR": "Folder of extra documents indexed by RAG.",
    "SLEUTH_EVAL_MIN_SCORE": "Answers scored below this get a 'please verify' note.",
    "SLEUTH_LLM_TOOLS_MAX_ROUNDS": "Tool-call rounds per chat turn.",
    "SLEUTH_RAG_DIR": "Where the local vector index is stored.",
    "SLEUTH_RAG_TOP_K": "Documents retrieved per RAG query.",
    "SLEUTH_VERIFY_ANSWERS": "Flag bug numbers an answer cites without evidence (cloud layer only).",
    "SMTP_PASSWORD": "SMTP password (Gmail: an App Password).",
    "SMTP_PORT": "SMTP port (587 with STARTTLS, 465 with SSL).",
    "SMTP_TIMEOUT": "SMTP connect/send timeout in seconds.",
    "SMTP_USERNAME": "SMTP login.",
    "SMTP_USE_SSL": "Implicit TLS (port 465).",
    "SMTP_USE_TLS": "STARTTLS (port 587).",
    "WEB_PUSH_ENABLED": "Master switch for browser push via Firebase Cloud Messaging.",
}

REQUIRED = {
    "APP_VERSION": "Yes (Compose refuses to start without it)",
    "POSTGRES_PASSWORD": "Yes for Docker Compose",
    "BOOTSTRAP_ADMIN_PASSWORD": "Yes on a fresh database (Compose requires it)",
    "BOOTSTRAP_ADMIN_EMAIL": "Yes on a fresh database",
    "SESSION_SECRET": "Yes in production (>= 32 chars, not a placeholder)",
    "APP_BASE_URL": "Yes with APP_ENV=production (https)",
    "GIT_CREDENTIAL_ENCRYPTION_KEY": "To save project Git tokens",
    "DATABASE_URL": "Only without Compose (external PostgreSQL)",
}
SECRET = re.compile(r"(PASSWORD$|SECRET|TOKEN|_KEY$|API_KEY|CREDENTIALS_JSON|HEADERS)")
INTERNAL = {"CONTAINER_APP_HOSTNAME", "CONTAINER_APP_NAME", "CONTAINER_APP_ENV_DNS_SUFFIX", "BCRYPT_TEST_ROUNDS"}

GROUPS = [
    ("Core", r"^(APP_|LOG_|CORS_|TRUST_PROXY|MAX_REQUEST|ENABLE_API_DOCS|BASE_IMAGE)"),
    ("Database", r"^(DATABASE_URL|DB_|POSTGRES_)"),
    ("Authentication and sessions", r"^(SESSION_|COOKIE_|BOOTSTRAP_|AUTO_LOGIN|PASSWORD_(?!BREACH)|FORGOT_)"),
    ("Email", r"^(EMAIL_|SMTP_)"),
    ("Git integration", r"^(GIT_|GITHUB_)"),
    ("Web push", r"^(WEB_PUSH|FCM_|FIREBASE_)"),
    ("Sleuth assistant", r"^(SLEUTH_|GROQ_|OPENROUTER_|GEMINI_|LLM_)"),
    ("Sign-in protection", r"^(LOGIN_|PASSWORD_BREACH)"),
    ("Observability", r"^(OTEL_)"),
    ("Reports", r"^(MAX_REPORT)"),
]
out = ["# Environment variable reference", "",
       "Generated from every environment variable read under `app/`, plus `docker-compose.yml` and the comments in",
       "`.env.example` (regenerate with `python scripts/gen_env_reference.py` after changing any of them). Configuration precedence: real process",
       "environment first, then `.env` (loaded with python-dotenv, `override=False`), then the default",
       "below. Docker Compose passes values from `.env` into the container through `${VAR}` interpolation.", "",
       "Values marked *secret* must come from `.env` or a platform secret store and never be committed.",
       "Production start-up validation (`app/main.py::_production_config_errors`) refuses weak or",
       "placeholder values for the settings marked required in production.", ""]
placed = set()
for title, pat in GROUPS:
    rows = [n for n in sorted(found) if re.match(pat, n) and n not in INTERNAL and n not in placed]
    if not rows:
        continue
    out += [f"## {title}", "", "| Variable | Default | Required | Secret | Purpose |", "|---|---|---|---|---|"]
    for n in rows:
        placed.add(n)
        d = found[n] or '""'
        d = d.replace("|", "\\|")[:60]
        purpose = (docs.get(n) or PURPOSE.get(n, "")).replace("|", "\\|")
        out.append(f"| `{n}` | `{d}` | {REQUIRED.get(n, 'No')} | {'secret' if SECRET.search(n) else ''} | {purpose[:260]} |")
    out.append("")
rest = [n for n in sorted(found) if n not in placed and n not in INTERNAL]
if rest:
    out += ["## Other", "", "| Variable | Default | Purpose |", "|---|---|---|"]
    out += [f"| `{n}` | `{found[n][:50]}` | {(docs.get(n) or '')[:200]} |" for n in rest]
    out.append("")
out += ["## Set by the platform (do not configure)", "",
        "`CONTAINER_APP_HOSTNAME`, `CONTAINER_APP_NAME` and `CONTAINER_APP_ENV_DNS_SUFFIX` are injected by",
        "Azure Container Apps and only used to derive a default `APP_BASE_URL`. `BCRYPT_TEST_ROUNDS` is a",
        "test-suite speed knob.", ""]
(ROOT / "audit/ENV_REFERENCE.md").write_text("\n".join(out), encoding="utf-8", newline="\n")
print(len(placed) + len(rest), "variables documented;", "undocumented purpose:",
      [n for n in sorted(found) if n not in INTERNAL and not (docs.get(n) or PURPOSE.get(n))])
