/**
 * Main SPA entry — port of the vanilla boot() auth gate (app.js L1572-1599).
 *
 * Theme is applied BEFORE first paint (no inline script allowed by CSP, so
 * it happens here at module top — the bundle is loaded with <script
 * type="module"> which executes before first render anyway).
 *
 * The server already redirects unauthenticated requests for "/" to
 * /login.html; the client-side check below is the belt-and-braces port of
 * the vanilla behavior (covers cached pages and revoked sessions).
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
