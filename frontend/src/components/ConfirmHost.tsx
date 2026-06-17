/**
 * Promise-based confirm dialog.
 *
 * Usage anywhere: `if (await confirmDialog("Delete bug #5?", {...})) ...`
 * The App shell renders <ConfirmHost/> once; it subscribes to a module-level
 * queue. Escape resolves false (wired in App's key handler via the
 * data-bh-modal close path calling onClose, which settles false).
 */
import { useEffect, useState } from "react";

export interface ConfirmOptions {
  title?: string;
  okLabel?: string;
  danger?: boolean;
}

interface Pending {
  message: string;
  opts: Required<ConfirmOptions>;
  resolve: (v: boolean) => void;
}

let push: ((p: Pending) => void) | null = null;

export function confirmDialog(
  message: string,
  opts: ConfirmOptions = {},
): Promise<boolean> {
  const full: Required<ConfirmOptions> = {
    title: opts.title ?? "Confirm",
    okLabel: opts.okLabel ?? "Delete",
    danger: opts.danger ?? true,
  };
  return new Promise<boolean>((resolve) => {
    if (!push) {
      resolve(false);
      return;
    }
    push({ message, opts: full, resolve });
  });
}

export default function ConfirmHost() {
  const [current, setCurrent] = useState<Pending | null>(null);

  useEffect(() => {
    push = (p: Pending) => {
      setCurrent((prev) => {
        // A second confirm while one is open cancels the first.
        prev?.resolve(false);
        return p;
      });
    };
    return () => {
      push = null;
    };
  }, []);

  const settle = (value: boolean) => {
    current?.resolve(value);
    setCurrent(null);
  };

  // Escape closes (capture phase to beat the global modal-Escape handler).
  useEffect(() => {
    if (!current) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        settle(false);
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current]);

  return (
    <div className="modal" id="modalConfirm" hidden={!current}>
      <div className="modal-card sm confirm-card">
        <div className="modal-head">
          <h2 id="confirmTitle">{current?.opts.title ?? "Confirm"}</h2>
          <button
            type="button"
            className="icon-btn modal-close"
            id="confirmClose"
            aria-label="Close"
            onClick={() => settle(false)}
          >
            ✕
          </button>
        </div>
        <div className="modal-body confirm-body">
          <p id="confirmMessage">{current?.message ?? ""}</p>
        </div>
        <div className="modal-foot confirm-foot">
          <button type="button" className="btn ghost" id="confirmCancel" onClick={() => settle(false)}>
            Cancel
          </button>
          <button
            type="button"
            className={`btn ${current?.opts.danger ? "danger" : "primary"}`}
            id="confirmOk"
            onClick={() => settle(true)}
          >
            {current?.opts.okLabel ?? "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}
