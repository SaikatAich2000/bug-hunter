/**
 * Project create/edit modal — port of #modalProject (index.html L723-750)
 * + openProjectForm()/submitProjectForm() (app.js L3316-3357).
 *
 * Driven by `projectModal` in AppContext: `project == null` means create
 * (color defaults to #c9764f, like the vanilla form.reset() + default),
 * otherwise edit. On success: close, jump to the list view, reload
 * projects, refresh bugs/stats, toast — same sequence as the vanilla
 * submit handler.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import Modal from "../components/Modal";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";

const DEFAULT_COLOR = "#c9764f";

export default function ProjectModal() {
  const { projectModal, closeProjectModal, loadProjects, refreshAll, setView } =
    useApp();
  const { open, project } = projectModal;

  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_COLOR);
  const [description, setDescription] = useState("");
  const nameRef = useRef<HTMLInputElement>(null);

  // Port of openProjectForm(): reset + prefill on every open, then focus
  // the name input after the modal becomes visible (vanilla 50ms timeout).
  useEffect(() => {
    if (!open) return;
    setName(project ? project.name : "");
    setColor(project ? project.color : DEFAULT_COLOR);
    setDescription(project ? project.description : "");
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, project]);

  // Port of submitProjectForm().
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
          closeProjectModal();
          setView("list");
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
      <form id="formProject" className="modal-body" noValidate onSubmit={onSubmit}>
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
