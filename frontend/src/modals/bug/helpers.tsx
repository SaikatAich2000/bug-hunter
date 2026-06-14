/**
 * Shared building blocks for the bug list + unified bug modal — ports of the
 * vanilla helpers in app/static/app.js:
 *
 *  - ITEM_TYPE_EMOJI / itemTypeEmoji            (L2199-2200)
 *  - canEditItem                                 (L2735-2740)
 *  - statusesForType                             (L1784-1787)
 *  - activityIcon                                (L3138-3150)
 *  - renderAttachmentCard → <AttachmentCard/>    (L3092-3124)
 *  - renderActivityRow    → <ActivityRow/>       (L3126-3136)
 *  - attachment STAGING (STATE.stagedFiles + _renderStagedFiles /
 *    clearStagedFiles / handleStagedInputChange / handleStagedListClick,
 *    L3152-3280) → useStagedFiles() + <StagedTile/>
 *
 * All class names / data-attributes match the vanilla markup exactly so
 * styles.css applies unchanged.
 */
import { useCallback, useState, type ReactNode } from "react";
import { fileIcon, formatBytes, formatDate } from "../../lib/format";
import type { ActivityOut, AttachmentOut, MetaOut } from "../../types";

// ---------------------------------------------------------------------------
// Item-type emoji (vanilla L2199-2200)
// ---------------------------------------------------------------------------

export const ITEM_TYPE_EMOJI: Record<string, string> = {
  Bug: "🐞",
  Requirement: "📐",
  Task: "✅",
};

export function itemTypeEmoji(t: string): string {
  return ITEM_TYPE_EMOJI[t] ?? "📝";
}

// ---------------------------------------------------------------------------
// Permissions / meta lookups
// ---------------------------------------------------------------------------

/**
 * Mirror of the backend can_edit_bug rule (vanilla canEditItem, L2735-2740).
 * Regular users can edit Bugs; Tasks and Requirements require manager/admin.
 */
export function canEditItemType(role: string, itemType: string | null | undefined): boolean {
  if (role === "admin" || role === "manager") return true;
  return (itemType || "Bug") === "Bug";
}

/** Port of statusesForType (vanilla L1784-1787). */
export function statusesForType(meta: MetaOut, itype: string): string[] {
  const byType = meta.statuses_by_type as Record<string, string[] | undefined>;
  return byType[itype || "Bug"] ?? meta.statuses ?? [];
}

/** Local-time YYYY-MM-DD (port of _isoDate, used for the create-mode due-date default). */
export function isoToday(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

/** Mirrors the vanilla `err.message` toast interpolation. */
export function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : String(err);
}

/** Staged-file count label: "Attach files" / "N file(s)" (vanilla _renderStagedFiles). */
export function stagedLabel(count: number, idle: string): string {
  return count ? `${count} file${count > 1 ? "s" : ""}` : idle;
}

// ---------------------------------------------------------------------------
// Activity icon + row (vanilla L3126-3150)
// ---------------------------------------------------------------------------

export function activityIcon(action: string): string {
  if (action.includes("session")) return "🔐";
  if (action.includes("login")) return "🔑";
  if (action.includes("logout")) return "👋";
  if (action.includes("password")) return "🔒";
  if (action.includes("created")) return "✨";
  if (action.includes("delete")) return "🗑";
  if (action.includes("comment")) return "💬";
  if (action.includes("attachment")) return "📎";
  if (action.includes("status")) return "🔄";
  if (action.includes("assign")) return "👥";
  return "📝";
}

export function ActivityRow({ activity }: { activity: ActivityOut }) {
  return (
    <div className="activity-row">
      <span className="activity-icon">{activityIcon(activity.action)}</span>
      <div className="activity-text">
        <div>
          <span className="activity-actor">{activity.actor_name}</span>
          <span className="activity-action">{activity.action}</span>
        </div>
        {activity.detail ? <div className="activity-detail">{activity.detail}</div> : null}
      </div>
      <span className="activity-time">{formatDate(activity.created_at)}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Attachment card (vanilla renderAttachmentCard, L3092-3124)
// ---------------------------------------------------------------------------

interface AttachmentCardProps {
  att: AttachmentOut;
  /** Used to build the download URL — /api/bugs/{bug_id}/attachments/{id}/download. */
  bugId: number;
  /** Admin-only delete button. */
  deletable: boolean;
  onDelete: (attId: number) => void;
}

export function AttachmentCard({ att, bugId, deletable, onDelete }: AttachmentCardProps) {
  const url = `/api/bugs/${bugId}/attachments/${att.id}/download`;
  const ct = (att.content_type || "").toLowerCase();
  // Inline rendering is safe for raster images and video. SVG can carry
  // inline JS (server downgrades it on download) so it gets the file icon.
  const isRasterImg = ct.startsWith("image/") && ct !== "image/svg+xml";
  let preview: ReactNode;
  if (isRasterImg) {
    preview = (
      <a href={url} target="_blank" rel="noopener">
        <img src={url} alt={att.filename} loading="lazy" />
      </a>
    );
  } else if (ct.startsWith("video/")) {
    preview = (
      <video controls preload="metadata">
        <source src={url} type={att.content_type} />
      </video>
    );
  } else {
    preview = (
      <a href={url} target="_blank" rel="noopener" className="file-icon">
        {fileIcon(att.content_type, att.filename)}
      </a>
    );
  }
  return (
    <div className="attach-card" data-att-id={att.id}>
      <div className="attach-preview">{preview}</div>
      <div className="attach-meta">
        <div className="attach-name" title={att.filename}>{att.filename}</div>
        <div className="attach-info">
          <span>{formatBytes(att.size_bytes)}</span>
          <span>{att.uploader_name}</span>
        </div>
      </div>
      <div className="attach-actions">
        <a href={url} target="_blank" rel="noopener">View</a>
        <a href={url} download={att.filename}>Download</a>
        {deletable && (
          <button
            type="button"
            className="danger"
            data-act="delete-attachment"
            data-id={att.id}
            onClick={() => onDelete(att.id)}
          >
            Delete
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Attachment staging (vanilla L3152-3280)
//
// `input.files` is a read-only FileList, so staged selections are copied into
// a plain array (with a blob URL each for the preview tile). Buckets are
// React state owned by the bug modal: createBug / comment / bugAttach.
// ---------------------------------------------------------------------------

export interface StagedFile {
  file: File;
  /** Blob URL for the preview tile — revoked on remove/clear. */
  url: string;
}

export interface StagedBucket {
  files: StagedFile[];
  /** ADD to (not replace) the bucket so files can be picked in batches. */
  addFiles: (files: File[] | FileList) => void;
  removeAt: (idx: number) => void;
  clear: () => void;
}

export function useStagedFiles(): StagedBucket {
  const [files, setFiles] = useState<StagedFile[]>([]);

  const addFiles = useCallback((fs: File[] | FileList) => {
    const list = Array.from(fs);
    if (!list.length) return;
    const staged = list.map((f) => ({ file: f, url: URL.createObjectURL(f) }));
    setFiles((prev) => [...prev, ...staged]);
  }, []);

  const removeAt = useCallback((idx: number) => {
    setFiles((prev) => {
      const target = prev[idx];
      if (!target) return prev;
      try {
        URL.revokeObjectURL(target.url);
      } catch {
        /* already revoked */
      }
      return prev.filter((_, i) => i !== idx);
    });
  }, []);

  const clear = useCallback(() => {
    setFiles((prev) => {
      for (const s of prev) {
        try {
          URL.revokeObjectURL(s.url);
        } catch {
          /* already revoked */
        }
      }
      return prev.length ? [] : prev;
    });
  }, []);

  return { files, addFiles, removeAt, clear };
}

interface StagedTileProps {
  staged: StagedFile;
  bucket: string;
  idx: number;
  onRemove: () => void;
}

/** One pending-upload preview tile (vanilla _renderStagedFiles per-file markup). */
export function StagedTile({ staged, bucket, idx, onRemove }: StagedTileProps) {
  // v2.6 fix: if the blob URL can't render as a real image, swap to the
  // generic file icon so the row stays readable.
  const [broken, setBroken] = useState(false);
  const f = staged.file;
  const wasImage = (f.type || "").startsWith("image/");
  const showImg = wasImage && !broken;
  const icon = fileIcon(f.type, f.name);
  return (
    <span className="attach-staged" data-bucket={bucket} data-idx={idx} data-obj-url={staged.url}>
      {showImg ? (
        <a className="attach-staged-link" href={staged.url} target="_blank" rel="noopener">
          <img
            className="attach-staged-thumb"
            src={staged.url}
            alt={f.name}
            data-fallback={icon}
            onError={() => setBroken(true)}
          />
        </a>
      ) : (
        <a
          className={`attach-staged-link${wasImage && broken ? " attach-staged-icon-only" : ""}`}
          href={staged.url}
          target="_blank"
          rel="noopener"
          title={wasImage ? undefined : `Open ${f.name}`}
        >
          {icon}
        </a>
      )}
      <span className="attach-staged-meta">
        <span className="attach-staged-name" title={f.name}>{f.name}</span>
        <span className="attach-staged-size muted small">{formatBytes(f.size)}</span>
      </span>
      <button
        type="button"
        className="attach-staged-remove"
        aria-label={`Remove ${f.name}`}
        title="Remove (not yet uploaded)"
        onClick={(e) => {
          e.preventDefault();
          onRemove();
        }}
      >
        ✕
      </button>
    </span>
  );
}
