/**
 * Work-items table. Data, paging, and the active-tab filter live in
 * AppContext; this component just renders whatever the context exposes.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useApp, type TabName } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { withLoader } from "../lib/loader";
import { formatDate, initials } from "../lib/format";
import { confirmDialog } from "../components/ConfirmHost";
import BhSelect from "../components/BhSelect";
import { itemTypeEmoji } from "../modals/bug/helpers";
import type { BugOut, BulkActionResult } from "../types";

// Column config per tab — which columns to show and what to call them.

type ColKey =
  | "id"
  | "title"
  | "title-with-type"
  | "project"
  | "status"
  | "priority"
  | "env"
  | "due"
  | "event"
  | "assignees"
  | "att"
  | "actions";

const TAB_COLUMNS: Record<TabName, ColKey[]> = {
  all: [
    "id", "title-with-type", "project", "status", "priority", "env", "assignees", "att", "actions",
  ],
  Bug: [
    "id", "title", "project", "status", "priority", "env", "assignees", "att", "actions",
  ],
  Requirement: [
    "id", "title", "project", "status", "priority", "assignees", "att", "actions",
  ],
  Task: [
    "id", "title", "project", "status", "priority", "due", "event", "assignees", "actions",
  ],
};

const COL_HEAD_LABEL: Record<ColKey, string> = {
  id: "#",
  title: "Title",
  "title-with-type": "Title",
  project: "Project",
  status: "Status",
  priority: "Priority",
  env: "Env",
  due: "Due",
  event: "Event",
  assignees: "Assignees",
  att: "📎",
  actions: "Actions",
};

const MUTED_DASH = <span className="muted">—</span>;

/** Panel header label per tab. */
const PANEL_LABEL: Record<TabName, string> = {
  all: "All work items",
  Bug: "Bugs",
  Requirement: "Requirements",
  Task: "Tasks",
};

/** Noun used in the pager count per tab. */
const PAGER_NOUN: Record<TabName, string> = {
  all: "items",
  Bug: "bugs",
  Requirement: "requirements",
  Task: "tasks",
};

export default function ListView() {
  const {
    bugs,
    page,
    setPage,
    total,
    totalPages,
    activeTab,
    isAdmin,
    meta,
    openBugDetail,
    refreshAll,
    closeBugModal,
  } = useApp();

  const cols = TAB_COLUMNS[activeTab] ?? TAB_COLUMNS.all;

  // If a poll or bulk delete reduces totalPages below the current page,
  // snap back rather than leaving the user on an empty page.
  useEffect(() => {
    if (totalPages > 0 && page > totalPages) {
      setPage(Math.max(1, totalPages));
    }
  }, [page, totalPages, setPage]);

  // Bulk selection is page-scoped. pageIdKey drives the effect below so a
  // 10s background poll with unchanged rows won't wipe an in-progress selection.
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const pageIds = useMemo(() => bugs.map((b) => b.id), [bugs]);
  const pageIdKey = pageIds.join(",");
  // Clear on page turn, filter change, or tab switch.
  useEffect(() => {
    setSelected(new Set());
  }, [pageIdKey]);
  // Drop any selected ids that are no longer on screen.
  const visibleSelected = useMemo(
    () => pageIds.filter((id) => selected.has(id)),
    [pageIds, selected],
  );
  const allSelected = bugs.length > 0 && visibleSelected.length === bugs.length;

  const toggleRow = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const toggleAll = () => {
    setSelected((prev) => {
      // All visible rows selected → deselect them; otherwise select all.
      if (pageIds.every((id) => prev.has(id))) {
        const next = new Set(prev);
        pageIds.forEach((id) => next.delete(id));
        return next;
      }
      return new Set([...prev, ...pageIds]);
    });
  };
  const clearSelection = () => setSelected(new Set());

  const applyingRef = useRef(false);
  const applyBulk = async (action: string, value?: string): Promise<void> => {
    const ids = [...selected];
    if (!ids.length || applyingRef.current) return;  // guard double-submit
    applyingRef.current = true;
    // Pass each row's last-seen version so the server can skip rows that
    // changed concurrently rather than clobbering them.
    const expected_versions: Record<number, number> = {};
    for (const id of ids) {
      const b = bugs.find((x) => x.id === id);
      if (b && typeof b.version === "number") expected_versions[id] = b.version;
    }
    try {
      const res = await withLoader(
        () =>
          api<BulkActionResult>("/bugs/bulk", {
            method: "POST",
            json: { action, ids, value, expected_versions },
          }),
        "Applying to selection…",
      );
      toast(res.message || "Done", res.updated ? "success" : "info");
      clearSelection();
      closeBugModal();
      await refreshAll();
    } catch (err) {
      toastError(err);
    } finally {
      applyingRef.current = false;
    }
  };

  const bulkDelete = async (): Promise<void> => {
    const n = selected.size;
    const ok = await confirmDialog(
      `Delete ${n} selected item${n === 1 ? "" : "s"}? This also deletes their comments and attachments. Cannot be undone`,
    );
    if (!ok) return;
    await applyBulk("delete");
  };

  // Pull the type from the cached row so the confirm prompt uses the right noun.
  const handleDeleteBug = async (bugId: number) => {
    const cached = bugs.find((b) => b.id === bugId);
    const itype = cached?.item_type || "Bug";
    const noun = itype.toLowerCase();
    const ok = await confirmDialog(
      `Delete ${noun} #${bugId}? This will also delete its comments and attachments. Cannot be undone`,
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}`, { method: "DELETE" });
        closeBugModal();
        await refreshAll();
      }, `Deleting ${noun}…`);
      toast(`${itype} #${bugId} deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  const renderCell = (col: ColKey, bug: BugOut): ReactNode => {
    switch (col) {
      case "id":
        return <td key={col} className="col-id">#{bug.id}</td>;
      case "title":
        // On a per-type tab the emoji prefix is redundant; show title only.
        return (
          <td key={col} className="col-title">
            <div className="title-cell">
              <strong className="title-text" title={bug.title}>{bug.title}</strong>
              <span className="title-meta">Updated {formatDate(bug.updated_at)}</span>
            </div>
          </td>
        );
      case "title-with-type": {
        const itype = bug.item_type || "Bug";
        return (
          <td key={col} className="col-title">
            <div className="title-cell">
              <strong className="title-text" title={bug.title}>
                <span className="inline-type" data-type={itype} title={itype}>
                  {itemTypeEmoji(itype)}
                </span>{" "}
                {bug.title}
              </strong>
              <span className="title-meta">{itype} · Updated {formatDate(bug.updated_at)}</span>
            </div>
          </td>
        );
      }
      case "project":
        return <td key={col} className="col-project">{bug.project_name || ""}</td>;
      case "status":
        return (
          <td key={col} className="col-status">
            <span className="badge" data-status={bug.status}>{bug.status}</span>
          </td>
        );
      case "priority":
        return (
          <td key={col} className="col-priority">
            <span className="badge" data-priority={bug.priority}>{bug.priority}</span>
          </td>
        );
      case "env":
        return (
          <td key={col} className="col-env">
            <span className="badge" data-env={bug.environment}>{bug.environment}</span>
          </td>
        );
      case "due":
        return <td key={col} className="col-due">{bug.due_date ? bug.due_date : MUTED_DASH}</td>;
      case "event":
        return (
          <td key={col} className="col-event">
            {bug.event_name ? (
              <span className="event-pill" title={bug.event_name}>📅 {bug.event_name}</span>
            ) : (
              MUTED_DASH
            )}
          </td>
        );
      case "assignees":
        return (
          <td key={col} className="col-assignees">
            <div className="assignee-stack">
              {bug.assignees.length
                ? bug.assignees.map((a) => (
                    <span key={a.id} className="assignee-chip" title={a.email}>
                      <span className="avatar">{initials(a.name)}</span>
                      <span className="assignee-chip-name">{a.name}</span>
                    </span>
                  ))
                : MUTED_DASH}
            </div>
          </td>
        );
      case "att":
        return (
          <td key={col} className="col-att">
            {bug.attachment_count > 0 ? (
              <span className="att-count">📎 {bug.attachment_count}</span>
            ) : (
              MUTED_DASH
            )}
          </td>
        );
      case "actions":
        return (
          <td key={col} className="col-actions">
            <div className="row-actions">
              {isAdmin && (
                <button
                  type="button"
                  className="icon-btn danger"
                  data-act="delete"
                  data-id={bug.id}
                  title="Delete"
                  onClick={(e) => {
                    // Stop the row-click handler from also opening the detail view.
                    e.stopPropagation();
                    void handleDeleteBug(bug.id);
                  }}
                >
                  🗑
                </button>
              )}
            </div>
          </td>
        );
      default:
        return <td key={col}></td>;
    }
  };

  return (
    <section className="view" id="viewList">
      <div className="panel">
        <div className="panel-h">
          <span>{PANEL_LABEL[activeTab] ?? "Work items"}</span>
          <span className="count">showing {bugs.length} of {total}</span>
        </div>

        {/* Bulk action bar — visible when at least one row is selected.
            Server skips items the caller can't act on and reports them in the response. */}
        {selected.size > 0 && (
          <section className="bulk-bar" id="bulkBar" aria-label="Bulk actions">
            <span className="bulk-count">{selected.size} selected</span>
            {/* Each picker fires a bulk op then snaps back to its placeholder
                because value is never stored in state. */}
            <div className="bulk-select-wrap">
              <BhSelect
                value=""
                ariaLabel="Set status for selected"
                options={[{ value: "", label: "Status…" },
                          ...meta.statuses.map((s) => ({ value: s, label: s }))]}
                onChange={(v) => { if (v) void applyBulk("set_status", v); }}
              />
            </div>
            <div className="bulk-select-wrap">
              <BhSelect
                value=""
                ariaLabel="Set priority for selected"
                options={[{ value: "", label: "Priority…" },
                          ...meta.priorities.map((s) => ({ value: s, label: s }))]}
                onChange={(v) => { if (v) void applyBulk("set_priority", v); }}
              />
            </div>
            <div className="bulk-select-wrap">
              <BhSelect
                value=""
                ariaLabel="Set environment for selected"
                options={[{ value: "", label: "Env…" },
                          ...meta.environments.map((s) => ({ value: s, label: s }))]}
                onChange={(v) => { if (v) void applyBulk("set_environment", v); }}
              />
            </div>
            {isAdmin && (
              <button type="button" className="btn danger bulk-delete" onClick={() => void bulkDelete()}>
                🗑 Delete
              </button>
            )}
            <button type="button" className="btn ghost bulk-clear" onClick={clearSelection}>
              Clear
            </button>
          </section>
        )}

        <div className="table-scroll">
        <table className="bug-table" id="bugTable" aria-label="Work items">
          <thead id="bugTableHead">
            <tr>
              <th className="col-select">
                <input
                  type="checkbox"
                  aria-label="Select all on this page"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = !allSelected && visibleSelected.length > 0;
                  }}
                  onChange={toggleAll}
                />
              </th>
              {cols.map((c) => (
                <th key={c} className={`col-${c === "title-with-type" ? "title" : c}`}>
                  {COL_HEAD_LABEL[c] ?? ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody id="bugTableBody">
            {bugs.map((bug) => (
              // Clicking a row opens the detail modal; there is no separate edit button.
              <tr
                key={bug.id}
                data-bug-id={bug.id}
                className={selected.has(bug.id) ? "row-selected" : undefined}
                onClick={() => void openBugDetail(bug.id)}
              >
                {/* Stop propagation so ticking the checkbox doesn't open the detail view. */}
                <td className="col-select" onClick={(e) => e.stopPropagation()}>
                  <input
                    type="checkbox"
                    aria-label={`Select item #${bug.id}`}
                    checked={selected.has(bug.id)}
                    onChange={() => toggleRow(bug.id)}
                  />
                </td>
                {cols.map((c) => renderCell(c, bug))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div id="emptyState" className="empty-state" hidden={bugs.length > 0}>
        <p>No items match your filters</p>
      </div>
      <div className="pagination" id="paginationBar">
        {/* Hidden when there is only one page. */}
        {totalPages > 1 && (
          <>
            <button id="pgPrev" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              ← Prev
            </button>
            <span>
              Page {page} of {totalPages} ({total} {PAGER_NOUN[activeTab] ?? "items"})
            </span>
            <button id="pgNext" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
              Next →
            </button>
          </>
        )}
      </div>
      </div>
    </section>
  );
}
