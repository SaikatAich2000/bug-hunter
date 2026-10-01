# Requirement and feature traceability

Source of requirements: README "Features" and "Sprints & Agile", DEPLOYMENT,
SECURITY, `.env.example`, CHANGELOG 4.0. No external specification exists, so
this matrix covers the behaviour discoverable from the repository.

Test levels: **U** unit/API tests (`pytest -m "not ui"`), **B** browser
end-to-end (`tests/test_e2e_browser.py` and the other `ui` suites), **F** API
fuzzing (Schemathesis), **P** PostgreSQL scenario scripts, **L** load tests, **V** Vitest
frontend unit tests.

| # | Feature | Source | Implementation | Data | UI | Tests | Status |
|---|---|---|---|---|---|---|---|
| 1 | Work items (Bug, Requirement, Task, Epic, Story, Sub-task), shared `#N` counter, per-type statuses, environment for bugs | README Features | `app/routes/bugs.py`, `app/schemas.py`, `app/models.py` | `bugs` | ListView, BugModal | U: `test_item_types.py`, `test_routes_work_items.py`, `test_api*.py`; B: create/open item; F | PASS |
| 2 | Type conversion in place | README | `bugs.py` conversion paths | `bugs.item_type` | BugModal | U: `test_item_types.py` | PASS |
| 3 | Projects and events (managers, scoping) | README | `routes/projects.py`, `routes/events.py`, `app/access.py` | `projects`, `events`, `user_projects` | sidebar, EventsView | U: `test_events.py`, `test_project_access.py`; B: events view, event deep link | PASS |
| 4 | Item links (relates, blocks, duplicate) | README | `bugs.py` links | `bug_links` | BugModal | U: `test_links.py`; P: upgrade preserves links | PASS |
| 5 | Comments (rich text) and attachments (PostgreSQL storage, EXIF stripping, paste to attach) | README | `bugs.py`, `app/image_strip.py`, sanitizer in `schemas.py` | `comments`, `attachments` | BugModal | U: `test_richtext_forms.py`, `test_export_attachment_safety.py`; B: attachment staging (smoke), comment editor; P: attachment bytes preserved (sha256) | PASS |
| 6 | Plain-text descriptions (xOPS design) | code + tests | validators, `_bug_to_out_dict`, `plainTextFromHtml` | `bugs.description` | BugModal (no toolbar) | U: `test_bug_stores_and_returns_plain_text_description`; B: description editor has no toolbar | PARTIAL (product decision pending; see HUMAN_ACTIONS 2) |
| 7 | Bulk actions (status, priority, environment, delete) | README | `POST /api/bugs/bulk` | `bugs` | ListView bulk bar | U: `test_bulk.py` | PASS |
| 8 | Bulk import (xlsx template, CSV, per-row errors) | README | `app/bulk_import.py` | `bugs` | BulkImportModal | U: `test_bulk_import.py` | PASS |
| 9 | Reports builder (multi-sheet Excel) | README | `app/reports/*`, `routes/reports.py` | read | ReportsView | U: `test_reports*.py`; B: reports view renders | PASS |
| 10 | Agile opt-in per project, default board and workflow | README Sprints | `app/agile/boards.py`, `routes/agile.py` | `boards`, `board_columns`, workflow tables | SprintsView | U: `test_agile_foundation.py`; B: seeded through API; P: enable on legacy project | PASS |
| 11 | Active sprint board: columns mapped to statuses, drag to transition, column min/max, swimlanes, quick filters, flags | README Sprints | `app/agile/workflow.py`, `boards.py`, `views.py`, `routes/agile_board.py` | `bugs.status`, `board_columns`, `work_item_changes` | BoardPanel | U: `test_agile_jira_rules.py`, `test_agile_slices_3_4_5_7.py`; B: drag persists status, card lands in the column under the pointer (Chromium, Firefox, WebKit; SQLite and PostgreSQL) | PASS |
| 12 | Backlog: ranked backlog and sprint lists, drag/multi-select/menu/keyboard moves, sprint create/edit/start/complete/cancel, carry-over | README Sprints | `app/agile/backlog.py`, `ranking.py`, `sprints.py`, `routes/agile_board.py` (move) | `bugs.rank`, `bugs.rank_scope`, `sprints` | BacklogPanel, SprintDialogs | U: `test_agile_sprints.py`, `test_agile_jira_rules.py`, `test_backlog_rank_assignment.py`; V: backlog logic tests; B: drag into sprint, start, complete with carry-over | PASS |
| 13 | Hierarchy: Epic > Story/Task/Bug/Requirement > Sub-task, enforced on every write path; legacy Collection/Feature data converted on boot | README Sprints | `app/agile/itemtypes.py`, `hierarchy.py`, `integrity.py`, `upgrade.py` | `bugs.epic_id`, `bugs.parent_id`, `epic_details`, `work_item_changes` | HierarchyPanel, BugModal | U: `test_agile_integrity.py`, `test_agile_jira_rules.py`, `test_agile_upgrade.py`; P: `test_postgres_migration.py` (legacy upgrade, two boots) | PASS |
| 14 | Sprint Planning: capacity, days off | README Sprints | `app/agile/planning.py`, `routes/agile_planning.py` | `sprint_capacity` | PlanningPanel | U: `test_agile_slices_3_4_5_7.py`, `test_parity_fixes.py` | PASS |
| 15 | Releases & Labels | README Sprints | `app/agile/releases.py`, `taxonomy.py`, `routes/agile_taxonomy.py` | `versions`, `components`, `labels` | TaxonomyPanel | U: `test_agile_taxonomy_routes.py`; duplicate-name 409 tests | PASS |
| 16 | Reports: burndown, burnup, sprint report, velocity, CFD, control chart, Epic report, daily summary, workload, CSV export | README Sprints | `app/agile/reports.py`, `routes/agile_reports.py` | `work_item_changes`, `sprints` | ReportsPanel, charts | U: `test_agile_reports_jira.py` (hand-computed Jira figures), CSV injection escaping; V: chart math tests; B: every report renders, sprint report after a real lifecycle (committed 8, completed 5, not completed 3) | PASS |
| 17 | Idempotency keys on lifecycle/bulk endpoints | README Sprints | `app/agile/idempotency.py` | `idempotency_keys` | - | U: agile tests | PASS |
| 18 | Notifications: bell, email (per event or digest), push; links to the item | README | `notification_service.py`, `email_service.py`, `jobs/email_digest.py`, `push_service.py`, deep-link handling | `notifications`, `push_subscriptions` | bell, profile menu | U: `test_notifications.py`, `test_email_digest.py`, `test_push.py`, `test_fcm_transport.py`; B: `#bug=`/`#event=` links incl. through login | PASS (live SMTP/FCM BLOCKED) |
| 19 | Audit log (retained after deletes) | README | `routes/audit.py`, `Activity` | `activity_log` | AuditView | U: `test_audit_search.py`; B: audit view; L: audit scenario | PASS |
| 20 | Sessions admin (list, revoke one device) | README | `routes/sessions.py` | `sessions` | SessionsView | U: session tests; B: sessions view, revoke smoke test | PASS |
| 21 | Login, three roles, password reset, lockout, breach check | README, SECURITY | `app/auth.py`, `routes/auth.py`, `account_lockout.py`, `password_breach.py` | `users`, `password_reset_tokens` | login/reset pages | U: `test_auth_*`, `test_role_policy.py`, `test_password_policy.py`; B: login, role gating, logout, off-site `next` refused | PASS |
| 22 | Git integration (Story feature branches, exact removal) | README Git | `app/git/*`, `routes/git.py` | `git_*` tables | GitBranchesPanel, ProjectGitSettings | U: `test_git_*.py` (mocked provider); F: config endpoints | PASS (live GitHub BLOCKED) |
| 23 | Observability (OTLP traces/metrics/logs, JSON logs, redaction) | README | `app/telemetry.py` | - | - | U: `test_telemetry.py` | PASS (live collector NOT TESTED) |
| 24 | API docs (self-hosted Swagger/ReDoc; off in production unless enabled) | README | `app/main.py`, `app/api_docs.py` | - | `/docs`, `/redoc` | U: `test_hardening_extra.py`; smoke: `/docs`, `/redoc`, `/openapi.json` 200 in development | PASS |
| 25 | Sleuth assistant (rules, classifier, optional local/cloud LLM, tool calling with confirmation) | README Sleuth | `app/chatbot/*` | chat tables | SleuthPanel | U: 20+ `test_sleuth_*` / `test_chat_*` files; smoke `/api/chat/ask` | PASS (live LLM BLOCKED) |
| 26 | UI: dark/light themes, responsive layout | README | `styles.css`, shell | - | all | B: every view renders; 375 px no horizontal overflow (list, sprints, events); axe WCAG AA in both themes | PASS |
| 27 | Production start-up guard | README, SECURITY | `app/main.py::_production_config_errors` | - | - | U: `test_hardening_extra.py`, `test_config*.py` | PASS |
| 28 | Additive, idempotent schema migration; multi-replica safety | README Live-data safety | `app/database.py::init_db` | all | - | U: `test_item_types.py`, `test_startup_degraded.py`; P: 3.1 -> 4.0 upgrade (22 checks), concurrent boot x3 | PASS |
| 29 | Release hygiene (`APP_VERSION` single source, release archive) | CONTRIBUTING | `scripts/make_release.py`, tests | - | version badge | U: `test_release_hygiene.py` | PASS |
| 30 | Local run and secrets generation | README Local development | `scripts/run_local.*`, `scripts/gen_local_env_secrets.py` | `.env` | - | clean-copy run: launcher builds env, server healthy, 30/30 smoke | PASS |
| 31 | Docker deployment (0.5 CPU / 512 MB target) | README Deployment | Dockerfile, compose, deploy.sh, down.sh | volume | - | hadolint, ShellCheck; L: Job Object limits at the Compose split | PARTIAL (Docker itself BLOCKED) |

## Gaps found by the matrix

- Documented before this pass but not implemented: notification deep links
  (row 18) - now implemented.
- Implemented but undocumented before this pass: plain-text descriptions
  (row 6), `SESSION_REQUIRE_JTI`, `TRUST_PROXY_HOP_COUNT`, lockout and
  breach-check settings - now documented.
- Tests that did not represent production behaviour: the old sprints browser
  suite and the dead-helper unit tests - replaced/removed.
- Backend endpoints unused by the UI: the Sleuth tool-calling routes and parts
  of the agile reporting API are used only by Sleuth or API clients; they are
  covered by unit tests and fuzzing.
