/**
 * Create/edit modal for a project (project == null → create, color defaults to #c9764f).
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import Modal from "../components/Modal";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

const DEFAULT_COLOR = "#c9764f";

export default function ProjectModal() {
  const { projectModal, closeProjectModal, loadProjects, refreshAll, canManage } =
    useApp();
  const { open, project } = projectModal;

  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);
  const [description, setDescription] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);

  // Prefill fields on open, then focus the name input.
  useEffect(() => {
    if (!open) return;
    setName(project ? project.name : "");
    // <input type="color"> coerces non-#rrggbb to #000000, so fall back to the default for legacy colors.
    setColor(project && /^#[0-9a-fA-F]{6}$/.test(project.color) ? project.color : DEFAULT_COLOR);
    setDescription(project ? project.description : "");
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, project]);

  // Fail-closed: gate here too (backend also enforces) so non-managers never see the form.
  if (!open || !canManage) return null;

  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const id = project?.id;
    const payload = {
      name: name.trim(),
      color,
      description,
    };
    try {
      await withLoader(
        async () => {
          if (id) {
            await api(`/projects/${id}`, { method: "PUT", json: payload });
          } else {
            await api("/projects", { method: "POST", json: payload });
          }
          // Close before reloading so the user stays on their current view.
          closeProjectModal();
          await loadProjects();
          await refreshAll();
        },
        id ? "Saving project…" : "Creating project…",
      );
      toast(id ? "Project updated" : "Project created", "success");
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
    </Modal>
  );
}
