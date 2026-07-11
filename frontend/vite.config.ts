import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// base "/static/" matches FastAPI's StaticFiles mount; outDir writes straight
// into app/static (no copy step); emptyOutDir false since some assets are committed.
export default defineConfig({
  plugins: [react()],
  base: "/static/",
  build: {
    outDir: resolve(__dirname, "..", "app", "static"),
    emptyOutDir: false,
    sourcemap: false,   // never ship source maps (would expose the TS source)
    rollupOptions: {
      input: {
        index: resolve(__dirname, "index.html"),
        login: resolve(__dirname, "login.html"),
        reset: resolve(__dirname, "reset.html"),
      },
      output: {
        // Split RichEditor into its own chunk (still a static import, not React.lazy).
        manualChunks(id: string) {
          if (id.includes("RichEditor")) return "rich-editor";
        },
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
