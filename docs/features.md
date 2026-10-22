<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Features and roles

Everything the app can do, and who can do it.

> [← Back to the README](../README.md)

## Features

| Area | What it does |
|---|---|
| Work items | Bugs, requirements and tasks share one `#N` counter with the agile types (Epic, Story, Sub-task). A tab strip filters KPIs, columns and analytics by type. Each type has its own statuses; bugs also have a DEV/UAT/PROD environment. Items can be converted between types in place. |
| Sprints & Agile | Opt-in per project, Scrum-style: ranked backlog and sprint planning, the active sprint board, Epic → issue → Sub-task hierarchy, board settings, and burndown/sprint/velocity/flow reports. See [Sprints & Agile](agile.md#sprints--agile). |
| Projects & events | Projects group your work. Events group items for a standup or review and have one or more managers. |
| Item links | Link items together: relates, blocks, or duplicate. |
| Comments & attachments | Rich-text comments (bold, italic, lists, code, quotes) and plain-text descriptions that keep line breaks. Images, PDF, video, Office documents and txt/csv/log/zip files, up to 50 MB each, are stored in the database. Only listed extensions are accepted and each file's leading bytes must match its extension (`app/upload_policy.py`; SVG and HTML are refused). Pasted images become real attachments, and image metadata is stripped. |
| Bulk actions | Change status, priority or environment, or delete, across many items at once. |
| Bulk import | Download an Excel template, fill it in (or use CSV), and import. Every row is validated on its own and errors are reported per row. |
| Reports | A report builder (manager/admin) that exports a multi-sheet Excel file, plus agile reports (burndown, burnup, velocity, cumulative flow, control chart, workload, scope change, epic progress). |
| Notifications | In-app bell, email (per event or one daily digest), and optional browser/FCM push. Every notification links straight to its item or event, through the login page if needed. |
| Audit log | Item, project, user, sprint and board changes, sign-ins (including failures and lockouts) and password events are recorded for admins and managers. Entries stay even after an item is deleted. |
| Sessions | Admins see every active session (user, role, IP, browser, time) and can log out a single device. |
| Login | Local accounts with bcrypt-hashed passwords, three roles (admin / manager / user), and email password reset. |
| Git integration | Optional: create a deterministic `feature_<id>_<slug>` branch in a GitHub repository for a User Story, and remove exactly that branch again. |
| Observability | Optional OpenTelemetry export (traces, metrics, logs) to SigNoz or any OTLP/gRPC collector. |
| API docs | Self-hosted Swagger UI (`/docs`) and ReDoc (`/redoc`) in development. Production hides them unless `ENABLE_API_DOCS=true`. |
| Sleuth assistant | Answers plain-English questions and runs actions (with confirmation). Runs locally by default; see [Sleuth](sleuth.md#sleuth). |
| Onboarding & help | A welcome screen, a resumable product tour and a help center with bundled guides. Progress is stored per user on the server (`GET`/`PUT /api/me/preferences`), so it follows you across browsers. |
| UI | A dark theme and a light theme, responsive layout, auto-refresh, motion that respects `prefers-reduced-motion`. |

### Roles

- **Admin** has full access, including user management and all deletes.
- **Manager** can edit any item or event, enable Agile, and manage sprints, boards, releases and labels. Managers can't delete items, grant the admin role, or edit existing admins.
- **User** can create and edit Bugs, User Stories and Sub-tasks, move them on the board, and view and export agile reports. Tasks and Requirements are read-only to users, and sprint lifecycle and board/workflow/taxonomy configuration need a manager.

Deleting items, editing/deleting comments, and deleting attachments are
admin-only for every type. Everyone sees only the projects they are a member of,
except admins.
