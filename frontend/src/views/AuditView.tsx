/**
 * Audit Trail view — port of the vanilla audit section (index.html L256-282),
 * refreshAudit (app.js L4164-4234), activityIcon (L3138-3150) and the filter
 * wiring (L4501-4511).
 *
 * Fetches GET /api/audit with entity_type / actor_user_id / q / limit /
 * offset. A filter change (or Clear / Refresh) replaces the list; the
 * "Load older entries" button appends the next page. When the server
 * returns a short page the trail is drained and we show the end-of-history
 * marker instead. The search box is debounced 300ms like the vanilla input
 * handler; entity / actor selects refetch immediately.
 */
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { toastError } from "../lib/toast";
import { formatDate } from "../lib/format";
import { DATA_POLL_MS, useApp } from "../state/AppContext";
import type { AuditRow } from "../types";

// v3.0 perf: page the audit trail in smaller chunks (the "Load more" offset
// affordance already pulls the next page on demand). Loading 5000 detail-rich
// rows up front was the largest single payload + serialization cost in the app;
// 300/page keeps the first paint light while staying well under the backend cap.
const AUDIT_PAGE_SIZE = 300;

/** Emoji per audit action — exact port of activityIcon (app.js L3138-3150). */
function activityIcon(action: string): string {
  if (action.includes("session")) return "🔐";
  if (action.includes("login")) return "🔑";
  if (action.includes("logout")) return "👋";
  if (action.includes("password")) return "🔒";
  if (action.includes("created")) return "✨";
  if (action.includes("delete")) return "🗑";
  if (action.includes("comment")) return "💬";
  if (action.includes("attachment")) return "📎";
  if (action.includes("status")) return "🔄";
  if (action.includes("assign")) return "👥";
  return "📝";
}

// PERF-09: each audit row is memoized so typing in the search box (which only
// changes view-local `q`) doesn't re-render the whole list. The row JSX is pure
// over `r` and module-scope helpers (activityIcon/formatDate) — no view-local
// closures — so memoization is behaviour-preserving.
const AuditRowView = memo(function AuditRowView({ r }: { r: AuditRow }) {
  return (
    <div className="audit-row">
      <span className="audit-icon">{activityIcon(r.action)}</span>
      <div className="audit-text">
        <div>
          <span className="audit-actor">{r.actor_name}</span>
          <span className="audit-action">{r.action}</span>
          {r.entity_type ? (
            <span className="audit-entity">
              {r.entity_type}
              {r.entity_id ? `#${r.entity_id}` : ""}
            </span>
          ) : null}
        </div>
        {r.detail ? <div className="audit-detail">{r.detail}</div> : null}
      </div>
      <span className="audit-time">{formatDate(r.created_at)}</span>
    </div>
  );
});

export default function AuditView() {
  const { users } = useApp();

  const [entity, setEntity] = useState("");
  const [actor, setActor] = useState("");
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<AuditRow[]>([]);
  /** Last page came back short → no more history behind it. */
  const [drained, setDrained] = useState(true);
  /** First fetch finished (gates the "no events" empty state). */
  const [loaded, setLoaded] = useState(false);
  /** Bumped by Refresh / Clear to force an immediate clean fetch. */
  const [tick, setTick] = useState(0);

  // Latest filter values for the stable fetcher (mirrors the vanilla code
  // reading the inputs' live values inside refreshAudit).
  const filtersRef = useRef({ entity, actor, q });
  filtersRef.current = { entity, actor, q };
  // Equivalent of the vanilla _auditLoaded offset counter.
  const loadedCountRef = useRef(0);
  // q value covered by the most recent fetch — lets the debounced search
  // effect skip when Clear's immediate fetch already covered the new q.
  const lastFetchedQRef = useRef<string | null>(null);

  const fetchAudit = useCallback(async (clean: boolean) => {
    const f = filtersRef.current;
    lastFetchedQRef.current = f.q;
    const params = new URLSearchParams();
    const qt = f.q.trim();
    if (f.entity) params.set("entity_type", f.entity);
    if (f.actor) params.set("actor_user_id", f.actor);
    if (qt) params.set("q", qt);
    if (clean) loadedCountRef.current = 0;
    params.set("limit", String(AUDIT_PAGE_SIZE));
    params.set("offset", String(loadedCountRef.current));
    try {
      const page = await api<AuditRow[]>(`/audit?${params.toString()}`);
      loadedCountRef.current += page.length;
      setRows((prev) => (clean ? page : [...prev, ...page]));
      setDrained(page.length < AUDIT_PAGE_SIZE);
      setLoaded(true);
    } catch (err) {
      toastError(err);
    }
  }, []);

  // Mount + entity/actor change + Refresh/Clear tick → immediate clean fetch.
  useEffect(() => {
    void fetchAudit(true);
  }, [entity, actor, tick, fetchAudit]);

  // Search → 300ms debounced clean fetch (port of the debounced input
  // listener, app.js L4504). Skips the mount run and any q already fetched
  // by the immediate effect above.
  const qFirstRef = useRef(true);
  useEffect(() => {
    if (qFirstRef.current) {
      qFirstRef.current = false;
      return;
    }
    const t = setTimeout(() => {
      if (filtersRef.current.q === lastFetchedQRef.current) return;
      void fetchAudit(true);
    }, 300);
    return () => clearTimeout(t);
  }, [q, fetchAudit]);

  // AUTO-04: live poll the audit trail on the shared cadence so events logged
  // by other users appear without a manual Refresh. A clean page-1 fetch keeps
  // the active filters but resets the offset — so it's SKIPPED when the user has
  // paged back via "Load older entries" (loadedCount past the first page), to
  // avoid collapsing their accumulated list. Paused while hidden; runs on focus.
  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      if (loadedCountRef.current > AUDIT_PAGE_SIZE) return; // user paged back; don't collapse
      void fetchAudit(true); // clean=true preserves active filters, resets to page 1
    };
    const id = setInterval(refresh, DATA_POLL_MS);
    document.addEventListener("visibilitychange", refresh);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", refresh); };
  }, [fetchAudit]);

  // Port of the auditClearBtn handler (app.js L4506-4511): reset all three
  // filters, then one immediate clean fetch (via the tick effect).
  const onClear = () => {
    setEntity("");
    setActor("");
    setQ("");
    setTick((t) => t + 1);
  };

  return (
    <section className="view" id="viewAudit">
      <div className="page-intro audit-intro">
        <div className="page-intro-icon" aria-hidden="true">🛡</div>
        <div className="page-intro-text">
          <h2>Audit Trail</h2>
          <p>Every create, update, delete, login and session event across Bug Hunter — useful for incident review and compliance</p>
        </div>
      </div>
      <div className="audit-controls">
        <select
          id="auditEntityFilter"
          value={entity}
          onChange={(e) => setEntity(e.target.value)}
        >
          <option value="">All entities</option>
          <option value="bug">Bugs</option>
          <option value="user">Users</option>
          <option value="project">Projects</option>
          <option value="attachment">Attachments</option>
          <option value="session">Sessions</option>
          <option value="auth">Auth events</option>
        </select>
        <select
          id="auditActorFilter"
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        >
          <option value="">All actors</option>
          {users.map((u) => (
            <option key={u.id} value={String(u.id)}>
              {u.name}
            </option>
          ))}
        </select>
        <input
          id="auditSearch"
          type="search"
          placeholder="Search audit log (try bug #, user name, action…)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="button" className="btn ghost" id="auditClearBtn" onClick={onClear}>
          Clear
        </button>
        <button
          type="button"
          className="btn ghost"
          id="auditRefreshBtn"
          onClick={() => setTick((t) => t + 1)}
        >
          Refresh
        </button>
      </div>
      <div className="audit-list" id="auditList">
        {loaded && rows.length === 0 ? (
          <p className="no-content">No audit events match</p>
        ) : null}
        {rows.map((r, i) => <AuditRowView key={`${i}-${r.id}`} r={r} />)}
        {!drained && rows.length > 0 ? (
          <div className="audit-load-more">
            <button
              type="button"
              className="btn ghost"
              id="auditLoadMoreBtn"
              onClick={() => void fetchAudit(false)}
            >
              Load older entries
            </button>
            <span className="muted small">Showing {rows.length} entries</span>
          </div>
        ) : null}
        {drained && rows.length > 0 ? (
          <div className="audit-load-more muted small">
            — end of history ({rows.length} entries) —
          </div>
        ) : null}
      </div>
    </section>
  );
}
