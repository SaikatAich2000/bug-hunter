# Known limitations

Each item is either a deliberate design trade-off, a documented scope limit,
or something this environment could not verify. None is known to lose data.

## Product behaviour

| Limitation | Detail | Workaround / next step |
|---|---|---|
| Item descriptions are plain text | xOPS design: the API stores and returns descriptions as plain text; 3.x formatted descriptions display flattened. Comments keep rich text. The first 4.0 boot keeps every 3.x original in `bugs.description_legacy_html`, so nothing is lost. | Product decision U1 in [USER_PROVIDED_INPUTS.md](USER_PROVIDED_INPUTS.md). |
| Views aren't in the URL | Switching views doesn't change the address; back/forward moves between pages (login, app), not views. Items and events can be deep-linked (`/#bug=N`, `/#event=N`). | Would need client-side routing. |
| No keyboard dragging on the board | Board cards drag with a mouse or a touch long-press. Keyboard and screen-reader users move a card with its actions menu ("Move to <status>"), as on Jira boards; the backlog also supports keyboard dragging through each row's drag handle. | Design choice (a keyboard drag across a wide board is slower than the menu). |
| Report history starts at the 4.0 upgrade | Reports replay the per-field change log, which starts when 4.0 first boots. Before that, the cumulative flow and control charts show each issue in its earliest recorded status, and sprints started before the upgrade are reported from their sprint event rows, with a notice that their history is incomplete. | None needed for work done after the upgrade. |
| Dark-theme primary buttons | White text on the Steam blue gradient is below 4.5:1 in places; axe can't measure gradients and the colours are part of the preserved theme. | Darken the gradient if strict AA is required. |
| Faint vs muted text | To pass WCAG AA on every surface, the dark theme's "faint" grey is now close to "muted", so that tier is subtler than before. | Theme choice. |

## API contract

| Limitation | Detail |
|---|---|
| 422 `detail` shape | Business-rule 422s return `detail` as a string; FastAPI validation 422s (and out-of-range ids) return the array shape. OpenAPI documents only the array shape. The frontend handles both. |
| OpenAPI precision | Some operations don't declare every 400/405/429 they can return, and validators enforce rules (minimum lengths, enums) the schema doesn't state. Schemathesis reports these as documentation findings, not server errors. |
| `405` `Allow` header | Lists one route's methods when several routes share a path (framework behaviour). |
| `GET /api/audit` default `limit` | 5,000 rows when the caller omits `limit` (the UI always sends a page size). Kept for API compatibility. |

## Operations

| Limitation | Detail |
|---|---|
| One worker per instance | Login rate limits, account lockout counters and the email-digest scheduler live in process memory. Running several workers or replicas multiplies limits and could send duplicate digests. |
| Startup timeout vs slow CPUs | The 3.1 -> 4.0 upgrade boot takes about 3 s on PostgreSQL (the 22 s reported earlier was defect C01, fixed); `DB_STARTUP_TIMEOUT_SECONDS` defaults to 60 s. |
| `SESSION_REQUIRE_JTI=false` by default | Keeps 3.x cookies valid during upgrade; turn on after the upgrade window. |
| Browser suites not in CI | They run locally (`pytest -m ui`). |
| One unexplained intermittent browser failure | The item-dialog accessibility scan failed once in Firefox (dark theme) in the final run, then passed 150 times in a row; its details were not captured. If it recurs, the scan's output names the rule. |
| Frontend unit coverage | Logic modules (backlog, board, estimates, chart maths, data loading) are unit-tested; the views themselves are covered by the browser suites (`tests/test_e2e_browser.py`, three engines) rather than component tests. |
| `starlette.testclient` deprecation | Tests warn that `httpx` support is deprecated in favour of `httpx2`; test-only, no runtime effect. |

## Container image

| Limitation | Detail |
|---|---|
| Debian packages with open CVEs | Trivy (2026-09-30): 0 critical, 0 in Python packages, 52 high in Debian 13 base packages (util-linux family, ncurses, systemd libs, curl, login, perl-base) that have no fixed version published. The build applies every available Debian security update; rebuild regularly to pick up fixes. |

## Not verified in this environment

| Area | Reason |
|---|---|
| Live Groq/OpenRouter, Gemini/RAG, local LLM | No keys / model (mocked tests pass) |
| Live Firebase push, GitHub branch operations | No credentials (mocked tests pass) |
| SMTP through a real provider (TLS, authentication) | No account; the plain-SMTP path was verified end to end against a local sink |
| Production host, DNS, TLS proxy, backups | Owner's infrastructure |

Verified since the first audit (Docker Desktop became available): image build,
`deploy.sh`, Compose health and restarts, database outage and recovery,
cgroup CPU/memory limits, HDD-like I/O throttling, a 10-minute soak, the
production-mode start-up guard, the Linux CI jobs, and a SonarQube analysis.
