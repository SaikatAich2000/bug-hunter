"""Source guards for the frontend theme and layout.

These tests can't run a DOM through the TestClient, so they pin the theme and
layout invariants by sniffing the React/CSS/HTML sources.

The invariants pinned here:

  1. Unified Steam page header — every view's title/subtitle lives once in
     PageHead. The old per-view `.page-intro` banners (which duplicated the
     title once PageHead rendered it for all views) are gone.
  2. Mobile drawer view-nav — the desktop chrome bar hides its horizontal nav
     on phones, so the view nav is surfaced inside the drawer (Sidebar) from a
     shared NAV_ITEMS module both nav surfaces consume, role-gated identically.
  3. Steam field-gradient modal headers (same surface family as the table
     panel header) and the title/subtitle stack styling.
  4. The Sessions help text points at the profile menu (where Log out lives),
     not the old sidebar.
  5. The reduced-radius token scale and dead-CSS cleanup.
  6. `meta theme-color` (Steam chrome) on all three HTML entry points.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
SRC = FRONTEND / "src"

STYLES = SRC / "styles" / "styles.css"
CHATBOT = SRC / "styles" / "chatbot.css"
NAVITEMS = SRC / "shell" / "navItems.ts"
TOPCHROME = SRC / "shell" / "TopChrome.tsx"
SIDEBAR = SRC / "shell" / "Sidebar.tsx"
PAGEHEAD = SRC / "shell" / "PageHead.tsx"
AUDIT = SRC / "views" / "AuditView.tsx"
SESSIONS = SRC / "views" / "SessionsView.tsx"
REPORTS = SRC / "views" / "ReportsView.tsx"
EVENTS = SRC / "views" / "EventsView.tsx"
APPCONTEXT = SRC / "state" / "AppContext.tsx"
MAIN = SRC / "main.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Unified page header — the duplicate per-view intro banners are gone
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("view_file", [AUDIT, SESSIONS, REPORTS, EVENTS])
def test_views_no_longer_render_duplicate_page_intro(view_file):
    """PageHead renders the title for every view, so a per-view `.page-intro`
    banner would print the same title twice. All four were removed."""
    src = _read(view_file)
    assert "page-intro" not in src, (
        f"{view_file.name} still renders a .page-intro banner — that duplicates "
        "the title PageHead already shows"
    )


def test_pagehead_supplies_per_view_subtitles():
    """Every view gets the same Steam header (title + subtitle). The static
    one-liners moved from the old intros into PageHead's VIEW_SUBTITLES."""
    src = _read(PAGEHEAD)
    assert "VIEW_SUBTITLES" in src, "PageHead must define the per-view subtitle map"
    for key in ("events", "analytics", "audit", "sessions", "reports"):
        assert f"{key}:" in src, f"VIEW_SUBTITLES missing a subtitle for {key!r}"


def test_pagehead_dead_intro_css_removed():
    """With every intro gone, the `.page-intro*` rules are dead and removed."""
    css = _read(STYLES)
    assert ".page-intro" not in css, ".page-intro CSS should be removed (no intros left)"


# ---------------------------------------------------------------------------
# 2. Mobile drawer view-nav (the fix for the nav vanishing on phones)
# ---------------------------------------------------------------------------
def test_nav_items_is_a_shared_module():
    """NAV_ITEMS lives in one module so the desktop chrome bar and the mobile
    drawer can't drift. All six top-level views are present."""
    src = _read(NAVITEMS)
    assert "export const NAV_ITEMS" in src
    for view in ("list", "events", "analytics", "reports", "audit", "sessions"):
        assert f'view: "{view}"' in src, f"NAV_ITEMS missing {view!r}"


def test_topchrome_and_sidebar_share_nav_items():
    top = _read(TOPCHROME)
    side = _read(SIDEBAR)
    assert 'from "./navItems"' in top, "TopChrome must import the shared NAV_ITEMS"
    assert 'from "./navItems"' in side, "Sidebar must import the shared NAV_ITEMS"


def test_sidebar_renders_role_gated_drawer_nav():
    """The Sidebar surfaces the view nav (mobile drawer) behind the same
    VIEW_MIN_ROLE gate the chrome uses, and closes the drawer on navigate."""
    src = _read(SIDEBAR)
    assert 'className="sidebar-nav"' in src, "Sidebar must render the drawer nav"
    assert "NAV_ITEMS" in src
    assert "VIEW_MIN_ROLE" in src, "drawer nav must be role-gated"
    assert "onNavigate" in src, "selecting a view must be able to close the drawer"


def test_drawer_nav_css_is_desktop_hidden_mobile_shown():
    """`.sidebar-nav` is hidden on desktop (chrome owns the nav) and unfolds
    inside the drawer at the phone breakpoint."""
    css = _read(STYLES)
    assert ".sidebar-nav { display: none; }" in css, "drawer nav must be hidden on desktop"
    # In the phone breakpoint it becomes a flex column.
    assert "max-width: 900px" in css
    nav_idx = css.find(".sidebar-nav {", css.find("max-width: 900px"))
    assert nav_idx != -1, ".sidebar-nav must be re-shown inside the 900px breakpoint"
    block = css[nav_idx:nav_idx + 200]
    assert "display: flex" in block


def test_frame_layout_and_drawer_outranks_chrome():
    """The shipped layout is the two-tier mockup: a full-width chrome (brand +
    nav) and supernav above a fixed 236px library-rail + scrolling main (the
    `.frame` grid). The rail can collapse to a narrow strip via the
    `body.sidebar-collapsed` class (see the collapse-feature tests), but the
    earlier `--rail-w` / `.app-shell` collapsible-grid experiment was reverted.
    On phones the off-canvas drawer must outrank the chrome (z 20) + backdrop
    (z 44)."""
    css = _read(STYLES)
    assert ".frame {" in css, "the two-tier frame grid must exist"
    assert "grid-template-columns: 236px 1fr" in css, "frame = fixed rail + main"
    assert ".brandmark" in css, "the brand mark lives in the chrome"
    # The earlier custom-property-driven collapsible-rail layout was reverted in
    # favour of the body.sidebar-collapsed approach.
    assert "--rail-w" not in css, "collapsible-rail custom property must be gone"
    assert ".app-shell" not in css, "the app-shell grid wrapper must be gone"
    assert "z-index: 45" in css, "mobile drawer must sit above chrome + backdrop"
    assert "z-index: 44" in css, "mobile backdrop must sit above the chrome"


# ---------------------------------------------------------------------------
# 3. Steam field-gradient modal header
# ---------------------------------------------------------------------------
def test_modal_head_uses_steam_field_gradient():
    css = _read(STYLES)
    start = css.find(".modal-head {")
    assert start != -1
    block = css[start:start + 500]
    assert "var(--panel-field)" in block, (
        "modal header must use the Steam field-gradient (same surface family as "
        "the table panel header)"
    )
    # The title + subtitle stack styling the Modal wrapper relies on.
    assert ".modal-head-text" in css
    assert ".modal-subtitle" in css


# ---------------------------------------------------------------------------
# 4. Stale Sessions copy fixed (Log out moved to the profile menu)
# ---------------------------------------------------------------------------
def test_sessions_copy_points_at_profile_menu_not_sidebar():
    src = _read(SESSIONS)
    assert "profile menu" in src, "Sessions help must direct users to the profile menu"
    assert "the sidebar" not in src, (
        "Sessions copy still references the old sidebar Log out (it moved to the "
        "profile menu in the v3.0 shell)"
    )


# ---------------------------------------------------------------------------
# 5. Reduced-radius token scale + dead-CSS cleanup
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("css_file", [STYLES, CHATBOT])
def test_oversized_radii_were_reduced(css_file):
    css = _read(css_file)
    assert "border-radius: 18px" not in css, "18px radii should have been reduced"
    assert "border-radius: 16px" not in css, "16px radii should have been reduced"


def test_reduced_radius_token_scale_present():
    css = _read(STYLES)
    for token in ("--r-card:", "--r-btn:", "--r-field:", "--r-chip:"):
        assert token in css, f"reduced-radius token {token} missing"


@pytest.mark.parametrize("dead", [".topbar", ".page-intro",
                                  ".side-footer", ".actor-select"])
def test_dead_selectors_removed(dead):
    css = _read(STYLES)
    assert dead not in css, f"dead selector {dead!r} should have been removed"


# ---------------------------------------------------------------------------
# 5b. Collapse Sidebar (GitLab-style rail collapse) — a real, persisted feature
#     keyed off `body.sidebar-collapsed`. These pin the wiring end to end so a
#     refactor can't silently break the collapse without tripping CI.
# ---------------------------------------------------------------------------
def test_collapse_sidebar_button_wired_in_sidebar():
    """The footer collapse control toggles the shared state and exposes its
    open/closed status to assistive tech."""
    src = _read(SIDEBAR)
    assert 'className="sidebar-collapse-btn"' in src, "collapse control must render"
    assert "toggleSidebarCollapsed" in src, "the button must call the toggle"
    assert "sidebarCollapsed" in src, "the button must read the collapsed state"
    assert "aria-expanded" in src, "collapse control must report expanded state"


def test_collapse_state_is_persisted_in_appcontext():
    """The collapsed flag survives reloads via localStorage and drives the
    `body.sidebar-collapsed` class the CSS keys off."""
    src = _read(APPCONTEXT)
    assert "sidebarCollapsed" in src and "toggleSidebarCollapsed" in src
    assert 'readLs("sidebarCollapsed"' in src, "initial state must hydrate from storage"
    assert 'localStorage.setItem("sidebarCollapsed"' in src, "toggle must persist"
    assert '"sidebar-collapsed"' in src, "the flag must toggle the body class"


def test_collapse_class_applied_before_first_paint():
    """The persisted state is applied in main.tsx before React mounts so a
    reload doesn't flash the expanded rail."""
    src = _read(MAIN)
    assert 'localStorage.getItem("sidebarCollapsed")' in src
    assert '"sidebar-collapsed"' in src, "main must add the body class pre-paint"


def test_collapse_css_is_live_and_animated():
    """`body.sidebar-collapsed` narrows the rail at the desktop breakpoint and
    the chevron rotates; the grid width change is transitioned, not abrupt."""
    css = _read(STYLES)
    assert "body.sidebar-collapsed" in css, "the collapse selector must be live"
    assert ".sidebar-collapse-btn" in css, "the collapse control must be styled"
    assert "body.sidebar-collapsed .frame" in css, "collapsing must narrow the frame grid"
    assert "transition: grid-template-columns" in css, "the collapse must animate"
    assert "rotate(180deg)" in css, "the collapse chevron must flip on toggle"


# ---------------------------------------------------------------------------
# 6. meta theme-color (Steam chrome) on every HTML entry point
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("html", ["index.html", "login.html", "reset.html"])
def test_html_has_steam_theme_color(html):
    src = (FRONTEND / html).read_text(encoding="utf-8")
    assert 'name="theme-color"' in src, f"{html} missing meta theme-color"
    assert "#171a21" in src, f"{html} theme-color should be the Steam chrome colour"


# ---------------------------------------------------------------------------
# Steam identity tokens (pin the palette so a token reskin can't silently
# revert the theme)
# ---------------------------------------------------------------------------
def test_steam_identity_tokens_present():
    css = _read(STYLES)
    assert "--chrome: #171a21" in css, "Steam chrome token"
    assert "--panel-field:" in css, "Steam steel field-gradient token"
    assert "--green-face:" in css, "Steam green install-button token"
    # The Steam font stack + self-hosted Asap stand-in.
    assert '"Motiva Sans", "Asap"' in css
