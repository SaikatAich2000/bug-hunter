/**
 * EventModal — the Event create / edit form.
 *
 *  - Create mode defaults scheduled_for to today (UTC).
 *  - The manager picker only offers active admin/manager users (the backend
 *    rejects regular users in that slot).
 *  - Submit POSTs /events or PUTs /events/{id} under the blocking loader.
 *  - After a successful save the modal closes and `onSaved` runs inside the
 *    loader (the parent refreshes the list and drills into the event).
 *
 * The date field uses the shared BhDateInput widget, and the modal head comes
 * from the shared <Modal> wrapper — #modalEventTitle is kept as a span inside
 * the h2 so selectors keep resolving.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import Modal from "../components/Modal";
import BhDateInput from "../components/BhDateInput";
import ChipPicker from "../components/ChipPicker";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import type { EventOut } from "../types";

interface Props {
  open: boolean;
  /** Event being edited; null = create mode. */
  event: EventOut | null;
  onClose: () => void;
  /** Runs inside the loader after a successful POST/PUT. */
  onSaved: (saved: EventOut, isEdit: boolean) => Promise<void> | void;
}

export default function EventModal({ open, event, onClose, onSaved }: Readonly<Props>) {
  const { users, projects } = useApp();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scheduledFor, setScheduledFor] = useState("");
  const [managerIds, setManagerIds] = useState<number[]>([]);
  // Owning project (required). "" = nothing picked yet. The dropdown only offers
  // projects the current actor can see — exactly the set the backend allows.
  const [projectId, setProjectId] = useState<number | "">("");
  const nameRef = useRef<HTMLInputElement>(null);

  // Seed the form whenever the modal opens.
  useEffect(() => {
    if (!open) return;
    if (event) {
      setName(event.name || "");
      setDescription(event.description || "");
      setScheduledFor(event.scheduled_for || "");
      setManagerIds(event.managers.map((m) => m.id));
      setProjectId(event.project_id ?? "");
    } else {
      setName("");
      setDescription("");
      // Default scheduled date to today so the morning-standup case is one click.
      setScheduledFor(new Date().toISOString().slice(0, 10));
      setManagerIds([]);
      // Default to the first project the actor can see (projects are loaded at
      // app boot, so this is populated by the time the modal opens).
      setProjectId(projects[0]?.id ?? "");
    }
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, event]);

  // Manager picker: filter to admin/manager users only (the backend
  // rejects regular users in that slot, so don't even show them as
  // options to avoid a confusing 400 response).
  const eligibleManagers = users.filter(
    (u) => u.is_active && (u.role === "admin" || u.role === "manager"),
  );
  // Render chips for the eligible managers PLUS any already-assigned manager
  // that's no longer eligible (e.g. since deactivated) — otherwise it has no
  // chip to deselect yet is silently re-submitted on save.
  const managerPickerItems = eligibleManagers.map((u) => ({ id: u.id, label: u.name, title: u.role }));
  const eligibleManagerIds = new Set(managerPickerItems.map((i) => i.id));
  for (const m of event?.managers ?? []) {
    if (managerIds.includes(m.id) && !eligibleManagerIds.has(m.id)) {
      managerPickerItems.push({ id: m.id, label: `${m.name} (inactive)`, title: m.role });
    }
  }

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const id = event?.id ?? null;
    const trimmedName = name.trim();
    if (!trimmedName) {
      toast("Event name is required", "error");
      return;
    }
    if (projectId === "") {
      toast("Pick a project for this event", "error");
      return;
    }
    const payload = {
      name: trimmedName,
      description,
      scheduled_for: scheduledFor || null,
      project_id: projectId,
      manager_ids: managerIds,
    };
    try {
      await withLoader(async () => {
        const result = id
          ? await api<EventOut>(`/events/${id}`, { method: "PUT", json: payload })
          : await api<EventOut>("/events", { method: "POST", json: payload });
        onClose();
        await onSaved(result, id != null);
        return result;
      }, id ? "Saving event…" : "Creating event…");
      toast(id ? "Event updated" : "Event created", "success");
    } catch (err) {
      toastError(err);
    }
  };

  return (
    <Modal
      id="modalEvent"
      open={open}
      title={
        <span id="modalEventTitle">
          {event ? `Edit "${event.name}"` : "New Event"}
        </span>
      }
      onClose={onClose}
    >
      <form id="formEvent" className="modal-body" onSubmit={submit}>
        <input type="hidden" name="id" value={event ? String(event.id) : ""} readOnly />
        <label className="field">
          <span>
            Name <em>*</em>
          </span>
          <input
            name="name"
            required
            minLength={2}
            maxLength={200}
            placeholder="Morning standup · 2026-05-28"
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            Project <em>*</em>
          </span>
          <select
            name="project_id"
            required
            value={projectId === "" ? "" : String(projectId)}
            onChange={(e) => setProjectId(e.target.value ? Number(e.target.value) : "")}
          >
            <option value="" disabled>
              Select a project…
            </option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <small className="hint">
            Only people with access to this project will see the event and its items.
          </small>
        </label>
        <label className="field">
          <span>Scheduled for</span>
          <BhDateInput name="scheduled_for" value={scheduledFor} onChange={setScheduledFor} />
        </label>
        <fieldset className="field">
          <legend>Managers</legend>
          <ChipPicker
            id="eventManagerPicker"
            items={managerPickerItems}
            selected={managerIds}
            onToggle={(uid) =>
              setManagerIds((prev) =>
                prev.includes(uid) ? prev.filter((x) => x !== uid) : [...prev, uid],
              )
            }
          />
          <small className="hint">
            Managers get notified when the event is created, edited or deleted — but not for
            individual tasks inside it. Only admin/manager users can be event managers
          </small>
        </fieldset>
        <label className="field">
          <span>Description</span>
          <textarea
            name="description"
            rows={3}
            maxLength={10000}
            placeholder="Agenda, attendees, notes — whatever's useful"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          ></textarea>
        </label>
        <div className="modal-foot">
          <button type="button" className="btn ghost" data-close-modal="" onClick={onClose}>
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
