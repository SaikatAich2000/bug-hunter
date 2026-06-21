/**
 * Shared attachment-upload limits and checks. Mirrors the server policy
 * (MAX_FILE_BYTES in app/routes/bugs.py) so the UI can stop an oversized file
 * before the upload starts and explain why in plain language.
 */
import { formatBytes } from "./format";

/** Largest file the server accepts per attachment (mirrors MAX_FILE_BYTES). */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/** Human-readable size limit used in messages. */
export const MAX_UPLOAD_LABEL = "50 MB";

/**
 * Return a user-facing message if the file is over the size limit, else null.
 */
export function fileTooLargeMessage(file: File): string | null {
  if (file.size <= MAX_UPLOAD_BYTES) return null;
  const name = file.name || "This file";
  return `"${name}" is too large (${formatBytes(file.size)}). The largest file you can attach is ${MAX_UPLOAD_LABEL}.`;
}

/**
 * Split files into those within the size limit and a ready-to-show message
 * naming any that were too large (null when none were).
 */
export function partitionBySize(files: File[]): {
  allowed: File[];
  tooLargeMessage: string | null;
} {
  const allowed: File[] = [];
  const tooLarge: File[] = [];
  for (const f of files) {
    if (f.size > MAX_UPLOAD_BYTES) tooLarge.push(f);
    else allowed.push(f);
  }
  if (!tooLarge.length) return { allowed, tooLargeMessage: null };

  let message: string;
  if (tooLarge.length === 1) {
    message = fileTooLargeMessage(tooLarge[0]!)!;
  } else {
    const names = tooLarge.map((f) => `"${f.name || "a file"}"`).join(", ");
    message = `${names} are too large to attach. The largest file you can attach is ${MAX_UPLOAD_LABEL}.`;
  }
  return { allowed, tooLargeMessage: message };
}
