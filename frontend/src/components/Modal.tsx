/** Generic modal wrapper; open/close via `hidden`. Escape handled centrally in App.tsx (top-most first). */
import type { ReactNode } from "react";

interface Props {
  id: string;
  open: boolean;
  title: ReactNode;
  subtitle?: ReactNode;
  size?: "sm" | "lg" | "xxl" | "";
  cardClass?: string;
  /** Extra content right-aligned in the head. */
  headExtra?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}

export default function Modal({
  id,
  open,
  title,
  subtitle,
  size = "",
  cardClass = "",
  headExtra,
  onClose,
  children,
}: Props) {
  return (
    <div className="modal" id={id} hidden={!open} data-bh-modal>
      <div className={`modal-card ${size} ${cardClass}`.trim()}>
        <div className="modal-head">
          <div className="modal-head-text">
            <h2>{title}</h2>
            {subtitle != null && <p className="modal-subtitle">{subtitle}</p>}
          </div>
          {headExtra}
          <button
            type="button"
            className="icon-btn modal-close"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
