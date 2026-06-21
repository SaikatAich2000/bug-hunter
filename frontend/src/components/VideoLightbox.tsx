/**
 * VideoLightbox — an overlay that hosts <VideoPlayer/> on a dark backdrop, used
 * to view video attachments. Opening the raw file in a new tab would use the
 * browser's native player, whose fullscreen chrome can't be themed; instead the
 * custom player opens here, large and centered, with autoplay.
 *
 * Portaled to <body> so it escapes the modal's stacking/transform context.
 * Closes on the close button, a backdrop click, or Escape.
 */
import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import VideoPlayer from "./VideoPlayer";

interface Props {
  src: string;
  type?: string;
  label?: string;
  onClose: () => void;
}

export default function VideoLightbox({ src, type, label, onClose }: Readonly<Props>) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeBtnRef = useRef<HTMLButtonElement>(null);

  // Focus management: save the element that had focus, move focus into the
  // dialog on open, and restore it on close/unmount.
  useEffect(() => {
    const prevActive = document.activeElement as HTMLElement | null;
    // Move focus to the close button.
    closeBtnRef.current?.focus();
    return () => {
      if (prevActive && typeof prevActive.focus === "function") prevActive.focus();
    };
  }, []);

  // Esc to close + lock background scroll while open. A simple focus trap keeps
  // Tab / Shift+Tab cycling within the dialog.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const root = dialogRef.current;
        if (!root) return;
        const focusable = Array.from(
          root.querySelectorAll<HTMLElement>(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => el.offsetParent !== null || el === document.activeElement);
        if (focusable.length === 0) {
          e.preventDefault();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const active = document.activeElement as HTMLElement | null;
        if (e.shiftKey) {
          if (active === first || !root.contains(active)) {
            e.preventDefault();
            last.focus();
          }
        } else if (active === last || !root.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  const stopProp = useCallback((e: { stopPropagation: () => void }) => e.stopPropagation(), []);

  return createPortal(
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div
      className="video-lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={label ? `Video: ${label}` : "Video"}
      ref={dialogRef}
      onClick={onClose}
    >
      <button
        type="button"
        className="video-lightbox-close"
        aria-label="Close video"
        ref={closeBtnRef}
        onClick={onClose}
      >
        ✕
      </button>
      {label ? <div className="video-lightbox-title">{label}</div> : null}
      {/* eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions */}
      <div className="video-lightbox-stage" onClick={stopProp}>
        <VideoPlayer src={src} type={type} label={label} autoPlay />
      </div>
    </div>,
    document.body,
  );
}
