/**
 * Attachment upload limits, mirroring MAX_FILE_BYTES in app/routes/bugs.py so
 * the UI can reject oversized files client-side before a request is made.
 */
import { formatBytes } from "./format";

/** Server-enforced per-attachment size cap. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

/** Human-readable size limit used in messages. */
export const MAX_UPLOAD_LABEL = "50 MB";

/** Returns an error message when the file exceeds the size limit, otherwise null. */
export function fileTooLargeMessage(file: File): string | null {
  if (file.size <= MAX_UPLOAD_BYTES) return null;
  const name = file.name || "This file";
  return `"${name}" is too large (${formatBytes(file.size)}). The largest file you can attach is ${MAX_UPLOAD_LABEL}.`;
}

/**
 * Splits files into allowed and oversized groups. Returns a display-ready
 * error message listing the oversized files, or null if all fit.
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
