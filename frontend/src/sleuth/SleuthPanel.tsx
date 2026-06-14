/**
 * Sleuth — Bug Hunter chatbot widget. React port of app/static/chatbot.js.
 *
 * Renders the same DOM the vanilla widget built imperatively: a floating
 * launcher (#sleuthFab) plus a dialog panel (#sleuthPanel) with the exact
 * ids/classes chatbot.css targets, so no CSS changes are needed.
 *
 * Security (same contract as vanilla):
 *  - All server-supplied text goes through escapeHtml() BEFORE the mdLite
 *    markdown transforms add any tags. dangerouslySetInnerHTML is only used
 *    on the output of that escape-then-format pipeline (and on the static
 *    welcome markdown, which vanilla also fed straight to mdLite).
 *  - Table / file / suggestions / confirm blocks render via JSX, which
 *    escapes automatically (the textContent equivalent).
 *  - Bug links carry data-open-bug attributes; a single delegated click
 *    handler (CSP-safe, no inline JS) dispatches "sleuth:open-bug" on
 *    document, which AppContext listens for.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { ChatBlock, ChatOut } from "../types";

// ---------------------------------------------------------------------------
// Tiny helpers (verbatim ports; .replace(/g) used instead of ES2021
// replaceAll because tsconfig targets ES2020 — identical behavior)
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
// Linear scan that converts `[text](url)` markdown links to <a> tags, only
// for safe URL schemes (http(s), mailto, fragment hashes). Hand-written, not
// a regex, for the same static-analyzer reasons documented in chatbot.js.
const _SAFE_URL_PREFIXES = ["http://", "https://", "mailto:", "#"];
function _hasSafeUrlPrefix(url: string): boolean {
  for (const p of _SAFE_URL_PREFIXES) {
    if (url.startsWith(p)) return true;
  }
  return false;
}
function _mdLiteLinks(s: string): string {
  let out = "";
  let i = 0;
  const n = s.length;
  while (i < n) {
    const lb = s.indexOf("[", i);
    if (lb < 0) {
      out += s.slice(i);
      break;
    }
    out += s.slice(i, lb);
    const rb = s.indexOf("]", lb + 1);
    if (rb < 0 || s.charAt(rb + 1) !== "(") {
      out += s.charAt(lb);
      i = lb + 1;
      continue;
    }
    const rp = s.indexOf(")", rb + 2);
    if (rp < 0) {
      out += s.charAt(lb);
      i = lb + 1;
      continue;
    }
    const txt = s.slice(lb + 1, rb);
    const url = s.slice(rb + 2, rp);
    if (txt && url && _hasSafeUrlPrefix(url)) {
      out += `<a href="${url}" target="_blank" rel="noopener noreferrer">${txt}</a>`;
    } else {
      out += s.slice(lb, rp + 1);
    }
    i = rp + 1;
  }
  return out;
}

function mdLite(escaped: string): string {
  let s = escaped;
  // Code spans first (so we don't transform markdown inside them).
  s = s.replace(/`([^`]+)`/g, (_m, code: string) => `<code>${code}</code>`);
  // Bold and italic — bold first.
  s = s.replace(/\*\*([^*]+)\*\*/g, (_m, b: string) => `<strong>${b}</strong>`);
  s = s.replace(
    /(^|[^*])\*([^*\n]+)\*/g,
    (_m, pre: string, it: string) => `${pre}<em>${it}</em>`,
  );
  // Links — only http(s)://, mailto:, or fragment hashes for safety.
  s = _mdLiteLinks(s);
  // Bulleted lists (lines starting with "- "). Process line-by-line.
  const lines = s.split(/\n/);
  const out: string[] = [];
  let inList = false;
  for (const line of lines) {
    const m = /^- (.*)$/.exec(line);
    if (m) {
      if (!inList) {
        out.push("<ul>");
        inList = true;
      }
      out.push(`<li>${m[1]}</li>`);
    } else {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      out.push(line);
    }
  }
  if (inList) out.push("</ul>");
  // Wrap consecutive plain lines into paragraphs separated by blank lines.
  const joined = out
    .join("\n")
    .replace(/\n{2,}/g, "</p><p>")
    .replace(/\n/g, "<br>");
  return `<p>${joined}</p>`;
}

// Static welcome banner — vanilla fed this trusted string straight to
// mdLite without escaping (it contains no markup-significant characters).
const WELCOME_HTML = mdLite(
  "Hi! I'm **Sleuth** — your Bug Hunter assistant.\n\n" +
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
);

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
// Block renderers — JSX ports of the vanilla render*Block() functions.
// ---------------------------------------------------------------------------
function TextBlock({ block }: Readonly<{ block: ChatBlock }>) {
  const safe = escapeHtml(str(block.payload.text) || "");
  let html = mdLite(safe);
  // Special case: the bug-detail handler emits an "Open in Bug Hunter"
  // pseudo-link ([label](#open-bug-N)). Vanilla attached click handlers to
  // those anchors after the fact; here we tag them with data-open-bug so
  // the panel's delegated click handler picks them up (CSP-safe).
  const openBugId = num(block.payload.open_bug_id);
  if (openBugId != null) {
    html = html
      .split(`<a href="#open-bug-${openBugId}"`)
      .join(`<a href="#open-bug-${openBugId}" data-open-bug="${openBugId}"`);
  }
  return <div dangerouslySetInnerHTML={{ __html: html }} />;
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
            const rawId = ids[idx];
            const clickable = Boolean(rawId); // vanilla: if (ids[idx])
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
  // All blocks for ONE response share a single .sleuth-msg bubble so they
  // read as one reply. Unknown kinds are skipped, exactly like vanilla.
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

  // Refs mirror state that async/imperative paths need synchronously
  // (the vanilla `state` object + element handles).
  const openRef = useRef(false);
  const inFlightRef = useRef(false);
  const renderedAnyUserMsgRef = useRef(false);
  const nextIdRef = useRef(1);

  const bodyRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const fabRef = useRef<HTMLButtonElement | null>(null);

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
    [push],
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
  // the autosize math reads the live scrollHeight exactly like vanilla.
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
    if (!Number.isFinite(bugId)) return;
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

  // Keep the newest message in view (vanilla scrolled after each append).
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
        aria-label="Open Sleuth chatbot"
        aria-expanded={open}
        title={open ? "Hide Sleuth" : "Ask Sleuth"}
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
        aria-label="Sleuth chatbot"
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
            <div className="sleuth-title">Sleuth</div>
            <div className="sleuth-status">Your Bug Hunter assistant</div>
          </div>
          <div className="sleuth-header-actions">
            <button
              type="button"
              className="sleuth-icon-btn"
              id="sleuthExpand"
              hidden={isNarrow}
              title={isExpanded ? "Shrink Sleuth" : "Expand Sleuth"}
              aria-label={isExpanded ? "Shrink Sleuth panel" : "Expand Sleuth panel"}
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
                    dangerouslySetInnerHTML={{ __html: WELCOME_HTML }}
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
            <div className="sleuth-typing" aria-label="Sleuth is thinking">
              <span></span>
              <span></span>
              <span></span>
            </div>
          )}
        </div>

        <div className="sleuth-input-row">
          <textarea
            id="sleuthInput"
            ref={inputRef}
            className="sleuth-input"
            placeholder="Ask anything — e.g. open bugs assigned to me"
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
