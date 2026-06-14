/**
 * Topbar — port of the index.html header (L88-105) and its behaviours:
 *  - #menuBtn opens the mobile sidebar (app.js L4457-4460)
 *  - #pageTitle from _VIEW_TITLES (L2307-2311, set in setView L2358)
 *  - #search, 300ms-debounced into filters.q (L4496-4499); shown only on
 *    the list view (_toggleViewChrome, L2339-2352)
 *  - "+ New" split button: label resolves the active tab first, then the
 *    user's last-chosen default type; caret opens the type menu
 *    (bindGlobalListeners L4240-4292)
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { debounce } from "../lib/format";
import NotificationsBell from "./NotificationsBell";
import ProfileMenu from "./ProfileMenu";
import type { ItemType, ViewName } from "../types";

/** Port of _VIEW_TITLES (app.js L2307-2311). */
const VIEW_TITLES: Record<ViewName, string> = {
  list: "All Work Items",
  events: "Events",
  analytics: "Analytics",
  audit: "Audit Trail",
  sessions: "Active Sessions",
  reports: "Reports",
};

const NEW_TYPES: { type: ItemType; label: string }[] = [
  { type: "Bug", label: "🐞 New Bug" },
  { type: "Requirement", label: "📐 New Requirement" },
  { type: "Task", label: "✅ New Task" },
];

interface Props {
  readonly onOpenMobile: () => void;
}

export default function Topbar({ onOpenMobile }: Props) {
  const {
    view,
    activeTab,
    defaultNewType,
    setDefaultNewType,
    openBugForm,
    filters,
    setFilters,
  } = useApp();

  const [menuOpen, setMenuOpen] = useState(false);

  // ----- search (debounced 300ms → filters.q, port of L4496-4499) ---------
  // Local input state + a "last sent" ref so external clears (Clear-all
  // button → clearFilters) flow back into the box without fighting typing.
  const [q, setQ] = useState(filters.q);
  const lastSent = useRef(filters.q);

  const sendQ = useMemo(
    () =>
      debounce((value: string) => {
        lastSent.current = value;
        setFilters((prev) => ({ ...prev, q: value }));
      }, 300),
    [setFilters],
  );

  useEffect(() => {
    if (filters.q !== lastSent.current) {
      lastSent.current = filters.q;
      setQ(filters.q);
    }
  }, [filters.q]);

  // ----- "+ New" split button (port of resolveNewType, L4247-4250) --------
  const resolvedNewType: ItemType = activeTab !== "all" ? activeTab : defaultNewType;

  // Close the new-item menu when clicking outside it (port of L4283-4289).
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

  return (
    <header className="topbar">
      <button className="icon-btn menu-btn" id="menuBtn" aria-label="Open menu" onClick={onOpenMobile}>
        ☰
      </button>
      <h1 className="page-title" id="pageTitle">
        {VIEW_TITLES[view]}
      </h1>
      <div className="search-wrap" style={{ display: showListChrome ? undefined : "none" }}>
        <input
          id="search"
          type="search"
          placeholder="Search bugs, requirements, tasks (title, description or #id)…"
          autoComplete="off"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            sendQ(e.target.value.trim());
          }}
        />
      </div>
      {/* Persistent right-hand cluster: New (list only) + bell + account.
          Keeping these in one auto-margined group is what makes the bell and
          profile sit in the SAME place on every view — the old layout let the
          bell snap left when the search box was hidden. */}
      <div className="topbar-right">
        {/* Single split button: clicking the main label creates the default
            type (active tab wins, else last-chosen); the caret opens a menu
            for the other types. */}
        <div className="new-item-wrap" style={{ display: showListChrome ? undefined : "none" }}>
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

        {/* Notifications bell — per-user; same spot on every view. */}
        <NotificationsBell />

        {/* Account menu — name + avatar → change-password / theme / log out. */}
        <ProfileMenu />
      </div>
    </header>
  );
}
