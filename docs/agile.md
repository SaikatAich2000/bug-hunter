<p align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg"><img alt="Bug Hunter" src="assets/logo-light.svg" width="64"></picture></p>

# Sprints and Agile

Scrum boards, backlog, sprints, epics and reports.

> [← Back to the README](../README.md)

## Sprints & Agile

Sprints follow the usual Scrum board workflow. Agile is **off for every
project** until a manager or admin opens **Sprints**, picks the project and
clicks **Set up Scrum board**. That creates the project's board (To Do, In
Progress, Testing, Done) and a backlog ranked from its open issues; nothing is
deleted or changed.

**Hierarchy.** There are three levels:

- **Epic**: a large body of work. Epics are never in a sprint.
- **Standard issues**: Story, Task, Bug and Requirement. Each can belong to one
  Epic and is planned into sprints.
- **Sub-task**: always has a standard issue as its parent, and always follows
  that parent's sprint and Epic.

The server enforces these rules on every write path (API, bulk edit, import,
Sleuth), and records each change to status, sprint, estimate, Epic, parent,
type and project in a change log the reports are built from.

| Tab | What it does |
|---|---|
| Backlog | Future and active sprints above the ranked backlog. Drag issues (or several ticked issues) to rank them or plan them into a sprint; the ⋯ menu and the keyboard do the same without dragging. Create, edit, start and complete sprints here. Starting needs at least one issue and opens the board. Completing keeps finished issues with the closed sprint and moves the rest to the backlog, a future sprint or a new sprint. |
| Active sprint | The board: columns map to statuses, and dragging a card moves it to that column's status. Column min/max (WIP) limits, swimlanes (stories, assignees, Epics, priority), quick filters and flags. An issue is done when its status is in the right-most column. |
| Epics | Each Epic with its issues and their sub-tasks, progress by estimate, and the issues not yet in an Epic. Link or unlink issues here. |
| Reports | Burndown, burnup, sprint report, velocity, cumulative flow diagram, control chart, Epic report, daily summary and workload, each with CSV export. |
| Releases & labels | Versions/releases, components and labels. |
| Capacity | Per-sprint capacity: each member's hours per day and days off against the committed estimate. |
| Board settings | Columns and their statuses, column limits, estimation statistic (story points, hours or issue count), working days and time zone (used by the burndown guideline), and quick filters. |

Reports replay the change log, so a sprint's commitment is what it held when
it started, scope added or removed afterwards is shown separately, and
re-estimates count as scope changes. Lifecycle and bulk endpoints accept an
`Idempotency-Key` header so retried requests are safe.

**Upgrading from the earlier Sprints preview.** The first boot converts the
retired Collection and Feature levels into labels on the issues they grouped
(a Story under a Feature joins that Feature's Epic), turns sub-tasks without a
valid parent into Tasks, takes Epics out of sprints and re-ranks every issue in
the list it sits in. The conversion runs once and is safe to repeat.
