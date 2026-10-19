import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL(".", import.meta.url));

export default defineConfig({
  plugins: [react()],
  server: { fs: { strict: false, allow: [fileURLToPath(new URL(".", import.meta.url))] } },
  test: {
    environment: "jsdom",
    pool: "forks",
    // Generous so tests stay green when the machine is busy (CI, scans); passing tests never wait this long.
    testTimeout: 20000,
    hookTimeout: 20000,
    include: ["src/**/*.test.jsx"],
    setupFiles: ["./vitest.setup.js"],
    exclude: ["node_modules/**"],
    coverage: {
      provider: "v8",
      reporter: ["text-summary", "json-summary", "lcov"],
      reportsDirectory: "./coverage",
      include: ["src/**/*.js", "src/**/*.jsx"],
      exclude: [
        "src/**/*.test.jsx", // the tests themselves
        "src/test/**", // fake-API test harness
      ],
      thresholds: { lines: 95, statements: 95 },
    },
  },
  resolve: {
    alias: { "@": resolve(root, "src") },
  },
});