/**
 * HTML sanitizer for content rendered via dangerouslySetInnerHTML.
 *
 * A few places render HTML produced elsewhere: comment bodies (rich text from
 * the server) and Sleuth answers (output of the escape-then-format pipeline).
 * The server sanitizes on write and the CSP blocks inline <script>; scrubbing
 * again at render time guards against a missed server path becoming stored XSS.
 * DOMPurify strips <script>, on* event handlers and javascript: URLs while
 * keeping the allowed rich-text tags (including inline data:image attachments,
 * which DOMPurify permits on <img> by default).
 */
import DOMPurify from "dompurify";

export function sanitizeHtml(html: string): string {
  // ADD_ATTR target: the rich editor emits target="_blank" on links, so keep it.
  return DOMPurify.sanitize(html ?? "", { ADD_ATTR: ["target"] });
}

/**
 * Stricter sanitizer for Sleuth chat answers. The mdLite pipeline emits only a
 * small known tag/attr set, so DOMPurify is locked to an explicit allowlist
 * (anything else is dropped) and data-* attributes are disabled except for the
 * one we rely on (data-open-bug). Used only by SleuthPanel; the richer rich-text
 * sinks use sanitizeHtml above.
 */
export function sanitizeSleuth(html: string): string {
  return DOMPurify.sanitize(html ?? "", {
    ALLOWED_TAGS: ["a", "strong", "em", "code", "ul", "li", "p", "br"],
    ALLOWED_ATTR: ["href", "target", "rel", "data-open-bug"],
    ALLOW_DATA_ATTR: false,
  });
}
