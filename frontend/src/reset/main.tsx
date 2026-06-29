// Entry point for /reset.html.
// Theme must be set before the React tree mounts to avoid a flash; inline
// scripts are blocked by CSP so this module is the earliest safe place.
import { createRoot } from "react-dom/client";
import ResetPage from "./ResetPage";
import "../styles/styles.css";

// Apply saved theme before first paint.
document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

const container = document.getElementById("root");
if (!container) {
  throw new Error("reset: #root container not found");
}
// "contents" makes the mount node layout-transparent so .auth-body flexbox
// keeps centering .auth-shell. CSSOM assignment avoids the style-src CSP.
container.style.display = "contents";

createRoot(container).render(<ResetPage />);
