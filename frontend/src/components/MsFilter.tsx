/**
 * Filter-bar multi-select dropdown. Class names must match styles.css:
 *
 *   <div class="ms-wrap" data-filter="{filterKey}">
 *     <button class="ms-btn [active]" data-ms-toggle aria-haspopup="menu" aria-expanded>
 *       <span class="ms-btn-label">All Projects | Alice | Projects (2)</span>
 *       <span class="ms-caret">▾</span>
 *     </button>
 *     <div class="ms-panel" [hidden] role="menu">
 *       <div class="ms-row [on]" data-ms-value role="menuitemcheckbox" aria-checked>
 *         <span class="ms-check">✓</span><span class="ms-text">…</span>
 *       </div> × n   (or <div class="ms-empty">No options</div>)
 *     </div>
 *   </div>
 *
 * Panel placement is pure CSS (absolute). Only one panel can be open at a time;
 * a module-level registry closes others when a new one opens.
 */
import { memo, useCallback, useEffect, useMemo, useState } from "react";

// Every mounted instance registers its close callback here; opening any panel
// calls closeOthers to shut the rest.
const closers = new Set<() => void>();
function closeOthers(except: () => void): void {
  closers.forEach((close) => {
    if (close !== except) close();
  });
}

/** Returns the button label: "All {label}", a single name, or "{noun} (n)". */
function msButtonLabel(
  label: string,
  noun: string,
  options: [string, string][],
  selected: string[],
): string {
  if (selected.length === 0) return `All ${label}`;
  if (selected.length === 1) {
    const only = selected[0];
    const match = options.find(([v]) => v === only);
    return match ? match[1] : only;
  }
  return `${noun} (${selected.length})`;
}

interface Props {
  /** Rendered as data-filter on the wrap div (e.g. "project_id"). */
  filterKey: string;
  /** Plural label for the empty state, e.g. "Projects" → "All Projects". */
  label: string;
  /** Plural noun for the multi-selected state, e.g. "Projects" → "Projects (2)". */
  noun: string;
  /** [value, displayLabel] pairs — value is sent to the API. */
  options: [string, string][];
  selected: string[];
  onToggle: (value: string) => void;
}

function MsFilter({ filterKey, label, noun, options, selected, onToggle }: Props) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);

  // Register/deregister this instance's close callback.
  useEffect(() => {
    closers.add(close);
    return () => {
      closers.delete(close);
    };
  }, [close]);

  // Click-outside to close. Toggle and row clicks call stopPropagation,
  // so no containment check is needed.
  useEffect(() => {
    if (!open) return;
    const onDocClick = () => setOpen(false);
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  // Capture-phase Escape so it pre-empts any surrounding modal's handler.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setOpen(false);
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const panelId = `ms-panel-${filterKey}`;

  return (
    <div className="ms-wrap" data-filter={filterKey}>
      <button
        type="button"
        className={`ms-btn${selected.length > 0 ? " active" : ""}`}
        data-ms-toggle=""
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={(e) => {
          e.stopPropagation();
          closeOthers(close);
          setOpen((o) => !o);
        }}
      >
        <span className="ms-btn-label">{msButtonLabel(label, noun, options, selected)}</span>
        <span className="ms-caret">▾</span>
      </button>
      <div className="ms-panel" id={panelId} hidden={!open} role="menu">
        {options.length === 0 ? (
          <div className="ms-empty">No options</div>
        ) : (
          options.map(([v, lbl]) => {
            const isOn = selectedSet.has(v);
            const toggle = (e: { stopPropagation: () => void }) => {
              e.stopPropagation();
              onToggle(v);
            };
            return (
              <div
                key={v}
                className={`ms-row${isOn ? " on" : ""}`}
                data-ms-value={v}
                role="menuitemcheckbox"
                aria-checked={isOn}
                tabIndex={0}
                onClick={toggle}
                onKeyDown={(e) => {
                  // Enter/Space mirror a mouse click on the row.
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggle(e);
                  }
                }}
              >
                <span className="ms-check">{isOn ? "✓" : ""}</span>
                <span className="ms-text">{lbl}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// Memo prevents parent re-renders (e.g. context polls) from cascading here.
// FilterBar passes stable useCallback handlers and memoized arrays, so the
// shallow prop compare reliably short-circuits.
export default memo(MsFilter);
