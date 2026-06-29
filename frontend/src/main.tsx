/**
 * SPA entry point and auth gate.
 *
 * Theme is read from localStorage here at module top because CSP blocks inline
 * scripts; a <script type="module"> runs before first render, so there is no
 * flash of the wrong theme.
 *
 * The server redirects unauthenticated "/" requests to /login.html. The
 * client-side check below adds a second layer for cached pages and revoked
 * sessions.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppProvider } from "./state/AppContext";
import type { MeOut } from "./types";
import "./styles/styles.css";
import "./styles/chatbot.css";

document.documentElement.dataset.theme =
  localStorage.getItem("theme") || "dark";

// Restore the collapsed sidebar before first paint to avoid an expand-then-collapse
// animation on reload. AppContext keeps the class in sync from here on.
if (localStorage.getItem("sidebarCollapsed") === "1") {
  document.body.classList.add("sidebar-collapsed");
}

async function boot(): Promise<void> {
  let me: MeOut;
  try {
    const res = await fetch("/api/auth/me", { credentials: "include" });
    if (!res.ok) {
      location.replace("/login.html");
      return;
    }
    me = (await res.json()) as MeOut;
  } catch {
    location.replace("/login.html");
    return;
  }

  const root = document.getElementById("root");
  if (!root) return;
  createRoot(root).render(
    <StrictMode>
      <AppProvider me={me}>
        <App />
      </AppProvider>
    </StrictMode>,
  );
}

void boot();
