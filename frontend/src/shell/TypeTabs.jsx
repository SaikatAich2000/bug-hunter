// Type filter tabs with badge counts from stats.by_type (list/analytics only).
import { useApp } from "../state/AppContext";

const TABS = [
  { tab: "all", text: "All", countId: "typeTabCountAll" },
  { tab: "Bug", text: "🐞 Bugs", countId: "typeTabCountBug" },
  { tab: "Requirement", text: "📐 Requirements", countId: "typeTabCountRequirement" },
  { tab: "Task", text: "🔧 Tasks", countId: "typeTabCountTask" },
];

export default function TypeTabs() {
  const { view, stats, activeTab, setActiveTab } = useApp();

  const show = view === "list" || view === "analytics";

  // Counts are global, not filtered by the active tab.
  const byType = stats?.by_type ?? {};
  const counts = {
    Bug: byType["Bug"] ?? 0,
    Requirement: byType["Requirement"] ?? 0,
    Task: byType["Task"] ?? 0,
    // "All" counts every type (Story, Epic, Sub-task have no tab of their own).
    all: Object.values(byType).reduce((sum, n) => sum + n, 0),
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
