/** Shared motion settings. The CSS tokens in styles/motion.css use the same durations; keep them in step. */

/** Duration of every UI transition in ms (the CSS --motion token). */
export const MOTION_MS = 200;
export const MOTION_EASE = "cubic-bezier(0.16, 1, 0.3, 1)";

/**
 * True when the person asked the system for less motion. Environments without matchMedia
 * (jsdom, very old browsers) count as reduced so nothing waits on an animation that cannot run.
 */
export function prefersReducedMotion() {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Drop animation for dnd-kit's DragOverlay: a short ease-out settle, or none under reduced motion. */
export function dragDropAnimation() {
  if (prefersReducedMotion()) return null;
  return { duration: MOTION_MS, easing: MOTION_EASE };
}
