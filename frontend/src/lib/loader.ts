/** Counter-based global loader; overlay stays up until all callers finish. */
let pending = 0;

function apply(message?: string): void {
  const el = document.getElementById("globalLoader");
  const txt = document.getElementById("globalLoaderText");
  if (!el) return;
  if (message && txt) txt.textContent = message;
  const on = pending > 0;
  el.hidden = !on;
  document.body.classList.toggle("is-loading", on);
}

export function showLoader(message = "Working…"): void {
  pending += 1;
  apply(message);
}

export function hideLoader(): void {
  pending = Math.max(0, pending - 1);
  apply();
}

export async function withLoader<T>(
  thunk: () => Promise<T>,
  message = "Working…",
): Promise<T> {
  showLoader(message);
  try {
    return await thunk();
  } finally {
    hideLoader();
  }
}
