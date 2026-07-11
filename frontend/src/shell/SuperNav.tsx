// Second chrome band: type tabs + search (list/analytics only; search is list-only).
// Search debounces 300 ms; lastSent ref lets external clears update the input without racing typing.
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
