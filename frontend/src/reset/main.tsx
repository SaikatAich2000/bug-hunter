// Entry for /reset.html.
//
// The app's CSP (script-src 'self', no 'unsafe-inline') forbids inline
// scripts, so the theme bootstrap happens here, before the React tree is
// created.
import { createRoot } from "react-dom/client";
import ResetPage from "./ResetPage";
import "../styles/styles.css";

// Theme persists across pages — apply it before render so the page
// never flashes the wrong theme.
document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

const container = document.getElementById("root");
if (!container) {
  throw new Error("reset: #root container not found");
}
// .auth-body centers .auth-shell with flexbox; make the React mount node
// layout-transparent so that CSS keeps working unchanged. (CSSOM assignment,
// not an inline style attribute, so the strict style-src CSP is not violated.)
container.style.display = "contents";

createRoot(container).render(<ResetPage />);
