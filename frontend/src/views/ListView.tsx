/**
 * ListView — the work-items table. Port of the vanilla list rendering:
 *
 *  - TAB_COLUMNS / COL_HEAD_LABEL / _renderCell / _renderTableHead /
 *    renderBugTable / renderPagination       (app/static/app.js L1932-2061)
 *  - row click → openBugDetail, admin delete (L4530-4539)
 *  - handleDeleteBug                          (L3540-3559)
 *  - markup: #viewList in app/static/index.html (table / #emptyState /
 *    #paginationBar) — ids/classes preserved exactly.
 *
 * Data, paging and the implicit tab filter all live in AppContext; this
 * component only renders STATE.bugs for the active tab.
 */
import type { ReactNode } from "react";
import { useApp, type TabName } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { withLoader } from "../lib/loader";
import { formatDate, initials } from "../lib/format";
import { confirmDialog } from "../components/ConfirmHost";
import { itemTypeEmoji } from "../modals/bug/helpers";
import type { BugOut } from "../types";

// ---------------------------------------------------------------------------
// Column tables (ports of TAB_COLUMNS / COL_HEAD_LABEL, L1932-1960)
// ---------------------------------------------------------------------------

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

export default function ListView() {
  const {
    bugs,
    page,
    setPage,
    total,
    totalPages,
    activeTab,
    isAdmin,
    openBugDetail,
    refreshAll,
    closeBugModal,
  } = useApp();

  const cols = TAB_COLUMNS[activeTab] ?? TAB_COLUMNS.all;

  // Port of handleDeleteBug (L3540-3559): noun comes from the cached row so
  // the confirm prompt + toast say "task"/"requirement"/"bug" correctly.
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

  // Port of _renderCell (L1962-2027) — same classes / data-attrs per cell.
  const renderCell = (col: ColKey, bug: BugOut): ReactNode => {
    switch (col) {
      case "id":
        return <td key={col} className="col-id">#{bug.id}</td>;
      case "title":
        // For per-type tabs the type prefix is redundant — the tab IS the type.
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
                    // The row click below opens the bug — don't do both.
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
      <div className="table-scroll">
        <table className="bug-table" id="bugTable" aria-label="Work items">
          <thead id="bugTableHead">
            <tr>
              {cols.map((c) => (
                <th key={c} className={`col-${c === "title-with-type" ? "title" : c}`}>
                  {COL_HEAD_LABEL[c] ?? ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody id="bugTableBody">
            {bugs.map((bug) => (
              // Row click opens the unified modal in edit/view mode — the
              // pencil edit button is gone; the row itself is the way in.
              <tr key={bug.id} data-bug-id={bug.id} onClick={() => void openBugDetail(bug.id)}>
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
        {/* Port of renderPagination (L2052-2061): empty when 1 page. */}
        {totalPages > 1 && (
          <>
            <button id="pgPrev" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              ← Prev
            </button>
            <span>
              Page {page} of {totalPages} ({total} bugs)
            </span>
            <button id="pgNext" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
              Next →
            </button>
          </>
        )}
      </div>
    </section>
  );
}
