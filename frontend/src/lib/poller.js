/**
 * A background poll that never overlaps itself, skips work while the tab is hidden, runs at
 * once when the tab becomes visible again, and backs off (doubling up to `maxMs`) while the
 * task keeps failing. `onError(err, failures)` and `onRecover()` let the caller tell the user
 * once instead of on every tick. Returns a stop function.
 *
 * It also eases off while nobody is using the page: after IDLE_AFTER_MS without input it polls
 * at a quarter of the rate, after PAUSE_AFTER_MS it stops until the next input. A server on a
 * small instance would otherwise spend its CPU answering tabs that nobody is looking at.
 */
const IDLE_AFTER_MS = 2 * 60_000;
const PAUSE_AFTER_MS = 10 * 60_000;
const IDLE_SLOWDOWN = 4;

let lastActivity = Date.now();
const wakers = new Set();
let listening = false;

function noteActivity() {
  const wasIdle = Date.now() - lastActivity >= IDLE_AFTER_MS;
  lastActivity = Date.now();
  if (wasIdle) wakers.forEach((wake) => wake());
}

function listenForActivity() {
  if (listening || typeof document === "undefined") return;
  listening = true;
  for (const type of ["pointerdown", "keydown", "touchstart", "wheel"]) {
    document.addEventListener(type, noteActivity, { passive: true, capture: true });
  }
}

export function startPoller(task, intervalMs, { maxMs = intervalMs * 8, onError, onRecover } = {}) {
  let timer = null;
  let running = false;
  let stopped = false;
  let failures = 0;
  listenForActivity();

  const idleFor = () => Date.now() - lastActivity;

  const schedule = () => {
    clearTimeout(timer);
    if (stopped) return;
    const base = failures ? Math.min(intervalMs * 2 ** failures, maxMs) : intervalMs;
    timer = setTimeout(run, idleFor() >= IDLE_AFTER_MS ? Math.max(base, intervalMs * IDLE_SLOWDOWN) : base);
  };

  async function run() {
    if (stopped || running) return;
    if (document.hidden || idleFor() >= PAUSE_AFTER_MS) {
      schedule();
      return;
    }
    running = true;
    try {
      await task();
      if (failures) onRecover?.();
      failures = 0;
    } catch (err) {
      failures += 1;
      onError?.(err, failures);
    } finally {
      running = false;
      schedule();
    }
  }

  const wake = () => { if (!document.hidden) void run(); };
  const onVisibility = () => {
    if (!document.hidden) {
      lastActivity = Date.now();
      void run();
    }
  };
  document.addEventListener("visibilitychange", onVisibility);
  wakers.add(wake);
  schedule();
  return () => {
    stopped = true;
    clearTimeout(timer);
    wakers.delete(wake);
    document.removeEventListener("visibilitychange", onVisibility);
  };
}
