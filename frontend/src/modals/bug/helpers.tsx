/**
 * Shared building blocks for the bug list and unified bug modal: item-type
 * emoji, permission/meta lookups, the activity icon + row, the attachment
 * card, and attachment staging (useStagedFiles + StagedTile).
 *
 * Class names and data-attributes match the markup that styles.css keys off.
 */
import { useCallback, useState, type ReactNode } from "react";
import { activityIcon, fileIcon, formatBytes, formatDate } from "../../lib/format";
import { toast } from "../../lib/toast";
import { partitionBySize } from "../../lib/upload";

// Executable / script extensions blocked client-side as UX (the server also
// rejects them — this just gives immediate feedback). Mirrors the backend
// _DANGEROUS_UPLOAD_EXTS denylist. Trailing dots/spaces are stripped first so
// "evil.exe." can't slip past.
const _DANGEROUS_EXTS = new Set([
  // .js intentionally allowed (legit source; neutralized at download). Mirrors
  // the backend _DANGEROUS_UPLOAD_EXTS denylist.
  "exe", "msi", "bat", "cmd", "com", "scr", "pif", "cpl", "hta", "jar",
  "jse", "vbs", "vbe", "wsf", "wsh", "ps1", "psm1", "sh", "bash",
  "app", "dmg", "pkg", "deb", "rpm", "apk", "msc", "reg", "lnk", "gadget",
  "dll", "sys", "elf",
]);

function isDangerousFile(name: string): boolean {
  const cleaned = (name || "").trim().replace(/[.\s]+$/, "");
  const dot = cleaned.lastIndexOf(".");
  if (dot < 0) return false;
  return _DANGEROUS_EXTS.has(cleaned.slice(dot + 1).trim().toLowerCase());
}

// Re-exported from lib/format so existing importers of this module keep working.
export { activityIcon };
import VideoLightbox from "../../components/VideoLightbox";
import type { ActivityOut, AttachmentOut, MetaOut } from "../../types";

// ---------------------------------------------------------------------------
// Item-type emoji
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
 * Mirror of the backend can_edit_bug rule. Regular users can edit Bugs; Tasks
 * and Requirements require manager/admin.
 */
export function canEditItemType(role: string, itemType: string | null | undefined): boolean {
  if (role === "admin" || role === "manager") return true;
  return (itemType || "Bug") === "Bug";
}

/** Allowed statuses for a given item type, falling back to the global list. */
export function statusesForType(meta: MetaOut, itype: string): string[] {
  const byType = meta.statuses_by_type as Record<string, string[] | undefined>;
  return byType[itype || "Bug"] ?? meta.statuses ?? [];
}

/** Local-time YYYY-MM-DD, used for the create-mode due-date default. */
export function isoToday(): string {
  const d = new Date();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
}

export function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : String(err);
}

/** Staged-file count label: "Attach files" / "N file(s)". */
export function stagedLabel(count: number, idle: string): string {
  if (!count) return idle;
  return `${count} file${count > 1 ? "s" : ""}`;
}

// ---------------------------------------------------------------------------
// Activity icon + row
// ---------------------------------------------------------------------------

export function ActivityRow({ activity }: Readonly<{ activity: ActivityOut }>) {
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
// Attachment card
// ---------------------------------------------------------------------------

interface AttachmentCardProps {
  att: AttachmentOut;
  /** Used to build the download URL — /api/bugs/{bug_id}/attachments/{id}/download. */
  bugId: number;
  /** Admin-only delete button. */
  deletable: boolean;
  onDelete: (attId: number) => void;
}

export function AttachmentCard({ att, bugId, deletable, onDelete }: Readonly<AttachmentCardProps>) {
  const url = `/api/bugs/${bugId}/attachments/${att.id}/download`;
  const ct = (att.content_type || "").toLowerCase();
  // Inline rendering is safe for raster images and video. SVG can carry
  // inline JS (server downgrades it on download) so it gets the file icon.
  const isRasterImg = ct.startsWith("image/") && ct !== "image/svg+xml";
  const isVideo = ct.startsWith("video/");
  // Video plays in the themed lightbox (so "View" never falls back to the
  // browser's native player), opened from the thumbnail or the View action.
  const [videoOpen, setVideoOpen] = useState(false);
  const openVideo = useCallback(() => setVideoOpen(true), []);

  let preview: ReactNode;
  if (isRasterImg) {
    preview = (
      <a href={url} target="_blank" rel="noopener">
        <img src={url} alt={att.filename} loading="lazy" />
      </a>
    );
  } else if (isVideo) {
    // Compact poster (the #t=0.1 fragment makes the browser paint the first
    // frame) + a play badge; clicking opens the full player in the lightbox.
    preview = (
      <button
        type="button"
        className="attach-video-thumb"
        onClick={openVideo}
        aria-label={`Play ${att.filename}`}
      >
        {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
        <video
          className="attach-video-poster"
          src={`${url}#t=0.1`}
          preload="metadata"
          muted
          playsInline
          tabIndex={-1}
        />
        <span className="attach-video-play" aria-hidden="true">▶</span>
      </button>
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
        {isVideo ? (
          <button type="button" data-act="view-video" onClick={openVideo}>
            View
          </button>
        ) : (
          <a href={url} target="_blank" rel="noopener">View</a>
        )}
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
      {isVideo && videoOpen && (
        <VideoLightbox
          src={url}
          type={att.content_type}
          label={att.filename}
          onClose={() => setVideoOpen(false)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Attachment staging
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
  /** Add to (not replace) the bucket so files can be picked in batches. */
  addFiles: (files: File[] | FileList) => void;
  removeAt: (idx: number) => void;
  clear: () => void;
}

export function useStagedFiles(): StagedBucket {
  const [files, setFiles] = useState<StagedFile[]>([]);

  const addFiles = useCallback((fs: File[] | FileList) => {
    const list = Array.from(fs);
    if (!list.length) return;
    // Stop files that are over the size limit before any upload starts.
    const { allowed: sized, tooLargeMessage } = partitionBySize(list);
    if (tooLargeMessage) toast(tooLargeMessage, "error");
    // Drop executable/script files (the server rejects them too); say which.
    const blocked = sized.filter((f) => isDangerousFile(f.name));
    const allowed = sized.filter((f) => !isDangerousFile(f.name));
    if (blocked.length) {
      toast(
        `Can't attach ${blocked.map((f) => f.name).join(", ")} — for safety, programs and scripts aren't allowed.`,
        "error",
      );
    }
    if (!allowed.length) return;
    const staged = allowed.map((f) => ({ file: f, url: URL.createObjectURL(f) }));
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

/** One pending-upload preview tile. */
export function StagedTile({ staged, bucket, idx, onRemove }: Readonly<StagedTileProps>) {
  // If the blob URL can't render as a real image, swap to the generic file
  // icon so the row stays readable.
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
