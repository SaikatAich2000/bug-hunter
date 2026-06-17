/**
 * Main SPA entry — the auth gate.
 *
 * Theme is applied before first paint (no inline script allowed by CSP, so it
 * happens here at module top — the bundle loads with <script type="module">,
 * which executes before first render anyway).
 *
 * The server already redirects unauthenticated requests for "/" to
 * /login.html; the client-side check below is belt-and-braces, covering cached
 * pages and revoked sessions.
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
