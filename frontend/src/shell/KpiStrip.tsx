/**
 * KPI strip — port of #kpiStrip (index.html L131-147), the tile numbers
 * (refreshStats, app.js L1645-1649), the active-tile highlight
 * (refreshKpiActiveState, L2381-2393) and the click-to-filter toggle
 * (handleKpiClick, L2395-2411).
 *
 * Visibility: shown on list + analytics only (_toggleViewChrome,
 * L2339-2352 — vanilla used style.display, replicated here).
 */
import { useApp, KPI_FILTER_MAP } from "../state/AppContext";

/** Port of _arraysEqualAsSets (app.js L2374-2379). */
function arraysEqualAsSets(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const sa = new Set(a);
  for (const x of b) if (!sa.has(x)) return false;
  return true;
}

interface Tile {
  key: string;
  numId: string;
  numClass: string;
  label: string;
  aria: string;
}

const TILES: Tile[] = [
  { key: "total", numId: "kpiBugs", numClass: "kpi-num", label: "Total", aria: "Show all bugs" },
  { key: "open", numId: "kpiOpen", numClass: "kpi-num kpi-open", label: "Open", aria: "Filter to open bugs" },
  { key: "resolved", numId: "kpiResolved", numClass: "kpi-num kpi-resolved", label: "Resolved", aria: "Filter to resolved bugs" },
  { key: "closed", numId: "kpiClosed", numClass: "kpi-num kpi-closed", label: "Closed", aria: "Filter to closed bugs" },
  { key: "resolve_later", numId: "kpiResolveLater", numClass: "kpi-num kpi-later", label: "Resolve Later", aria: "Filter to resolve-later bugs" },
];

export default function KpiStrip() {
  const { view, setView, stats, filters, setFilters } = useApp();

  const show = view === "list" || view === "analytics";

  const numbers: Record<string, number> = {
    total: stats?.bugs ?? 0,
    open: stats?.open ?? 0,
    resolved: stats?.resolved ?? 0,
    closed: stats?.closed ?? 0,
    resolve_later: stats?.resolve_later ?? 0,
  };

  const isActive = (key: string): boolean => {
    const target = KPI_FILTER_MAP[key];
    if (!target) return false;
    // "Total" is active when no status filter is set.
    return key === "total"
      ? filters.status.length === 0
      : arraysEqualAsSets(filters.status, target);
  };

  // Port of handleKpiClick: toggle — clicking the active filter clears it.
  const onKpiClick = (key: string) => {
    const target = KPI_FILTER_MAP[key];
    if (!target) return;
    const next =
      arraysEqualAsSets(filters.status, target) && target.length > 0 ? [] : [...target];
    setFilters((prev) => ({ ...prev, status: next }));
    // Make sure we're showing the list so the user can see the result.
    if (view !== "list") setView("list");
  };

  return (
    <section className="kpi-strip" id="kpiStrip" style={{ display: show ? undefined : "none" }}>
      {TILES.map((t) => (
        <button
          key={t.key}
          type="button"
          className={`kpi${isActive(t.key) ? " active" : ""}`}
          data-kpi={t.key}
          aria-label={t.aria}
          onClick={() => onKpiClick(t.key)}
        >
          <div className={t.numClass} id={t.numId}>
            {numbers[t.key] ?? 0}
          </div>
          <div className="kpi-label">{t.label}</div>
        </button>
      ))}
    </section>
  );
}
