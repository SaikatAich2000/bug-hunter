/**
 * Audit Trail view.
 *
 * Fetches GET /api/audit with entity_type / actor_user_id / q / limit /
 * offset. A filter change (or Clear / Refresh) replaces the list; the "Load
 * older entries" button appends the next page. When the server returns a short
 * page the trail is drained and the end-of-history marker shows instead. The
 * search box is debounced 300ms; entity / actor selects refetch immediately.
 */
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { toastError } from "../lib/toast";
import { activityIcon, formatDate } from "../lib/format";
import { DATA_POLL_MS, useApp } from "../state/AppContext";
import type { AuditRow } from "../types";

// Page the audit trail in chunks; the "Load more" button pulls the next page
// on demand. 300/page keeps the first paint light while staying under the
// backend cap.
const AUDIT_PAGE_SIZE = 300;

// Each audit row is memoized so typing in the search box (which only changes
// view-local `q`) doesn't re-render the whole list. The row JSX is pure over
// `r` and module-scope helpers (activityIcon/formatDate) — no view-local
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

  // Latest filter values for the stable fetcher, read live inside fetchAudit.
  const filtersRef = useRef({ entity, actor, q });
  filtersRef.current = { entity, actor, q };
  // Offset counter for paging.
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
      // Appended pages can overlap rows already shown (offset paging over a
      // table that grew under us), producing duplicate React keys. De-duplicate
      // by id when appending, dropping any row whose id is already present.
      setRows((prev) => {
        if (clean) return page;
        const seen = new Set(prev.map((r) => r.id));
        return [...prev, ...page.filter((r) => !seen.has(r.id))];
      });
      setDrained(page.length < AUDIT_PAGE_SIZE);
    } catch (err) {
      toastError(err);
      // Stop the "Load older" button from re-issuing the same failing request.
      setDrained(true);
    } finally {
      // Render the empty-state instead of a blank screen when the first fetch errors.
      setLoaded(true);
    }
  }, []);

  // Mount + entity/actor change + Refresh/Clear tick → immediate clean fetch.
  useEffect(() => {
    void fetchAudit(true);
  }, [entity, actor, tick, fetchAudit]);

  // Search → 300ms debounced clean fetch. Skips the mount run and any q
  // already fetched by the immediate effect above.
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

  // Live poll the audit trail on the shared cadence so events logged by other
  // users appear without a manual Refresh. A clean page-1 fetch keeps the
  // active filters but resets the offset — so it's skipped when the user has
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

  // Reset all three filters, then one immediate clean fetch (via the tick
  // effect).
  const onClear = () => {
    setEntity("");
    setActor("");
    setQ("");
    setTick((t) => t + 1);
  };

  return (
    <section className="view" id="viewAudit">
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
        {rows.map((r) => <AuditRowView key={r.id} r={r} />)}
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
