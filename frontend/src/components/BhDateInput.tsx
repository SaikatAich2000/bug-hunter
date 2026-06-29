/**
 * BhDateInput — custom date picker. Class names must match styles.css:
 *
 *   <div class="bh-date-wrap">
 *     <input type="hidden" class="bh-date-native" …>   ← form-submission value
 *     <button class="bh-date-btn" aria-haspopup="dialog" aria-expanded>
 *       <span class="bh-date-icon">📅</span>
 *       <span class="bh-date-label [bh-date-placeholder]">Jun 12, 2026 | Select date</span>
 *     </button>
 *     <button class="bh-date-clear" type="button">×</button> ← only when a value is set
 *     <div class="bh-date-pop" role="dialog">               ← only while open
 *       .bh-date-head  (‹ nav / .bh-date-title / › nav)
 *       .bh-date-grid  (7 × .bh-date-dow + 42 × .bh-date-cell)
 *       .bh-date-foot  (.bh-date-today)
 *     </div>
 *   </div>
 *
 * The popover is portaled to <body> so `position: fixed` coordinates are always
 * viewport-relative. Rendering it in-place would make any ancestor with a
 * transform/filter/contain (modal backdrop, entrance animation, etc.) the
 * containing block, shifting the panel and inflating <main>'s scroll height.
 * popRef still points at the real node, keeping the outside-click guard and
 * placePop math correct. Value is fully controlled via props.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const DOW = ["S", "M", "T", "W", "T", "F", "S"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Format a JS Date as YYYY-MM-DD in local time. */
function isoDate(d: Date): string {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

/** Parse YYYY-MM-DD as a local-time Date, rejecting impossible dates like
 *  2026-02-31 (JS rolls those forward) via a round-trip equality check. */
function parseIso(s: string): Date | null {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split("-").map((n) => Number.parseInt(n, 10));
  const date = new Date(y, m - 1, d);
  // Round-trip check: JS silently rolls over out-of-range dates.
  if (isoDate(date) !== s) return null;
  return date;
}

/** "Jun 12, 2026". */
function formatHumanDate(s: string): string {
  const d = parseIso(s);
  if (!d) return "";
  return `${MONTHS[d.getMonth()].slice(0, 3)} ${d.getDate()}, ${d.getFullYear()}`;
}

interface Cell {
  dayNum: number;
  cellMonth: number;
  cellYear: number;
  muted: boolean;
}

/** One calendar cell, marking prev/next-month bleed. */
function computeCell(
  offset: number,
  daysInMonth: number,
  daysInPrev: number,
  viewYear: number,
  viewMonth: number,
): Cell {
  if (offset < 0) {
    let cellMonth = viewMonth - 1;
    let cellYear = viewYear;
    if (cellMonth < 0) { cellMonth = 11; cellYear--; }
    return { dayNum: daysInPrev + offset + 1, cellMonth, cellYear, muted: true };
  }
  if (offset >= daysInMonth) {
    let cellMonth = viewMonth + 1;
    let cellYear = viewYear;
    if (cellMonth > 11) { cellMonth = 0; cellYear++; }
    return { dayNum: offset - daysInMonth + 1, cellMonth, cellYear, muted: true };
  }
  return { dayNum: offset + 1, cellMonth: viewMonth, cellYear: viewYear, muted: false };
}

interface Props {
  name?: string;
  /** "YYYY-MM-DD" or "" for empty. */
  value: string;
  onChange: (iso: string) => void;
  required?: boolean;
  disabled?: boolean;
  /** Goes on the hidden native input (the label target). */
  id?: string;
}

export default function BhDateInput({ name, value, onChange, required, disabled, id }: Props) {
  const [open, setOpen] = useState(false);
  // Viewed month is independent of the selected value so the user can browse
  // without committing. Re-seeded from value each time the popover opens.
  const [view, setView] = useState<{ y: number; m: number }>(() => {
    const seed = parseIso(value) ?? new Date();
    return { y: seed.getFullYear(), m: seed.getMonth() };
  });
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  // Align to the trigger button, flipping above when below would clip.
  const placePop = useCallback(() => {
    const btn = btnRef.current;
    const pop = popRef.current;
    if (!btn || !pop) return;
    const r = btn.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const ph = pop.offsetHeight || 360;
    const pw = pop.offsetWidth || 320;
    const spaceBelow = vh - r.bottom;
    const spaceAbove = r.top;
    const openAbove = spaceBelow < ph + 12 && spaceAbove > spaceBelow;
    pop.style.position = "fixed";
    pop.style.top = openAbove
      ? `${Math.max(8, r.top - ph - 6)}px`
      : `${Math.min(vh - ph - 8, r.bottom + 6)}px`;
    let left = r.left;
    if (left + pw > vw - 8) left = Math.max(8, r.right - pw);
    pop.style.left = `${left}px`;
  }, []);

  // Place before paint, then again next frame (fonts/styles can shift
  // measurements), and track the trigger on scroll/resize.
  useLayoutEffect(() => {
    if (!open) return;
    placePop();
    const raf = requestAnimationFrame(placePop);
    window.addEventListener("resize", placePop);
    window.addEventListener("scroll", placePop, true);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", placePop);
      window.removeEventListener("scroll", placePop, true);
    };
  }, [open, placePop]);

  // Outside click closes.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      const t = e.target instanceof Node ? e.target : null;
      if (
        t &&
        ((wrapRef.current?.contains(t) ?? false) || (popRef.current?.contains(t) ?? false))
      ) {
        return;
      }
      setOpen(false);
    };
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  const openPop = () => {
    const seed = parseIso(value) ?? new Date();
    setView({ y: seed.getFullYear(), m: seed.getMonth() });
    setOpen(true);
  };

  const commit = (iso: string) => {
    onChange(iso);
    setOpen(false);
  };

  const navMonth = (delta: -1 | 1) => {
    setView((v) => {
      let m = v.m + delta;
      let y = v.y;
      if (m < 0) { m = 11; y--; }
      if (m > 11) { m = 0; y++; }
      return { y, m };
    });
  };

  // Build the 6×7 = 42-cell grid (covers any month with overflow).
  const startDow = new Date(view.y, view.m, 1).getDay(); // 0=Sun
  const daysInMonth = new Date(view.y, view.m + 1, 0).getDate();
  const daysInPrev = new Date(view.y, view.m, 0).getDate();
  const todayIso = isoDate(new Date());
  const cells: { iso: string; dayNum: number; cls: string }[] = [];
  for (let i = 0; i < 42; i++) {
    const { dayNum, cellMonth, cellYear, muted } =
      computeCell(i - startDow, daysInMonth, daysInPrev, view.y, view.m);
    const iso = isoDate(new Date(cellYear, cellMonth, dayNum));
    const cls = [
      "bh-date-cell",
      muted ? "muted" : "",
      iso === todayIso ? "is-today" : "",
      iso === value ? "is-selected" : "",
    ].filter(Boolean).join(" ");
    cells.push({ iso, dayNum, cls });
  }

  return (
    <div className="bh-date-wrap" ref={wrapRef}>
      {/* Hidden input carries name/value for form submission. */}
      <input
        type="hidden"
        className="bh-date-native"
        name={name}
        id={id}
        value={value}
        required={required}
        disabled={disabled}
      />
      <button
        type="button"
        className="bh-date-btn"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        ref={btnRef}
        onClick={() => {
          if (open) setOpen(false);
          else openPop();
        }}
      >
        <span className="bh-date-icon" aria-hidden="true">📅</span>
        <span className={`bh-date-label${value ? "" : " bh-date-placeholder"}`}>
          {value ? formatHumanDate(value) : "Select date"}
        </span>
      </button>
      {value && !disabled ? (
        <button
          type="button"
          className="bh-date-clear"
          aria-label="Clear date"
          title="Clear"
          onClick={(e) => {
            e.stopPropagation();
            onChange("");
          }}
        >
          ×
        </button>
      ) : null}
      {open &&
        createPortal(
          <div
            className="bh-date-pop"
            role="dialog"
            aria-label="Pick a date"
            ref={popRef}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.stopPropagation();
                setOpen(false);
              }
            }}
          >
            <div className="bh-date-head">
              <button
                type="button"
                className="bh-date-nav"
                data-nav="prev"
                aria-label="Previous month"
                onClick={(e) => { e.stopPropagation(); navMonth(-1); }}
              >
                ‹
              </button>
              <span className="bh-date-title">{MONTHS[view.m]} {view.y}</span>
              <button
                type="button"
                className="bh-date-nav"
                data-nav="next"
                aria-label="Next month"
                onClick={(e) => { e.stopPropagation(); navMonth(1); }}
              >
                ›
              </button>
            </div>
            <div className="bh-date-grid">
              {DOW.map((d, i) => (
                <span key={i} className="bh-date-dow">{d}</span>
              ))}
              {cells.map((c) => (
                <button
                  type="button"
                  key={c.iso}
                  className={c.cls}
                  data-iso={c.iso}
                  onClick={(e) => { e.stopPropagation(); commit(c.iso); }}
                >
                  {c.dayNum}
                </button>
              ))}
            </div>
            <div className="bh-date-foot">
              <button
                type="button"
                className="bh-date-today"
                onClick={(e) => { e.stopPropagation(); commit(isoDate(new Date())); }}
              >
                Today
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
