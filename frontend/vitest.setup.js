// Tell React this is a test environment that wraps updates in act(); without
// it every render logs "The current testing environment is not configured to
// support act(...)".
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// jsdom has no media pipeline: HTMLMediaElement.play()/pause() throw "Not
// implemented". Resolve/no-op them so video components behave like a browser
// that isn't playing anything.
if (typeof window !== "undefined" && window.HTMLMediaElement) {
  window.HTMLMediaElement.prototype.play = function play() { return Promise.resolve(); };
  window.HTMLMediaElement.prototype.pause = function pause() {};
}

// Unmount whatever a test rendered with Testing Library so DOM and timers never leak between tests.
import { afterEach } from "vitest";
import { cleanup, configure } from "@testing-library/react";

// findBy*/waitFor poll for up to this long; the 1 s default flakes under CPU load.
configure({ asyncUtilTimeout: 8000 });
afterEach(() => {
  cleanup();
  localStorage.clear();
});
import "@testing-library/jest-dom/vitest";

// jsdom has no blob URLs; the staged-attachment tiles need them.
URL.createObjectURL = () => `blob:test/${Math.random().toString(36).slice(2)}`;
URL.revokeObjectURL = () => {};
