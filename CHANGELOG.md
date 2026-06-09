# Changelog

All notable changes to Bug Hunter. Format roughly follows
[Keep a Changelog](https://keepachangelog.com/). The project predates
this file; releases older than v2.4 live in git history only.

## [2.9] — 2026-06-09

**Reports view + Sleuth report intent.** No DB schema change — the
"who resolved this item and when" attribution is derived from the
existing `activity_log` table, so the production database is
byte-for-byte untouched on upgrade.

- *Reports sidebar view* — Jira-style report builder behind a manager /
  admin role gate (`data-needs-role="manager"`). Nine report types:
  Item Detail Export, Resolution Throughput, Pending Items Snapshot,
  Status Distribution, Priority Distribution, Project Breakdown,
  Aging, Created-vs-Resolved Timeline, Time to Resolution. Universal
  filter set (date range, item types, statuses, priorities,
  environments, projects, assignees, reporters, free text).
- *Excel export* — multi-sheet XLSX with the aggregated view, a
  drill-down "Items" sheet (raw rows for the manager who wants
  everything), and an audit "Filters Applied" sheet. CSV-injection
  defense (G2) migrated from the CSV export to the XLSX writer.
- *Sleuth report intent* — natural-language report queries reuse the
  same engine. *"report of who solved how many bugs last week"*,
  *"pending bugs report"*, *"throughput last 7 days"* etc. produce an
  inline preview plus a downloadable spreadsheet.
- *Legacy CSV export retired* — `/api/bugs/export.csv` removed; the
  sidebar "Export CSV" button replaced by the Reports entry. Tests
  that exercised the CSV path moved to the XLSX path with the same
  invariants.
- *Comment composer keeps attachments separate* — files attached via
  the comment composer always become comment attachments, never bug-
  level. Bug-level attachments still have their own 📎 uploader.
- *Delete-attachment button no longer submits the bug form* — added
  the missing `type="button"` so a comment-attachment delete clicks
  cleanly without saving the bug or closing the modal.

## [2.8] — 2026-06-04

**Security hardening** — OWASP audit + remediation. Eight items, all
additive, no DB schema change.

- *Login timing equalised* (G1) — bcrypt runs even for unknown emails so
  response latency stops leaking account existence.
- *CSV export defanged* (G2) — cells starting with `=`/`+`/`-`/`@`/`\t`/`\r`
  prefixed with `'` to neutralise Excel formula injection.
- *Body-size middleware* (G3) — 60 MB cap (env-tunable via
  `MAX_REQUEST_BODY_BYTES`) rejects oversized requests before the body
  is read.
- *X-Forwarded-For trust gate* (G4) — `auth.py` now honours
  `TRUST_PROXY_FORWARDED_FOR` like the rest of the stack, so spoofed
  XFF can't poison the audit IP on direct deploys.
- *PII out of logs* (G5) — INFO log lines mask emails to `a***@domain`.
- *Per-account lockout* (T3) — 10 fails / 15 min triggers a 15-min 429.
  Email-keyed so unknown-email and known-email behave identically. Env
  tunables: `LOGIN_FAIL_LIMIT`, `LOGIN_FAIL_WINDOW_SECONDS`,
  `LOGIN_LOCKOUT_SECONDS`.
- *Breach-corpus check* (T4) — HaveIBeenPwned k-anonymity API rejects
  known-pwned passwords on every set-password path. Fail-open on
  network errors; off-switch via `PASSWORD_BREACH_CHECK_ENABLED=false`.
- *Image EXIF strip* (T6) — Pillow drops GPS / camera-serial / XMP /
  ICC from uploaded JPEG / PNG / GIF / WEBP / BMP / TIFF. Non-images
  pass through. +47 security tests (518 total).

## [2.7]

Quality, security, stability. SonarQube quality gate green (0 issues,
0 unreviewed hotspots, ~84% backend coverage). Cognitive complexity
refactored across 14 large functions (11 Python, 3 JS). 10 security
hotspots remediated in code rather than via UI review. Mechanical SPA
modernization (`Number.parseInt`, optional chaining, `replaceAll`,
`dataset`, `globalThis`). +66 new unit tests (471 total).
Accessibility polish (8 `aria-label` / role fixes). CSS contrast and
deduplication. Zero schema changes; production DBs byte-for-byte safe.

## [2.6]

Rich-text editor for descriptions and comments (B / I / U / lists /
blockquote / code / image paste-as-attachment), with Chrome-148
workaround that hand-rolls inline formatting in DOM code instead of
`execCommand`. Custom calendar / date-picker and custom dropdowns
replace browser-native widgets. Sidebar names are clickable to edit.
Newest-first comments / attachments / tasks. Audit log loads up to
5 000 rows with *Load older entries* button.

## [2.5]

Per-item-type status sets. Admin-curated comments and attachments.
Post-creation 📎 *Add attachment* button on the item detail. Global
blocking loader on every server action. Card-style controls bar on
Events / Sessions / Audit views.

## [2.4]

Audit history survives bug deletion (`activity_log.bug_id` becomes
`ON DELETE SET NULL` for fresh installs). Audit search LEFT-JOINs the
bugs table so live titles and types are searchable. Frontend-level
read-only mode for restricted users with a clear banner.

---

Older releases: see git history (`git log --oneline`).
