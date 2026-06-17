/**
 * RichEditor — a contentEditable rich-text editor with a custom toolbar.
 *
 * - React renders the shell (wrap > toolbar > surface) once; the
 *   contentEditable surface body is uncontrolled. All formatting is done with
 *   direct DOM manipulation inside the surface — do not convert the surface to
 *   controlled JSX.
 * - Output flows through the onChange prop and the getHtml()/setHtml()
 *   imperative handle; pasted files flow through the onPasteFile prop.
 * - execCommand appears only as a last-resort fallback. Every browser still
 *   implements it, and the primary paths here are manual DOM code for
 *   cross-browser correctness; those helpers are load-bearing and must not be
 *   "modernized".
 * - Styling comes from the .bh-rt-* rules in styles.css (including the
 *   :empty::before data-placeholder empty-state).
 */

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
} from "react";
import type {
  ClipboardEvent as ReactClipboardEvent,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
} from "react";
import { toast } from "../lib/toast";

// ---------------------------------------------------------------------------
// Unsafe-file paste filter. Block anything whose extension or MIME would let
// the user trick a colleague into opening a hostile payload from the
// attachments grid. The list mirrors the Windows "potentially dangerous" set
// plus the obvious cross-platform binaries.
// ---------------------------------------------------------------------------
const UNSAFE_EXTS = new Set<string>([
  "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "msc",
  "jar", "vbs", "vbe", "js", "jse", "wsf", "wsh",
  "ps1", "psm1", "psd1", "ps1xml", "psc1",
  "sh", "bash", "csh", "zsh", "ksh",
  "app", "deb", "rpm", "dmg", "appimage",
  "reg", "lnk", "inf", "ins", "isp",
  "ade", "adp", "cpl", "mde", "shs", "sct", "vb", "cer", "hta",
]);
const UNSAFE_MIMES = new Set<string>([
  "application/x-msdownload",
  "application/x-msdos-program",
  "application/x-msi",
  "application/vnd.microsoft.portable-executable",
  "application/x-elf",
  "application/x-mach-binary",
  "application/x-sh",
  "application/x-shellscript",
]);
const EXT_RE = /\.([a-z0-9]+)$/;

function isUnsafeExt(filename: string): boolean {
  if (!filename) return false;
  const m = EXT_RE.exec(filename.toLowerCase());
  const ext = m?.[1];
  return ext !== undefined && UNSAFE_EXTS.has(ext);
}

function isUnsafeMime(mime: string): boolean {
  if (!mime) return false;
  const first = mime.toLowerCase().split(";")[0];
  return first !== undefined && UNSAFE_MIMES.has(first.trim());
}

function describeError(err: unknown): string {
  return err instanceof Error && err.message ? err.message : String(err);
}

// ---------------------------------------------------------------------------
// Editor constants
// ---------------------------------------------------------------------------

// Inline-style commands toggled via manual wrappers (Chrome 148 silently
// no-ops execCommand("bold") inside certain stacking-context / overflow
// ancestors, so Bold/Italic/Underline/Strike are pure DOM code).
const INLINE_TOGGLE: Record<string, string> = {
  bold: "b",
  italic: "i",
  underline: "u",
  strikeThrough: "s",
};

// Zero-width space used to arm the "next typed character is styled" state.
const ZWSP = "​";

// Word characters for the auto word-select: letters / digits / underscore /
// hyphen / apostrophe, plus the Latin-extended, Greek and CJK ranges
// (À-ɏ Ͱ-῿ 一-鿿).
const WORD_RE = /[A-Za-z0-9_'\-À-ɏͰ-῿一-鿿]/;

const MAX_HISTORY = 100;

interface HistoryEntry {
  html: string;
  caret: number;
}

interface HistoryState {
  stack: HistoryEntry[];
  idx: number;
  debounce: number | null;
  restoring: boolean;
}

// ---------------------------------------------------------------------------
// Component contract
// ---------------------------------------------------------------------------

export interface RichEditorHandle {
  setHtml(html: string): void;
  getHtml(): string;
  focus(): void;
}

interface Props {
  initialHtml?: string;
  onChange?: (html: string) => void;
  onPasteFile?: (file: File) => void;
  placeholder?: string;
  ariaLabel?: string;
  disabled?: boolean;
  /**
   * Rendered on the visually-hidden native <textarea> kept behind the surface
   * (`.bh-rt-native`, the form-submission source of truth). Automation and
   * tests fill that textarea directly — it is clipped to 1×1px, not
   * display:none, precisely so it stays fillable.
   */
  name?: string;
  textareaId?: string;
}

const RichEditor = forwardRef<RichEditorHandle, Props>(function RichEditor(
  {
    initialHtml = "",
    onChange,
    onPasteFile,
    placeholder,
    ariaLabel,
    disabled = false,
    name,
    textareaId,
  },
  ref,
) {
  const editorRef = useRef<HTMLDivElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const nativeRef = useRef<HTMLTextAreaElement>(null);

  // Track the last selection that was inside the editor. Toolbar buttons
  // restore this on click so the formatting helpers have a valid range to
  // operate on even if focus briefly left the editor for the button.
  const savedRangeRef = useRef<Range | null>(null);

  // Snapshot-based undo/redo state. Custom history instead of the browser's
  // contenteditable undo stack because our toolbar helpers mutate the DOM
  // directly (range.insertNode, createElement, ...) — those mutations are
  // NOT recorded on the native undo stack, so Ctrl+Z would skip every
  // formatting operation in between.
  const historyRef = useRef<HistoryState>({
    stack: [],
    idx: -1,
    debounce: null,
    restoring: false,
  });

  // Uncontrolled seeding: initialHtml is read once on mount; later changes
  // to the prop are intentionally ignored (use the setHtml handle instead).
  const initialHtmlRef = useRef(initialHtml);

  // Latest-prop refs so DOM-driven callbacks never go stale.
  const onChangeRef = useRef<Props["onChange"]>(onChange);
  const onPasteFileRef = useRef<Props["onPasteFile"]>(onPasteFile);
  useEffect(() => {
    onChangeRef.current = onChange;
    onPasteFileRef.current = onPasteFile;
  });

  // ----- value sync --------------------------------------------------------

  // Empty heuristic: contenteditable inserts a leading <br> or empty <div>
  // when the user clears all text. Treat "<br>" / "<div><br></div>" as empty
  // so length-1 validation downstream still rejects them.
  const computeHtml = (): string => {
    const editor = editorRef.current;
    if (!editor) return "";
    const html = editor.innerHTML;
    const stripped = html
      .replace(/<br\s*\/?>/gi, "")
      .replace(/<\/?(?:div|p)>/gi, "")
      .trim();
    return stripped ? html : "";
  };

  const sync = (): void => {
    const html = computeHtml();
    // Keep the hidden native textarea mirroring the surface — it is the
    // form-submission / automation contract.
    if (nativeRef.current && nativeRef.current.value !== html) {
      nativeRef.current.value = html;
    }
    onChangeRef.current?.(html);
  };

  // ----- snapshot-based undo / redo ---------------------------------------

  const getCaretOffset = (): number => {
    const editor = editorRef.current;
    if (!editor) return 0;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return 0;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.endContainer)) return 0;
    const pre = document.createRange();
    pre.selectNodeContents(editor);
    pre.setEnd(r.endContainer, r.endOffset);
    return pre.toString().length;
  };

  const setCaretOffset = (offset: number): void => {
    const editor = editorRef.current;
    if (!editor) return;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    let remaining = offset;
    let target: Text | null = null;
    let targetOffset = 0;
    let node = walker.nextNode();
    while (node) {
      const len = (node.nodeValue ?? "").length;
      if (remaining <= len) {
        target = node as Text;
        targetOffset = remaining;
        break;
      }
      remaining -= len;
      node = walker.nextNode();
    }
    const range = document.createRange();
    if (target) {
      range.setStart(target, targetOffset);
    } else {
      // No text content (or offset past end) — drop caret at editor end.
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    range.collapse(true);
    const s = window.getSelection();
    if (!s) return;
    s.removeAllRanges();
    s.addRange(range);
  };

  const snapshot = (): void => {
    const editor = editorRef.current;
    const h = historyRef.current;
    if (!editor || h.restoring) return;
    const html = editor.innerHTML;
    if (h.idx >= 0 && h.stack[h.idx]?.html === html) return;
    // Drop the redo tail when pushing after an undo.
    if (h.idx < h.stack.length - 1) {
      h.stack.length = h.idx + 1;
    }
    h.stack.push({ html, caret: getCaretOffset() });
    if (h.stack.length > MAX_HISTORY) {
      h.stack.shift();
    } else {
      h.idx++;
    }
  };

  // Push a typing snapshot debounced per input event so we don't store one
  // entry per character (would blow the cap on a paragraph).
  const scheduleSnapshot = (): void => {
    const h = historyRef.current;
    if (h.debounce !== null) window.clearTimeout(h.debounce);
    h.debounce = window.setTimeout(() => {
      h.debounce = null;
      snapshot();
    }, 600);
  };

  const flushSnapshot = (): void => {
    const h = historyRef.current;
    if (h.debounce !== null) {
      window.clearTimeout(h.debounce);
      h.debounce = null;
      snapshot();
    }
  };

  const restore = (entry: HistoryEntry): void => {
    const editor = editorRef.current;
    if (!editor) return;
    const h = historyRef.current;
    h.restoring = true;
    editor.innerHTML = entry.html || "";
    try {
      setCaretOffset(entry.caret || 0);
    } catch {
      /* selection may fail in detached editor */
    }
    h.restoring = false;
    sync();
  };

  const undoEdit = (): boolean => {
    flushSnapshot();
    const h = historyRef.current;
    if (h.idx <= 0) return false;
    h.idx--;
    const entry = h.stack[h.idx];
    if (entry) restore(entry);
    return true;
  };

  const redoEdit = (): boolean => {
    flushSnapshot();
    const h = historyRef.current;
    if (h.idx >= h.stack.length - 1) return false;
    h.idx++;
    const entry = h.stack[h.idx];
    if (entry) restore(entry);
    return true;
  };

  const resetHistory = (): void => {
    const h = historyRef.current;
    h.stack = [];
    h.idx = -1;
    if (h.debounce !== null) {
      window.clearTimeout(h.debounce);
      h.debounce = null;
    }
    snapshot();
  };

  // ----- selection management ----------------------------------------------

  const captureSelection = (): void => {
    const editor = editorRef.current;
    if (!editor) return;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return;
    const r = s.getRangeAt(0);
    if (editor.contains(r.commonAncestorContainer)) savedRangeRef.current = r.cloneRange();
  };

  const ensureCaret = (): void => {
    const editor = editorRef.current;
    if (!editor) return;
    const s = window.getSelection();
    if (!s) return;
    // If savedRange points OUTSIDE the editor (stale, e.g. the user clicked
    // from a different input), discard it and drop a fresh caret at the end
    // of the editor — otherwise the formatting helpers act on a range that
    // isn't in our editor and the user sees "Bold did nothing".
    const saved = savedRangeRef.current;
    if (saved && editor.contains(saved.commonAncestorContainer)) {
      s.removeAllRanges();
      s.addRange(saved);
      return;
    }
    savedRangeRef.current = null;
    const r = document.createRange();
    r.selectNodeContents(editor);
    r.collapse(false);
    s.removeAllRanges();
    s.addRange(r);
    savedRangeRef.current = r.cloneRange();
  };

  // True if the current selection is anchored inside <tag> within the editor.
  const inAncestor = (tagNames: string[]): boolean => {
    const editor = editorRef.current;
    if (!editor) return false;
    const wanted = new Set(tagNames.map((t) => t.toUpperCase()));
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    let n: Node | null = s.getRangeAt(0).startContainer;
    while (n && n !== editor) {
      if (n.nodeType === Node.ELEMENT_NODE && wanted.has((n as Element).tagName)) return true;
      n = n.parentNode;
    }
    return false;
  };

  // ----- inline formatting ---------------------------------------------------

  // For inline-style commands on a COLLAPSED caret, Chrome's typing-state
  // model is fragile: the reliable cross-browser workaround is to manually
  // toggle a wrapper element around a zero-width placeholder, then drop the
  // caret inside it — the next character lands inside the wrapper and
  // inherits its styling.
  const toggleInlineAtCaret = (tag: string): boolean => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    if (!r.collapsed) return false; // the wrap path handles the non-empty case
    // Look for an existing wrapper of the same tag among the caret's
    // ancestors. If found, "exit" it: insert a ZWSP text node AFTER the
    // wrapper and place the caret after that ZWSP — unambiguously outside
    // the wrapper, so subsequent typing lands in unstyled text. (Chrome
    // greedily extends an adjacent inline element when the caret sits right
    // after its close tag, so setStartAfter(<b>) alone isn't enough.)
    let n: Node | null = r.startContainer;
    while (n && n !== editor) {
      if (n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName.toLowerCase() === tag) {
        const sep = document.createTextNode(ZWSP);
        n.parentNode?.insertBefore(sep, n.nextSibling);
        const after = document.createRange();
        after.setStart(sep, 1); // after the ZWSP
        after.collapse(true);
        s.removeAllRanges();
        s.addRange(after);
        return true;
      }
      n = n.parentNode;
    }
    // Not in wrapper → insert `<tag>ZWSP</tag>` and put the caret BETWEEN
    // the ZWSP and the end of the wrapper so the next key appears inside.
    const wrapEl = document.createElement(tag);
    const zw = document.createTextNode(ZWSP);
    wrapEl.appendChild(zw);
    r.insertNode(wrapEl);
    const inside = document.createRange();
    inside.setStart(zw, 1); // after the ZWSP
    inside.collapse(true);
    s.removeAllRanges();
    s.addRange(inside);
    return true;
  };

  // When the user clicks Bold (or Italic / Underline / Strike) with NO text
  // selected but with content in the editor, the natural expectation is
  // "make the word I just typed bold". This auto-selects the word at the
  // caret. Returns true if a selection was made (or one already existed).
  const selectWordAtCaret = (): boolean => {
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!r.collapsed) return true;
    // Normalize: when the caret sits in an ELEMENT (e.g. after a
    // selectNodeContents+collapse on an <li>), descend into the adjacent
    // text node child so the word walk can run.
    let node: Node | null = r.startContainer;
    let pos = r.startOffset;
    if (node.nodeType === Node.ELEMENT_NODE) {
      const kids = node.childNodes;
      const before = pos > 0 ? kids[pos - 1] : undefined;
      const at = pos < kids.length ? kids[pos] : undefined;
      if (before && before.nodeType === Node.TEXT_NODE) {
        node = before;
        pos = (node.textContent || "").length;
      } else if (at && at.nodeType === Node.TEXT_NODE) {
        node = at;
        pos = 0;
      } else {
        return false;
      }
    }
    if (!node || node.nodeType !== Node.TEXT_NODE) return false;
    const text = node.textContent || "";
    const left = pos > 0 ? text.charAt(pos - 1) : "";
    const right = pos < text.length ? text.charAt(pos) : "";
    if (!WORD_RE.test(left) && !WORD_RE.test(right)) return false;
    let start = pos;
    while (start > 0 && WORD_RE.test(text.charAt(start - 1))) start--;
    let end = pos;
    while (end < text.length && WORD_RE.test(text.charAt(end))) end++;
    if (start === end) return false;
    const wordRange = document.createRange();
    wordRange.setStart(node, start);
    wordRange.setEnd(node, end);
    s.removeAllRanges();
    s.addRange(wordRange);
    return true;
  };

  // Manual wrap/unwrap for inline styles — Chrome 148 silently no-ops
  // document.execCommand("bold") inside certain stacking-context / overflow
  // ancestors, so toggling Bold/Italic/Underline/Strike is done entirely in
  // DOM code here.
  const applyInlineWrap = (tag: string): boolean => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (r.collapsed) return false;
    if (!editor.contains(r.commonAncestorContainer)) return false;
    // If selection sits entirely inside a wrapper of this tag, UNWRAP by
    // replacing the wrapper with its children. We test by walking both
    // ends — if they share the same ancestor of this tag, it's "inside".
    const findWrapper = (start: Node): Element | null => {
      let n: Node | null = start;
      while (n && n !== editor) {
        if (n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName.toLowerCase() === tag) {
          return n as Element;
        }
        n = n.parentNode;
      }
      return null;
    };
    const startAnc = findWrapper(r.startContainer);
    const endAnc = findWrapper(r.endContainer);
    if (startAnc && startAnc === endAnc) {
      const wrapEl = startAnc;
      const parent = wrapEl.parentNode;
      if (!parent) return false;
      // Capture inner text so we can re-select it after unwrap.
      const txt = wrapEl.textContent ?? "";
      while (wrapEl.firstChild) parent.insertBefore(wrapEl.firstChild, wrapEl);
      wrapEl.remove();
      // Re-select the unwrapped text so the user can re-toggle without
      // re-selecting it manually.
      const first = parent.firstChild;
      if (first && first.nodeType === Node.TEXT_NODE && first.textContent === txt) {
        const re = document.createRange();
        re.setStart(first, 0);
        re.setEnd(first, txt.length);
        s.removeAllRanges();
        s.addRange(re);
      }
      return true;
    }
    // Wrap the selection. surroundContents is the cleanest path when the
    // range is well-formed; when the selection straddles block boundaries
    // we extract + wrap.
    const wrapEl = document.createElement(tag);
    try {
      r.surroundContents(wrapEl);
    } catch {
      const frag = r.extractContents();
      wrapEl.appendChild(frag);
      r.insertNode(wrapEl);
    }
    // Re-select the now-wrapped content so chained toggles work.
    const inside = document.createRange();
    inside.selectNodeContents(wrapEl);
    s.removeAllRanges();
    s.addRange(inside);
    return true;
  };

  // ----- lists -----------------------------------------------------------------

  // Manual list toggle (ul / ol). We treat the LINE containing the caret as
  // the target: if it's already inside a list of the requested type, unwrap;
  // otherwise wrap it.
  const applyList = (listTag: "ul" | "ol"): boolean => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    // Find the enclosing block (LI, P, DIV, BLOCKQUOTE, PRE, or none).
    const block = ((): Element | null => {
      let n: Node | null = r.startContainer;
      while (n && n !== editor) {
        if (
          n.nodeType === Node.ELEMENT_NODE &&
          /^(LI|P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test((n as Element).tagName)
        ) {
          return n as Element;
        }
        n = n.parentNode;
      }
      return null;
    })();
    // Already in a LI of the same list type → unwrap to a <p>.
    if (block && block.tagName === "LI") {
      const list = block.parentElement;
      if (list && list.tagName === listTag.toUpperCase()) {
        const p = document.createElement("p");
        while (block.firstChild) p.appendChild(block.firstChild);
        list.parentNode?.insertBefore(p, list);
        block.remove();
        if (!list.firstChild) list.remove();
        // Place caret at start of new <p>
        const re = document.createRange();
        re.setStart(p, 0);
        re.collapse(true);
        s.removeAllRanges();
        s.addRange(re);
        return true;
      }
    }
    // Wrap path. Three shapes:
    //   (a) Caret/selection sits inside a P/DIV/BLOCKQUOTE/etc. — replace
    //       that block with `<ul><li>…</li></ul>` carrying its contents.
    //   (b) Selection is non-empty but lives directly at the editor root —
    //       extract the selection and wrap it in `<ul><li>…</li></ul>`
    //       inserted at the extracted position.
    //   (c) Caret is collapsed at the editor root — move every editor child
    //       into a single `<li>`.
    const list = document.createElement(listTag);
    const li = document.createElement("li");
    if (block && block !== (editor as Element)) {
      while (block.firstChild) li.appendChild(block.firstChild);
      list.appendChild(li);
      block.parentNode?.replaceChild(list, block);
    } else if (!r.collapsed) {
      // (b) Extract the selected fragment and put it inside the <li>.
      const frag = r.extractContents();
      li.appendChild(frag);
      list.appendChild(li);
      r.insertNode(list);
    } else {
      // (c) Wrap the whole editor's current content.
      while (editor.firstChild) li.appendChild(editor.firstChild);
      list.appendChild(li);
      editor.appendChild(list);
    }
    // An empty <li> renders without a marker in some browsers — drop a <br>
    // so the list is visible even before the user types.
    if (!li.firstChild) li.appendChild(document.createElement("br"));
    const re = document.createRange();
    re.selectNodeContents(li);
    re.collapse(false);
    s.removeAllRanges();
    s.addRange(re);
    return true;
  };

  // ----- block wrappers (blockquote / pre) -------------------------------------

  // Manual block toggle for blockquote / pre. Wrap the current block, or
  // unwrap if already wrapped.
  const applyBlockWrap = (tag: string): boolean => {
    const editor = editorRef.current;
    if (!editor) return false;
    const s = window.getSelection();
    if (!s || s.rangeCount === 0) return false;
    const r = s.getRangeAt(0);
    if (!editor.contains(r.commonAncestorContainer)) return false;
    // Find the nearest matching wrapper.
    const existing = ((): Element | null => {
      let n: Node | null = r.startContainer;
      while (n && n !== editor) {
        if (n.nodeType === Node.ELEMENT_NODE && (n as Element).tagName.toLowerCase() === tag) {
          return n as Element;
        }
        n = n.parentNode;
      }
      return null;
    })();
    if (existing) {
      // Unwrap: replace existing with a <p> carrying its children.
      const p = document.createElement("p");
      while (existing.firstChild) p.appendChild(existing.firstChild);
      existing.parentNode?.replaceChild(p, existing);
      const re = document.createRange();
      re.selectNodeContents(p);
      re.collapse(false);
      s.removeAllRanges();
      s.addRange(re);
      return true;
    }
    // Wrap the current block (or insert a new block at caret).
    const block = ((): Element | null => {
      let n: Node | null = r.startContainer;
      while (n && n !== editor) {
        if (
          n.nodeType === Node.ELEMENT_NODE &&
          /^(P|DIV|BLOCKQUOTE|PRE|H[1-6])$/.test((n as Element).tagName)
        ) {
          return n as Element;
        }
        n = n.parentNode;
      }
      return null;
    })();
    const wrapEl = document.createElement(tag);
    if (block && block !== (editor as Element)) {
      while (block.firstChild) wrapEl.appendChild(block.firstChild);
      block.parentNode?.replaceChild(wrapEl, block);
    } else {
      // Empty editor or top-level text — push everything in editor into wrap.
      while (editor.firstChild) wrapEl.appendChild(editor.firstChild);
      editor.appendChild(wrapEl);
    }
    const re = document.createRange();
    re.selectNodeContents(wrapEl);
    re.collapse(false);
    s.removeAllRanges();
    s.addRange(re);
    return true;
  };

  // ----- command dispatch --------------------------------------------------------

  // Run a formatting command with a valid selection — restore the last
  // intra-editor range, run the command, then re-capture so chained toolbar
  // clicks build on the correct anchor.
  const runCmd = (cmd: string, arg: string | null = null): void => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.focus();
    ensureCaret();
    const inlineTag = INLINE_TOGGLE[cmd];
    if (inlineTag !== undefined) {
      const s = window.getSelection();
      if (s && s.rangeCount > 0 && s.getRangeAt(0).collapsed) {
        // Auto-select the word at the caret — this matches "I just typed a
        // word, click Bold, the word bolds" expectation.
        if (!selectWordAtCaret()) {
          // No word at caret → arm typing state via a ZWSP wrapper.
          if (toggleInlineAtCaret(inlineTag)) {
            captureSelection();
            sync();
            return;
          }
        }
      }
      if (applyInlineWrap(inlineTag)) {
        captureSelection();
        sync();
        return;
      }
    }
    if (cmd === "insertUnorderedList" && applyList("ul")) {
      captureSelection();
      sync();
      return;
    }
    if (cmd === "insertOrderedList" && applyList("ol")) {
      captureSelection();
      sync();
      return;
    }
    if (cmd === "formatBlock") {
      // arg looks like "<blockquote>" / "<pre>" / "<p>"
      const tag = (arg || "").replace(/[<>]/g, "").toLowerCase();
      if (tag && tag !== "p" && applyBlockWrap(tag)) {
        captureSelection();
        sync();
        return;
      }
      // Falling back to <p> — unwrap any existing blockquote/pre.
      if (tag === "p") {
        for (const t of ["blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]) {
          if (applyBlockWrap(t)) {
            captureSelection();
            sync();
            return;
          }
        }
      }
    }
    // Last-resort fallback for anything we haven't manually implemented.
    try {
      document.execCommand(cmd, false, arg ?? undefined);
    } catch {
      /* ignore */
    }
    captureSelection();
    sync();
  };

  // Open a hidden file picker and route the chosen file through onPasteFile.
  // We DO NOT embed images inline in the contenteditable — the surface can't
  // reliably resize <img> elements or position a caret after them. By
  // treating the image as just another attachment the editor stays text-only
  // and the file surfaces in the parent form's attachment grid.
  const pickFileAsAttachment = (): void => {
    const handler = onPasteFileRef.current;
    if (!handler) {
      toast("Attach files via the attachment area for this form.", "info");
      return;
    }
    const f = document.createElement("input");
    f.type = "file";
    f.accept = "image/*,application/pdf";
    f.style.display = "none";
    f.addEventListener("change", () => {
      const file = f.files?.[0];
      if (file) {
        if (isUnsafeExt(file.name) || isUnsafeMime(file.type)) {
          toast(`Blocked unsafe file: ${file.name}`, "error");
        } else {
          try {
            const result: unknown = handler(file);
            if (result instanceof Promise) {
              result.catch((err: unknown) => {
                toast(`Failed to attach: ${describeError(err)}`, "error");
              });
            }
          } catch (err) {
            toast(`Failed to attach: ${describeError(err)}`, "error");
          }
        }
      }
      f.remove();
    });
    document.body.appendChild(f);
    f.click();
  };

  const handleToolbarCmd = (btn: HTMLButtonElement): void => {
    const cmd = btn.dataset.cmd;
    if (!cmd) return;
    const arg = btn.dataset.arg || null;
    if (cmd === "bh-image") {
      pickFileAsAttachment();
      return;
    }
    if (cmd === "formatBlock") {
      const tag = (arg || "").toLowerCase();
      if (tag && inAncestor([tag])) {
        runCmd("formatBlock", "<p>");
      } else {
        runCmd("formatBlock", `<${tag}>`);
      }
      updateActiveStates();
      return;
    }
    runCmd(cmd, arg);
    updateActiveStates();
  };

  // Reflect the current inline-formatting state on the toolbar buttons so
  // the user sees that Bold is "on" before they start typing. Imperative
  // classList toggling (not React state) — the buttons are rendered once
  // with static props, so React reconciliation never clobbers the class.
  const updateActiveStates = (): void => {
    const toolbar = toolbarRef.current;
    if (!toolbar) return;
    toolbar.querySelectorAll<HTMLButtonElement>("button[data-cmd]").forEach((b) => {
      const c = b.dataset.cmd ?? "";
      let active = false;
      // Inline styles: check DOM ancestor for the tag we toggled in.
      // queryCommandState is unreliable for the manual-wrapper path.
      const inlineTag = INLINE_TOGGLE[c];
      if (inlineTag !== undefined) {
        active = inAncestor([inlineTag]);
        // Fallback to queryCommandState for the non-wrapped path.
        if (!active) {
          try {
            active = !!document.queryCommandState(c);
          } catch {
            /* unsupported command */
          }
        }
      } else if (c === "insertUnorderedList") {
        active = inAncestor(["ul"]);
      } else if (c === "insertOrderedList") {
        active = inAncestor(["ol"]);
      } else if (c === "formatBlock") {
        const arg = (b.dataset.arg || "").toLowerCase();
        if (arg) active = inAncestor([arg]);
      }
      b.classList.toggle("is-active", active);
    });
  };

  // Snapshot wrapper around handleToolbarCmd so undo can step over every
  // formatting change as one unit.
  const runToolbarCmd = (btn: HTMLButtonElement): void => {
    flushSnapshot();
    snapshot(); // pre-state
    handleToolbarCmd(btn);
    snapshot(); // post-state
  };

  // ----- toolbar events ------------------------------------------------------------

  // Both capture-selection and preventDefault must run on mousedown.
  // preventDefault stops the browser from shifting focus away from the
  // contenteditable to the button — without it, the editor's typing state is
  // wiped before the user can type anything. The command also runs on
  // mousedown for immediate feedback, and the click handler's mouse path is
  // swallowed entirely.
  const handleToolbarMouseDown = (e: ReactMouseEvent<HTMLDivElement>): void => {
    const target = e.target instanceof Element ? e.target : null;
    const btn = target?.closest<HTMLButtonElement>("button[data-cmd]");
    if (!btn || btn.disabled) return;
    // Capture selection FIRST — preventDefault stops the focus shift but
    // the selection getter still works at this point.
    captureSelection();
    // KEEP editor focused so the typing state survives across the press.
    e.preventDefault();
    // Run the command immediately on mousedown so it lands inside the same
    // focus session as the user's click.
    runToolbarCmd(btn);
  };

  // Keyboard accessibility — Enter / Space on a focused toolbar button.
  const handleToolbarClick = (e: ReactMouseEvent<HTMLDivElement>): void => {
    // Mouse path is already handled by mousedown above; suppress the
    // duplicate run that would otherwise come from the click event.
    if (e.detail !== 0) {
      e.preventDefault();
      return;
    }
    const target = e.target instanceof Element ? e.target : null;
    const btn = target?.closest<HTMLButtonElement>("button[data-cmd]");
    if (!btn || btn.disabled) return;
    e.preventDefault();
    runToolbarCmd(btn);
  };

  // ----- editor events ---------------------------------------------------------------

  const handleInput = (): void => {
    sync();
    scheduleSnapshot();
    updateActiveStates();
  };

  // External writes to the hidden native textarea (Playwright fill, any form
  // automation) mirror into the surface — the inverse of sync(). The surface
  // is canonical, so propagate eagerly on the textarea's input event.
  const handleNativeInput = (): void => {
    const editor = editorRef.current;
    const ta = nativeRef.current;
    if (!editor || !ta) return;
    if (editor.innerHTML !== ta.value) {
      editor.innerHTML = ta.value;
      sync();
      scheduleSnapshot();
    }
  };

  const handleKeyUp = (): void => {
    captureSelection();
    updateActiveStates();
  };

  const handleMouseUp = (): void => {
    captureSelection();
    updateActiveStates();
  };

  const handleFocus = (): void => {
    captureSelection();
  };

  const handleKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey) return;
    const key = e.key.toLowerCase();
    // Undo / redo — runs against OUR snapshot history so formatting changes
    // from toolbar buttons are tracked too. The browser-native undo would
    // miss them entirely.
    if (key === "z" && !e.shiftKey) {
      e.preventDefault();
      undoEdit();
      updateActiveStates();
      return;
    }
    if (key === "z" && e.shiftKey) {
      e.preventDefault();
      redoEdit();
      updateActiveStates();
      return;
    }
    if (key === "y" && !e.shiftKey) {
      e.preventDefault();
      redoEdit();
      updateActiveStates();
      return;
    }
    if (e.shiftKey) return;
    if (key === "b") {
      e.preventDefault();
      flushSnapshot();
      runCmd("bold");
      snapshot();
      updateActiveStates();
    } else if (key === "i") {
      e.preventDefault();
      flushSnapshot();
      runCmd("italic");
      snapshot();
      updateActiveStates();
    } else if (key === "u") {
      e.preventDefault();
      flushSnapshot();
      runCmd("underline");
      snapshot();
      updateActiveStates();
    }
  };

  // File paste: intercept clipboard FILES (image / PDF / anything except
  // the unsafe blocklist) and hand them to onPasteFile — the caller decides
  // whether to upload immediately or stage for submit. We never embed the
  // file inline in the editor HTML. Plain-text/HTML paste falls through to
  // the browser default.
  const handlePaste = (e: ReactClipboardEvent<HTMLDivElement>): void => {
    const items = e.clipboardData ? Array.from(e.clipboardData.items) : [];
    const files: File[] = [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return; // plain-text paste: let the browser handle it.
    e.preventDefault();
    const handler = onPasteFileRef.current;
    for (const f of files) {
      if (isUnsafeExt(f.name) || isUnsafeMime(f.type)) {
        toast(`Blocked unsafe file: ${f.name || f.type}`, "error");
        continue;
      }
      if (!handler) {
        toast(`No attachment target for ${f.name || "file"}`, "info");
        continue;
      }
      try {
        const result: unknown = handler(f);
        if (result instanceof Promise) {
          result.catch((err: unknown) => {
            toast(`Paste failed for ${f.name || "file"}: ${describeError(err)}`, "error");
          });
        }
      } catch (err) {
        toast(`Paste failed for ${f.name || "file"}: ${describeError(err)}`, "error");
      }
    }
  };

  // ----- lifecycle ------------------------------------------------------------------

  // Seed the surface once on mount (uncontrolled thereafter) and seed the
  // initial history state so the first Ctrl+Z can return to it.
  useLayoutEffect(() => {
    const editor = editorRef.current;
    if (editor) {
      editor.innerHTML = initialHtmlRef.current;
      resetHistory();
    }
    return () => {
      const h = historyRef.current;
      if (h.debounce !== null) {
        window.clearTimeout(h.debounce);
        h.debounce = null;
      }
      h.stack = [];
      h.idx = -1;
      h.restoring = false;
      savedRangeRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Track selection while the editor is the active element (document-level
  // selectionchange listener, scoped to the surface).
  useEffect(() => {
    const onSelectionChange = (): void => {
      if (document.activeElement === editorRef.current) captureSelection();
    };
    document.addEventListener("selectionchange", onSelectionChange);
    return () => document.removeEventListener("selectionchange", onSelectionChange);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useImperativeHandle(ref, () => ({
    setHtml(html: string): void {
      const editor = editorRef.current;
      if (!editor) return;
      editor.innerHTML = html || "";
      sync();
      // The editor content was replaced wholesale — drop the snapshot
      // history and seed a fresh one. Otherwise Ctrl+Z would jump the user
      // back to unrelated previous content.
      resetHistory();
    },
    getHtml(): string {
      return computeHtml();
    },
    focus(): void {
      editorRef.current?.focus();
    },
  }));

  return (
    <div className="bh-rt-wrap">
      {/* Visually-hidden native textarea (.bh-rt-native): clipped to 1×1px so
          it stays programmatically fillable, synced both ways with the
          contenteditable surface. */}
      <textarea
        ref={nativeRef}
        className="bh-rt-native"
        name={name}
        id={textareaId}
        defaultValue={initialHtml}
        tabIndex={-1}
        disabled={disabled}
        onInput={handleNativeInput}
        aria-hidden="true"
      />
      <div
        ref={toolbarRef}
        className="bh-rt-toolbar"
        onMouseDown={handleToolbarMouseDown}
        onClick={handleToolbarClick}
      >
        <button type="button" data-cmd="bold" title="Bold (Ctrl+B)" aria-label="Bold" disabled={disabled}>
          <b>B</b>
        </button>
        <button type="button" data-cmd="italic" title="Italic (Ctrl+I)" aria-label="Italic" disabled={disabled}>
          <i>I</i>
        </button>
        <button type="button" data-cmd="underline" title="Underline (Ctrl+U)" aria-label="Underline" disabled={disabled}>
          <u>U</u>
        </button>
        <button type="button" data-cmd="strikeThrough" title="Strikethrough" aria-label="Strikethrough" disabled={disabled}>
          <s>S</s>
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="insertUnorderedList" title="Bulleted list" aria-label="Bulleted list" disabled={disabled}>
          {"•"}
        </button>
        <button type="button" data-cmd="insertOrderedList" title="Numbered list" aria-label="Numbered list" disabled={disabled}>
          1.
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="formatBlock" data-arg="blockquote" title="Quote" aria-label="Quote" disabled={disabled}>
          {'"'}
        </button>
        <button type="button" data-cmd="formatBlock" data-arg="pre" title="Code block" aria-label="Code block" disabled={disabled}>
          {"</>"}
        </button>
        <span className="bh-rt-divider" />
        <button type="button" data-cmd="bh-image" title="Insert image" aria-label="Insert image" disabled={disabled}>
          {"🖼"}
        </button>
      </div>
      <div
        ref={editorRef}
        className="bh-rt-editor"
        contentEditable={!disabled}
        suppressContentEditableWarning
        role="textbox"
        aria-multiline="true"
        aria-label={ariaLabel}
        aria-disabled={disabled || undefined}
        data-placeholder={placeholder || undefined}
        onInput={handleInput}
        onKeyUp={handleKeyUp}
        onMouseUp={handleMouseUp}
        onFocus={handleFocus}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
      />
    </div>
  );
});

RichEditor.displayName = "RichEditor";

export default RichEditor;
