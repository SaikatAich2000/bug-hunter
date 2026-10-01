# Cleanup report

Every removal was preceded by a reference search across `app/`, `frontend/src`,
`tests/`, `scripts/`, CI, Compose, docs and the packaging allowlist.

## Removed files

| Path | Reason | References checked |
|---|---|---|
| `tests/test_playwright_sprints.py` | Needed a hand-started server with existing data, printed "[OK]" instead of asserting, called `sys.exit(1)`; replaced by `tests/test_e2e_browser.py` | `pyproject.toml` ruff override (retargeted), docstring in `test_playwright_project_state.py` (updated) |
| `.schemathesis/`, `.hypothesis/`, `frontend/coverage/` | Tool caches created by this audit's runs (moved to the scratchpad or deleted); now ignored | - |

(The port to 4.0 had earlier removed the TypeScript sources, `tsconfig.json`,
the old build assets and the empty `EpicsPanel.jsx`; see CHANGELOG.)

## Removed code

| Location | What | Why |
|---|---|---|
| `app/auth.py` | `can_delete_project`, `can_manage_users`, `can_delete_user`, `can_view_audit`, `can_manage_sessions`, `get_current_user_optional` | Referenced only by their own tests; routes enforce the same rules with `require_admin` / `require_manager_or_admin`. The cookie-resolver test now targets the live `_user_from_request`. |
| `app/config.py`, `docker-compose.yml` | `API_KEY` setting | Read, passed through, never used; suggested protection that doesn't exist |
| tests (6 files) | `monkeypatch.setenv("API_KEY", "")` | Referred to the removed setting |
| `scripts/load_test.py` | `ChangeMe123!` password fallback | No install uses it; now requires an explicit password |
| `pyproject.toml` | pytest-asyncio "auto mode" comment; unused `slow` and `migration` markers | No async tests, no marker users |
| `frontend/package.json` | `happy-dom` dev dependency | Unused (tests run on jsdom); carried critical advisories |

## Comment cleanup

About 170 comments and docstrings cited sections and "slices" of an xOPS
planning document that was never part of this repository ("(Section 6.5 of
the Agile plan)", "Slice 3 —", "see docs/AGILE_DISCOVERY_NOTES.md"). 122 were
removed by pattern and ~50 rewritten by hand; the explanatory text was kept.
A comparison against the xOPS originals confirmed the rewritten sentences stay
grammatical (one lost full stop was restored). No runtime string contained a
citation (checked with the Python tokenizer).

No `TODO`, `FIXME`, `HACK` or `XXX` markers, stray `print()` calls or
`console.log` calls exist in application code.

## Ignore rules added

`.gitignore` and `.dockerignore`: `.schemathesis/`, `.hypothesis/`.

## Deliberately retained

| Item | Why |
|---|---|
| ORM models `FeatureAssignee`, `CollectionAssignee` | Flagged by Vulture as unreferenced classes, but they define association tables used through `secondary=` |
| `app/static/swagger-ui/*` | Self-hosted API docs assets (CSP forbids a CDN); third-party TODOs inside are upstream code |
| `tests/test_agile_slices_3_4_5_7.py` and similar file names | Renaming test files has no benefit and loses history |
| `app/chatbot/llm.py` local-LLM path | Optional feature, dormant without a model file; documented |
| `bug_hunter.db`, `.env` in the working tree | The owner's local instance (ignored by git) |
