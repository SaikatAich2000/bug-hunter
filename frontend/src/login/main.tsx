// Entry point for /login.html.
//
// CSP (script-src 'self', no 'unsafe-inline') blocks inline scripts, so
// theme bootstrap must happen here before the React tree mounts.
import { createRoot } from "react-dom/client";
import LoginPage from "./LoginPage";
import "../styles/styles.css";

// Apply saved theme before render to avoid a flash on load.
document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

const container = document.getElementById("root");
if (!container) {
  throw new Error("login: #root container not found");
}
// "contents" makes this mount node layout-transparent so .auth-body's
// flexbox centering of .auth-shell still works. Using CSSOM rather than
// an inline style attribute avoids tripping the style-src CSP.
container.style.display = "contents";

createRoot(container).render(<LoginPage />);
