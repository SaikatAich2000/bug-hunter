// SPA entry point and auth gate (client-side /api/auth/me check backs up the
// server redirect for cached pages / revoked sessions).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppProvider } from "./state/AppContext";
import type { MeOut } from "./types";
import "./styles/styles.css";
import "./styles/chatbot.css";

// Theme set before first render (CSP blocks inline scripts) — no wrong-theme flash.
document.documentElement.dataset.theme =
  localStorage.getItem("theme") || "dark";

// Restore collapsed sidebar before first paint to avoid a collapse animation on reload.
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
