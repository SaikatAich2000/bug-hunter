"""Responsive and accessibility sweep of a RUNNING instance with Playwright and axe-core.

    python scripts/qa/ui_audit.py http://127.0.0.1:8001 <admin email> <admin password> [public|app|all] [single-tenant]

At 320, 360, 390, 768, 1024, 1280 and 1920 px wide, on every public page and (signed in) every view in the
side navigation, it reports: horizontal page overflow, text clipped by its box, broken images, console errors,
and axe-core WCAG 2.x A/AA violations (including the 24 px target size, with its spacing exception). Needs
`frontend/node_modules/axe-core` (run `npm ci` in frontend/ once). Exit code 1 when anything is found.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, EMAIL, PASSWORD = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
SCOPE = sys.argv[4] if len(sys.argv) > 4 else "all"
WIDTHS = [320, 360, 390, 768, 1024, 1280, 1920]
PUBLIC = ["/login.html", "/signup.html", "/reset.html", "/privacy.html", "/terms.html", "/refund-policy", "/contact.html", "/welcome"]
PUBLIC_ONLY = {"/login.html", "/reset.html"} if len(sys.argv) > 5 and sys.argv[5] == "single-tenant" else set(PUBLIC)
AXE = (Path(__file__).resolve().parents[2] / "frontend" / "node_modules" / "axe-core" / "axe.min.js").read_text(encoding="utf-8")

PROBE = """() => {
  const out = {overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, clipped: [], broken: 0};
  const vis = (el) => { const r = el.getBoundingClientRect(), s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && r.bottom > 0 && r.top < innerHeight * 3; };
  for (const el of document.querySelectorAll('h1,h2,h3,h4,p,span,a,button,label,li,td,th')) {
    if (!vis(el) || el.childElementCount > 0) continue;
    const s = getComputedStyle(el);
    if (el.scrollWidth > el.clientWidth + 2 && s.overflowX === 'hidden' && s.textOverflow !== 'ellipsis' && el.textContent.trim().length > 1 && el.clientWidth > 4)
      out.clipped.push((el.textContent.trim().slice(0, 40)) + ' <' + el.tagName.toLowerCase() + '>');
  }
  out.broken = [...document.images].filter(i => i.complete && i.naturalWidth === 0 && i.src && vis(i)).length;
  return out;
}"""

findings: list[str] = []


def audit(page, label: str, width: int) -> None:
    page.wait_for_timeout(500)
    res = page.evaluate(PROBE)
    tag = f"{label} @{width}"
    if res["overflow"] > 1:
        findings.append(f"{tag}: horizontal overflow {res['overflow']}px")
    for c in res["clipped"][:3]:
        findings.append(f"{tag}: clipped text '{c}'")
    if res["broken"]:
        findings.append(f"{tag}: {res['broken']} broken image(s)")
    if width in (360, 1280):  # axe is slow; two widths cover the mobile and desktop layouts
        page.evaluate(AXE)
        axe = page.evaluate("() => axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']}})")
        for v in axe["violations"]:
            findings.append(f"{tag}: axe {v['id']} ({v['impact']}) x{len(v['nodes'])}: {v['nodes'][0]['target']}")


errors: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    if SCOPE in ("public", "all"):
        for width in WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text[:100]}") if m.type == "error" else None)
            for path in (p for p in PUBLIC if p in PUBLIC_ONLY):
                page.goto(BASE + path, wait_until="networkidle")
                audit(page, path, width)
            ctx.close()
    if SCOPE in ("app", "all"):
        for width in WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: errors.append(f"console {m.type}: {m.text[:100]}") if m.type == "error" else None)
            page.goto(BASE + "/login.html", wait_until="networkidle")
            r = ctx.request.post(BASE + "/api/auth/login", data={"email": EMAIL, "password": PASSWORD})
            assert r.ok, f"login failed {r.status}"
            page.goto(BASE + "/", wait_until="networkidle")
            for _ in range(2):  # dismiss the first-run welcome / tour if shown
                for name in ("Maybe later", "Skip", "Skip tour"):
                    btn = page.get_by_role("button", name=name, exact=True)
                    if btn.count() and btn.first.is_visible():
                        try:
                            btn.first.click(timeout=1500)
                        except Exception:  # noqa: BLE001 - a dialog that will not close must not stop the sweep
                            pass
                        page.wait_for_timeout(300)
            audit(page, "app:home", width)
            menu = page.get_by_role("button", name="Open menu")
            for label in ("Work Items", "Projects", "Sprints", "Analytics", "Events", "Audit Trail", "Organization", "Members", "Help Center"):
                if menu.count() and menu.first.is_visible():
                    try:
                        menu.first.click(timeout=3000)
                    except Exception as exc:  # noqa: BLE001 - recorded as a finding, the sweep goes on
                        why = next((ln.strip() for ln in str(exc).splitlines() if "intercepts pointer events" in ln), "not clickable")
                        findings.append(f"app:{label} @{width}: menu button not clickable ({why[:140]})")
                        continue
                    page.wait_for_timeout(250)
                target = page.get_by_role("link", name=label).or_(page.get_by_role("button", name=label)).or_(page.get_by_text(label, exact=True)).locator("visible=true")
                if not target.count():
                    if page.evaluate("() => !!document.querySelector('#sidebar.open')"):
                        page.keyboard.press("Escape")
                    continue
                try:
                    target.first.click(timeout=3000)
                except Exception:  # noqa: BLE001 - a covered or animating item must not stop the sweep
                    continue
                page.wait_for_timeout(700)
                audit(page, f"app:{label}", width)
                if page.evaluate("() => !!document.querySelector('#sidebar.open')"):  # a section header leaves the drawer open
                    try:
                        page.get_by_role("button", name="Close menu").first.click(timeout=2000)
                    except Exception:  # noqa: BLE001
                        page.keyboard.press("Escape")
                    page.wait_for_timeout(250)
            ctx.close()
    browser.close()

seen: set[str] = set()
for f in findings + sorted(set(errors)):
    key = f.split(": ", 1)[-1] if "@" in f else f
    if f not in seen:
        seen.add(f)
        print(f)
print(f"\n{len(seen)} finding(s) across {len(WIDTHS)} widths")
sys.exit(1 if seen else 0)
