/**
 * VideoLightbox — a full-screen-ish overlay that hosts <VideoPlayer/> on a dark
 * backdrop. This is the "View" experience for video attachments: instead of
 * opening the raw file in a new browser tab (which would use the browser's
 * NATIVE player, whose fullscreen chrome we can't theme or control), the same
 * custom player opens here, large and centered, with autoplay.
 *
 * Portaled to <body> so it escapes the modal's stacking/transform context.
 * Closes on the ✕ button, a backdrop click, or Escape.
 */
import { useCallback, useEffect } from "react";
import { createPortal } from "react-dom";
import VideoPlayer from "./VideoPlayer";

interface Props {
  src: string;
  type?: string;
  label?: string;
  onClose: () => void;
}

export default function VideoLightbox({ src, type, label, onClose }: Readonly<Props>) {
  // Esc to close + lock background scroll while open.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
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
      onClick={onClose}
    >
      <button
        type="button"
        className="video-lightbox-close"
        aria-label="Close video"
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
