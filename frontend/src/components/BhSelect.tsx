/**
 * BhSelect — React port of the vanilla custom single-select dropdown
 * (`enhanceCustomSelect`, app/static/app.js L1446-L1570).
 *
 * DOM (class names must match styles.css exactly):
 *
 *   <div class="bh-sel-wrap">
 *     <select class="bh-sel-native" …>                  ← form value + keyboard path
 *     <button class="bh-sel-btn [is-disabled]" aria-haspopup="listbox" aria-expanded>
 *       <span class="bh-sel-label [bh-sel-placeholder]">…</span>
 *       <span class="bh-sel-caret">▾</span>             ← hidden while disabled
 *     </button>
 *     <div class="bh-sel-pop" role="listbox">           ← only while open
 *       <button class="bh-sel-row [is-selected]" role="menuitemcheckbox"
 *               aria-checked data-bh-sel-i="i">…</button> × n
 *     </div>
 *   </div>
 *
 * Differences from vanilla: the popover renders in-place instead of on
 * document.body (still `position: fixed`, placed with the same placePop
 * math incl. the trigger-width min-width), and the value is controlled.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

export interface BhSelectOption {
  value: string;
  label: string;
  /** Optional tooltip — mirrors the title attr fillFormSelect put on <option>. */
  title?: string;
}

interface Props {
  value: string;
  onChange: (v: string) => void;
  options: BhSelectOption[];
  disabled?: boolean;
  id?: string;
  name?: string;
  ariaLabel?: string;
}

// Placeholder options look like "— select —" (port of the vanilla regex).
const PLACEHOLDER_RE = /^—.*—$/;

export default function BhSelect({ value, onChange, options, disabled, id, name, ariaLabel }: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  // Port of placePop: viewport-anchored fixed positioning with the
  // flip-above-when-clipped rule; panel min-width matches the trigger.
  const placePop = useCallback(() => {
    const btn = btnRef.current;
    const pop = popRef.current;
    if (!btn || !pop) return;
    const r = btn.getBoundingClientRect();
    const vh = window.innerHeight;
    const vw = window.innerWidth;
    const ph = pop.offsetHeight || 240;
    const pw = Math.max(pop.offsetWidth || 200, r.width);
    pop.style.minWidth = `${r.width}px`;
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

  // Outside click closes (port of the vanilla `outside` containment check).
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

  // Resolve the selected option the way the native select would:
  // unmatched value → selectedIndex -1 → label falls back to "—".
  const selectedIndex = options.findIndex((o) => o.value === value);
  const selOpt = selectedIndex >= 0 ? options[selectedIndex] : undefined;
  const labelText = selOpt ? selOpt.label : "";
  const isPlaceholder = !value && selOpt !== undefined && PLACEHOLDER_RE.test(selOpt.label || "");

  return (
    <div className="bh-sel-wrap" ref={wrapRef}>
      {/* Kept in the DOM so form submission picks the value up via .name,
          and so keyboard users retain the native select behaviour. */}
      <select
        className="bh-sel-native"
        id={id}
        name={name}
        value={value}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map((o) => (
          <option key={o.value} value={o.value} title={o.title}>
            {o.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        className={`bh-sel-btn${disabled ? " is-disabled" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        ref={btnRef}
        onClick={() => {
          if (disabled) return;
          setOpen((o) => !o);
        }}
      >
        <span className={`bh-sel-label${isPlaceholder ? " bh-sel-placeholder" : ""}`}>
          {labelText || "—"}
        </span>
        {/* A disabled select can't be opened, so the caret would mislead —
            render a plain pill instead (vanilla v2.6 fix). */}
        {!disabled && <span className="bh-sel-caret" aria-hidden="true">▾</span>}
      </button>
      {open && (
        <div className="bh-sel-pop" role="listbox" ref={popRef}>
          {options.map((o, i) => {
            const isSel = i === selectedIndex;
            return (
              <button
                type="button"
                key={o.value}
                className={`bh-sel-row${isSel ? " is-selected" : ""}`}
                role="menuitemcheckbox"
                aria-checked={isSel}
                data-bh-sel-i={i}
                title={o.title}
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
              >
                {o.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
