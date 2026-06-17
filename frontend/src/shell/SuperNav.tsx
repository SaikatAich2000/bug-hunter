/**
 * SuperNav — the second chrome band: the work-item type tabs on the left + the
 * search box on the right. The band only appears on the views the type tabs
 * apply to (list / analytics); the search box is list-only.
 *
 * The search debounces a 300ms write into filters.q, with a "last sent" ref so
 * external clears (the Clear-all button) flow back into the box without
 * fighting typing.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../state/AppContext";
import { debounce } from "../lib/format";
import TypeTabs from "./TypeTabs";

export default function SuperNav() {
  const { view, filters, setFilters } = useApp();

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

  // The band exists only where the type tabs apply.
  if (view !== "list" && view !== "analytics") return null;

  return (
    <div className="supernav">
      <TypeTabs />
      <span className="grow"></span>
      {view === "list" && (
        <div className="search-wrap">
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
      )}
    </div>
  );
}
