/**
 * User create/edit modal.
 *
 * Driven by `userModal` in AppContext: `user == null` means create, otherwise
 * edit. Password is required on create and optional on edit (blank = keep
 * current), with the placeholder/hint/required-mark swapping accordingly. On
 * success: close, reload users, refresh bugs/stats, toast.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import ChipPicker from "../components/ChipPicker";
import Modal from "../components/Modal";
import PasswordInput from "../components/PasswordInput";
import { api } from "../lib/api";
import { withLoader } from "../lib/loader";
import { toast, toastError } from "../lib/toast";
import { useApp } from "../state/AppContext";
import {
  PASSWORD_HINT,
  PASSWORD_MIN_LENGTH,
  isValidEmail,
  validatePassword,
} from "../lib/constants";
import type { Role } from "../types";

// UI placeholder strings, not credentials (key names intentionally avoid the
// word "password"). The create hint states the actual server policy.
const HINTS = {
  editPlaceholder: "Leave blank to keep current",
  editHint: "Leave blank to keep current",
  createPlaceholder: `Min ${PASSWORD_MIN_LENGTH} characters`,
  createHint: PASSWORD_HINT,
} as const;

export default function UserModal() {
  const { userModal, closeUserModal, loadUsers, refreshAll, canManage, isAdmin, projects } = useApp();
  const { open, user } = userModal;
  const isEdit = user != null;

  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("user");
  const [isActive, setIsActive] = useState(true);
  const [password, setPassword] = useState("");
  // Project tags this user may access. Empty = untagged (a non-admin sees
  // nothing). The picker only offers projects the current actor can see, which
  // is exactly the set the backend will let them grant.
  const [projectIds, setProjectIds] = useState<number[]>([]);
  const nameRef = useRef<HTMLInputElement>(null);

  // Reset and prefill on every open, then focus the name input.
  useEffect(() => {
    if (!open) return;
    setName(user ? user.name : "");
    setEmail(user ? user.email : "");
    setRole(user ? user.role || "user" : "user");
    setIsActive(user ? user.is_active : true);
    setPassword("");
    setProjectIds(user?.project_ids ?? []);
    const t = setTimeout(() => nameRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, user]);

  function toggleProject(pid: number): void {
    setProjectIds((cur) =>
      cur.includes(pid) ? cur.filter((x) => x !== pid) : [...cur, pid],
    );
  }

  // Fail-closed: only admins/managers may create/update users, independent of
  // whoever set the open state. The backend enforces this too
  // (require_manager_or_admin); this keeps the form from rendering at all.
  if (!open || !canManage) return null;

  async function onSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const id = user?.id;
    const trimmedEmail = email.trim();
    if (!isValidEmail(trimmedEmail)) {
      toast("Enter a valid email address", "error");
      return;
    }
    const payload: {
      name: string;
      email: string;
      role: Role;
      is_active: boolean;
      password?: string;
      project_ids: number[];
    } = {
      name: name.trim(),
      email: trimmedEmail,
      role,
      is_active: isActive,
      // Always sent: on edit this replaces the user's memberships with exactly
      // what the picker shows (idempotent when unchanged).
      project_ids: projectIds,
    };
    // Only include password if the user typed one (on edit, blank = keep current)
    if (password) {
      const pwErr = validatePassword(password);
      if (pwErr) {
        toast(pwErr, "error");
        return;
      }
      payload.password = password;
    } else if (!id) {
      toast("Password is required for new users", "error");
      return;
    }

    try {
      await withLoader(
        async () => {
          if (id) {
            await api(`/users/${id}`, { method: "PUT", json: payload });
          } else {
            await api("/users", { method: "POST", json: payload });
          }
          closeUserModal();
          await loadUsers();
          await refreshAll();
        },
        id ? "Saving user…" : "Creating user…",
      );
      toast(id ? "User updated" : "User created", "success");
    } catch (err) {
      toastError(err);
    }
  }

  return (
    <Modal
      id="modalUser"
      open={open}
      title={
        <span id="modalUserTitle">{user ? `Edit ${user.name}` : "New User"}</span>
      }
      onClose={closeUserModal}
    >
      <form id="formUser" className="modal-body" noValidate onSubmit={onSubmit}>
        <input type="hidden" name="id" value={user ? user.id : ""} readOnly />
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
          <span>
            Email <em>*</em>
          </span>
          <input
            name="email"
            type="email"
            required
            maxLength={254}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className="field">
          <span>
            Role <em>*</em>
          </span>
          <select
            name="role"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            <option value="user">User — only edit own bugs</option>
            <option value="manager">Manager — edit any bug, manage projects</option>
            {/* Only an admin may grant the admin role; managers cannot. */}
            {(isAdmin || role === "admin") && (
              <option value="admin">Admin — full access including users</option>
            )}
          </select>
        </label>
        <label className="field" id="userPasswordField">
          <span>
            Password{" "}
            <em className={isEdit ? "js-required hidden" : "js-required"}>*</em>
          </span>
          <PasswordInput
            name="password"
            required={!isEdit}
            minLength={8}
            maxLength={200}
            placeholder={isEdit ? HINTS.editPlaceholder : HINTS.createPlaceholder}
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <small className="hint" id="userPasswordHint">
            {isEdit ? HINTS.editHint : HINTS.createHint}
          </small>
        </label>
        <label className="field check-row">
          <input
            type="checkbox"
            name="is_active"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          <span>Active (can log in)</span>
        </label>
        <div className="field" id="userProjectsField">
          <span>Projects</span>
          <ChipPicker
            id="userProjects"
            items={projects.map((p) => ({ id: p.id, label: p.name }))}
            selected={projectIds}
            onToggle={toggleProject}
          />
          <small className="hint">
            Managers and users only see the bugs, requirements, tasks and events
            in the projects tagged here. Leave empty for no access. Admins always
            see everything, regardless of tags.
          </small>
        </div>
        <div className="modal-foot">
          <button
            type="button"
            className="btn ghost"
            data-close-modal
            onClick={closeUserModal}
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
