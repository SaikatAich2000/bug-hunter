/** Test support: a Scrum-enabled project on top of the fake API (see backend.jsx). */
import { screen, waitFor } from "@testing-library/react";
import { expect } from "vitest";
import { createBackend, empty, mountApp } from "./backend";

export const COLUMNS = [
  { id: 1, name: "To Do", category: "todo", statuses: ["New"], wip_limit: null, min_cards: null, position: 0 },
  { id: 2, name: "In Progress", category: "in_progress", statuses: ["In Progress"], wip_limit: 1, min_cards: null, position: 1 },
  { id: 3, name: "Done", category: "done", statuses: ["Resolved"], wip_limit: null, min_cards: 1, position: 2 },
];

export const BOARD = {
  id: 9, project_id: 1, name: "Alpha Board", estimation_mode: "story_points", swimlane_mode: "none",
  columns: COLUMNS, parallel_sprints: false, feature_flags: {}, version: 1,
  timezone: "UTC", working_weekdays: [1, 2, 3, 4, 5],
};

export function card(over = {}) {
  const id = over.id ?? 301;
  return {
    id, display_id: `ALP-${id}`, title: `Story ${id}`, item_type: "Task", status: "New", column_id: 1,
    status_category: "todo", priority: "Medium", can_edit: true, flagged: false, version: 1, assignees: [],
    epic_id: null, parent_id: null, story_points: 3, updated_at: new Date().toISOString(), labels: [],
    subtask_count: 0, subtasks_done: 0, ...over,
  };
}

export function boardView(over = {}) {
  const cards = [
    card({ id: 301, title: "Wire login", column_id: 1, status: "New", assignees: [{ id: 1, name: "Ada Admin" }] }),
    card({ id: 302, title: "Fix export", column_id: 2, status: "In Progress", status_category: "in_progress", priority: "High", story_points: null }),
    card({ id: 303, title: "Ship docs", column_id: 3, status: "Resolved", status_category: "done", priority: "Low", flagged: true }),
  ];
  const columns = COLUMNS.map((c) => ({ ...c, cards: cards.filter((k) => k.column_id === c.id) }));
  return {
    sprint: { id: 5, name: "Sprint 5", state: "active", goal: "Ship it", start_date: "2026-01-01", end_date: "2099-01-14", version: 3 },
    active_sprints: [{ id: 5, name: "Sprint 5" }],
    columns, unmapped: [], epics: [{ id: 40, title: "Onboarding", color: "#3366ff", archived: false }],
    ...over,
  };
}

export function planningData(over = {}) {
  return {
    epics: [{ id: 40, title: "Onboarding", color: "#3366ff", archived: false }],
    sprints: [
      { id: 5, name: "Sprint 5", state: "active", goal: "Ship it", start_date: "2026-01-01", end_date: "2099-01-14", version: 3,
        issues: [card({ id: 301, title: "Wire login" }), card({ id: 302, title: "Fix export", status: "In Progress", status_category: "in_progress" })] },
      { id: 6, name: "Sprint 6", state: "future", goal: "", start_date: null, end_date: null, version: 1,
        issues: [card({ id: 304, title: "Plan retro" })] },
    ],
    backlog: [card({ id: 305, title: "Backlog idea", epic_id: 40 }), card({ id: 306, title: "Another idea", item_type: "Bug" })],
    backlog_total: 2, parallel_sprints: false, ...over,
  };
}

export function hierarchyData(over = {}) {
  const progress = (c, d, i, te, ce) => ({ child_count: c, completed_child_count: d, in_progress_child_count: i, total_estimate: te, completed_estimate: ce });
  return {
    epics: [
      { id: 40, display_id: "ALP-40", title: "Onboarding", color: "#3366ff", archived: false, status: "In Progress", status_category: "in_progress", start_date: "2026-01-01", target_date: "2026-03-01", version: 2, progress: progress(4, 1, 1, 12, 3) },
      { id: 41, display_id: "ALP-41", title: "Billing", color: null, archived: false, status: "New", status_category: "todo", start_date: null, target_date: null, version: 1, progress: progress(0, 0, 0, 0, 0) },
      { id: 42, display_id: "ALP-42", title: "Old stuff", color: null, archived: true, status: "Resolved", status_category: "done", start_date: null, target_date: null, version: 1, progress: progress(1, 1, 0, 2, 2) },
      { id: 43, display_id: "ALP-43", title: "Dead epic", color: null, archived: true, status: "Resolved", status_category: "done", start_date: null, target_date: null, version: 1, progress: progress(0, 0, 0, 0, 0) },
    ],
    issues: [
      card({ id: 310, title: "Signup form", epic_id: 40, sprint_id: 5, subtask_count: 2, subtasks_done: 1 }),
      card({ id: 311, title: "Welcome mail", epic_id: 40, can_edit: false, status: "Resolved", status_category: "done" }),
      card({ id: 312, title: "Archived child", epic_id: 42 }),
      card({ id: 313, title: "Loose story", epic_id: null }),
    ],
    subtasks: [
      card({ id: 320, title: "Validate email", item_type: "Sub-task", parent_id: 310 }),
      card({ id: 321, title: "Show errors", item_type: "Sub-task", parent_id: 310, status: "Resolved", status_category: "done" }),
    ],
    sprints: [{ id: 5, name: "Sprint 5", state: "active" }],
    unparented_total: 1,
    ...over,
  };
}

export function sprintPlanningData(over = {}) {
  return {
    item_count: 4, total_estimate: 12, unestimated_count: 1, ready_to_start: false,
    readiness: [
      { key: "goal", passed: true, severity: "warning", message: "Sprint has a goal" },
      { key: "dates", passed: false, severity: "warning", message: "Dates are not set" },
      { key: "items", passed: false, severity: "failed", message: "No issues in the sprint" },
    ],
    capacity: [{ user_id: 2, capacity_value: 8, capacity_unit: "points", notes: "part time" }],
    ...over,
  };
}

/** Turn project 1 into a Scrum project and fake the agile endpoints (override with backend.on). */
export function makeAgile(backend, { view = boardView(), planning = planningData(), board = BOARD } = {}) {
  backend.state.projects[0].agile_enabled = true;
  backend.state.agile = { 1: { agile_enabled: true, board_id: board.id } };
  backend.state.board = board;
  backend.state.boardView = view;
  backend.state.planning = planning;
  backend.state.quickFilters = [];
  backend.on("GET", /^\/agile\/boards\/\d+$/, () => backend.state.board);
  backend.on("GET", /^\/agile\/boards\/\d+\/view/, () => backend.state.boardView);
  backend.on("GET", /^\/agile\/boards\/\d+\/planning/, () => backend.state.planning);
  backend.on("GET", /^\/agile\/boards\/\d+\/quick-filters/, () => backend.state.quickFilters);
  backend.on("GET", /^\/agile\/sprints\?/, () => backend.state.planning.sprints.map(({ issues: _issues, ...s }) => s));
  backend.on("GET", /^\/agile\/epics/, () => backend.state.planning.epics);
  backend.state.labels = [{ id: 5, display_name: "backend", color: null, usage_count: 3 }];
  backend.state.releases = [
    { id: 1, name: "v1.0", status: "unreleased", release_date: null, version: 2 },
    { id: 2, name: "v0.9", status: "released", release_date: "2025-12-01", version: 1 },
  ];
  backend.state.components = [
    { id: 1, name: "Backend", default_assignee_policy: "none", archived: false },
    { id: 2, name: "Legacy", default_assignee_policy: "none", archived: true },
  ];
  backend.state.hierarchy = hierarchyData();
  backend.state.sprintPlanning = sprintPlanningData();
  backend.on("GET", /^\/agile\/labels/, () => backend.state.labels);
  backend.on("GET", /^\/agile\/releases/, () => backend.state.releases);
  backend.on("GET", /^\/agile\/components/, () => backend.state.components);
  backend.on("GET", /^\/agile\/projects\/\d+\/hierarchy/, () => backend.state.hierarchy);
  backend.on("GET", /^\/agile\/sprints\/\d+\/planning$/, () => backend.state.sprintPlanning);
  for (const [method, pattern] of [
    ["POST", /^\/agile\/work-items\/\d+\/(transition|flag)$/], ["POST", /^\/agile\/boards\/\d+\/move$/],
    ["POST", /^\/agile\/sprints/], ["PUT", /^\/agile\/sprints\/\d+$/], ["PUT", /^\/agile\/boards\/\d+/],
    ["POST", /^\/agile\/(releases|components|labels|epics\/\d+\/archive)/], ["PUT", /^\/agile\/(epics|work-items|sprints)\/\d+\/(hierarchy|capacity)$/],
    ["PUT", /^\/agile\/epics\/\d+$/],
    ["POST", /^\/agile\/boards\/\d+\/quick-filters$/], ["PUT", /^\/agile\/quick-filters\/\d+$/],
  ]) backend.on(method, pattern, () => ({}));
  backend.on("DELETE", /^\/agile\//, () => empty());
  return backend;
}

/** Mount the app on the Sprints view of the Scrum project and open a tab ("board" is the default). */
export async function openSprints(user, { backend = makeAgile(createBackend()), tab, role } = {}) {
  if (role) backend.state.me = { ...backend.state.me, role };
  await mountApp({ backend });
  await user.click(document.querySelector('.nav [data-view="sprints"]'));
  await screen.findByRole("tablist", { name: "Sprint views" });
  if (tab) {
    await user.click(await screen.findByRole("tab", { name: new RegExp(tab) }));
    await waitFor(() => expect(screen.getByRole("tabpanel")).toBeInTheDocument());
  }
  return backend;
}
