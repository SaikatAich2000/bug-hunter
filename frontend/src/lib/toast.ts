/**
 * Toast notifications.
 *
 * The app shell renders a single `<div id="toast" class="toast" hidden>`;
 * toast() swaps its text + type class and unhides it for 3.5s.
 *
 * Imperative so it can be called from anywhere (API error paths, editors)
 * without prop-drilling.
 */
import { ApiError } from "./api";

export type ToastType = "info" | "success" | "error";

let toastTimer: ReturnType<typeof setTimeout> | undefined;

export function toast(msg: string, type: ToastType = "info"): void {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `toast ${type}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 3500);
}

/** Error toast that stays quiet for the silent 401-redirect ApiError. */
export function toastError(err: unknown): void {
  if (err instanceof ApiError && err.silent) return;
  let msg = "Something went wrong";
  if (err instanceof Error) msg = err.message;
  else if (typeof err === "string") msg = err;
  toast(msg, "error");
}
