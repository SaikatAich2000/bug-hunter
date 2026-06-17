/**
 * MsFilter — the filter-bar multi-select dropdown. Class names must match
 * styles.css:
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
 * The panel is plain absolute-positioned CSS (no placement JS). Only one panel
 * may be open across all instances: a module-level subscriber registry closes
 * the others when one opens.
 */
import { memo, useCallback, useEffect, useMemo, useState } from "react";

// --- one-open-panel-at-a-time registry. Every mounted instance subscribes its
// --- close callback; opening one instance closes all the others.
const closers = new Set<() => void>();
function closeOthers(except: () => void): void {
  closers.forEach((close) => {
    if (close !== except) close();
  });
}

/**
 * "All {label}" when nothing is selected, the single option's label when
 * exactly one, "{noun} (n)" when several.
 */
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
  /** Filter key, rendered as data-filter on the wrap (e.g. "project_id"). */
  filterKey: string;
  /** Plural label for the empty state, e.g. "Projects" → "All Projects". */
  label: string;
  /** Plural noun for the n-selected state, e.g. "Projects" → "Projects (2)". */
  noun: string;
  /** [value, label] pairs — value goes to the API, label to the user. */
  options: [string, string][];
  selected: string[];
  onToggle: (value: string) => void;
}

function MsFilter({ filterKey, label, noun, options, selected, onToggle }: Props) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);

  // Subscribe this instance's closer for the lifetime of the component.
  useEffect(() => {
    closers.add(close);
    return () => {
      closers.delete(close);
    };
  }, [close]);

  // Any document click closes the panel. Toggle and row clicks stopPropagation
  // to survive, so no containment check is needed here.
  useEffect(() => {
    if (!open) return;
    const onDocClick = () => setOpen(false);
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  return (
    <div className="ms-wrap" data-filter={filterKey}>
      <button
        type="button"
        className={`ms-btn${selected.length > 0 ? " active" : ""}`}
        data-ms-toggle=""
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          closeOthers(close);
          setOpen((o) => !o);
        }}
      >
        <span className="ms-btn-label">{msButtonLabel(label, noun, options, selected)}</span>
        <span className="ms-caret">▾</span>
      </button>
      <div className="ms-panel" hidden={!open} role="menu">
        {options.length === 0 ? (
          <div className="ms-empty">No options</div>
        ) : (
          options.map(([v, lbl]) => {
            const isOn = selectedSet.has(v);
            return (
              <div
                key={v}
                className={`ms-row${isOn ? " on" : ""}`}
                data-ms-value={v}
                role="menuitemcheckbox"
                aria-checked={isOn}
                onClick={(e) => {
                  e.stopPropagation();
                  onToggle(v);
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

// Memoized so a parent re-render (e.g. the 10s/15s context polls) doesn't
// re-render every filter when its own props are unchanged. FilterBar now
// passes stable useCallback handlers + useMemo'd option/selected arrays, so
// the shallow prop compare actually holds.
export default memo(MsFilter);
