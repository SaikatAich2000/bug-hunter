/**
 * ItemPicker — a Jira-style searchable, MULTI-SELECT combobox for linking work
 * items. Open it, optionally filter by type (All / Bugs / Requirements /
 * Tasks), type to search by title or #id, and tick one or more results; the
 * chosen items appear as removable chips and are all linked together when the
 * caller's "Link" button is pressed.
 *
 * Controlled: `selected` is the array of staged items; toggling a row calls
 * onChange with the next array. `excludeIds` removes the current item and
 * anything already linked so you can't pick a duplicate or self-link. The list
 * is NOT restricted to bugs — GET /bugs returns every item type; the type tabs
 * scope it server-side.
 *
 * Styling: the search input, type tabs and popover all use the shared field /
 * surface tokens so the control matches every other dropdown in light + dark.
 */
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { api } from "../lib/api";
import { debounce } from "../lib/format";
import { itemTypeEmoji } from "../modals/bug/helpers";
import type { BugListResponse } from "../types";

export interface PickerItem {
  id: number;
  title: string;
  item_type: string;
  status: string;
}

interface Props {
  selected: PickerItem[];
  onChange: (items: PickerItem[]) => void;
  excludeIds: number[];
  placeholder?: string;
  disabled?: boolean;
}

const PAGE = 20;

type TypeKey = "all" | "Bug" | "Requirement" | "Task";
const TYPE_TABS: ReadonlyArray<{ key: TypeKey; label: string }> = [
  { key: "all", label: "All" },
  { key: "Bug", label: "Bugs" },
  { key: "Requirement", label: "Requirements" },
  { key: "Task", label: "Tasks" },
];

export default function ItemPicker({
  selected,
  onChange,
  excludeIds,
  placeholder = "Search items by title or #id…",
  disabled,
}: Readonly<Props>) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<TypeKey>("all");
  const [results, setResults] = useState<PickerItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);

  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  const excludeRef = useRef<number[]>(excludeIds);
  excludeRef.current = excludeIds;
  const selectedIds = new Set(selected.map((s) => s.id));

  // Last-writer-wins so a slow earlier response can't clobber a newer one.
  const seqRef = useRef(0);

  const runSearch = useCallback(async (q: string, type: TypeKey) => {
    const seq = ++seqRef.current;
    setLoading(true);
    try {
      const params = new URLSearchParams({ page_size: String(PAGE) });
      if (q.trim()) params.set("q", q.trim());
      if (type !== "all") params.set("item_type", type);
      const res = await api<BugListResponse>(`/bugs?${params}`);
      if (seq !== seqRef.current) return;
      const exclude = new Set(excludeRef.current);
      setResults(
        res.items
          .filter((b) => !exclude.has(b.id))
          .map((b) => ({ id: b.id, title: b.title, item_type: b.item_type, status: b.status })),
      );
      setActiveIdx(0);
    } catch {
      if (seq === seqRef.current) setResults([]);
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  const debouncedSearch = useRef(
    debounce((q: string, type: TypeKey) => void runSearch(q, type), 200),
  ).current;

  // Open → focus search + load an initial page for the active type tab.
  useEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    void runSearch(query, typeFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Switching the type tab while open re-queries immediately (no debounce).
  useEffect(() => {
    if (open) void runSearch(query, typeFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typeFilter]);

  // Outside-click closes.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target instanceof Node ? e.target : null;
      if (t && wrapRef.current?.contains(t)) return;
      setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Toggle membership — the dropdown stays open so several items can be picked.
  const toggle = (item: PickerItem) => {
    if (selectedIds.has(item.id)) {
      onChange(selected.filter((s) => s.id !== item.id));
    } else {
      onChange([...selected, item]);
    }
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = results[activeIdx];
      if (item) toggle(item);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    }
  };

  const renderResults = () => {
    if (loading && results.length === 0) {
      return <div className="item-picker-empty muted">Searching…</div>;
    }
    if (results.length === 0) {
      return <div className="item-picker-empty muted">No matching items</div>;
    }
    return results.map((it, i) => {
      const checked = selectedIds.has(it.id);
      return (
        <button
          type="button"
          key={it.id}
          role="option"
          aria-selected={checked}
          className={`item-picker-row${i === activeIdx ? " is-active" : ""}${checked ? " is-checked" : ""}`}
          onMouseEnter={() => setActiveIdx(i)}
          onClick={() => toggle(it)}
        >
          <span className="item-picker-check" aria-hidden="true">{checked ? "✓" : ""}</span>
          <span className="inline-type" data-type={it.item_type}>
            {itemTypeEmoji(it.item_type)}
          </span>
          <span className="item-picker-row-id">#{it.id}</span>
          <span className="item-picker-row-title" title={it.title}>
            {it.title}
          </span>
          <span className="badge" data-status={it.status}>
            {it.status}
          </span>
        </button>
      );
    });
  };

  return (
    <div className="item-picker" ref={wrapRef}>
      {/* Staged selections — removable chips. */}
      {selected.length > 0 && (
        <div className="item-picker-chips">
          {selected.map((it) => (
            <span key={it.id} className="item-picker-chip" title={it.title}>
              <span className="inline-type" data-type={it.item_type}>
                {itemTypeEmoji(it.item_type)}
              </span>
              <span className="item-picker-chip-id">#{it.id}</span>
              <span className="item-picker-chip-title">{it.title}</span>
              <button
                type="button"
                className="item-picker-clear"
                aria-label={`Remove #${it.id}`}
                disabled={disabled}
                onClick={() => onChange(selected.filter((s) => s.id !== it.id))}
              >
                ✕
              </button>
            </span>
          ))}
        </div>
      )}

      <button
        type="button"
        className={`bh-sel-btn item-picker-trigger${disabled ? " is-disabled" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        disabled={disabled}
        onClick={() => !disabled && setOpen((o) => !o)}
      >
        <span className="bh-sel-label bh-sel-placeholder">
          {selected.length ? `${selected.length} selected — add more…` : placeholder}
        </span>
        <span className="bh-sel-caret" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="item-picker-pop">
          <div className="item-picker-tabs" role="tablist">
            {TYPE_TABS.map((t) => (
              <button
                type="button"
                key={t.key}
                role="tab"
                aria-selected={typeFilter === t.key}
                className={`item-picker-tab${typeFilter === t.key ? " is-active" : ""}`}
                onClick={() => setTypeFilter(t.key)}
              >
                {t.label}
              </button>
            ))}
          </div>
          <input
            ref={inputRef}
            type="search"
            className="item-picker-search"
            placeholder={placeholder}
            autoComplete="off"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              debouncedSearch(e.target.value, typeFilter);
            }}
            onKeyDown={onKeyDown}
          />
          <div className="item-picker-list" role="listbox" aria-multiselectable="true" id={listboxId}>
            {renderResults()}
          </div>
        </div>
      )}
    </div>
  );
}
