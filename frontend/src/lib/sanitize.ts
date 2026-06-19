/**
 * Defense-in-depth HTML sanitizer.
 *
 * A few places must render HTML produced elsewhere via dangerouslySetInnerHTML:
 * comment bodies (rich text from the server) and Sleuth answers (the output of
 * the escape-then-format pipeline). The server already sanitizes on write and
 * the CSP blocks inline <script>, but a single missed server path shouldn't
 * become stored XSS — so we ALSO scrub at render time. DOMPurify strips
 * <script>, on* event handlers and javascript: URLs while keeping the rich-text
 * tags we allow (including inline data:image attachments, which DOMPurify
 * permits on <img> by default).
 */
import DOMPurify from "dompurify";

export function sanitizeHtml(html: string): string {
  // ADD_ATTR target: the rich editor emits target="_blank" on links; keep it.
  return DOMPurify.sanitize(html ?? "", { ADD_ATTR: ["target"] });
}
