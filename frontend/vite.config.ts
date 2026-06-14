import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Bug Hunter frontend build.
//
// - base "/static/" because FastAPI serves the bundle from its existing
//   StaticFiles mount at /static (see app/main.py). The three HTML entry
//   pages themselves are served by FastAPI's _serve_html() (auth-gated /,
//   /login.html, /reset.html) which also substitutes __APP_VERSION__.
// - outDir is app/static directly: `npm run build` produces the exact tree
//   the backend serves — no copy step, deploy.sh/Docker unchanged.
// - emptyOutDir false: app/static is shared with a few committed assets;
//   stale hashed bundles are pruned by the build script when needed.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: resolve(__dirname, "..", "app", "static"),
    emptyOutDir: false,
    rollupOptions: {
      input: {
        index: resolve(__dirname, "index.html"),
        login: resolve(__dirname, "login.html"),
        reset: resolve(__dirname, "reset.html"),
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      // npm run dev → proxy API calls to a locally-running backend.
      "/api": { target: "http://localhost:8765", changeOrigin: false },
    },
  },
});
