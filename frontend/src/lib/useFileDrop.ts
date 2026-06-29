/**
 * Hook that turns any element into a file drop target.
 * Spread `dropProps` on the element; use `dragging` to highlight the zone.
 * Ignores non-file drags and prevents the browser from navigating to dropped files.
 */
import { useRef, useState, type DragEvent } from "react";

export interface FileDrop {
  dragging: boolean;
  dropProps: {
    onDragEnter: (e: DragEvent) => void;
    onDragOver: (e: DragEvent) => void;
    onDragLeave: (e: DragEvent) => void;
    onDrop: (e: DragEvent) => void;
  };
}

function hasFiles(e: DragEvent): boolean {
  return Array.from(e.dataTransfer?.types ?? []).includes("Files");
}

export function useFileDrop(onFiles: (files: File[]) => void): FileDrop {
  const [dragging, setDragging] = useState(false);
  // Counts nested enter/leave events so highlight doesn't flicker over child elements.
  const depth = useRef(0);

  return {
    dragging,
    dropProps: {
      onDragEnter: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current += 1;
        setDragging(true);
      },
      onDragOver: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      },
      onDragLeave: (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      },
      onDrop: (e) => {
        if (!hasFiles(e)) return;
        // A nested drop target may have already claimed this event; still clear
        // the highlight but don't double-add the files.
        const claimed = e.isDefaultPrevented();
        e.preventDefault();
        depth.current = 0;
        setDragging(false);
        if (claimed) return;
        const files = Array.from(e.dataTransfer?.files ?? []);
        if (files.length) onFiles(files);
      },
    },
  };
}
