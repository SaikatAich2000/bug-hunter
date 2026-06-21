/**
 * Sleuth — Bug Hunter chatbot widget: a floating launcher (#sleuthFab) plus a
 * dialog panel (#sleuthPanel). Class names match what chatbot.css targets.
 *
 * Security:
 *  - All server-supplied text goes through escapeHtml() before the mdLite
 *    markdown transforms add any tags. dangerouslySetInnerHTML is only used on
 *    the output of that escape-then-format pipeline (including the static
 *    welcome banner, which is escaped before mdLite like any other text), and
 *    the result is passed through the fail-closed sanitizeSleuth allowlist.
 *  - Table / file / suggestions / confirm blocks render via JSX, which escapes
 *    automatically.
 *  - Bug links carry data-open-bug attributes; a single delegated click handler
 *    (CSP-safe, no inline JS) dispatches "sleuth:open-bug" on document, which
 *    AppContext listens for.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { sanitizeSleuth } from "../lib/sanitize";
import { useApp } from "../state/AppContext";
import type { ChatBlock, ChatOut } from "../types";

// ---------------------------------------------------------------------------
// Tiny helpers (.replace(/g) is used instead of ES2021 replaceAll because
// tsconfig targets ES2020 — identical behavior)
// ---------------------------------------------------------------------------
const ESCAPE_MAP: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

const escapeHtml = (s: string | null | undefined): string =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ESCAPE_MAP[c]);

const formatBytes = (n: number | null): string => {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
};

// Loose payload accessors — ChatBlock.payload is Record<string, unknown>.
const str = (v: unknown): string => (typeof v === "string" ? v : "");
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const rec = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" ? (v as Record<string, unknown>) : {};

/**
 * Convert a tiny markdown subset to HTML. Operates on an already-escaped
 * string so user text can never inject tags. Supported:
 *    **bold**         -> <strong>
 *    *italic*         -> <em>
 *    `code`           -> <code>
 *    [text](url)      -> <a>
 *    - bullet list    -> <ul><li>...</li></ul>
 *    Newlines         -> <br> (within a paragraph)
 *
 * Order matters: bold before italic so "**foo**" doesn't get half-eaten.
 */
// Safe URL schemes for markdown links. Fragment links are restricted to the
// exact pseudo-link the bug-detail handler emits ("#open-bug-<n>"); a blanket
// "any #..." allowance would let an attacker plant arbitrary in-app fragments.
const _SAFE_URL_PREFIXES = ["http://", "https://", "mailto:"];
const _OPEN_BUG_FRAGMENT_RE = /^#open-bug-\d+$/;
function _hasSafeUrlPrefix(url: string): boolean {
  if (_OPEN_BUG_FRAGMENT_RE.test(url)) return true;
  for (const p of _SAFE_URL_PREFIXES) {
    if (url.startsWith(p)) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// mdLite: convert a tiny markdown subset to HTML in a single inline pass that
// skips already-emitted `code` regions (so markdown inside backticks stays
// literal), then assembles block-level HTML (lists are real <ul> blocks, never
// nested inside a <p>, and no <br> sits adjacent to a block tag).
//
// The input is already html-escaped, so user text can never inject tags.
// ---------------------------------------------------------------------------

/**
 * Format inline markdown for one line: `code`, **bold**, *italic*, [text](url).
 * Code spans are emitted verbatim and excluded from any later transform — the
 * scanner walks the string once and only formats outside code regions.
 */
function formatInline(line: string): string {
  let out = "";
  let i = 0;
  const n = line.length;
  while (i < n) {
    const ch = line.charAt(i);
    // --- code span: copy verbatim, no inner transforms ---
    if (ch === "`") {
      const close = line.indexOf("`", i + 1);
      if (close > i) {
        out += `<code>${line.slice(i + 1, close)}</code>`;
        i = close + 1;
        continue;
      }
      // Unbalanced backtick — emit literally.
      out += ch;
      i += 1;
      continue;
    }
    // --- link [text](url) ---
    if (ch === "[") {
      const rb = line.indexOf("]", i + 1);
      if (rb > i && line.charAt(rb + 1) === "(") {
        const rp = line.indexOf(")", rb + 2);
        if (rp > rb) {
          const txt = line.slice(i + 1, rb);
          const url = line.slice(rb + 2, rp);
          if (txt && url && _hasSafeUrlPrefix(url)) {
            out += `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
            i = rp + 1;
            continue;
          }
        }
      }
      out += ch;
      i += 1;
      continue;
    }
    // --- bold **...** ---
    if (ch === "*" && line.charAt(i + 1) === "*") {
      const close = line.indexOf("**", i + 2);
      if (close > i + 1) {
        out += `<strong>${formatInline(line.slice(i + 2, close))}</strong>`;
        i = close + 2;
        continue;
      }
    }
    // --- italic *...* (single, no embedded *) ---
    if (ch === "*") {
      const close = line.indexOf("*", i + 1);
      if (close > i && !line.slice(i + 1, close).includes("*")) {
        out += `<em>${formatInline(line.slice(i + 1, close))}</em>`;
        i = close + 1;
        continue;
      }
    }
    out += ch;
    i += 1;
  }
  return out;
}

function mdLite(escaped: string): string {
  const lines = escaped.split(/\n/);
  // Assemble at the BLOCK level: paragraphs accumulate, lists are flushed as
  // standalone <ul> blocks. No <br> ever touches a block-tag boundary.
  const blocks: string[] = [];
  let para: string[] = []; // inline-formatted lines awaiting a <p>
  let listItems: string[] = []; // <li> contents awaiting a <ul>

  const flushPara = () => {
    if (para.length) {
      blocks.push(`<p>${para.join("<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (listItems.length) {
      blocks.push(`<ul>${listItems.map((li) => `<li>${li}</li>`).join("")}</ul>`);
      listItems = [];
    }
  };

  for (const line of lines) {
    const m = /^- (.*)$/.exec(line);
    if (m) {
      // A list item — close any open paragraph first.
      flushPara();
      listItems.push(formatInline(m[1]));
      continue;
    }
    // A non-list line — close any open list first.
    flushList();
    if (line.trim() === "") {
      // Blank line ends the current paragraph (paragraph break).
      flushPara();
    } else {
      para.push(formatInline(line));
    }
  }
  flushPara();
  flushList();
  return blocks.join("");
}

// Static welcome banner. Escape first (the copy contains quotes/apostrophes),
// then run mdLite over the escaped text exactly like server-supplied text.
const WELCOME_HTML = mdLite(escapeHtml(
  "Hi! I'm your **Bug Hunter AI Assistant**.\n\n" +
    "I can **answer questions** like:\n" +
    "- *show open bugs assigned to alice*\n" +
    "- *how many critical bugs in PROD?*\n" +
    "- *export bugs in apollo to excel*\n" +
    "- *bug 42*  ·  *summary*\n\n" +
    "I can also **do things** (with your confirmation):\n" +
    "- *close bug 5*  ·  *reopen #12*\n" +
    "- *assign bug 7 to bob*\n" +
    "- *set bug 3 priority to high*\n" +
    "- *comment on #5: looks fixed*\n" +
    '- *create a bug titled "Login broken" in project Apollo*\n\n' +
    "Type **help** for the full guide.",
));

// ---------------------------------------------------------------------------
// Message log — discriminated union held in React state. Not persisted
// across reloads (chat history can contain sensitive bug info).
// ---------------------------------------------------------------------------
type MsgBody =
  | { kind: "welcome" }
  | { kind: "user"; text: string }
  | { kind: "bot"; blocks: ChatBlock[] }
  | { kind: "error"; text: string };

type Msg = MsgBody & { id: number };

const KNOWN_KINDS = new Set(["text", "table", "file", "suggestions", "confirm"]);

// ---------------------------------------------------------------------------
// Panel sizing — the user can drag-resize the window (grip at the top-left
// corner; the panel is anchored bottom-right, so it grows up-and-left) or
// one-click expand/shrink. The chosen size persists across reloads.
// ---------------------------------------------------------------------------
const SLEUTH_MIN_W = 340;
const SLEUTH_MIN_H = 420;
const SLEUTH_DEFAULT_W = 400;
const SLEUTH_DEFAULT_H = 560;
const SLEUTH_SIZE_KEY = "bh.sleuth.size";
// Below this viewport width the CSS media query takes the panel near-fullscreen;
// we stop applying an inline size so it can't fight that rule.
const SLEUTH_NARROW = 540;

interface SleuthSize {
  w: number;
  h: number;
}

function readStoredSize(): SleuthSize {
  try {
    const raw = localStorage.getItem(SLEUTH_SIZE_KEY);
    if (raw) {
      const s: unknown = JSON.parse(raw);
      if (
        s !== null &&
        typeof s === "object" &&
        typeof (s as SleuthSize).w === "number" &&
        typeof (s as SleuthSize).h === "number"
      ) {
        return { w: (s as SleuthSize).w, h: (s as SleuthSize).h };
      }
    }
  } catch {
    /* malformed / unavailable storage — fall back to defaults */
  }
  return { w: SLEUTH_DEFAULT_W, h: SLEUTH_DEFAULT_H };
}

// Clamp a desired size into [min, viewport-margin].
function clampSize(w: number, h: number): SleuthSize {
  const maxW = Math.max(SLEUTH_MIN_W, window.innerWidth - 32);
  const maxH = Math.max(SLEUTH_MIN_H, window.innerHeight - 120);
  return {
    w: Math.round(Math.max(SLEUTH_MIN_W, Math.min(maxW, w))),
    h: Math.round(Math.max(SLEUTH_MIN_H, Math.min(maxH, h))),
  };
}

// ---------------------------------------------------------------------------
// Block renderers
// ---------------------------------------------------------------------------
function TextBlock({ block }: Readonly<{ block: ChatBlock }>) {
  const safe = escapeHtml(str(block.payload.text) || "");
  let html = mdLite(safe);
  // Special case: the bug-detail handler emits an "Open in Bug Hunter"
  // pseudo-link ([label](#open-bug-N)). Tag those anchors with data-open-bug
  // so the panel's delegated click handler picks them up (CSP-safe). Only a
  // positive integer id is honoured.
  const openBugId = num(block.payload.open_bug_id);
  if (openBugId != null && Number.isInteger(openBugId) && openBugId > 0) {
    html = html
      .split(`<a href="#open-bug-${openBugId}"`)
      .join(`<a href="#open-bug-${openBugId}" data-open-bug="${openBugId}"`);
  }
  return <div dangerouslySetInnerHTML={{ __html: sanitizeSleuth(html) }} />;
}

function TableBlock({ block }: Readonly<{ block: ChatBlock }>) {
  const headers = arr(block.payload.headers);
  const rows = arr(block.payload.rows);
  const ids = arr(block.payload.row_bug_ids);
  return (
    <div className="sleuth-table-wrap">
      <table className="sleuth-table">
        <thead>
          <tr>
            {headers.map((h, i) => (
              <th key={i}>{String(h ?? "")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, idx) => {
            const rawId = num(ids[idx]);
            // Clickable only for a positive integer id (numeric guard, not
            // Boolean(); bug ids start at 1, so 0 is rejected here).
            const clickable = rawId != null && Number.isInteger(rawId) && rawId > 0;
            const bugId = clickable ? String(rawId) : undefined;
            return (
              <tr
                key={idx}
                className={clickable ? "clickable" : undefined}
                data-bug-id={bugId}
                data-open-bug={bugId}
                title={clickable ? "Open this bug" : undefined}
              >
                {arr(row).map((cell, ci) => (
                  <td key={ci}>{String(cell ?? "")}</td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FileBlock({ block }: Readonly<{ block: ChatBlock }>) {
  const filename = str(block.payload.filename) || "bugs.xlsx";
  const rowCount = num(block.payload.row_count);
  let rowCountText = "";
  if (rowCount != null) {
    const suffix = rowCount === 1 ? "" : "s";
    rowCountText = rowCount + " row" + suffix;
  }
  const meta = [rowCountText, formatBytes(num(block.payload.size_bytes))]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="sleuth-file">
      <div className="sleuth-file-icon">📊</div>
      <div className="sleuth-file-info">
        <div className="sleuth-file-name">{filename}</div>
        <div className="sleuth-file-meta">{meta}</div>
      </div>
      {/* No target=_blank — the browser handles the download in place,
          which keeps screen-reader behavior predictable. */}
      <a
        className="sleuth-file-btn"
        href={"/api/chat/download/" + encodeURIComponent(str(block.payload.download_token))}
        download={filename}
      >
        Download
      </a>
    </div>
  );
}

function SuggestionsBlock({
  block,
  onSend,
}: Readonly<{
  block: ChatBlock;
  onSend: (text: string) => void;
}>) {
  return (
    <div className="sleuth-suggestions">
      {arr(block.payload.items).map((s, i) => {
        // Items can be plain strings (legacy) or {label, send} objects.
        const label = typeof s === "string" ? s : str(rec(s).label);
        const send =
          typeof s === "string" ? s : str(rec(s).send) || str(rec(s).label);
        if (!label) return null;
        return (
          <button key={i} type="button" onClick={() => onSend(send)}>
            {label}
          </button>
        );
      })}
    </div>
  );
}

// Confirmation blocks — explicit user approval before a write. Both buttons
// disable immediately on first click so an over-eager user can't double-fire.
function ConfirmBlock({
  block,
  onSend,
}: Readonly<{
  block: ChatBlock;
  onSend: (text: string) => void;
}>) {
  const [used, setUsed] = useState(false);
  const choose = (answer: string) => {
    setUsed(true);
    onSend(answer);
  };
  return (
    <div className="sleuth-confirm">
      <div className="sleuth-confirm-summary">
        {str(block.payload.summary) || "Confirm action"}
      </div>
      <div className="sleuth-confirm-actions">
        <button
          type="button"
          className="sleuth-confirm-yes"
          disabled={used}
          onClick={() => choose("yes")}
        >
          {str(block.payload.yes_label) || "Yes, do it"}
        </button>
        <button
          type="button"
          className="sleuth-confirm-no"
          disabled={used}
          onClick={() => choose("no")}
        >
          {str(block.payload.no_label) || "Cancel"}
        </button>
      </div>
    </div>
  );
}

function BotMessage({
  blocks,
  onSend,
}: Readonly<{
  blocks: ChatBlock[];
  onSend: (text: string) => void;
}>) {
  // All blocks for one response share a single .sleuth-msg bubble so they
  // read as one reply. Unknown kinds are skipped.
  const known = blocks.filter((b) => KNOWN_KINDS.has(b.kind));
  return (
    <div className="sleuth-msg bot">
      {known.length === 0 ? (
        // No renderable blocks — show a safe default so the UI doesn't go silent.
        <p>(no answer)</p>
      ) : (
        known.map((b, i) => {
          switch (b.kind) {
            case "text":
              return <TextBlock key={i} block={b} />;
            case "table":
              return <TableBlock key={i} block={b} />;
            case "file":
              return <FileBlock key={i} block={b} />;
            case "suggestions":
              return <SuggestionsBlock key={i} block={b} onSend={onSend} />;
            default:
              return <ConfirmBlock key={i} block={b} onSend={onSend} />;
          }
        })
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The widget
// ---------------------------------------------------------------------------
export default function SleuthPanel() {
  // Admins get the document-import affordance (the one Sleuth write surface).
  const { isAdmin, refreshAll } = useApp();
  // Intents that mean Sleuth just mutated data — refresh the lists + bell badge
  // immediately so the change (and its notification) shows without waiting for
  // the next poll.
  const refreshIfMutated = useCallback(
    (intent: string) => {
      if (intent === "action_done" || intent === "ingest_done") void refreshAll();
    },
    [refreshAll],
  );
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [typing, setTyping] = useState(false);
  const [inFlight, setInFlight] = useState(false);
  const [unread, setUnread] = useState(0);

  // ----- resizable panel ----------------------------------------------------
  const [size, setSize] = useState<SleuthSize>(readStoredSize);
  const [isNarrow, setIsNarrow] = useState(
    () => typeof window !== "undefined" && window.innerWidth <= SLEUTH_NARROW,
  );
  // Drag origin captured on pointerdown (null when not dragging).
  const resizeStart = useRef<{ x: number; y: number; w: number; h: number } | null>(null);

  // Refs mirror state that async/imperative paths need synchronously.
  const openRef = useRef(false);
  const inFlightRef = useRef(false);
  const renderedAnyUserMsgRef = useRef(false);
  const nextIdRef = useRef(1);

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fabRef = useRef<HTMLButtonElement | null>(null);
  const uploadRef = useRef<HTMLInputElement | null>(null);

  const push = useCallback((body: MsgBody) => {
    setMessages((prev) => [...prev, { ...body, id: nextIdRef.current++ }]);
  }, []);

  const showWelcome = useCallback(() => {
    setMessages([{ kind: "welcome", id: nextIdRef.current++ }]);
    renderedAnyUserMsgRef.current = false;
  }, []);

  // ----- network -----------------------------------------------------------
  const sendMessage = useCallback(
    async (raw: string) => {
      const text = (raw || "").trim();
      if (!text || inFlightRef.current) return;
      inFlightRef.current = true;
      setInFlight(true);

      push({ kind: "user", text }); // user text never gets HTML interpretation
      renderedAnyUserMsgRef.current = true;
      setTyping(true);
      try {
        const data = await api<ChatOut>("/chat/ask", {
          method: "POST",
          json: { message: text },
        });
        setTyping(false);
        push({ kind: "bot", blocks: data.blocks || [] });
        refreshIfMutated(data.intent);
        if (!openRef.current) setUnread((u) => u + 1);
      } catch (err) {
        setTyping(false);
        // 401: api() already bounced to login — stay silent.
        if (err instanceof ApiError && err.silent) return;
        push({
          kind: "error",
          text:
            err instanceof Error && err.message ? err.message : "Network error.",
        });
        if (!openRef.current) setUnread((u) => u + 1);
      } finally {
        inFlightRef.current = false;
        setInFlight(false);
        inputRef.current?.focus();
      }
    },
    [push, refreshIfMutated],
  );

  // ----- open / close ------------------------------------------------------
  const openPanel = useCallback(() => {
    if (openRef.current) return;
    openRef.current = true;
    setOpen(true);
    setUnread(0);
    if (!renderedAnyUserMsgRef.current) {
      // First open in this session — show the welcome banner.
      showWelcome();
    }
    globalThis.setTimeout(() => inputRef.current?.focus(), 50);
  }, [showWelcome]);

  const closePanel = useCallback(() => {
    if (!openRef.current) return;
    openRef.current = false;
    setOpen(false);
    fabRef.current?.focus();
  }, []);

  const toggle = () => (openRef.current ? closePanel() : openPanel());

  // ----- resize / expand ---------------------------------------------------
  const onResizePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    resizeStart.current = { x: e.clientX, y: e.clientY, w: size.w, h: size.h };
    try {
      e.currentTarget.setPointerCapture(e.pointerId);
    } catch {
      /* no active pointer (e.g. synthetic event) — drag still works via move */
    }
  };
  const onResizePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = resizeStart.current;
    if (!s) return;
    // Anchored bottom-right: dragging up / left enlarges the panel.
    setSize(clampSize(s.w + (s.x - e.clientX), s.h + (s.y - e.clientY)));
  };
  const onResizePointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!resizeStart.current) return;
    resizeStart.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* pointer already released */
    }
  };

  // Treat "noticeably larger than default" as expanded for the toggle's state.
  const isExpanded =
    size.w > SLEUTH_DEFAULT_W + 16 || size.h > SLEUTH_DEFAULT_H + 16;
  const toggleExpand = () => {
    setSize(
      isExpanded
        ? { w: SLEUTH_DEFAULT_W, h: SLEUTH_DEFAULT_H }
        : clampSize(560, 760),
    );
  };

  // ----- input wiring ------------------------------------------------------
  // Auto-grow textarea up to the CSS max-height (~120px). Uncontrolled, so
  // the autosize math reads the live scrollHeight.
  const autosize = () => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

  // Single send path — read the current value, clear the field, then dispatch.
  const sendCurrent = () => {
    const el = inputRef.current;
    if (!el) return;
    const text = el.value;
    if (!text.trim() || inFlightRef.current) return;
    el.value = "";
    autosize();
    void sendMessage(text);
  };

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendCurrent();
    }
  };

  // ----- admin document import ---------------------------------------------
  // Sleuth's one write surface: an admin uploads a doc of bugs and the server
  // (AI when configured, parser otherwise) creates a work item per entry. The
  // reply renders inline as a normal Sleuth message (text + a table of links).
  const onUploadClick = () => uploadRef.current?.click();

  const onFilePicked = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-picking the same file
    if (!file || inFlightRef.current) return;
    inFlightRef.current = true;
    setInFlight(true);
    if (!openRef.current) openPanel();
    push({ kind: "user", text: `📎 Uploaded ${file.name}` });
    renderedAnyUserMsgRef.current = true;
    setTyping(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const data = await api<ChatOut>("/chat/ingest", { method: "POST", body: fd });
      setTyping(false);
      push({ kind: "bot", blocks: data.blocks || [] });
    } catch (err) {
      setTyping(false);
      if (err instanceof ApiError && err.silent) return;
      push({
        kind: "error",
        text: err instanceof Error && err.message ? err.message : "Upload failed.",
      });
    } finally {
      inFlightRef.current = false;
      setInFlight(false);
      inputRef.current?.focus();
    }
  };

  // Escape closes when the panel is open and focus is anywhere inside.
  const onPanelKeyDown = (e: React.KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      closePanel();
    }
  };

  // Delegated click handler for everything tagged data-open-bug (mdLite
  // [label](#open-bug-N) anchors and clickable table rows). Dispatches the
  // custom event AppContext listens for — no inline handlers in innerHTML.
  const onPanelClick = (e: React.MouseEvent<HTMLElement>) => {
    const target = (e.target as HTMLElement).closest<HTMLElement>(
      "[data-open-bug]",
    );
    if (!target) return;
    e.preventDefault();
    const bugId = Number(target.dataset.openBug);
    // Dispatch only for a real positive-integer id.
    if (!Number.isInteger(bugId) || bugId <= 0) return;
    document.dispatchEvent(
      new CustomEvent("sleuth:open-bug", { detail: { bugId } }),
    );
  };

  // Keyboard shortcut to summon Sleuth: Ctrl+/  (or Cmd+/ on Mac).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "/") {
        e.preventDefault();
        openPanel();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openPanel]);

  // Keep the newest message in view.
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing]);

  // Track the narrow breakpoint (below it the CSS media query owns the size)
  // and keep the stored size within the viewport when it shrinks.
  useEffect(() => {
    const onResize = () => {
      setIsNarrow(window.innerWidth <= SLEUTH_NARROW);
      setSize((s) => clampSize(s.w, s.h));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Persist the chosen size across reloads.
  useEffect(() => {
    try {
      localStorage.setItem(SLEUTH_SIZE_KEY, JSON.stringify(size));
    } catch {
      /* storage unavailable (private mode) — non-fatal */
    }
  }, [size]);

  // ----- render ------------------------------------------------------------
  return (
    <>
      <button
        type="button"
        className="sleuth-fab"
        id="sleuthFab"
        ref={fabRef}
        aria-label="Open Bug Hunter AI Assistant"
        aria-expanded={open}
        title={open ? "Hide AI Assistant" : "Ask the AI Assistant"}
        data-unread={unread}
        onClick={(e) => {
          e.stopPropagation();
          toggle();
        }}
      >
        <img
          src="/static/sleuth.svg"
          alt=""
          draggable={false}
          className="sleuth-fab-icon"
        />
      </button>

      <aside
        className="sleuth-panel"
        id="sleuthPanel"
        hidden={!open}
        role="dialog"
        aria-label="Bug Hunter AI Assistant"
        style={isNarrow ? undefined : { width: `${size.w}px`, height: `${size.h}px` }}
        onKeyDown={onPanelKeyDown}
        onClick={onPanelClick}
      >
        {/* Drag-to-resize grip — top-left corner (panel grows up-and-left).
            Mouse/touch only; the keyboard-accessible path is the Expand
            button below, so the grip is hidden from assistive tech. */}
        <div
          className="sleuth-resize"
          aria-hidden="true"
          title="Drag to resize"
          onPointerDown={onResizePointerDown}
          onPointerMove={onResizePointerMove}
          onPointerUp={onResizePointerUp}
        />
        <header className="sleuth-header">
          <div className="sleuth-avatar" aria-hidden="true">
            <img src="/static/sleuth.svg" alt="" className="sleuth-avatar-icon" />
          </div>
          <div className="sleuth-title-block">
            <div className="sleuth-title">Bug Hunter AI Assistant</div>
            <div className="sleuth-status">Online</div>
          </div>
          <div className="sleuth-header-actions">
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthExpand"
              hidden={isNarrow}
              title={isExpanded ? "Shrink panel" : "Expand panel"}
              aria-label={isExpanded ? "Shrink assistant panel" : "Expand assistant panel"}
              aria-pressed={isExpanded}
              onClick={toggleExpand}
            >
              {isExpanded ? "⤡" : "⤢"}
            </button>
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthClear"
              title="Clear conversation"
              aria-label="Clear conversation"
              onClick={showWelcome}
            >
              ↻
            </button>
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthClose"
              title="Close"
              aria-label="Close"
              onClick={closePanel}
            >
              ✕
            </button>
          </div>
        </header>

        <div
          className="sleuth-body"
          id="sleuthBody"
          ref={bodyRef}
          aria-live="polite"
          aria-relevant="additions"
        >
          {messages.map((m) => {
            switch (m.kind) {
              case "welcome":
                return (
                  <div
                    key={m.id}
                    className="sleuth-msg bot"
                    dangerouslySetInnerHTML={{ __html: sanitizeSleuth(WELCOME_HTML) }}
                  />
                );
              case "user":
                return (
                  <div key={m.id} className="sleuth-msg user">
                    {m.text}
                  </div>
                );
              case "bot":
                return (
                  <BotMessage key={m.id} blocks={m.blocks} onSend={sendMessage} />
                );
              default:
                return (
                  <div key={m.id} className="sleuth-msg bot error">
                    <p>{m.text || "Something went wrong."}</p>
                  </div>
                );
            }
          })}
          {typing && (
            <div className="sleuth-typing" role="status" aria-label="Sleuth is thinking">
              <span aria-hidden="true"></span>
              <span aria-hidden="true"></span>
              <span aria-hidden="true"></span>
            </div>
          )}
        </div>

        <div className="sleuth-input-row">
          {/* Admin-only document import. The hidden input is driven by the
              paperclip button so the control reads as one affordance. */}
          {isAdmin && (
            <>
              <input
                ref={uploadRef}
                type="file"
                id="sleuthUpload"
                className="sleuth-upload-input"
                hidden
                accept=".txt,.csv,.tsv,.json,.md,.xlsx"
                tabIndex={-1}
                aria-hidden="true"
                onChange={onFilePicked}
              />
              <button
                type="button"
                className="sleuth-upload-btn"
                id="sleuthUploadBtn"
                title="Import a document of bugs — Sleuth creates an item per entry (admin)"
                aria-label="Import bugs from a document"
                disabled={inFlight}
                onClick={onUploadClick}
              >
                📎
              </button>
            </>
          )}
          <textarea
            id="sleuthInput"
            ref={inputRef}
            className="sleuth-input"
            placeholder="Ask Me Anything"
            rows={1}
            maxLength={2000}
            autoComplete="off"
            onChange={autosize}
            onKeyDown={onInputKeyDown}
          ></textarea>
          <button
            type="button"
            className="sleuth-send"
            id="sleuthSend"
            disabled={inFlight}
            onClick={sendCurrent}
          >
            Send
          </button>
        </div>
      </aside>
    </>
  );
}
