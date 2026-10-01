/**
 * Create/edit modal for a project (project == null → create, color defaults to #c9764f).
 */
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import ProjectGitSettings from "../components/ProjectGitSettings";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

const DEFAULT_COLOR = "#c9764f";

// New projects default to Agile so the Sprint Board / Backlog work out of the
// box; untick to start with Bugs, Requirements and Tasks only.
const DEFAULT_AGILE_ENABLED = true;

export default function ProjectModal() {
  const { projectModal, closeProjectModal, loadProjects, upsertProject, refreshAll, canManage } =
    useApp();
  const { open, project } = projectModal;

  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);
  const [description, setDescription] = useState("");
  const [agileEnabled, setAgileEnabled] = useState(DEFAULT_AGILE_ENABLED);
  const nameRef = useRef(null);

  // Prefill fields on open, then focus the name input.
  useEffect(() => {
    if (!open) return;
    setName(project ? project.name : "");
    // <input type="color"> coerces non-#rrggbb to #000000, so fall back to the default for legacy colors.
    setColor(project && /^#[0-9a-fA-F]{6}$/.test(project.color) ? project.color : DEFAULT_COLOR);
    setDescription(project ? project.description : "");
    setAgileEnabled(DEFAULT_AGILE_ENABLED);
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, project]);

  // Fail-closed: gate here too (backend also enforces) so non-managers never see the form.
  if (!open || !canManage) return null;

  async function onSubmit(e) {
    e.preventDefault();
    const id = project?.id;
    const payload = {
      name: name.trim(),
      color,
      description,
    };
    // Only the create payload carries the Agile choice; later it is set up from
    // the Sprints page, which provisions and validates the board.
    if (!id) payload.agile_enabled = agileEnabled;
    try {
      await withLoader(
        async () => {
          let saved = null;
          if (id) {
            saved = await api(`/projects/${id}`, { method: "PUT", json: payload });
          } else {
            saved = await api("/projects", { method: "POST", json: payload });
          }
          // Insert/update the saved project in global state immediately, then
          // reload so server-side details (members, counts) converge.
          if (saved?.id) upsertProject(saved);
          // Close before reloading so the user stays on their current view.
          closeProjectModal();
          await loadProjects();
          await refreshAll();
        },
        id ? "Saving project…" : "Creating project…",
      );
      if (id) toast("Project updated", "success");
      else if (agileEnabled) toast("Project created with Agile enabled", "success");
      else toast("Project created", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <Modal
      id="modalProject"
      open={open}
      title={
        <span id="modalProjectTitle">
          {project ? `Edit "${project.name}"` : "New Project"}
        </span>
      }
      onClose={closeProjectModal}
    >
      <div className="project-modal-content">
      <form id="formProject" className="modal-body" onSubmit={onSubmit}>
        <input type="hidden" name="id" value={project ? project.id : ""} readOnly />
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input
            name="name"
            required
            minLength={2}
            maxLength={120}
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Color</span>
          <input
            name="color"
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Description</span>
          <textarea
            name="description"
            rows={2}
            maxLength={1000}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        {!project && (
          <>
            <label className="field check-row">
              <input
                type="checkbox"
                name="agile_enabled"
                id="projectAgileEnabled"
                checked={agileEnabled}
                onChange={(e) => setAgileEnabled(e.target.checked)}
              />
              <span>Enable Agile</span>
            </label>
            <div className="field">
              <small className="hint" id="projectAgileHint">
                Creates the project with its Scrum board and backlog ready, so Epics,
                Stories and Sub-tasks can be added straight away. Turn it off for a
                plain Bug / Requirement / Task tracker; the board can be set up later
                from the Sprints page.
              </small>
            </div>
          </>
        )}
        <div className="modal-foot">
          <button
            type="button"
            className="btn ghost"
            data-close-modal
            onClick={closeProjectModal}
          >
            Cancel
          </button>
          <button type="submit" className="btn primary">
            Save
          </button>
        </div>
      </form>
      {project?.id && <ProjectGitSettings projectId={project.id} />}
      </div>
    </Modal>
  );
}
