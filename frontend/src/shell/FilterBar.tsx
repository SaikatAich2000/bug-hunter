/**
 * Filter bar — port of #filterBar (index.html L150-188) driven by six
 * MsFilter multi-selects (option sources: _msOptions, app.js L2182-2196;
 * labels/nouns: MS_LABELS / MS_NOUNS, L2168-2180) plus the Clear button
 * (L4484-4495).
 *
 * Visibility rules:
 *  - whole bar only on the list view (_toggleViewPanels, L2336)
 *  - Type multi-select hidden when a specific tab is active; Env hidden
 *    on the Requirement / Task tabs (refreshFilterBarVisibility,
 *    L1920-1926). Vanilla kept the wraps in the DOM with display:none;
 *    here the corresponding MsFilter simply isn't rendered.
 */
import { useApp } from "../state/AppContext";
import MsFilter from "../components/MsFilter";
import type { ItemType } from "../types";

/** Per-type emoji marker (port of ITEM_TYPE_EMOJI, app.js L2199). */
const ITEM_TYPE_EMOJI: Record<ItemType, string> = {
  Bug: "🐞",
  Requirement: "📐",
  Task: "✅",
};

/** Toggle a value's membership in an array (port of the splice/push). */
function toggled<T>(list: T[], v: T): T[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export default function FilterBar() {
  const { view, activeTab, meta, projects, users, filters, setFilters, clearFilters } = useApp();

  // ----- option lists (port of _msOptions) ---------------------------------
  const projectOptions: [string, string][] = projects.map((p) => [String(p.id), p.name]);
  const itemTypeOptions: [string, string][] = meta.item_types.map((t) => [
    t,
    `${ITEM_TYPE_EMOJI[t]} ${t}`,
  ]);
  const statusOptions: [string, string][] = meta.statuses.map((s) => [s, s]);
  const priorityOptions: [string, string][] = meta.priorities.map((s) => [s, s]);
  const environmentOptions: [string, string][] = meta.environments.map((s) => [s, s]);
  const assigneeOptions: [string, string][] = users
    .filter((u) => u.is_active)
    .map((u) => [String(u.id), u.name]);

  // ----- tab-aware visibility (port of refreshFilterBarVisibility) ---------
  const showTypeFilter = activeTab === "all";
  const showEnvFilter = activeTab !== "Requirement" && activeTab !== "Task";

  return (
    <section className="filter-bar" id="filterBar" hidden={view !== "list"}>
      <MsFilter
        filterKey="project_id"
        label="Projects"
        noun="Projects"
        options={projectOptions}
        selected={filters.project_id.map(String)}
        onToggle={(v) =>
          setFilters((prev) => ({ ...prev, project_id: toggled(prev.project_id, Number(v)) }))
        }
      />
      {showTypeFilter && (
        <MsFilter
          filterKey="item_type"
          label="Types"
          noun="Types"
          options={itemTypeOptions}
          selected={filters.item_type}
          onToggle={(v) =>
            setFilters((prev) => ({ ...prev, item_type: toggled(prev.item_type, v) }))
          }
        />
      )}
      <MsFilter
        filterKey="status"
        label="Statuses"
        noun="Statuses"
        options={statusOptions}
        selected={filters.status}
        onToggle={(v) => setFilters((prev) => ({ ...prev, status: toggled(prev.status, v) }))}
      />
      <MsFilter
        filterKey="priority"
        label="Priorities"
        noun="Priorities"
        options={priorityOptions}
        selected={filters.priority}
        onToggle={(v) =>
          setFilters((prev) => ({ ...prev, priority: toggled(prev.priority, v) }))
        }
      />
      {showEnvFilter && (
        <MsFilter
          filterKey="environment"
          label="Envs"
          noun="Envs"
          options={environmentOptions}
          selected={filters.environment}
          onToggle={(v) =>
            setFilters((prev) => ({ ...prev, environment: toggled(prev.environment, v) }))
          }
        />
      )}
      <MsFilter
        filterKey="assignee_id"
        label="Assignees"
        noun="Assignees"
        options={assigneeOptions}
        selected={filters.assignee_id.map(String)}
        onToggle={(v) =>
          setFilters((prev) => ({ ...prev, assignee_id: toggled(prev.assignee_id, Number(v)) }))
        }
      />
      <button className="btn ghost" id="clearFiltersBtn" onClick={clearFilters}>
        Clear
      </button>
    </section>
  );
}
