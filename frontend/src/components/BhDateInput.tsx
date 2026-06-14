/**
 * BhDateInput — React port of the vanilla custom date picker
 * (`enhanceDateInput`, app/static/app.js L290-L567).
 *
 * DOM (class names must match styles.css exactly):
 *
 *   <div class="bh-date-wrap">
 *     <input type="hidden" class="bh-date-native" …>   ← form-submission value
 *     <button class="bh-date-btn" aria-haspopup="dialog" aria-expanded>
 *       <span class="bh-date-icon">📅</span>
 *       <span class="bh-date-label [bh-date-placeholder]">Jun 12, 2026 | Select date</span>
 *       <span class="bh-date-clear">×</span>            ← only when a value is set
 *     </button>
 *     <div class="bh-date-pop" role="dialog">           ← only while open
 *       .bh-date-head  (‹ nav / .bh-date-title / › nav)
 *       .bh-date-grid  (7 × .bh-date-dow + 42 × .bh-date-cell)
 *       .bh-date-foot  (.bh-date-today)
 *     </div>
 *   </div>
 *
 * Differences from vanilla: the popover renders in-place instead of being
 * appended to document.body (it is still `position: fixed`, placed by the
 * same placePop math, so it escapes overflow-clipping ancestors), and the
 * value is fully controlled via props instead of living on the input.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

const DOW = ["S", "M", "T", "W", "T", "F", "S"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Format a JS Date as YYYY-MM-DD in local time (port of _isoDate). */
function isoDate(d: Date): string {
  const yyyy = d.getFullYear();
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

/** Parse YYYY-MM-DD as a local-time Date (port of _parseIso). */
function parseIso(s: string): Date | null {
  if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
  const [y, m, d] = s.split("-").map((n) => Number.parseInt(n, 10));
  return new Date(y, m - 1, d);
}

/** "Jun 12, 2026" (port of _formatHumanDate). */
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

/** Port of _computeCell: one calendar cell, marking prev/next-month bleed. */
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
  /** Goes on the hidden native input (where labels pointed in vanilla). */
  id?: string;
}

export default function BhDateInput({ name, value, onChange, required, disabled, id }: Props) {
  const [open, setOpen] = useState(false);
  // The month being VIEWED (independent of selection so the user can
  // browse without committing). Re-seeded from value on every open.
  const [view, setView] = useState<{ y: number; m: number }>(() => {
    const seed = parseIso(value) ?? new Date();
    return { y: seed.getFullYear(), m: seed.getMonth() };
  });
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  // Port of placePop: fixed-position against the trigger, flipping above
  // when below would clip and above has more room.
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

  // Place before paint on open, re-place next frame (fonts/styles may
  // shift the measurement), and follow the trigger on scroll/resize.
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

  // Outside click closes (port of outsideClose's containment check).
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
    // Port of initView: seed the viewed month from the value or today.
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
      {/* Kept in the form so submission still finds name + value. */}
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
        onClick={(e) => {
          // Don't toggle when the user clicked the inline clear ✕.
          if (e.target instanceof HTMLElement && e.target.classList.contains("bh-date-clear")) {
            e.stopPropagation();
            onChange("");
            return;
          }
          if (open) setOpen(false);
          else openPop();
        }}
      >
        <span className="bh-date-icon" aria-hidden="true">📅</span>
        <span className={`bh-date-label${value ? "" : " bh-date-placeholder"}`}>
          {value ? formatHumanDate(value) : "Select date"}
        </span>
        {value ? (
          <span className="bh-date-clear" aria-label="Clear" title="Clear">×</span>
        ) : null}
      </button>
      {open && (
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
        </div>
      )}
    </div>
  );
}
