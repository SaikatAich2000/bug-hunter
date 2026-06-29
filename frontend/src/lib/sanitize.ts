/**
 * Client-side sanitization for content rendered via dangerouslySetInnerHTML.
 *
 * The server sanitizes on write and CSP blocks inline scripts, but a second
 * pass at render time guards against any missed server path becoming stored XSS.
 */
import DOMPurify from "dompurify";

export function sanitizeHtml(html: string): string {
  // target="_blank" is emitted by the rich editor; preserve it.
  return DOMPurify.sanitize(html ?? "", { ADD_ATTR: ["target"] });
}

/**
 * Stricter variant for Sleuth chat output. Locks DOMPurify to the exact tags
 * the mdLite pipeline can produce; drops everything else. data-* is disabled
 * globally except for data-open-bug, which SleuthPanel uses for bug links.
 */
export function sanitizeSleuth(html: string): string {
  return DOMPurify.sanitize(html ?? "", {
    ALLOWED_TAGS: ["a", "strong", "em", "code", "ul", "li", "p", "br"],
    ALLOWED_ATTR: ["href", "target", "rel", "data-open-bug"],
    ALLOW_DATA_ATTR: false,
  });
}
