/**
 * Unified create/edit/view modal for work items (bug == null → create).
 * Test contracts: #bugSubmitBtn inside #formBug, reporter_id select disabled, #bugCommentsSection visible in edit/view, #commentPostBtn type="button", no nested <form> in #formBug (composer is a <div>).
 */
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useApp } from "../state/AppContext";
import { api } from "../lib/api";
import { toast, toastError } from "../lib/toast";
import { withLoader } from "../lib/loader";
import { formatDate, initials } from "../lib/format";
import { fileTooLargeMessage, MAX_UPLOAD_BYTES } from "../lib/upload";
import { sanitizeHtml } from "../lib/sanitize";
import { confirmDialog } from "../components/ConfirmHost";
import BhSelect, { type BhSelectOption } from "../components/BhSelect";
import BhDateInput from "../components/BhDateInput";
import ChipPicker from "../components/ChipPicker";
import ItemPicker, { type PickerItem } from "../components/ItemPicker";
import { useFileDrop } from "../lib/useFileDrop";
import RichEditor, { type RichEditorHandle } from "../components/RichEditor";
import {
  ActivityRow,
  AttachmentCard,
  StagedTile,
  canEditItemType,
  errorMessage,
  isoToday,
  itemTypeEmoji,
  stagedLabel,
  statusesForType,
  useStagedFiles,
} from "./bug/helpers";
import type { BugOut, CommentOut, EventOut, ItemType } from "../types";

const NO_EVENT_OPTION: BhSelectOption = { value: "", label: "— No event —" };
const SELECT_PLACEHOLDER: BhSelectOption = { value: "", label: "— select —" };

/** Mirrors ALLOWED_LINK_TYPES in app/schemas.py. */
const LINK_TYPE_OPTS: BhSelectOption[] = [
  { value: "relates", label: "relates to" },
  { value: "blocks", label: "blocks" },
  { value: "duplicate", label: "duplicates" },
];

async function uploadCreateBugFiles(
  createdId: number,
  files: File[],
): Promise<{ done: number; failed: number }> {
  let done = 0;
  let failed = 0;
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    try {
      await api(`/bugs/${createdId}/attachments`, { method: "POST", body: fd });
      done++;
    } catch (err) {
      failed++;
      toast(`Attachment ${f.name}: ${errorMessage(err)}`, "error");
    }
  }
  return { done, failed };
}

async function toastAfterCreate(
  created: BugOut | null,
  ctype: string,
  files: File[],
): Promise<void> {
  if (!(files.length && created?.id)) {
    toast(`${ctype} created`, "success");
    return;
  }
  const { done, failed } = await uploadCreateBugFiles(created.id, files);
  if (done) toast(`${ctype} #${created.id} created · ${done} file(s) attached`, "success");
  else if (failed) toast(`${ctype} #${created.id} created (no attachments saved)`, "info");
  else toast(`${ctype} created`, "success");
}

function commentSubmitLabel(body: string, fileCount: number): string {
  if (body && fileCount) return "Posting comment and uploading…";
  if (body) return "Posting comment…";
  return "Uploading file(s)…";
}

export default function BugModal() {
  const {
    bugModal,
    closeBugModal,
    reloadBugModal,
    refreshAll,
    refreshBugs,
    openBugDetail,
    currentUser,
    meta,
    users,
    projects,
    isAdmin,
    defaultNewType,
    setDefaultNewType,
  } = useApp();

  const { open, bug, defaultType, defaultEventId } = bugModal;
  const isEdit = bug != null;
  const bugId = bug?.id ?? null;

  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState("");
  const [itemType, setItemType] = useState<string>("Bug");
  const [status, setStatus] = useState("New");
  const [priority, setPriority] = useState("Medium");
  const [environment, setEnvironment] = useState("DEV");
  const [eventId, setEventId] = useState("");
  const [eventOptions, setEventOptions] = useState<BhSelectOption[]>([NO_EVENT_OPTION]);
  const [dueDate, setDueDate] = useState("");
  const [assigneeIds, setAssigneeIds] = useState<number[]>([]);
  const [editingCommentId, setEditingCommentId] = useState<number | null>(null);
  // Items chosen in the picker, not raw IDs.
  const [linkTargets, setLinkTargets] = useState<PickerItem[]>([]);
  const [linkType, setLinkType] = useState("relates");

  const descRef = useRef<RichEditorHandle>(null);
  const commentRef = useRef<RichEditorHandle>(null);
  const editCommentRef = useRef<RichEditorHandle>(null);
  const titleRef = useRef<HTMLInputElement>(null);
  // Version frozen at open time so the server can detect concurrent edits on save.
  const expectedVersionRef = useRef<number | null>(null);

  // Three staging buckets: new-item, comment, and post-creation attachments.
  const createStaged = useStagedFiles();
  const commentStaged = useStagedFiles();
  const bugAttachStaged = useStagedFiles();

  const createDrop = useFileDrop((files) => createStaged.addFiles(files));
  const bugAttachDrop = useFileDrop((files) => bugAttachStaged.addFiles(files));
  const commentDrop = useFileDrop((files) => commentStaged.addFiles(files));

  // Create is always editable; edit prefers server can_edit, falling back to the role heuristic.
  const canEditThis = isEdit ? (bug.can_edit ?? canEditItemType(currentUser.role, bug.item_type)) : true;
  const readOnly = isEdit && !canEditThis;

  // Seed form fields on open or item switch; reloadBugModal() keeps the id so half-typed fields survive.
  useEffect(() => {
    if (!open) return;
    setEditingCommentId(null);
    // Don't carry staged files across items.
    createStaged.clear();
    commentStaged.clear();
    bugAttachStaged.clear();
    commentRef.current?.setHtml("");
    setLinkTargets([]);
    setLinkType("relates");

    if (bug) {
      // edit / view
      setTitle(bug.title || "");
      setProjectId(bug.project_id ? String(bug.project_id) : "");
      setItemType(bug.item_type || "Bug");
      setStatus(bug.status || "New");
      setPriority(bug.priority || "Medium");
      setEnvironment(bug.environment || "DEV");
      setEventId(bug.event_id ? String(bug.event_id) : "");
      setDueDate(bug.due_date || "");
      setAssigneeIds(bug.assignees ? bug.assignees.map((a) => a.id) : []);
      descRef.current?.setHtml(bug.description || "");
      expectedVersionRef.current = bug.version ?? null;
    } else {
      // create
      setTitle("");
      setProjectId("");
      setItemType(defaultType || defaultNewType || "Bug");
      setStatus("New");
      setPriority("Medium");
      setEnvironment("DEV");
      setEventId(defaultEventId ? String(defaultEventId) : "");
      setDueDate(isoToday()); // default to today
      setAssigneeIds([]);
      descRef.current?.setHtml("");
      expectedVersionRef.current = null;
    }

    // Seed the current event immediately; replace with the full list once /events loads.
    const seed: BhSelectOption[] = [NO_EVENT_OPTION];
    if (bug && bug.event_id && bug.event_name) {
      seed.push({ value: String(bug.event_id), label: bug.event_name });
    }
    setEventOptions(seed);
    let cancelled = false;
    api<EventOut[]>("/events")
      .then((events) => {
        if (cancelled) return;
        setEventOptions([
          NO_EVENT_OPTION,
          ...(events || []).map((ev) => ({
            value: String(ev.id),
            label: ev.scheduled_for ? `${ev.name} · ${ev.scheduled_for}` : ev.name,
          })),
        ]);
      })
      .catch(() => {
        /* leave the placeholder if /events fails */
      });

    // Delay focus so the modal finishes mounting first.
    const ro = readOnly;
    let timer: number | undefined;
    if (!ro) timer = window.setTimeout(() => titleRef.current?.focus(), 50);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // Keyed on open + bug id only — see seeding note above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, bugId]);

  // Focus the inline editor when it appears.
  useEffect(() => {
    if (editingCommentId != null) editCommentRef.current?.focus();
  }, [editingCommentId]);

  // Bail when closed so poll ticks don't recompute options / rebuild RichEditors. All hooks run above.
  if (!open) return null;

  // Keep any status the item already holds even if invalid for the type — flagged "(legacy)".
  const validStatuses = statusesForType(meta, itemType);
  const statusOpts: BhSelectOption[] = [
    SELECT_PLACEHOLDER,
    ...validStatuses.map((s) => ({ value: s, label: s })),
  ];
  if (status && !validStatuses.includes(status)) {
    statusOpts.push({ value: status, label: `${status} (legacy)` });
  }

  const itemTypeOpts: BhSelectOption[] = [
    SELECT_PLACEHOLDER,
    ...(meta.item_types || ["Bug", "Requirement", "Task"]).map((t) => ({ value: t, label: t })),
  ];
  const projectOpts: BhSelectOption[] = [
    SELECT_PLACEHOLDER,
    ...projects.map((p) => ({ value: String(p.id), label: p.name })),
  ];
  const priorityOpts: BhSelectOption[] = [
    SELECT_PLACEHOLDER,
    ...meta.priorities.map((s) => ({ value: s, label: s })),
  ];
  const environmentOpts: BhSelectOption[] = [
    { value: "DEV", label: "DEV" },
    { value: "UAT", label: "UAT" },
    { value: "PROD", label: "PROD" },
  ];

  // Show the current event by name if the list hasn't loaded yet.
  const eventOpts =
    eventId && bug?.event_name && !eventOptions.some((o) => o.value === eventId)
      ? [...eventOptions, { value: eventId, label: bug.event_name }]
      : eventOptions;

  // Reporter is fixed — inject the original reporter when it isn't the current user.
  const me = currentUser;
  const reporterOption =
    isEdit && bug.reporter && bug.reporter.id !== me.id
      ? bug.reporter
      : { id: me.id, name: me.name, email: me.email };
  const reporterId = isEdit && bug.reporter ? bug.reporter.id : me.id;

  const assigneeItems = users
    .filter((u) => u.is_active)
    .map((u) => ({ id: u.id, label: u.name, title: u.role }));

  const headType = isEdit ? bug.item_type || "Bug" : defaultType || defaultNewType || "Bug";
  const headTitle = isEdit
    ? `${itemTypeEmoji(headType)} ${headType} #${bug.id}`
    : `${itemTypeEmoji(headType)} New ${headType}`;
  const headSubtitle = isEdit ? bug.title || "" : "";

  // Edit: pasted images upload immediately as bug-level attachments. Create: staged until save.
  const onDescPaste = async (f: File): Promise<void> => {
    if (bugId) {
      const tooBig = fileTooLargeMessage(f);
      if (tooBig) {
        toast(tooBig, "error");
        return;
      }
      const fd = new FormData();
      fd.append("file", f);
      await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
      await reloadBugModal();
      toast(`Attached: ${f.name || "pasted file"}`, "success");
    } else {
      createStaged.addFiles([f]); // addFiles() checks the size limit
      if (f.size <= MAX_UPLOAD_BYTES) toast(`Staged: ${f.name || "pasted file"}`, "info");
    }
  };

  // Pasted files stage in the comment bucket, uploaded with comment_id once the comment posts.
  const onComposerPaste = (f: File): void => {
    commentStaged.addFiles([f]);
    toast(`Staged: ${f.name || "pasted file"}`, "info");
  };

  // Inline-editor comment is already saved, so pastes attach at the bug level (no comment_id to backfill).
  const onEditCommentPaste = async (f: File): Promise<void> => {
    if (!bugId) return;
    const tooBig = fileTooLargeMessage(f);
    if (tooBig) {
      toast(tooBig, "error");
      return;
    }
    const fd = new FormData();
    fd.append("file", f);
    await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
    toast(`Attached: ${f.name || "pasted file"}`, "success");
  };

  const onDeleteBug = async (): Promise<void> => {
    if (!bugId) return;
    const itype = bug?.item_type || "Bug";
    const noun = itype.toLowerCase();
    const ok = await confirmDialog(
      `Delete ${noun} #${bugId}? This will also delete its comments and attachments. Cannot be undone`,
    );
    if (!ok) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}`, { method: "DELETE" });
        closeBugModal();
        await refreshAll();
      }, `Deleting ${noun}…`);
      toast(`${itype} #${bugId} deleted`, "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onDeleteAttachment = async (attId: number): Promise<void> => {
    const ok = await confirmDialog("Delete this attachment?");
    if (!ok || !bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/attachments/${attId}`, { method: "DELETE" });
        await reloadBugModal();
        await refreshBugs();
      }, "Deleting attachment…");
      toast("Attachment deleted", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onDeleteComment = async (commentId: number): Promise<void> => {
    const ok = await confirmDialog(
      "Delete this comment? Its attachments will be removed too. Cannot be undone",
    );
    if (!ok || !bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/comments/${commentId}`, { method: "DELETE" });
        await reloadBugModal();
        await refreshBugs();
      }, "Deleting comment…");
      toast("Comment deleted", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onSaveEditComment = async (commentId: number): Promise<void> => {
    if (!bugId) return;
    const body = (editCommentRef.current?.getHtml() ?? "").trim();
    if (!body) {
      toast("Comment body can't be empty", "error");
      return;
    }
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/comments/${commentId}`, { method: "PUT", json: { body } });
        setEditingCommentId(null);
        await reloadBugModal();
      }, "Saving comment…");
      toast("Comment updated", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onCancelEditComment = (): void => {
    setEditingCommentId(null);
    void reloadBugModal();
  };

  // Composer files always attach to the comment, not the bug.
  const onPostComment = async (): Promise<void> => {
    if (!bugId) return;
    const body = (commentRef.current?.getHtml() ?? "").trim();
    const files = commentStaged.files;
    if (!body && files.length === 0) {
      toast("Add a comment or attach a file", "error");
      commentRef.current?.focus();
      return;
    }
    if (!body && files.length > 0) {
      toast(
        "Add some comment text — files attached here become comment " +
          "attachments. For a bug-level file, use the 📎 Add attachment " +
          "button above.",
        "error",
      );
      commentRef.current?.focus();
      return;
    }
    try {
      await withLoader(async () => {
        const comment = await api<CommentOut>(`/bugs/${bugId}/comments`, {
          method: "POST",
          json: { body },
        });
        let failed = 0;
        for (const s of files) {
          const fd = new FormData();
          fd.append("file", s.file);
          fd.append("comment_id", String(comment.id));
          try {
            await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
          } catch (err) {
            failed++;
            toast(`Attachment ${s.file.name}: ${errorMessage(err)}`, "error");
          }
        }
        if (body) toast("Comment posted", "success");
        else if (files.length && !failed) {
          toast(`${files.length} file${files.length > 1 ? "s" : ""} attached`, "success");
        }
        // Reset for the next post.
        commentRef.current?.setHtml("");
        commentStaged.clear();
        await reloadBugModal();
        await refreshBugs();
      }, commentSubmitLabel(body, files.length));
    } catch (err) {
      toastError(err);
    }
  };

  // Flush staged bug-level attachments on "Upload N file(s)".
  const flushBugAttach = async (): Promise<void> => {
    if (!bugId) return;
    const files = bugAttachStaged.files;
    if (!files.length) return;
    let done = 0;
    for (const s of files) {
      const fd = new FormData();
      fd.append("file", s.file);
      try {
        await api(`/bugs/${bugId}/attachments`, { method: "POST", body: fd });
        done++;
      } catch (err) {
        toast(`Attachment ${s.file.name}: ${errorMessage(err)}`, "error");
      }
    }
    bugAttachStaged.clear();
    if (done) toast(`${done} file${done > 1 ? "s" : ""} attached`, "success");
    // Refresh so the new attachment_count shows in the table cell too.
    await reloadBugModal();
    await refreshBugs();
  };

  // Link this item to every item chosen in the picker.
  const onAddLink = async (): Promise<void> => {
    if (!bugId) return;
    if (linkTargets.length === 0) {
      toast("Pick one or more items to link", "error");
      return;
    }
    try {
      await withLoader(async () => {
        let done = 0;
        let failed = 0;
        for (const t of linkTargets) {
          try {
            await api(`/bugs/${bugId}/links`, {
              method: "POST",
              json: { target_bug_id: t.id, link_type: linkType },
            });
            done++;
          } catch (err) {
            failed++;
            toast(`Link to #${t.id}: ${errorMessage(err)}`, "error");
          }
        }
        setLinkTargets([]);
        await reloadBugModal();
        if (done) {
          toast(
            `Linked ${done} item${done > 1 ? "s" : ""}${failed ? ` (${failed} failed)` : ""}`,
            "success",
          );
        }
      }, linkTargets.length > 1 ? "Linking items…" : "Linking…");
    } catch (err) {
      toastError(err);
    }
  };

  const onRemoveLink = async (linkId: number): Promise<void> => {
    if (!bugId) return;
    try {
      await withLoader(async () => {
        await api(`/bugs/${bugId}/links/${linkId}`, { method: "DELETE" });
        await reloadBugModal();
      }, "Removing link…");
      toast("Link removed", "success");
    } catch (err) {
      toastError(err);
    }
  };

  const onSubmit = async (e: FormEvent<HTMLFormElement>): Promise<void> => {
    e.preventDefault();
    const id = bugId;
    // New items use the current user as reporter; edits preserve the original.
    const reporterFromForm = isEdit ? bug.reporter?.id ?? null : null;
    const reporterFromMe = currentUser.id || null;
    const payload = {
      project_id: Number.parseInt(projectId, 10),
      title: title.trim(),
      description: descRef.current?.getHtml() ?? "",
      reporter_id: id ? reporterFromForm || reporterFromMe : reporterFromMe,
      item_type: itemType || "Bug",
      status,
      priority,
      environment,
      due_date: dueDate || null,
      // Empty or "0" → null so the server treats it as an explicit unlink.
      event_id: eventId && eventId !== "0" ? Number.parseInt(eventId, 10) : null,
      assignee_ids: assigneeIds,
      // Optimistic concurrency: send the opened version so the server 409s on a concurrent edit.
      ...(id && expectedVersionRef.current != null
        ? { expected_version: expectedVersionRef.current }
        : {}),
    };
    // Persist the chosen type so the next new-item modal defaults to it.
    if (!id) setDefaultNewType((payload.item_type as ItemType) || "Bug");
    if (!payload.project_id) {
      toast("Please pick a project", "error");
      return;
    }
    if (!payload.title) {
      toast("Title is required", "error");
      return;
    }
    if (!payload.reporter_id) {
      toast("Reporter is required", "error");
      return;
    }

    // Guard staged-but-never-uploaded files (edit mode only; create uploads them in-flow).
    if (id) {
      if (bugAttachStaged.files.length > 0) {
        const n = bugAttachStaged.files.length;
        toast(
          `You have ${n} attachment${n > 1 ? "s" : ""} that ${n > 1 ? "haven't" : "hasn't"} been uploaded yet. ` +
            'Click "Upload" in the Attachments section, or remove ' +
            `${n > 1 ? "them" : "it"}, before saving.`,
          "error",
        );
        return;
      }
      if (commentStaged.files.length > 0) {
        const n = commentStaged.files.length;
        toast(
          `You have ${n} file${n > 1 ? "s" : ""} attached to a comment you haven't posted yet. ` +
            'Click "Post" to add the comment, or remove ' +
            `${n > 1 ? "them" : "it"}, before saving.`,
          "error",
        );
        return;
      }
    }

    // refreshAll() keeps the Work Items list in sync after the modal closes.
    try {
      if (id) {
        const result = await withLoader(async () => {
          const updated = await api<BugOut>(`/bugs/${id}`, { method: "PUT", json: payload });
          closeBugModal();
          await refreshAll();
          return updated;
        }, "Saving changes…");
        const utype = result?.item_type || payload.item_type || "Bug";
        toast(`${utype} #${id} updated`, "success");
      } else {
        // POST the item, then upload staged files; per-file failures toast but don't roll back the create.
        await withLoader(async () => {
          const created = await api<BugOut>("/bugs", { method: "POST", json: payload });
          const ctype = created?.item_type || payload.item_type || "Bug";
          const files = createStaged.files.map((s) => s.file);
          await toastAfterCreate(created, ctype, files);
          createStaged.clear();
          closeBugModal();
          await refreshAll();
        }, "Creating item…");
      }
    } catch (err) {
      toastError(err);
    }
  };

  // Ctrl/Cmd+Enter to post.
  const onComposerKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      void onPostComment();
    }
  };

  // Ctrl/Cmd+Enter to save inline edits.
  const onCommentsListKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && editingCommentId != null) {
      e.preventDefault();
      void onSaveEditComment(editingCommentId);
    }
  };

  const renderComment = (c: CommentOut) => {
    const hasBody = (c.body || "").trim().length > 0;
    const isEditing = editingCommentId === c.id;
    return (
      <div className="comment" data-comment-id={c.id} key={c.id}>
        <div className="comment-head">
          <div className="comment-head-left">
            <span className="avatar">{initials(c.author_name)}</span>
            <span className="comment-author">{c.author_name}</span>
          </div>
          <span className="comment-head-right">
            <span className="comment-time">{formatDate(c.created_at)}</span>
            {isAdmin && (
              <span className="comment-admin-actions">
                {hasBody && (
                  <button
                    type="button"
                    className="icon-btn"
                    data-act="edit-comment"
                    data-id={c.id}
                    title="Edit comment"
                    onClick={() => setEditingCommentId(c.id)}
                  >
                    ✎
                  </button>
                )}
                <button
                  type="button"
                  className="icon-btn danger"
                  data-act="delete-comment"
                  data-id={c.id}
                  title="Delete comment"
                  onClick={() => void onDeleteComment(c.id)}
                >
                  🗑
                </button>
              </span>
            )}
          </span>
        </div>
        {isEditing ? (
          <div className="comment-edit-row">
            <RichEditor
              key={`comment-edit-${c.id}`}
              ref={editCommentRef}
              initialHtml={c.body || ""}
              placeholder="Edit comment…"
              onPasteFile={onEditCommentPaste}
            />
            <div className="comment-edit-actions">
              <button
                type="button"
                className="btn ghost"
                data-act="cancel-edit-comment"
                data-id={c.id}
                onClick={onCancelEditComment}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn primary"
                data-act="save-edit-comment"
                data-id={c.id}
                onClick={() => void onSaveEditComment(c.id)}
              >
                Save
              </button>
            </div>
          </div>
        ) : hasBody ? (
          // Server sanitizes on write; sanitizeHtml() here is a defensive second pass.
          <div
            className="comment-body bh-rich-content"
            data-comment-body={c.id}
            dangerouslySetInnerHTML={{ __html: sanitizeHtml(c.body) }}
          />
        ) : null}
        {c.attachments.length > 0 && (
          <div className="comment-attachments">
            <div className="attachment-grid">
              {c.attachments.map((a) => (
                <AttachmentCard
                  key={a.id}
                  att={a}
                  bugId={c.bug_id}
                  deletable={isAdmin}
                  onDelete={(attId) => void onDeleteAttachment(attId)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="modal" id="modalBug" hidden={!open}>
      <div className="modal-card xxl">
        <div className="modal-head">
          <div className="bug-modal-head-left">
            <h2 id="modalBugTitle">{headTitle}</h2>
            <span className="bug-modal-subtitle muted small" id="modalBugSubtitle">
              {headSubtitle}
            </span>
          </div>
          <div className="bug-modal-head-actions">
            <button
              type="button"
              className="btn danger"
              id="bugDeleteBtn"
              data-needs-role="admin"
              hidden={!(isEdit && isAdmin) || readOnly}
              onClick={() => void onDeleteBug()}
            >
              🗑 Delete
            </button>
            <button
              type="button"
              className="icon-btn modal-close"
              data-close-modal
              aria-label="Close"
              onClick={closeBugModal}
            >
              ✕
            </button>
          </div>
        </div>
        <form
          id="formBug"
          className="modal-body bug-modal-body"
          noValidate
          data-read-only={readOnly ? "1" : ""}
          onSubmit={(e) => void onSubmit(e)}
        >
          {readOnly && (
            <div className="bug-readonly-banner">
              {`Read-only — only admins and managers can edit ${(bug?.item_type || "item").toLowerCase()}s.`}
            </div>
          )}
          <input type="hidden" name="id" value={bugId != null ? String(bugId) : ""} readOnly />

          {/* Title — full-width headline */}
          <label className="field f-grow bug-title-field">
            <span>
              Title <em>*</em>
            </span>
            <input
              name="title"
              required
              minLength={3}
              maxLength={200}
              placeholder="Short summary…"
              ref={titleRef}
              value={title}
              disabled={readOnly}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>

          <div className="bug-modal-grid">
            {/* Main column */}
            <div className="bug-modal-main">
              {/* div not label: a <label> would route clicks to the hidden native textarea, not the surface. */}
              <div className="field">
                <span>Description</span>
                <RichEditor
                  ref={descRef}
                  name="description"
                  placeholder="Context, acceptance criteria, repro steps — whatever's useful…"
                  disabled={readOnly}
                  onPasteFile={onDescPaste}
                />
              </div>

              {/* Attachments for new items — staged and uploaded after the POST. */}
              <section
                className="bug-section bug-create-attach-section"
                id="bugCreateAttachSection"
                hidden={isEdit}
              >
                <div
                  className={`comment-form attach-dropzone${createDrop.dragging ? " is-dragging" : ""}`}
                  {...createDrop.dropProps}
                >
                  <div className="attach-drop-hint">Drop files to attach</div>
                  <div className="comment-form-row">
                    <label className="comment-attach-btn" title="Attach files">
                      📎 <span id="createFileLabel">{stagedLabel(createStaged.files.length, "Attach files")}</span>
                      <input
                        type="file"
                        multiple
                        id="createBugFiles"
                        aria-label="Attachments for new bug"
                        onChange={(e) => {
                          // Reset value so re-picking the same file fires onChange again.
                          createStaged.addFiles(e.currentTarget.files ?? []);
                          e.currentTarget.value = "";
                        }}
                      />
                    </label>
                    <div className="attach-staged-list" id="createFilePreview">
                      {createStaged.files.map((s, i) => (
                        <StagedTile
                          key={s.url}
                          staged={s}
                          bucket="createBug"
                          idx={i}
                          onRemove={() => createStaged.removeAt(i)}
                        />
                      ))}
                    </div>
                  </div>
                  <p className="muted small create-attach-hint">
                    Files attach to the item itself. Add more later via the Add-attachment button or via comments
                  </p>
                </div>
              </section>

              {/* Bug-level attachments (edit mode); drop files here to stage. */}
              <section
                className={`bug-section${canEditThis && bugAttachDrop.dragging ? " attach-dropzone is-dragging" : ""}`}
                id="bugAttachmentsSection"
                hidden={!isEdit}
                {...(canEditThis ? bugAttachDrop.dropProps : {})}
              >
                {canEditThis && <div className="attach-drop-hint">Drop files to attach</div>}
                <div className="bug-section-head">
                  <h3>
                    Attachments{" "}
                    <span className="muted small" id="attachmentsCount">
                      {bug && bug.attachments.length ? `(${bug.attachments.length})` : ""}
                    </span>
                  </h3>
                  <label
                    className="comment-attach-btn bug-attach-add-btn"
                    id="bugAttachAddLabel"
                    title="Add an attachment to this item"
                    hidden={!isEdit || !canEditThis}
                  >
                    📎 <span id="bugAttachAddText">{stagedLabel(bugAttachStaged.files.length, "Add attachment")}</span>
                    <input
                      type="file"
                      multiple
                      id="bugAttachAddInput"
                      aria-label="Add an attachment to this item"
                      onChange={(e) => {
                        bugAttachStaged.addFiles(e.currentTarget.files ?? []);
                        e.currentTarget.value = "";
                      }}
                    />
                  </label>
                </div>
                <div className="attach-staged-list" id="bugAttachAddPreview">
                  {bugAttachStaged.files.map((s, i) => (
                    <StagedTile
                      key={s.url}
                      staged={s}
                      bucket="bugAttach"
                      idx={i}
                      onRemove={() => bugAttachStaged.removeAt(i)}
                    />
                  ))}
                  {bugAttachStaged.files.length > 0 && (
                    <div className="attach-staged-actions">
                      <button
                        type="button"
                        className="btn primary attach-staged-upload"
                        onClick={() => {
                          withLoader(flushBugAttach, "Uploading attachment(s)…").catch(toastError);
                        }}
                      >
                        Upload {bugAttachStaged.files.length} file
                        {bugAttachStaged.files.length > 1 ? "s" : ""}
                      </button>
                      <button
                        type="button"
                        className="btn ghost attach-staged-cancel"
                        onClick={() => bugAttachStaged.clear()}
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
                <div className="attachment-grid" id="bugAttachmentsGrid">
                  {bug?.attachments.map((a) => (
                    <AttachmentCard
                      key={a.id}
                      att={a}
                      bugId={bug.id}
                      deletable={isAdmin}
                      onDelete={(attId) => void onDeleteAttachment(attId)}
                    />
                  ))}
                </div>
                <div
                  className="bug-attach-empty muted small"
                  id="bugAttachEmpty"
                  hidden={!bug || bug.attachments.length > 0 || !canEditThis}
                >
                  No attachments yet. Click <strong>📎 Add attachment</strong> above to upload one
                </div>
              </section>

              {/* Comments — visible in edit and read-only view. Composer is a <div>, not a <form> (HTML5 forbids nesting). */}
              <section className="bug-section" id="bugCommentsSection" hidden={!isEdit}>
                <h3>
                  Comments{" "}
                  <span className="muted small" id="commentsCount">
                    {bug ? `(${bug.comments.length})` : ""}
                  </span>
                </h3>
                <div
                  id="bugCommentsList"
                  className="bug-comments-list"
                  hidden={!bug || bug.comments.length === 0}
                  onKeyDown={onCommentsListKeyDown}
                >
                  {bug?.comments.map((c) => renderComment(c))}
                </div>
                <div
                  className={`comment-form attach-dropzone${commentDrop.dragging ? " is-dragging" : ""}`}
                  id="commentForm"
                  hidden={readOnly}
                  onKeyDown={onComposerKeyDown}
                  {...commentDrop.dropProps}
                >
                  <div className="attach-drop-hint">Drop files to attach</div>
                  <RichEditor
                    ref={commentRef}
                    textareaId="commentBody"
                    placeholder="Add a comment, attach a file, or both (Ctrl/Cmd+Enter to post)"
                    onPasteFile={onComposerPaste}
                  />
                  <div className="comment-form-row">
                    <label className="comment-attach-btn" title="Attach files">
                      📎 <span id="fileLabel">{stagedLabel(commentStaged.files.length, "Attach files")}</span>
                      <input
                        type="file"
                        multiple
                        id="commentFiles"
                        onChange={(e) => {
                          commentStaged.addFiles(e.currentTarget.files ?? []);
                          e.currentTarget.value = "";
                        }}
                      />
                    </label>
                    <div className="attach-staged-list" id="filePreview">
                      {commentStaged.files.map((s, i) => (
                        <StagedTile
                          key={s.url}
                          staged={s}
                          bucket="comment"
                          idx={i}
                          onRemove={() => commentStaged.removeAt(i)}
                        />
                      ))}
                    </div>
                    {commentStaged.files.length > 0 && (
                      <button
                        type="button"
                        className="btn ghost attach-staged-cancel"
                        style={{ marginLeft: "auto" }}
                        onClick={() => commentStaged.clear()}
                      >
                        Cancel
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn primary"
                      id="commentPostBtn"
                      style={commentStaged.files.length > 0 ? undefined : { marginLeft: "auto" }}
                      onClick={() => void onPostComment()}
                    >
                      Post
                    </button>
                  </div>
                </div>
              </section>

              {/* Linked items — inverse phrasing computed server-side. */}
              <section className="bug-section bug-links-section" id="bugLinksSection" hidden={!isEdit}>
                <h3>
                  Linked items{" "}
                  <span className="muted small" id="linksCount">
                    {bug ? `(${bug.links.length})` : ""}
                  </span>
                </h3>
                {bug && bug.links.length > 0 ? (
                  <div className="bug-links-list" id="bugLinksList">
                    {bug.links.map((link) => (
                      <div className="bug-link-row" data-link-id={link.id} key={link.id}>
                        <span className="bug-link-rel">{link.label}</span>
                        <button
                          type="button"
                          className="bug-link-target"
                          title={`Open #${link.other_bug_id}`}
                          onClick={() => void openBugDetail(link.other_bug_id)}
                        >
                          <span className="inline-type" data-type={link.other_bug_item_type}>
                            {itemTypeEmoji(link.other_bug_item_type)}
                          </span>{" "}
                          #{link.other_bug_id} {link.other_bug_title}
                        </button>
                        <span className="badge" data-status={link.other_bug_status}>
                          {link.other_bug_status}
                        </span>
                        {canEditThis && (
                          <button
                            type="button"
                            className="icon-btn danger bug-link-remove"
                            title="Remove link"
                            aria-label="Remove link"
                            onClick={() => void onRemoveLink(link.id)}
                          >
                            ✕
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="no-content">No linked items yet</p>
                )}
                {canEditThis && (
                  <div className="bug-link-add" id="bugLinkAdd">
                    <div className="bug-link-add-type">
                      <BhSelect
                        name="link_type"
                        value={linkType}
                        onChange={setLinkType}
                        options={LINK_TYPE_OPTS}
                      />
                    </div>
                    <div className="bug-link-add-target">
                      <ItemPicker
                        selected={linkTargets}
                        onChange={setLinkTargets}
                        excludeIds={[
                          ...(bugId != null ? [bugId] : []),
                          ...(bug?.links.map((l) => l.other_bug_id) ?? []),
                        ]}
                      />
                    </div>
                    <button
                      type="button"
                      className="btn primary"
                      disabled={linkTargets.length === 0}
                      onClick={() => void onAddLink()}
                    >
                      Link{linkTargets.length > 1 ? ` ${linkTargets.length}` : ""}
                    </button>
                  </div>
                )}
              </section>

              {/* Activity — collapsible */}
              <section
                className="bug-section bug-activity-section"
                id="bugActivitySection"
                hidden={!isEdit}
              >
                <details>
                  <summary>
                    Activity history{" "}
                    <span className="muted small" id="activityCount">
                      {bug ? `(${bug.activities.length})` : ""}
                    </span>
                  </summary>
                  <div id="bugActivityList" className="bug-activity-list">
                    {bug && bug.activities.length > 0 ? (
                      bug.activities.map((a) => <ActivityRow key={a.id} activity={a} />)
                    ) : (
                      <p className="no-content">No activity yet</p>
                    )}
                  </div>
                </details>
              </section>
            </div>

            {/* Side column — metadata */}
            <aside className="bug-modal-side" aria-label="Work item metadata">
              <label className="field">
                <span>
                  Type <em>*</em>
                </span>
                <BhSelect
                  name="item_type"
                  value={itemType}
                  onChange={(v) => setItemType(v)}
                  options={itemTypeOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>
                  Project <em>*</em>
                </span>
                <BhSelect
                  name="project_id"
                  value={projectId}
                  onChange={setProjectId}
                  options={projectOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>Status</span>
                <BhSelect
                  name="status"
                  value={status}
                  onChange={setStatus}
                  options={statusOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>Priority</span>
                <BhSelect
                  name="priority"
                  value={priority}
                  onChange={setPriority}
                  options={priorityOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>Environment</span>
                <BhSelect
                  name="environment"
                  value={environment}
                  onChange={setEnvironment}
                  options={environmentOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>Reporter</span>
                {/* Fixed to current user (or original reporter on edit); disabled, id comes from state. Hand-rolled so the reporter-select class survives (BhSelect can't add classes). */}
                <div className="bh-sel-wrap">
                  <select
                    className="bh-sel-native reporter-select"
                    name="reporter_id"
                    disabled
                    value={String(reporterId)}
                    onChange={() => undefined}
                  >
                    <option value="">— select —</option>
                    <option value={String(reporterOption.id)} title={reporterOption.email}>
                      {reporterOption.name}
                    </option>
                  </select>
                  <button
                    type="button"
                    className="bh-sel-btn is-disabled"
                    aria-haspopup="listbox"
                    aria-expanded={false}
                    disabled
                  >
                    <span className="bh-sel-label">{reporterOption.name}</span>
                  </button>
                </div>
              </label>
              <fieldset className="field">
                <legend>Assignees</legend>
                <ChipPicker
                  id="assigneePicker"
                  items={assigneeItems}
                  selected={assigneeIds}
                  disabled={readOnly}
                  onToggle={(uid) =>
                    setAssigneeIds((prev) =>
                      prev.includes(uid) ? prev.filter((x) => x !== uid) : [...prev, uid],
                    )
                  }
                />
              </fieldset>
              <label className="field">
                <span>Event</span>
                <BhSelect
                  name="event_id"
                  value={eventId}
                  onChange={setEventId}
                  options={eventOpts}
                  disabled={readOnly}
                />
              </label>
              <label className="field">
                <span>Due date</span>
                <BhDateInput
                  name="due_date"
                  value={dueDate}
                  onChange={setDueDate}
                  disabled={readOnly}
                />
              </label>

              {/* Timestamps — visible in edit mode only */}
              <div className="bug-side-meta" id="bugSideMeta" hidden={!isEdit}>
                <div className="bug-side-meta-row">
                  <span className="k">Created</span>
                  <span className="v" id="bugMetaCreated">
                    {bug ? formatDate(bug.created_at) : "—"}
                  </span>
                </div>
                <div className="bug-side-meta-row">
                  <span className="k">Updated</span>
                  <span className="v" id="bugMetaUpdated">
                    {bug ? formatDate(bug.updated_at) : "—"}
                  </span>
                </div>
              </div>
            </aside>
          </div>

          <div className="modal-foot">
            <button type="button" className="btn ghost" data-close-modal onClick={closeBugModal}>
              Cancel
            </button>
            {/* #bugSubmitBtn must stay inside #formBug — tests depend on it. */}
            <button type="submit" className="btn primary" id="bugSubmitBtn" hidden={readOnly}>
              {isEdit ? "Save changes" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
