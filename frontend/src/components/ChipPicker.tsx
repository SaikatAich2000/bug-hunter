/**
 * ChipPicker — a selectable chip list. Class names must match styles.css:
 *
 *   <div class="chip-picker [locked]" id="…">
 *     <span class="chip [selected]" data-id="…">
 *       Alice <span class="chip-sub">· admin</span>
 *     </span> × n
 *     — or — <span class="chip-empty">— none available —</span>
 *   </div>
 *
 * Selection is controlled: clicking a chip calls onToggle(id) and the parent
 * flips membership in `selected`. `disabled` applies the `.locked` read-only
 * treatment (pointer-events: none via styles.css).
 */

export interface ChipItem {
  id: number;
  label: string;
  /** Secondary text rendered as the `.chip-sub` span (e.g. the user's role). */
  title?: string;
}

interface Props {
  items: ChipItem[];
  selected: number[];
  onToggle: (id: number) => void;
  disabled?: boolean;
  id?: string;
}

export default function ChipPicker({ items, selected, onToggle, disabled, id }: Props) {
  const selectedSet = new Set(selected);

  return (
    <div className={`chip-picker${disabled ? " locked" : ""}`} id={id}>
      {items.length === 0 ? (
        <span className="chip-empty">— none available —</span>
      ) : (
        items.map((item) => (
          <span
            key={item.id}
            className={`chip${selectedSet.has(item.id) ? " selected" : ""}`}
            data-id={item.id}
            onClick={() => {
              if (!disabled) onToggle(item.id);
            }}
          >
            {item.label}
            {item.title ? (
              <>
                {" "}
                <span className="chip-sub">· {item.title}</span>
              </>
            ) : null}
          </span>
        ))
      )}
    </div>
  );
}
