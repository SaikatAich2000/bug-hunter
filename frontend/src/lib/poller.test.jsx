import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startPoller } from "./poller";

describe("startPoller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("never runs the task twice at once", async () => {
    let release;
    const task = vi.fn(() => new Promise((r) => { release = r; }));
    const stop = startPoller(task, 1000);
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(5000);
    expect(task).toHaveBeenCalledTimes(1);
    release();
    await vi.advanceTimersByTimeAsync(1000);
    expect(task).toHaveBeenCalledTimes(2);
    stop();
  });

  it("backs off while failing and reports once per failure streak", async () => {
    const onError = vi.fn();
    const onRecover = vi.fn();
    let fail = true;
    const task = vi.fn(async () => { if (fail) throw new Error("down"); });
    const stop = startPoller(task, 1000, { maxMs: 4000, onError, onRecover });
    await vi.advanceTimersByTimeAsync(1000); // 1st try fails
    await vi.advanceTimersByTimeAsync(1999);
    expect(task).toHaveBeenCalledTimes(1); // next try waits 2 s
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    fail = false;
    await vi.advanceTimersByTimeAsync(4000);
    expect(task).toHaveBeenCalledTimes(3);
    expect(onError.mock.calls.map((c) => c[1])).toEqual([1, 2]);
    expect(onRecover).toHaveBeenCalledTimes(1);
    stop();
  });

  it("stops cleanly", async () => {
    const task = vi.fn(async () => {});
    const stop = startPoller(task, 1000);
    stop();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(task).not.toHaveBeenCalled();
  });
});

describe("startPoller while the page is idle", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const touch = () => document.dispatchEvent(new Event("pointerdown"));

  it("polls at a quarter of the rate after two idle minutes, and stops after ten", async () => {
    touch();
    const task = vi.fn(async () => {});
    const stop = startPoller(task, 30_000);
    await vi.advanceTimersByTimeAsync(120_000); // active for two minutes: one poll every 30 s
    const active = task.mock.calls.length;
    expect(active).toBeGreaterThanOrEqual(3);
    await vi.advanceTimersByTimeAsync(480_000); // minutes 2 to 10, idle: one poll every 2 minutes
    const idle = task.mock.calls.length - active;
    expect(idle).toBeLessThanOrEqual(5);
    expect(idle).toBeGreaterThanOrEqual(3);
    const atPause = task.mock.calls.length;
    await vi.advanceTimersByTimeAsync(600_000); // past ten idle minutes: nothing
    expect(task.mock.calls.length).toBe(atPause);
    stop();
  });

  it("wakes up at once on the next input after being idle", async () => {
    touch();
    const task = vi.fn(async () => {});
    const stop = startPoller(task, 30_000);
    await vi.advanceTimersByTimeAsync(11 * 60_000);
    const before = task.mock.calls.length;
    touch();
    await vi.advanceTimersByTimeAsync(10);
    expect(task.mock.calls.length).toBe(before + 1);
    stop();
  });

  it("keeps the normal rate while the user keeps interacting", async () => {
    const task = vi.fn(async () => {});
    touch();
    const stop = startPoller(task, 30_000);
    for (let i = 0; i < 10; i += 1) {
      await vi.advanceTimersByTimeAsync(60_000);
      touch();
    }
    expect(task.mock.calls.length).toBeGreaterThanOrEqual(19); // 10 minutes at 30 s
    stop();
  });
});
