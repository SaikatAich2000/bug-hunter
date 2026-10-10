/** Motion helpers: reduced motion is honoured by every JS-driven animation. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { MOTION_MS, dragDropAnimation, prefersReducedMotion } from "./motion.js";

const original = window.matchMedia;

function mockMedia(reduce) {
  window.matchMedia = vi.fn((query) => ({ matches: reduce && query.includes("reduce"), media: query }));
}

afterEach(() => {
  window.matchMedia = original;
});

describe("prefersReducedMotion", () => {
  it("treats an environment without matchMedia as reduced", () => {
    window.matchMedia = undefined;
    expect(prefersReducedMotion()).toBe(true);
  });

  it("follows the system setting", () => {
    mockMedia(true);
    expect(prefersReducedMotion()).toBe(true);
    mockMedia(false);
    expect(prefersReducedMotion()).toBe(false);
  });
});

describe("dragDropAnimation", () => {
  it("settles in the standard duration when motion is allowed", () => {
    mockMedia(false);
    expect(dragDropAnimation()).toMatchObject({ duration: MOTION_MS });
    expect(MOTION_MS).toBeGreaterThanOrEqual(150);
    expect(MOTION_MS).toBeLessThanOrEqual(250);
  });

  it("drops instantly under reduced motion", () => {
    mockMedia(true);
    expect(dragDropAnimation()).toBeNull();
  });
});
