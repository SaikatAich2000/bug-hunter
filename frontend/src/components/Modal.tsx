/**
 * Generic modal wrapper:
 *
 *   <div class="modal" id={id} hidden>
 *     <div class="modal-card {size}">
 *       <div class="modal-head"><h2>{title}</h2><button class="icon-btn modal-close">✕</button></div>
 *       {children}
 *     </div>
 *   </div>
 *
 * Open/close is driven by the `hidden` attribute (styles.css:
 * `.modal[hidden] { display:none }`). Escape-to-close is handled centrally in
 * App.tsx so stacked modals close top-most first.
 */
import type { ReactNode } from "react";

interface Props {
  id: string;
  open: boolean;
  title: ReactNode;
  subtitle?: ReactNode;
  size?: "sm" | "lg" | "xxl" | "";
  cardClass?: string;
  /** Extra content right-aligned in the head (e.g. the bug Delete button). */
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
