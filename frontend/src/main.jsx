// SPA entry point and auth gate (client-side /api/auth/me check backs up the
// server redirect for cached pages / revoked sessions).
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AppProvider } from "./state/AppContext";
import "./styles/styles.css";
import "./styles/chatbot.css";
import "./styles/onboarding.css";
import "./styles/motion.css";

// Theme + collapsed-sidebar state read before first render (CSP blocks inline
// scripts) — no wrong-theme flash / collapse animation on reload. Guarded:
// blocked storage (e.g. SecurityError) must not abort SPA boot.
let storedTheme = null;
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

/** The signed-in user, or null after redirecting to the login page. */
async function loadSession() {
  try {
    const res = await fetch("/api/auth/me", { credentials: "include" });
    if (res.ok) return await res.json();
  } catch {
    /* treated like an expired session */
  }
  location.replace("/login.html");
  return null;
}

const me = await loadSession();
const root = me ? document.getElementById("root") : null;
if (root) {
  createRoot(root).render(
    <StrictMode>
      <AppProvider me={me}>
        <App />
      </AppProvider>
    </StrictMode>,
  );
}
