"""Source-level guards for frontend behavior that cannot be exercised via TestClient.

Covers:
  1. Reports chip checkbox: native input hidden, custom indicator span rendered.
  2. Delete-attachment button carries type="button" so it cannot submit the bug form.
  3. Comment composer rejects files-only submissions with a toast.
  4. Reports panels use theme tokens instead of hard-coded white.
  5. App version is reported consistently.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

# These tests pin fix-markers in the React source so a refactor that silently
# drops a fix still breaks CI. We read the human-readable source rather than
# the built bundle, which is content-hashed and minified.
FRONTEND = REPO_ROOT / "frontend" / "src"
RICH_EDITOR = FRONTEND / "components" / "RichEditor.tsx"
BUG_MODAL = FRONTEND / "modals" / "BugModal.tsx"
BUG_HELPERS = FRONTEND / "modals" / "bug" / "helpers.tsx"
REPORTS_VIEW = FRONTEND / "views" / "ReportsView.tsx"
SIDEBAR = FRONTEND / "shell" / "Sidebar.tsx"
# NAV_ITEMS is shared between TopChrome (desktop) and Sidebar (mobile drawer)
# so the two nav surfaces can't drift.
TOPCHROME = FRONTEND / "shell" / "TopChrome.tsx"
NAVITEMS = FRONTEND / "shell" / "navItems.ts"
TYPES = FRONTEND / "types.ts"
FORMAT_TS = FRONTEND / "lib" / "format.ts"
STYLES_CSS = FRONTEND / "styles" / "styles.css"


# ---------------------------------------------------------------------------
# Application version
# ---------------------------------------------------------------------------
def test_app_version_is_3_1():
    from app import __version__
    assert __version__ == "3.1"


def test_config_default_app_version_is_3_1(monkeypatch):
    """The baked-in APP_VERSION default must match the current release so a
    clean deploy without a .env override still reports the right version.
    Reloads config with the dotenv loader stubbed and APP_VERSION unset to read
    the literal default rather than any local override."""
    import importlib
    import dotenv
    import app.config as config

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    try:
        reloaded = importlib.reload(config)
        assert reloaded.get_settings().APP_VERSION == "3.1"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ---------------------------------------------------------------------------
# Delete-attachment button must not submit the bug form
# ---------------------------------------------------------------------------
def test_delete_attachment_button_has_type_button():
    """A bare <button> inside a <form> defaults to type=submit in both HTML and JSX,
    so clicking delete would also save the bug. type="button" prevents that."""
    src = BUG_HELPERS.read_text(encoding="utf-8")
    assert 'data-act="delete-attachment"' in src
    needle = 'data-act="delete-attachment"'
    pos = 0
    occurrences = 0
    while True:
        idx = src.find(needle, pos)
        if idx == -1:
            break
        # Walk back to the opening <button and confirm type="button" appears before data-act.
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
# Comment composer files-only must not silently become bug-level
# ---------------------------------------------------------------------------
def test_post_comment_rejects_files_without_body():
    """Files in the comment composer must accompany text; attaching files alone
    would silently create a bug-level attachment instead. The reject branch shows
    a toast directing the user to the correct uploader."""
    src = BUG_MODAL.read_text(encoding="utf-8")
    assert "if (!body && files.length > 0)" in src, (
        "postComment is missing the files-only reject branch"
    )
    assert "Add attachment" in src
    # The old "commentId = body ? ... : null" three-way split produced bug-level
    # attachments; the new path always creates a comment first.
    assert "const commentId = body ? await _postCommentCreate" not in src


# ---------------------------------------------------------------------------
# Chip checkbox UI — custom indicator span + hidden native checkbox
# ---------------------------------------------------------------------------
def test_reports_chip_indicator_span_is_rendered():
    """The indicator span replaces the native checkbox visually. CSS hides the
    native input (opacity:0 + absolute positioning) and reveals the span when
    :has(input:checked) matches. If either side breaks, the chip degrades to a
    plain checkbox."""
    js = REPORTS_VIEW.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert "reports-chip-indicator" in js, "ReportsView no longer emits the indicator span"
    assert ".reports-chip-indicator" in css, "indicator selector missing from CSS"
    assert ".reports-chip input[type=\"checkbox\"]" in css
    assert "opacity: 0" in css
    assert ".reports-chip:has(input:checked)" in css


# ---------------------------------------------------------------------------
# Reports view uses theme tokens, not literal white
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("selector_block,must_contain", [
    (".reports-side {", "var(--bg-elev)"),
    (".reports-main {", "var(--bg-elev)"),
    (".reports-table thead th {", "var(--bg-elev-3)"),
    (".reports-summary-card {", "var(--bg-elev-2)"),
])
def test_reports_view_uses_theme_tokens(selector_block, must_contain):
    """Reports panels must use CSS theme variables so they work in both dark and
    light themes, not hard-coded white."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    start = css.find(selector_block)
    assert start != -1, f"selector {selector_block!r} missing from CSS"
    block = css[start:start + 800]
    assert must_contain in block, (
        f"{selector_block} no longer uses {must_contain}; got block: {block[:200]!r}"
    )
    assert "background: #fff" not in block
    assert "background: white" not in block


def test_no_literal_export_csv_button_in_index_html():
    """The sidebar Export CSV button was replaced by the Reports view. Pins that
    the old button is gone, VIEW_MIN_ROLE in types.ts still gates Reports to
    manager+, and both nav surfaces (TopChrome + Sidebar) consume that map so
    visibility can't drift."""
    sidebar_src = SIDEBAR.read_text(encoding="utf-8")
    assert 'id="exportCsvBtn"' not in sidebar_src
    assert "Export CSV" not in sidebar_src
    navitems_src = NAVITEMS.read_text(encoding="utf-8")
    assert 'view: "reports"' in navitems_src, "Reports nav entry must exist in NAV_ITEMS"
    # VIEW_MIN_ROLE is the source of truth; Reports must be gated to manager+.
    types_src = TYPES.read_text(encoding="utf-8")
    m = re.search(r"VIEW_MIN_ROLE[^{]*\{(.*?)\}", types_src, re.S)
    assert m, "VIEW_MIN_ROLE map must exist in types.ts"
    assert re.search(r'reports:\s*"manager"', m.group(1)), (
        "Reports must be gated to manager+ in VIEW_MIN_ROLE"
    )
    # Both nav surfaces (desktop chrome bar and mobile drawer) must consume the
    # gate so the map and the rendered button set can't drift.
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
    # /api/health exposes whatever version the app is configured with (3.1
    # baked in, or an APP_VERSION env override). Compare against the configured
    # value rather than a hard-coded literal so the check holds under either.
    from app.main import settings
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["version"] == settings.APP_VERSION


def test_login_page_shows_version(client):
    """The login page renders "Version <X>" below the sign-in card so users can
    identify the release without logging in. <X> is the app's configured version
    (3.1 default, or an APP_VERSION override), substituted server-side."""
    from app.main import settings
    r = client.get("/login.html")
    assert r.status_code == 200
    body = r.text
    # Placeholder substitution must have happened server-side.
    assert "__APP_VERSION__" not in body
    assert f"Version {settings.APP_VERSION}" in body
    # Styling hook must be present.
    assert 'class="auth-version"' in body


# ---------------------------------------------------------------------------
# Rich-text editor undo/redo: snapshot-based history so Ctrl+Z works across
# typed characters and toolbar formatting commands. The browser's native
# contenteditable undo skips toolbar mutations because those go through direct
# DOM manipulation rather than document.execCommand.
# ---------------------------------------------------------------------------
def test_rich_editor_has_snapshot_history():
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # History structure must exist on every editor instance (stack + index,
    # typed as HistoryState).
    assert "HistoryState" in src and "stack:" in src and "idx:" in src, (
        "snapshot history state missing from RichEditor"
    )
    # Ctrl+Z / Ctrl+Y must call our undo/redo, not the browser's native undo.
    assert "undoEdit()" in src
    assert "redoEdit()" in src
    # Toolbar mousedown must go through the snapshot wrapper.
    assert "runToolbarCmd(btn)" in src
    # setHtml must reset history so undo can't jump back to a previous bug's content.
    assert "resetHistory()" in src


def test_rich_editor_seeds_initial_snapshot():
    """The first Ctrl+Z must be able to return to the editor's initial state.
    That requires a seed snapshot at mount time — resetHistory() (called from
    the mount layout effect) pushes it."""
    src = RICH_EDITOR.read_text(encoding="utf-8")
    # resetHistory must seed an initial snapshot().
    start = src.find("const resetHistory")
    assert start != -1, "resetHistory helper missing"
    body = src[start:start + 400]
    assert "snapshot()" in body, "resetHistory must seed an initial snapshot()"
    # Must run at mount, before any user input arrives: the call lives inside
    # the mount layout effect.
    assert "resetHistory();" in src
    le = src.find("useLayoutEffect(() => {")
    assert le != -1, "mount layout effect missing"
    assert "resetHistory();" in src[le:le + 700], (
        "resetHistory() must be seeded from the mount layout effect"
    )


# ---------------------------------------------------------------------------
# Timestamp formatter: must always render both date and time (comments, audit
# log, session list, bug modal metadata) with no today-only shortcut that hides
# one half.
# ---------------------------------------------------------------------------
def test_format_date_always_includes_date_and_time():
    """The formatDate() helper must combine the locale date and locale time.
    Verifies both toLocale*() calls are present and the today-only shortcut
    is gone."""
    src = FORMAT_TS.read_text(encoding="utf-8")
    # Slice until the next top-level export so the check spans the whole
    # function body regardless of formatting.
    start = src.find("export function formatDate")
    assert start != -1, "formatDate helper missing"
    end = src.find("\nexport function", start + 1)
    assert end != -1
    body = src[start:end]
    assert "toLocaleDateString" in body
    assert "toLocaleTimeString" in body
    # The old "sameDay ? time : date" branch must be gone.
    assert "sameDay" not in body, (
        "formatDate still uses the today/not-today shortcut; both pieces of "
        "the timestamp must be visible everywhere"
    )
    # Rendered string must concatenate date + time.
    assert "${date}, ${time}" in body
