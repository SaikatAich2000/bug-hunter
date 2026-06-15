/**
 * App root: global chrome (blocking loader, toast slot, confirm dialog),
 * the Sleuth chat panel, the shell (sidebar + topbar + views + modals),
 * and the global Escape handler (port of closeTopModal, app.js L244-247 +
 * the keydown delegation at L4751-4769).
 */
import { lazy, Suspense, useEffect } from "react";
import Shell from "./shell/Shell";
import ConfirmHost from "./components/ConfirmHost";
// Sleuth (the chat assistant + its rules/markdown deps) is a sizeable, secondary
// feature shown only behind the chat FAB — lazy-load it out of the main bundle.
const SleuthPanel = lazy(() => import("./sleuth/SleuthPanel"));

export default function App() {
  // Escape closes the top-most open modal — DOM-driven exactly like the
  // vanilla helper so it works uniformly for every modal (and skips when
  // focus is in a text field, matching the original guard).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const t = e.target as HTMLElement | null;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      ) {
        return;
      }
      const open = document.querySelectorAll<HTMLElement>(".modal:not([hidden])");
      const top = open[open.length - 1];
      if (!top) return;
      const close = top.querySelector<HTMLButtonElement>(".modal-close");
      close?.click();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      {/* Global blocking loader — markup mirrors vanilla index.html L18-23 */}
      <div
        id="globalLoader"
        className="global-loader"
        hidden
        aria-hidden="true"
        role="alert"
      >
        <div className="global-loader-card">
          <div className="global-loader-spinner" aria-hidden="true"></div>
          <div className="global-loader-text" id="globalLoaderText">
            Working…
          </div>
        </div>
      </div>

      <Shell />

      {/* Single toast element — vanilla index.html L879 */}
      <div id="toast" className="toast" hidden></div>

      <ConfirmHost />
      <Suspense fallback={null}>
        <SleuthPanel />
      </Suspense>
    </>
  );
}
