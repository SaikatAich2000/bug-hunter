/**
 * Type tabs — port of #typeTabs (index.html L113-126) + tab switching
 * (setActiveTab / refreshTypeTabActiveState, app.js L1888-1915) and the
 * badge counts from stats.by_type (refreshStats, L1650-1657).
 *
 * Visibility: shown on list + analytics only (_toggleViewChrome, L2339-2352
 * — vanilla used style.display, replicated here).
 */
import { useApp, type TabName } from "../state/AppContext";

const TABS: { tab: TabName; text: string; countId: string }[] = [
  { tab: "all", text: "All", countId: "typeTabCountAll" },
  { tab: "Bug", text: "🐞 Bugs", countId: "typeTabCountBug" },
  { tab: "Requirement", text: "📐 Requirements", countId: "typeTabCountRequirement" },
  { tab: "Task", text: "✅ Tasks", countId: "typeTabCountTask" },
];

export default function TypeTabs() {
  const { view, stats, activeTab, setActiveTab } = useApp();

  const show = view === "list" || view === "analytics";

  // Badge counts always come from by_type (global), port of L1650-1657.
  const byType = stats?.by_type ?? {};
  const counts: Record<TabName, number> = {
    Bug: byType["Bug"] ?? 0,
    Requirement: byType["Requirement"] ?? 0,
    Task: byType["Task"] ?? 0,
    all: (byType["Bug"] ?? 0) + (byType["Requirement"] ?? 0) + (byType["Task"] ?? 0),
  };

  return (
    <div
      className="type-tabs"
      id="typeTabs"
      role="tablist"
      aria-label="Work item type"
      style={{ display: show ? undefined : "none" }}
    >
      {TABS.map(({ tab, text, countId }) => (
        <button
          key={tab}
          type="button"
          className={`type-tab${activeTab === tab ? " active" : ""}`}
          data-tab={tab}
          role="tab"
          aria-selected={activeTab === tab}
          onClick={() => setActiveTab(tab)}
        >
          <span>{text}</span>
          <span className="type-tab-count" id={countId}>
            {counts[tab]}
          </span>
        </button>
      ))}
    </div>
  );
}
