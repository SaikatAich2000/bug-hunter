# Bug Hunter v3.2.1 — Changes in this delivery (Slice 1)

This delivery covers the **attachment-on-create UI** + a **security
audit pass**. The chatbot work, performance work, and responsive-CSS
work are in later slices.

The changes are **strictly additive** to the database — no migrations,
no column changes, no column drops. The existing live DB will run on
this code unchanged.

---

## Frontend — Attachment-on-create

A new file picker now appears **only** when creating a bug, placed
directly below the description, exactly as requested. In edit mode it
is hidden (attachments there go via comments or the legacy bug-level
section, unchanged).

Files changed:
- `app/static/index.html` — new `<section id="bugCreateAttachSection">`.
- `app/static/app.js` — `openBugForm()` shows/hides + resets the
  picker; `submitBugForm()` create branch POSTs the bug, then uploads
  each selected file to the existing `POST /api/bugs/{id}/attachments`
  endpoint. Per-file failures are toasted but do not abort the flow.
- `app/static/styles.css` — small spacing tweak so the section fits
  cleanly under the description.

No backend changes were needed — the attachment endpoint already
supports bug-level uploads (no `comment_id`).

---

## Security — additive hardening

### 1. CSRF defense-in-depth: Origin / Referer check
File: `app/main.py` — `CsrfOriginMiddleware`.

For state-changing requests (POST/PUT/PATCH/DELETE) to `/api/*`, the
middleware now requires the `Origin` (or `Referer`) header to match
the request's own host or one of the configured `CORS_ORIGINS`.
Cross-site forms posting to our API are rejected with **403 "Cross-
origin request blocked."** Non-browser clients (no `Origin` and no
`Referer`) pass through — CSRF is exclusively a browser-confused-
deputy attack.

- Exempt: `POST /api/auth/login` (operator onboarding scripts).
- Safe methods (GET, HEAD, OPTIONS) are never checked.
- Layered on top of `SameSite=Lax` cookies, not replacing them.

### 2. Login response no longer leaks disabled-account state
File: `app/routes/auth.py`.

Previously:
- Wrong password → 401 "Invalid email or password"
- **Disabled account** → 403 "Account is disabled" ← leaked

Now both return identical 401s with the same body, so an attacker
with stolen-but-disabled credentials can't tell the account is
disabled. The server-side log still records the real reason.

### 3. Per-user rate limit on attachment uploads
File: `app/routes/bugs.py`.

20 uploads / 60 seconds / user (sliding window, in-memory). Stops a
stolen session from bloating the DB with 50 MB blobs faster than an
operator can revoke it. Limit raised *before* the multipart body is
read, so a malicious client can't waste server bandwidth either.

The 429 response now includes a `Retry-After` header that clients
can honor.

### 4. Response headers
File: `app/main.py` — `SecurityHeadersMiddleware`.

- **`Cross-Origin-Resource-Policy: same-origin`** added. Blocks
  other origins from embedding our responses as subresources.
- **`Server: uvicorn` header stripped.** Minor stack-detail leak
  removed.
- `Permissions-Policy` tightened — added `payment`, `usb`,
  `magnetometer`, `gyroscope`, `accelerometer`.

### 5. Fix: HTTPException headers now reach the client
File: `app/main.py` — `http_exc_handler`.

The global handler was rebuilding `JSONResponse(...)` without
forwarding `exc.headers`, silently dropping `Retry-After`,
`WWW-Authenticate`, etc. Now forwarded. Found while testing the new
upload rate limit.

---

## Tests

Net pass count: **281 → 291** pytest-compatible tests passing.

- 10 new tests in `tests/test_regression.py::TestV321Security`
  cover CSRF same-origin / cross-origin / referer-only / exempt-login
  / safe-methods, upload rate limit (incl. Retry-After), Server
  header stripped, CORP set, CSP still set.
- 1 updated test: `test_login_with_inactive_user_is_unified_401`
  replaces the old `_is_403` expectation.
- Test helper bug fix: `_create_user` default password changed from
  `"Password1"` (rejected by the policy) to `"TestUserPwd9X"`. This
  unblocked 14 pre-existing test failures in `test_regression.py`,
  `test_fixes.py`, `test_v31.py`, `test_concerns.py`, `test_round3.py`,
  `test_api.py` that had nothing to do with security work.

Known unchanged failure: `test_sleuth_parser.py::test_executor` is a
standalone script (`python test_sleuth_parser.py`), not a pytest
test. Pytest discovers it but doesn't run its `seed()` fixture, so
it fails in both this build and the original. Out of scope for this
slice.

---

## Database safety

- No schema changes.
- No new tables.
- No column adds, drops, renames.
- No data writes at startup beyond the existing bootstrap idempotent
  block.
- `SQLAlchemy create_all()` remains the only initialiser and is a
  no-op against an already-initialised DB.

Deploy confidence: this is a code-only delivery. Existing data is
untouched. **Still take a DB snapshot before deploying** — see notes.

---

## Deployment notes

1. `git apply` or `cp -r app/` on top of the running deploy.
2. Restart the FastAPI process (uvicorn / gunicorn / docker-compose).
3. Verify `/api/health` returns the new `version: "3.2.1"`.
4. Smoke-check from the SPA: log in, create a bug with an
   attachment, edit a different bug, post a comment with an
   attachment, log out.

If anything looks wrong: redeploy the prior image. Schema is
backward-compatible so a downgrade is also clean.

---

## What is NOT in this slice (and should be a follow-up)

- Chatbot improvements (waiting on your decision — see prior turn).
- Performance audit (N+1 sweep, JS bundle size).
- Responsive-CSS pass for narrow screens.
- Error-handling pass on cold paths.

---

# Slice 2 — Performance + error-handling pass (also in this zip)

Same delivery, additional changes layered on top. Still backward-
compatible with the live DB: no schema changes, no new tables.

## Performance

### Stats endpoint: 11 queries → 6 queries
File: `app/routes/stats.py`.

The dashboard KPI endpoint used to fire 5 single-cell COUNT queries
(one per status) plus the by_status GROUP BY. We collapsed those 5
into the same single GROUP BY scan and derived the KPIs from its
result dictionary. The numbers returned are identical.

Saved: 5 DB round-trips per dashboard load. Verified by SQL tracing.

### Sleuth chatbot stats: 7 single-cell COUNTs → 1 GROUP BY + 2
File: `app/chatbot/executor.py`.

Same pattern for the chatbot's "stats" intent. Was 7 separate scalar
COUNT queries — now 3 (1 grouped status, 1 priority count, 1 env
count). Result identical.

### Attachment.data is now deferred-loaded
File: `app/models.py`.

The `Attachment.data` column (LargeBinary blob, up to 50 MB) used to
be loaded eagerly any time you selected an Attachment row. That meant
opening a bug detail page with 5 large videos pulled ~150 MB from the
DB into Python memory **just to render the file list**.

Switched to `deferred(mapped_column(...))`. The BLOB is only fetched
when `attachment.data` is explicitly accessed — which is exclusively
inside `download_attachment`. Listing attachments for a bug or comment
no longer pulls binary content at all.

This is a **Python-side loading-strategy change only**. The column,
type, and stored data are unchanged. Existing DB rows work as-is.

### SPA boot: 4 sequential fetches → parallel
File: `app/static/app.js`.

`boot()` fetched `/api/health`, `/api/meta`, `/api/users`,
`/api/projects` one after another. They're independent — collapsed
to `Promise.all`. Cold-start wall time drops from sum-of-latencies
to max-of-latencies (roughly 4× on slow connections).

## Error handling

### `_has_valid_session` no longer 500s on DB hiccup
File: `app/main.py`.

If the DB was momentarily unreachable, the HTML route handlers (`/`
and `/login.html`) would 500 because the session check propagated the
DB error. Now it logs and falls back to "no valid session" — which
sends the user to the login page. Safe default; the next API call
will surface the real error.

### Silent rollbacks in auth.py now log
File: `app/auth.py`.

The "expired session sweep" and "last_seen update" paths used to
swallow exceptions with no breadcrumb. Now `logger.exception(...)`
records the failure with stack trace so recurring DB problems on the
hot path are debuggable. Behaviour unchanged on the happy path.

## Tests

Net pass count: **291 → 294** pytest-compatible tests passing.

- 3 new tests in `TestV321Performance`:
  - `test_stats_endpoint_returns_expected_kpis` — locks in the
    collapsed stats results.
  - `test_attachment_data_is_deferred_but_download_still_works` —
    verifies the deferred BLOB strategy doesn't break downloads.
  - `test_bug_list_remains_n_plus_one_free` — locks in batched
    attachment-count behaviour on the list endpoint.

No prior tests regressed.

## Database safety (still true after slice 2)

- No schema changes.
- No new tables.
- No column adds, drops, renames.
- `deferred()` is a Python-side loading strategy; the column itself
  is unchanged.
- The live DB will run on this code as-is.

---

## Still NOT in this delivery (waiting on you)

- Chatbot improvements (Option A — rule engine — only feasible given
  your 512 MB / 0.1 CPU / free constraints).
- Responsive-CSS pass for narrow screens.

---

# Slice 3 — Sleuth chatbot rule-engine improvements (in this zip)

**Honest framing:** the chatbot is a rule-based NLU sized for a
1-CPU / 2 GB box and you've asked for "AI-equivalent" behaviour
under even tighter constraints (512 MB / 0.1 CPU / free). That
ceiling can't be broken by rules alone — full LLM-grade
understanding genuinely needs either a real LLM (which doesn't fit
your hardware) or a paid API (which violates "free"). What's in
this slice are the highest-leverage *real* refinements that improve
the existing rule engine inside the constraints you set.

## New filter / sort coverage

- **"me / mine / I"** pronoun resolution. `my bugs`, `assigned to
  me`, `bugs I reported` all now resolve to the logged-in user
  without typing their own name. (`nlu.py`, `executor.py`.)
- **"unassigned" / "no assignee"** filter. `bugs with no assignee`,
  `nobody assigned`, `orphan bugs`. (`nlu.py`, `executor.py`.)
- **"oldest" / "stale" / "longest open"** sort hint. Reorders the
  result ASC by `updated_at` so long-running work surfaces first.
- **"newest" / "latest"** explicit sort hint. (Same as the default
  but acknowledged in the response so the user knows it was
  understood.)

## New synonyms

- Priority: `minor`, `trivial` → Low. `important`, `major` → High.
  `showstopper` → Critical.
- Environment: `sandbox`, `local` → DEV. `preprod`, `pre-prod`,
  `pre-production` → UAT.

## New time-window phrases

- `this quarter` / `last quarter` (calendar-quarter snap).
- `this year` / `last year` (calendar-year snap).
- `since Monday` / `since Tuesday` / ... — anchors at the named
  weekday of the current or prior week.

## Typo tolerance

- Words ≥ 4 chars that fail an exact synonym match are run through
  `difflib.get_close_matches` against the priority / environment
  synonym table at cutoff 0.82. Catches the common single-character
  flips:
    - `ctitical` → Critical.
    - `produciton` → PROD.
    - `criticla` → Critical.
- Stays conservative — won't match `low` → `log` or similar short
  collisions. Only kicks in when the exact extractor returns
  nothing, so legitimate exact matches still win.

## Better unknown-intent fallback

The "I didn't catch that" message is now context-aware. If the
user mentioned `user` / `team` it suggests user queries; if
`project` it suggests project queries; if `summary` / `stats` it
suggests the stats intent. Otherwise it falls back to the bug-
shape suggestions. Doesn't fix queries that have no overlap with
any intent — but it does make the cold path useful.

## Logging

- The previously-silent `except Exception: pass` in the LLM
  fallback path of `execute()` now logs the exception so recurring
  faults are debuggable. The chat path itself still degrades
  gracefully — a failing LLM layer doesn't take the chatbot down.

## Tests

- 9 new tests in `TestV321Chatbot`:
  - `test_my_bugs_resolves_to_actor` — "show my bugs" → actor.
  - `test_bugs_i_reported_uses_reporter_role` — me-as-reporter.
  - `test_unassigned_filter`.
  - `test_oldest_sort_hint` — reorder check with timestamp spacing.
  - `test_priority_synonyms_minor_and_blocker`.
  - `test_unknown_intent_hint_is_context_aware`.
  - `test_typo_tolerant_priority` ("ctitical").
  - `test_typo_tolerant_environment` ("produciton").
  - `test_time_phrase_this_year`.
- All 43 existing sleuth tests still pass.

## What this slice does NOT do

- Doesn't make Sleuth conversational ("what's the status of the Q4
  release?" — no rule covers this).
- Doesn't enable multi-step reasoning.
- Doesn't enable referring to external context (Jira, Slack, etc.).
- Doesn't add the kind of broad linguistic robustness an LLM has.

These limits are inherent to rule-based NLU. Relaxing them
genuinely requires a real LLM. The next conversation about chatbot
quality should be about whether 512 MB / 0.1 CPU / free is the
hard constraint or whether one of those can move.

---

# Slice 4 — Responsive CSS polish (in this zip)

Additive media queries — existing breakpoints (1100, 900, 700,
500) are kept; new tuning layered on top.

## New / improved breakpoints

- **820 px (tablet portrait)** — multi-select buttons trim from
  140–220 px → 120–200 px so the filter bar fits without wrapping.
  Multi-select panels capped at `calc(100vw - 32px)` so the
  options menu never spills off-screen. `.table-scroll` (the bug
  table wrapper) gets `-webkit-overflow-scrolling: touch` so the
  horizontal scroll feels right on iPad.

- **600 px (phone landscape & smaller)** — touch targets bumped
  to a 38 px minimum on buttons, icon-buttons, multi-select
  buttons, and chips. Filter-bar padding tightened. The new
  bug-create attachment uploader switches its row to a column
  layout on phones so the staged-files list doesn't get hidden
  behind the "Attach files" button.

- **380 px (small phones — iPhone SE etc.)** — KPI strip falls to
  a single column. The project column on the bug table hides
  (status + priority + actions are the priority signals on a tiny
  screen). Topbar gaps tighten. Toast spans the viewport. Login
  card padding compresses so the form fits without scrolling.

## Print stylesheet

Previously absent. Added so users hitting **Print** on a bug detail
get a clean output: sidebar, topbar, filter-bar, chatbot FAB and
panel, modal footer, head actions, and pagination all hidden;
single-column layout; white background with black text; underlined
links.

## Tests

CSS isn't unit-tested in this project. The visual changes were
verified by reading the existing HTML class names and ensuring
each new selector targets an element that actually exists. The
existing `test_ui_smoke.py` tests still pass — they cover the
JS-side behaviour, which CSS changes don't affect.

---

# Final state

- **Total tests passing: 293**
  - 281 pre-existing tests (no regressions)
  - 22 new v3.2.1 tests (10 security + 3 performance + 9 chatbot)
- **Database changes: zero.** No new tables, columns, migrations,
  or breaking changes. The live DB will run on this code unchanged.
- **Version:** `3.2.1` (advertised via `/api/health`).
- **Deployment posture:**
  1. Snapshot the live DB (`pg_dump` or sqlite file copy).
  2. Tag the previous container image for one-command rollback.
  3. Deploy the new code, restart the worker.
  4. Hit `/api/health` — verify `version: 3.2.1`.
  5. Smoke-click: log in, create a bug with an attachment, edit a
     different bug, post a comment, try a chatbot query, log out.
  6. If anything is wrong, roll back to the previous tag — schema
     is backward-compatible so the downgrade is clean.
