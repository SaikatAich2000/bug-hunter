// SPA entry point and auth gate (client-side /api/auth/me check backs up the
// server redirect for cached pages / revoked sessions).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppProvider } from "./state/AppContext";
import type { MeOut } from "./types";
import "./styles/styles.css";
import "./styles/chatbot.css";

// Theme + collapsed-sidebar state read before first render (CSP blocks inline
// scripts) — no wrong-theme flash / collapse animation on reload. Guarded:
// blocked storage (e.g. SecurityError) must not abort SPA boot.
let storedTheme: string | null = null;
let collapsed = false;
try {
  storedTheme = localStorage.getItem("theme");
  collapsed = localStorage.getItem("sidebarCollapsed") === "1";
} catch {
  /* storage blocked */
}
document.documentElement.dataset.theme = storedTheme || "dark";
if (collapsed) {
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
