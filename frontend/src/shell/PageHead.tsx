/** Header strip: view title, contextual subtitle, and the "+ New" split button (list view only). */
import { useEffect, useState, type ReactNode } from "react";
import { useApp } from "../state/AppContext";
import type { ItemType, ViewName } from "../types";

const VIEW_TITLES: Record<ViewName, string> = {
  list: "All Work Items",
  events: "Events",
  analytics: "Analytics",
  audit: "Audit Trail",
  sessions: "Active Sessions",
  reports: "Reports",
};

/* Static subtitles for every view except "list", which builds its own live summary below. */
const VIEW_SUBTITLES: Partial<Record<ViewName, string>> = {
  events:
    "Group work items into an event — a standup, a sprint meeting, a release — and track them together",
  analytics: "Visual breakdown of your work by status, priority, environment, project and assignee",
  audit:
    "Every create, update, delete, login and session event across Bug Hunter — for incident review and compliance",
  sessions:
    "Every device currently signed in. Revoke a session to log that device out without touching anything else",
  reports:
    "Build a report from any combination of filters, preview it inline, then download the full data as Excel",
};

const NEW_TYPES: { type: ItemType; label: string }[] = [
  { type: "Bug", label: "🐞 New Bug" },
  { type: "Requirement", label: "📐 New Requirement" },
  { type: "Task", label: "✅ New Task" },
];

export default function PageHead() {
  const {
    view,
    activeTab,
    defaultNewType,
    setDefaultNewType,
    openBugForm,
    projects,
    stats,
  } = useApp();

  const [menuOpen, setMenuOpen] = useState(false);

  // Active tab takes priority over the persisted default type.
  const resolvedNewType: ItemType = activeTab !== "all" ? activeTab : defaultNewType;

  // Dismiss the type menu on any outside click.
  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.closest("#newItemMenu") || t.closest("#newItemCaretBtn"))) return;
      setMenuOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [menuOpen]);

  const showListChrome = view === "list";

  // List view gets a live portfolio summary; all other views use the static subtitle.
  let sub: ReactNode = VIEW_SUBTITLES[view] ?? null;
  if (view === "list") {
    const open = stats?.open ?? 0;
    const n = projects.length;
    sub = (
      <>
        Across {n} {n === 1 ? "project" : "projects"} · <b>{open} open</b>
      </>
    );
  }

  return (
    <div className="pagehead">
      {/* key={view} re-mounts on navigation to replay the entrance animation */}
      <div className="pagehead-titles" key={view}>
        <h1 id="pageTitle">{VIEW_TITLES[view]}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="actions">
        {showListChrome && (
          <div className="new-item-wrap">
            <button
              className="btn primary"
              id="newBugBtn"
              type="button"
              onClick={() => openBugForm({ defaultType: resolvedNewType })}
            >
              {`+ New ${resolvedNewType}`}
            </button>
            <button
              className="btn primary new-item-caret"
              id="newItemCaretBtn"
              type="button"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="Choose item type"
              onClick={(e) => {
                e.stopPropagation();
                setMenuOpen((o) => !o);
              }}
            >
              ▾
            </button>
            <div className="new-item-menu" id="newItemMenu" hidden={!menuOpen} role="menu">
              {NEW_TYPES.map(({ type, label }) => (
                <button
                  key={type}
                  type="button"
                  role="menuitem"
                  data-new-type={type}
                  onClick={(e) => {
                    e.stopPropagation();
                    setDefaultNewType(type);
                    setMenuOpen(false);
                    openBugForm({ defaultType: type });
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
