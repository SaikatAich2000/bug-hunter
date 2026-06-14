/**
 * Sidebar — port of the index.html aside (L28-84) plus its behaviours:
 *  - brand block + collapse button (app.js L4466-4478)
 *  - nav buttons with role gating (applyRoleVisibility, L1651-1667)
 *  - project / user side lists (renderProjectList / renderUserList,
 *    L2066-2146) with the swatch/avatar=filter vs name=edit click split
 *    and delete handlers (L4547-4599, handleDeleteProject/User L3561-3600)
 *  - account card (renderAccountCard, L1669-1676)
 *  - change-password / logout / theme buttons (L4404-4427)
 *
 * Role gating note: vanilla hid `[data-needs-role]` elements via CSS and
 * deleted the attribute for allowed roles. Here gated elements simply
 * aren't rendered for insufficient roles, and allowed ones render without
 * the attribute — same end state in both cases.
 */
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { initials } from "../lib/format";
import { withLoader } from "../lib/loader";
import { confirmDialog } from "../components/ConfirmHost";
import type { Role, ViewName } from "../types";

const NAV_ITEMS: { view: ViewName; icon: string; label: string; needs?: Role }[] = [
  { view: "list", icon: "📋", label: "Work Items" },
  { view: "events", icon: "📅", label: "Events" },
  { view: "analytics", icon: "📊", label: "Analytics" },
  { view: "reports", icon: "📈", label: "Reports", needs: "manager" },
  { view: "audit", icon: "🛡", label: "Audit Trail", needs: "manager" },
  { view: "sessions", icon: "🔐", label: "Sessions", needs: "admin" },
];

interface Props {
  readonly mobileOpen: boolean;
  readonly onCloseMobile: () => void;
}

export default function Sidebar({ mobileOpen, onCloseMobile }: Props) {
  const {
    currentUser,
    health,
    projects,
    users,
    filters,
    setFilters,
    view,
    setView,
    sidebarCollapsed,
    toggleSidebarCollapsed,
    openProjectForm,
    openUserForm,
    loadProjects,
    loadUsers,
    refreshAll,
    roleRank,
    canManage,
    isAdmin,
  } = useApp();

  const allowed = (needs?: Role): boolean =>
    !needs || roleRank(currentUser.role) >= roleRank(needs);

  // ----- filter toggles (port of the #projectList/#userList delegation) ---
  const toggleProjectFilter = (id: number) => {
    setFilters((prev) => ({
      ...prev,
      project_id: prev.project_id.includes(id)
        ? prev.project_id.filter((x) => x !== id)
        : [...prev.project_id, id],
    }));
  };

  const toggleAssigneeFilter = (id: number) => {
    setFilters((prev) => ({
      ...prev,
      assignee_id: prev.assignee_id.includes(id)
        ? prev.assignee_id.filter((x) => x !== id)
        : [...prev.assignee_id, id],
    }));
  };

  // ----- deletes (ports of handleDeleteProject / handleDeleteUser) --------
  const handleDeleteProject = async (id: number) => {
    const project = projects.find((p) => p.id === id);
    const name = project ? project.name : `#${id}`;
    const ok = await confirmDialog(`Delete project "${name}"?\nThis only works if it has no bugs`);
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/projects/${id}`, { method: "DELETE" });
        // Drop the deleted project from the multi-select filter so we don't
        // keep filtering by a no-longer-existing id.
        setFilters((prev) => ({
          ...prev,
          project_id: prev.project_id.filter((v) => v !== id),
        }));
        await loadProjects();
        await refreshAll();
      }, "Deleting project…");
      toast(`Project "${name}" deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  const handleDeleteUser = async (id: number) => {
    const user = users.find((u) => u.id === id);
    const name = user ? user.name : `#${id}`;
    const ok = await confirmDialog(
      `Delete user "${name}"?\nThis user will be removed from all bug assignments.\nReports they filed will become "unassigned reporter"`,
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/users/${id}`, { method: "DELETE" });
        await loadUsers();
        await refreshAll();
      }, "Deleting user…");
      toast(`User "${name}" deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  // Account actions (change-password / theme / log out) now live in the
  // top-right ProfileMenu — the sidebar is navigation + entity lists only.

  const activeProjectIds = new Set(filters.project_id);
  const activeUsers = users.filter((u) => u.is_active);

  return (
    <aside
      className={`sidebar${mobileOpen ? " open" : ""}`}
      id="sidebar"
      aria-label="Primary navigation"
    >
      <div className="brand">
        <div className="brand-logo">
          <img src="/static/icon.png" alt="Bug Hunter" />
        </div>
        <div className="brand-text">
          <div className="brand-title">Bug Hunter</div>
          <div className="brand-version" id="brandVersion">
            {health ? `v${health.version}` : "v—"}
          </div>
        </div>
        <button
          className="icon-btn sidebar-collapse-btn"
          id="sidebarCollapseBtn"
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label="Collapse sidebar"
          onClick={(e) => {
            e.stopPropagation();
            toggleSidebarCollapsed();
          }}
        >
          {sidebarCollapsed ? "»" : "«"}
        </button>
      </div>

      <nav className="nav" aria-label="Main sections">
        {NAV_ITEMS.filter((item) => allowed(item.needs)).map((item) => (
          <button
            key={item.view}
            className={`nav-btn${view === item.view ? " active" : ""}`}
            data-view={item.view}
            onClick={() => {
              setView(item.view);
              onCloseMobile();
            }}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <section className="side-section">
        <div className="side-section-header">
          <span>Projects</span>
          {canManage && (
            <button
              className="icon-btn"
              id="newProjectBtn"
              title="New project"
              aria-label="New project"
              onClick={() => openProjectForm()}
            >
              +
            </button>
          )}
        </div>
        <ul className="side-list" id="projectList">
          {projects.length === 0 ? (
            <li className="side-item muted no-cursor">No projects — click + to add</li>
          ) : (
            projects.map((p) => (
              <li
                key={p.id}
                className={`side-item${activeProjectIds.has(p.id) ? " active" : ""}`}
                data-project-id={p.id}
                title={p.name}
              >
                <span
                  className="swatch"
                  data-act="filter"
                  style={{ background: p.color }}
                  title="Toggle filter"
                  onClick={() => toggleProjectFilter(p.id)}
                ></span>
                <span
                  className="label-text"
                  data-act="open-project"
                  title={canManage ? `${p.name} — click to edit` : p.name}
                  onClick={() =>
                    canManage ? openProjectForm(p) : toggleProjectFilter(p.id)
                  }
                >
                  {p.name}
                </span>
                <span className="row-actions">
                  {canManage && (
                    <button
                      className="icon-btn"
                      data-act="edit-project"
                      data-id={p.id}
                      title="Edit"
                      onClick={() => openProjectForm(p)}
                    >
                      ✎
                    </button>
                  )}
                  {isAdmin && (
                    <button
                      className="icon-btn danger"
                      data-act="delete-project"
                      data-id={p.id}
                      title="Delete"
                      onClick={() => void handleDeleteProject(p.id)}
                    >
                      🗑
                    </button>
                  )}
                </span>
              </li>
            ))
          )}
        </ul>
      </section>

      {canManage && (
        <section className="side-section">
          <div className="side-section-header">
            <span>Users</span>
            <button
              className="icon-btn"
              id="newUserBtn"
              title="New user"
              aria-label="New user"
              onClick={() => openUserForm()}
            >
              +
            </button>
          </div>
          <ul className="side-list" id="userList">
            {activeUsers.length === 0 ? (
              <li className="side-item muted no-cursor">No users yet — click + to add</li>
            ) : (
              activeUsers.map((u) => (
                <li
                  key={u.id}
                  className="side-item"
                  data-user-id={u.id}
                  title={`${u.email}${u.role ? " — " + u.role : ""}`}
                >
                  <span
                    className="avatar"
                    data-act="filter-user"
                    title="Toggle filter"
                    onClick={() => toggleAssigneeFilter(u.id)}
                  >
                    {initials(u.name)}
                  </span>
                  <span
                    className="label-text"
                    data-act="open-user"
                    title={canManage ? `${u.name} — click to edit` : u.name}
                    onClick={() =>
                      canManage ? openUserForm(u) : toggleAssigneeFilter(u.id)
                    }
                  >
                    {u.name}
                    {u.role ? <span className="meta"> · {u.role}</span> : null}
                  </span>
                  <span className="row-actions">
                    {canManage && (
                      <button
                        className="icon-btn"
                        data-act="edit-user"
                        data-id={u.id}
                        title="Edit"
                        onClick={() => openUserForm(u)}
                      >
                        ✎
                      </button>
                    )}
                    {isAdmin && (
                      <button
                        className="icon-btn danger"
                        data-act="delete-user"
                        data-id={u.id}
                        title="Delete"
                        onClick={() => void handleDeleteUser(u.id)}
                      >
                        🗑
                      </button>
                    )}
                  </span>
                </li>
              ))
            )}
          </ul>
        </section>
      )}

    </aside>
  );
}
