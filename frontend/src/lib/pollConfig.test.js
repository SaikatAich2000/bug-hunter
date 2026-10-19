import { describe, expect, it } from "vitest";

import { applyPollConfig, dataPollMs, notificationPollMs, sessionPollMs } from "./pollConfig.js";

describe("pollConfig", () => {
  it("keeps the built-in intervals until the server answers", () => {
    expect([dataPollMs(), sessionPollMs(), notificationPollMs()]).toEqual([30_000, 30_000, 15_000]);
  });

  it("adopts /api/meta values and ignores invalid ones", () => {
    applyPollConfig({ data_poll_seconds: 5, session_poll_seconds: "x", notification_poll_seconds: 0 });
    expect(dataPollMs()).toBe(5000);
    expect(sessionPollMs()).toBe(30_000);
    expect(notificationPollMs()).toBe(15_000);
    applyPollConfig(undefined);
    expect(dataPollMs()).toBe(5000);
    applyPollConfig({ data_poll_seconds: 30 });
  });
});
