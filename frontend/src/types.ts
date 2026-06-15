// Bug Hunter SPA domain types.
// Hand-mirrored from the FastAPI backend (app/schemas.py, app/routes/*,
// app/reports/*, app/chatbot/router.py). Dates are ISO-8601 strings as
// serialized by FastAPI. When a backend schema changes, update here.

// ---------------------------------------------------------------------------
// Core string unions
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py ALLOWED_ITEM_TYPES. */
export type ItemType = "Bug" | "Requirement" | "Task";

/** Mirrors app/schemas.py ALLOWED_ROLES. */
export type Role = "admin" | "manager" | "user";

/** SPA-side main-view selector (frontend-only, no backend schema). */
export type ViewName = "list" | "events" | "analytics" | "audit" | "sessions" | "reports";

/** Minimum role required to OPEN a view. Single source of truth shared by the
 *  Sidebar (which hides the nav button) and the Shell (which refuses to mount
 *  the view + fire its fetch) so the two can't drift. Views not listed are
 *  open to every authenticated user. The backend remains the real authority —
 *  these routes are independently role-gated server-side. */
export const VIEW_MIN_ROLE: Partial<Record<ViewName, Role>> = {
  reports: "manager",
  audit: "manager",
  sessions: "admin",
};

// ---------------------------------------------------------------------------
// Users / auth
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py MeOut (login response / current-user refresh). */
export interface MeOut {
  id: number;
  name: string;
  email: string;
  role: Role;
  is_active: boolean;
}

/** Mirrors app/schemas.py UserOut (user-management endpoints). */
export interface UserOut {
  id: number;
  name: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

/** Mirrors app/schemas.py UserBrief (embedded reporter / assignees / managers). */
export interface UserBrief {
  id: number;
  name: string;
  email: string;
  role: Role;
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py ProjectOut. */
export interface ProjectOut {
  id: number;
  name: string;
  description: string;
  color: string;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Work items (bugs / requirements / tasks)
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py AttachmentBrief. */
export interface AttachmentOut {
  id: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  uploader_user_id: number | null;
  uploader_name: string;
  comment_id: number | null;
  created_at: string;
}

/** Mirrors app/schemas.py CommentOut (body is sanitized rich HTML). */
export interface CommentOut {
  id: number;
  bug_id: number;
  author_user_id: number | null;
  author_name: string;
  body: string;
  created_at: string;
  attachments: AttachmentOut[];
}

/** Mirrors app/schemas.py ActivityOut (per-bug history rows). */
export interface ActivityOut {
  id: number;
  bug_id: number | null;
  entity_type: string;
  entity_id: number | null;
  actor_user_id: number | null;
  actor_name: string;
  action: string;
  detail: string;
  created_at: string;
}

/** Mirrors GET /api/audit rows (app/routes/audit.py, response_model=list[ActivityOut]). */
export type AuditRow = ActivityOut;

/** Mirrors app/schemas.py BugOut (list-row shape; description is sanitized rich HTML). */
export interface BugOut {
  id: number;
  project_id: number;
  project_name: string | null;
  title: string;
  description: string;
  reporter: UserBrief | null;
  assignees: UserBrief[];
  item_type: ItemType;
  status: string;
  priority: string;
  environment: string;
  due_date: string | null;
  event_id: number | null;
  event_name: string | null;
  created_at: string;
  updated_at: string;
  attachment_count: number;
  can_edit: boolean;
}

/** Mirrors app/schemas.py BugDetail (GET /api/bugs/{id}: BugOut + detail arrays). */
export interface BugDetail extends BugOut {
  comments: CommentOut[];
  activities: ActivityOut[];
  attachments: AttachmentOut[];
}

/** Mirrors app/schemas.py BugListResponse (GET /api/bugs). */
export interface BugListResponse {
  items: BugOut[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py EventOut. */
export interface EventOut {
  id: number;
  name: string;
  description: string;
  scheduled_for: string | null;
  created_by_user_id: number | null;
  created_by_name: string | null;
  item_count: number;
  assignee_count: number;
  managers: UserBrief[];
  created_at: string;
  updated_at: string;
}

/** Mirrors app/schemas.py EventDetail (GET /api/events/{id}: EventOut + items). */
export interface EventDetail extends EventOut {
  items: BugOut[];
}

// ---------------------------------------------------------------------------
// Sessions (admin only)
// ---------------------------------------------------------------------------

/** Mirrors app/schemas.py SessionOut (GET /api/sessions rows). */
export interface SessionOut {
  id: number;
  user_id: number;
  user_name: string | null;
  user_email: string | null;
  user_role: Role | null;
  ip_address: string;
  user_agent: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  is_current: boolean;
}

// ---------------------------------------------------------------------------
// Stats / analytics
// ---------------------------------------------------------------------------

/** Mirrors one by_project entry built in app/routes/stats.py stats(). */
export interface StatsProjectSlice {
  id: number;
  name: string;
  color: string;
  count: number;
}

/** Mirrors one by_assignee entry built in app/routes/stats.py stats(). */
export interface StatsAssigneeSlice {
  id: number;
  name: string;
  email: string;
  count: number;
}

/** Mirrors one 14-day timeline entry built in app/routes/stats.py stats(). */
export interface StatsTimelinePoint {
  date: string;
  count: number;
}

/** Mirrors app/schemas.py StatsOut (GET /api/stats; by_type also carries an "Event" key). */
export interface StatsOut {
  bugs: number;
  open: number;
  resolved: number;
  closed: number;
  resolve_later: number;
  projects: number;
  users: number;
  by_status: Record<string, number>;
  by_priority: Record<string, number>;
  by_environment: Record<string, number>;
  by_type: Record<string, number>;
  by_project: StatsProjectSlice[];
  by_assignee: StatsAssigneeSlice[];
  timeline: StatsTimelinePoint[];
}

// ---------------------------------------------------------------------------
// Meta / health
// ---------------------------------------------------------------------------

/** Mirrors app/main.py meta() (GET /api/meta). */
export interface MetaOut {
  statuses: string[];
  statuses_by_type: Record<ItemType, string[]>;
  priorities: string[];
  environments: string[];
  item_types: ItemType[];
}

/** Mirrors app/main.py health() (GET /api/health). */
export interface HealthOut {
  status: string;
  version: string;
  asset_version: string;
}

// ---------------------------------------------------------------------------
// Reports (manager / admin only)
// ---------------------------------------------------------------------------

/** Mirrors one app/reports/catalog.py REPORT_TYPES entry. */
export interface ReportTypeMeta {
  key: string;
  label: string;
  icon: string;
  description: string;
  result_shape: "detail" | "aggregate";
  default_window: string;
}

/** Mirrors the vocab dict in app/routes/reports.py list_report_types(). */
export interface ReportVocab {
  item_types: ItemType[];
  statuses: string[];
  priorities: string[];
  environments: string[];
  open_statuses_by_type: Record<ItemType, string[]>;
  resolved_statuses_by_type: Record<ItemType, string[]>;
}

/** Mirrors the GET /api/reports/types response (app/routes/reports.py list_report_types()). */
export interface ReportTypesOut {
  types: ReportTypeMeta[];
  vocab: ReportVocab;
}

/** Mirrors app/reports/engine.py ReportColumn.to_dict(). */
export interface ReportColumn {
  key: string;
  label: string;
  width: number;
  align: string;
  kind: "text" | "number" | "date" | "datetime";
}

/** Mirrors app/reports/engine.py ReportResult.to_api() plus the truncated/truncated_cap keys added by app/routes/reports.py run() (POST /api/reports/run). */
export interface ReportRunResult {
  report_key: string;
  report_label: string;
  columns: ReportColumn[];
  rows: Array<Record<string, unknown>>;
  summary: Record<string, unknown>;
  filters: Record<string, unknown>;
  total: number;
  has_detail: boolean;
  detail_total: number;
  truncated: boolean;
  truncated_cap?: number;
}

// ---------------------------------------------------------------------------
// Notifications (per-user, v3.0)
// ---------------------------------------------------------------------------

/** Notification kind — mirrors the `kind` strings written by app/notification_service.py callers. */
export type NotificationKind = "assigned" | "reported" | "updated" | "comment" | "event";

/** Mirrors app/schemas.py NotificationOut (GET /api/notifications rows). read_at null = unread. */
export interface NotificationOut {
  id: number;
  kind: NotificationKind;
  title: string;
  body: string;
  bug_id: number | null;
  event_id: number | null;
  actor_name: string;
  read_at: string | null;
  created_at: string;
}

/** Mirrors app/schemas.py UnreadCountOut (GET /api/notifications/unread_count). */
export interface UnreadCountOut {
  unread: number;
}

// ---------------------------------------------------------------------------
// Sleuth chatbot
// ---------------------------------------------------------------------------

/** Mirrors app/chatbot/router.py _BlockOut. */
export interface ChatBlock {
  kind: string;
  payload: Record<string, unknown>;
}

/** Mirrors app/chatbot/router.py ChatOut (POST /api/chat/ask). */
export interface ChatOut {
  blocks: ChatBlock[];
  summary: string;
  intent: string;
}

// ---------------------------------------------------------------------------
// SPA client-side state
// ---------------------------------------------------------------------------

/** Client-side list-filter state mapped onto GET /api/bugs query params (frontend-only). */
export interface Filters {
  project_id: number[];
  status: string[];
  priority: string[];
  environment: string[];
  assignee_id: number[];
  item_type: string[];
  reporter_id: number | null;
  q: string;
}
