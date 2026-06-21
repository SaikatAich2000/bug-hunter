/**
 * useFileDrop — small hook that turns any element into a file drop target.
 * Returns a `dragging` flag (for the drop-zone highlight) and `dropProps` to
 * spread onto the element. Only reacts to dragged FILES (ignores text/element
 * drags), preventDefaults so the browser doesn't navigate to the dropped file,
 * and hands the dropped File[] to `onFiles`.
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
  // Depth counter so dragging over child elements doesn't flicker the highlight
  // (dragenter/leave fire per descendant).
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
        // An inner drop target (e.g. the rich-text editor) may have already
        // claimed this drop and called preventDefault. In that case clear our
        // own highlight but don't add the files a second time.
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
