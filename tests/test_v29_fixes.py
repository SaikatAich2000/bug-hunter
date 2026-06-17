"""Regression tests for a set of frontend bug fixes.

The fixes covered here:

  1. Reports chip checkbox UI redesigned — native checkbox hidden, custom
     indicator span rendered.
  2. Comment-attachment Delete button no longer submits the enclosing
     bug form (must carry type="button").
  3. Comment composer files-only no longer leaks as a bug-level
     attachment — postComment() rejects with a clear toast and the user
     is directed to the dedicated bug-level uploader.
  4. Reports view theme uses the app's theme tokens instead of white
     panel fallbacks.
  5. Application version reported consistently everywhere.

The frontend invariants cannot run through a TestClient (no DOM), so they
are asserted by sniffing the source files — cheap, deterministic and
catches accidental regressions when someone refactors the SPA.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# The shipped frontend is the Vite-built bundle in app/static/assets/
# (content-hashed, not human-readable). These guard tests pin behavioural
# fix-markers in the React source: a refactor that drops a fix still breaks
# CI. The stylesheet lives at frontend/src/styles/styles.css.
FRONTEND = REPO_ROOT / "frontend" / "src"
RICH_EDITOR = FRONTEND / "components" / "RichEditor.tsx"
BUG_MODAL = FRONTEND / "modals" / "BugModal.tsx"
BUG_HELPERS = FRONTEND / "modals" / "bug" / "helpers.tsx"
REPORTS_VIEW = FRONTEND / "views" / "ReportsView.tsx"
SIDEBAR = FRONTEND / "shell" / "Sidebar.tsx"
# The view nav lives outside the Sidebar. NAV_ITEMS lives in a shared module
# (navItems.ts) consumed by both the desktop chrome bar (TopChrome) and the
# mobile drawer (Sidebar); the role gate (VIEW_MIN_ROLE) is applied at each
# consumer so the two nav surfaces cannot drift.
TOPCHROME = FRONTEND / "shell" / "TopChrome.tsx"
NAVITEMS = FRONTEND / "shell" / "navItems.ts"
TYPES = FRONTEND / "types.ts"
FORMAT_TS = FRONTEND / "lib" / "format.ts"
STYLES_CSS = FRONTEND / "styles" / "styles.css"


# ---------------------------------------------------------------------------
# Fix 5: version
# ---------------------------------------------------------------------------
def test_app_version_is_3_0():
    from app import __version__
    assert __version__ == "3.0"


def test_config_default_app_version_is_3_0():
    # The config default is what runs when no APP_VERSION env var is
    # set — production deployments override via env, but the baked-in
    # default must still be current so /api/health reports the right
    # version locally.
    from app.config import get_settings
    get_settings.cache_clear()
    assert get_settings().APP_VERSION == "3.0"


# ---------------------------------------------------------------------------
# Fix 2: delete-attachment button must NOT submit the bug form
# ---------------------------------------------------------------------------
def test_delete_attachment_button_has_type_button():
    """A button inside <form> with no `type` attribute defaults to
    `submit` and triggers a form submit on click. The fix added
    type="button" so deleting a comment's attachment no longer also
    saves the bug and closes the modal."""
    src = BUG_HELPERS.read_text(encoding="utf-8")
    # The AttachmentCard component emits the delete button; verify every
    # rendered occurrence carries type="button". (JSX, like HTML, defaults a
    # bare <button> inside a <form> to type=submit.)
    assert 'data-act="delete-attachment"' in src
    needle = 'data-act="delete-attachment"'
    pos = 0
    occurrences = 0
    while True:
        idx = src.find(needle, pos)
        if idx == -1:
            break
        # Walk back to the opening <button for this element and assert it
        # declares type="button" before reaching the data-act attribute.
        start = src.rfind("<button", 0, idx)
        assert start != -1, "delete-attachment is not inside a <button>"
        btn = src[start:idx]
        assert 'type="button"' in btn, (
            f"delete-attachment button at offset {idx} lacks "
            f'type="button"; context: {btn!r}'
        )
        occurrences += 1
        pos = idx + len(needle)
    assert occurrences >= 1, "no rendered delete-attachment button found"


# ---------------------------------------------------------------------------
# Fix 3: comment composer files-only must NOT silently become bug-level
# ---------------------------------------------------------------------------
def test_post_comment_rejects_files_without_body():
    """The postComment() function used to allow files-only by quietly
    routing those uploads to the bug-level endpoint. They are now kept
    strictly separate — files attached in the comment composer must
    accompany comment text, or the composer refuses with a clear
    toast pointing the user at the bug-level uploader."""
    src = BUG_MODAL.read_text(encoding="utf-8")
    # The new code path must reject files-only:
    assert "if (!body && files.length > 0)" in src, (
        "postComment is missing the files-only reject branch"
    )
    # The toast must mention the proper uploader so users know where to go.
    assert "Add attachment" in src
    # And the old "commentId = body ? ... : null" three-way that produced
    # bug-level attachments must be gone — the new code always creates
    # a comment first.
    assert "const commentId = body ? await _postCommentCreate" not in src


# ---------------------------------------------------------------------------
# Fix 1: chip checkbox UI — custom indicator span + hidden native checkbox
# ---------------------------------------------------------------------------
def test_reports_chip_indicator_span_is_rendered():
    """The chip indicator (the little checkmark dot) lives in its own
    span. CSS hides the native checkbox via absolute positioning +
    opacity:0 and shows the indicator span only when checked, via
    :has(input:checked). If either side breaks, the chip falls back to
    an ugly raw checkbox — what the user complained about."""
    js = REPORTS_VIEW.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert "reports-chip-indicator" in js, "ReportsView no longer emits the indicator span"
    assert ".reports-chip-indicator" in css, "indicator selector missing from CSS"
    # The native checkbox must be visually hidden so the indicator is
    # what the user sees.
    assert ".reports-chip input[type=\"checkbox\"]" in css
    assert "opacity: 0" in css
    # The :has() selector drives the "checked" visual state.
    assert ".reports-chip:has(input:checked)" in css


# ---------------------------------------------------------------------------
# Fix 4: Reports view uses theme tokens, not literal white
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("selector_block,must_contain", [
    (".reports-side {", "var(--bg-elev)"),
    (".reports-main {", "var(--bg-elev)"),
    (".reports-table thead th {", "var(--bg-elev-3)"),
    (".reports-summary-card {", "var(--bg-elev-2)"),
])
def test_reports_view_uses_theme_tokens(selector_block, must_contain):
    """Each Reports block must use the app's theme variables so the
    panel matches the dark/light theme. White fallbacks (`#fff`) were
    the visual bug the user reported."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    start = css.find(selector_block)
    assert start != -1, f"selector {selector_block!r} missing from CSS"
    block = css[start:start + 800]
    assert must_contain in block, (
        f"{selector_block} no longer uses {must_contain}; got block: {block[:200]!r}"
    )
    # And no hard-coded white panel background.
    assert "background: #fff" not in block
    assert "background: white" not in block


def test_no_literal_export_csv_button_in_index_html():
    """The sidebar Export CSV button was removed in favour of the Reports
    view. The React Sidebar must not resurrect it, and the Reports nav entry
    must stay role-gated to manager+.

    The view nav (NAV_ITEMS) lives in the TopChrome bar; role gating flows from
    a single source of truth — VIEW_MIN_ROLE in types.ts — which the nav
    (button visibility) and the Shell (view mount + fetch) both consult so they
    cannot drift. This test pins all the facts: the Export-CSV button stays
    gone from the sidebar, the gate map still gates Reports to manager+, and
    the nav source actually consumes that map to hide the button."""
    sidebar_src = SIDEBAR.read_text(encoding="utf-8")
    assert 'id="exportCsvBtn"' not in sidebar_src
    assert "Export CSV" not in sidebar_src
    # The Reports nav entry now lives in the shared NAV_ITEMS module.
    navitems_src = NAVITEMS.read_text(encoding="utf-8")
    assert 'view: "reports"' in navitems_src, "Reports nav entry must exist in NAV_ITEMS"
    # The gate map is the source of truth and must gate Reports to manager+.
    types_src = TYPES.read_text(encoding="utf-8")
    m = re.search(r"VIEW_MIN_ROLE[^{]*\{(.*?)\}", types_src, re.S)
    assert m, "VIEW_MIN_ROLE map must exist in types.ts"
    assert re.search(r'reports:\s*"manager"', m.group(1)), (
        "Reports must be gated to manager+ in VIEW_MIN_ROLE"
    )
    # And BOTH nav surfaces must consume that gate so the map and the rendered
    # button set can't drift — the desktop chrome bar and the mobile drawer.
    nav_src = TOPCHROME.read_text(encoding="utf-8")
    assert "VIEW_MIN_ROLE" in nav_src, "TopChrome must consult VIEW_MIN_ROLE to gate nav buttons"
    assert "allowed(item.view)" in nav_src, "TopChrome must filter NAV_ITEMS through the role gate"
    assert "NAV_ITEMS" in nav_src, "TopChrome must render the shared NAV_ITEMS list"
    assert "NAV_ITEMS" in sidebar_src, "Sidebar (mobile drawer) must render the shared NAV_ITEMS list"
    assert "VIEW_MIN_ROLE" in sidebar_src, "Sidebar must consult VIEW_MIN_ROLE to gate its drawer nav"


# ---------------------------------------------------------------------------
# Smoke: a manager-only API still works after the version bump
# ---------------------------------------------------------------------------
def test_health_endpoint_reports_version(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"] == "3.0"


def test_login_page_shows_version(client):
    """The login page renders "Version 3.0" below the sign-in card so a
    user looking at the landing page knows which release they are on
    without having to log in first."""
    r = client.get("/login.html")
    assert r.status_code == 200
    body = r.text
    # Server-side placeholder substitution must have replaced the token.
    assert "__APP_VERSION__" not in body
    assert "Version 3.0" in body
    # And the styling hook must be there.
    assert 'class="auth-version"' in body


# ---------------------------------------------------------------------------
# Rich-text editor undo/redo: snapshot-based history so Ctrl+Z works
# across both typed characters AND toolbar formatting commands. The old
# editor relied on the browser's native contenteditable undo, which
# silently skipped every toolbar mutation because those went through
# direct DOM manipulation (not document.execCommand). Users reported
# undo/redo "not working properly at all".
# ---------------------------------------------------------------------------
def test_rich_editor_has_snapshot_history():
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # The history structure must exist on every editor instance (stack +
    # index, typed as HistoryState).
    assert "HistoryState" in src and "stack:" in src and "idx:" in src, (
        "snapshot history state missing from RichEditor"
    )
    # Ctrl+Z / Ctrl+Y handlers must explicitly call our undo/redo,
    # not fall back to the browser's broken native undo.
    assert "undoEdit()" in src
    assert "redoEdit()" in src
    # The toolbar mousedown path must run through the snapshot wrapper.
    assert "runToolbarCmd(btn)" in src
    # setHtml should reset the history so undo can't jump back to a
    # previous (unrelated) bug's content.
    assert "resetHistory()" in src


def test_rich_editor_seeds_initial_snapshot():
    """The first Ctrl+Z must be able to return to the editor's initial
    state. That requires a seed snapshot taken at mount time —
    resetHistory() (called from the mount layout effect) pushes it."""
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # resetHistory seeds an initial snapshot().
    start = src.find("const resetHistory")
    assert start != -1, "resetHistory helper missing"
    body = src[start:start + 400]
    assert "snapshot()" in body, "resetHistory must seed an initial snapshot()"
    # And it runs at mount, before any user input can arrive.
    assert "resetHistory();" in src
    assert "Seed the surface once on mount" in src


# ---------------------------------------------------------------------------
# Timestamp formatter: must always render BOTH date and time everywhere
# (comments, audit log, session list, bug modal metadata). The old
# "today shortcut" hid one half of the timestamp; users wanted both.
# ---------------------------------------------------------------------------
def test_format_date_always_includes_date_and_time():
    """The formatDate() helper must combine the locale date and the
    locale time. Static-source check: verify both toLocale*() calls are
    in the same expression and the today-only shortcut is gone."""
    src = FORMAT_TS.read_text(encoding="utf-8")
    # Extract the formatDate body (until the next top-level export) so the
    # slice covers the whole function regardless of formatting.
    start = src.find("export function formatDate")
    assert start != -1, "formatDate helper missing"
    end = src.find("\nexport function", start + 1)
    assert end != -1
    body = src[start:end]
    assert "toLocaleDateString" in body
    assert "toLocaleTimeString" in body
    # The old "sameDay ? time : date" branch must be gone.
    assert "sameDay" not in body, (
        "formatDate still uses the today/not-today shortcut; users wanted "
        "both pieces of the timestamp visible everywhere"
    )
    # The rendered string must concatenate date + time.
    assert "${date}, ${time}" in body
