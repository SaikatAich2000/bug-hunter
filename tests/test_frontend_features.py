"""Source guards for item links, bulk actions, Sleuth document-ingest, the
two-tier layout, and the removal of the watchers and labels features.

Behavior is covered end-to-end by test_links and test_bulk; these sniff the
source wiring and the removals so a refactor can't silently re-introduce a
dropped feature or break the layout.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"
APP = REPO / "app"

STYLES = SRC / "styles" / "styles.css"
CHATBOT_CSS = SRC / "styles" / "chatbot.css"
TYPES = SRC / "types.ts"
APPCTX = SRC / "state" / "AppContext.tsx"
BUGMODAL = SRC / "modals" / "BugModal.tsx"
LISTVIEW = SRC / "views" / "ListView.tsx"
SIDEBAR = SRC / "shell" / "Sidebar.tsx"
TOPCHROME = SRC / "shell" / "TopChrome.tsx"
SHELL = SRC / "shell" / "Shell.tsx"
SLEUTH = SRC / "sleuth" / "SleuthPanel.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Kept features — item links + bulk + ingest
# ---------------------------------------------------------------------------
def test_models_define_buglink_only():
    models = _read(APP / "models.py")
    assert "class BugLink(" in models, "item-linking model must stay"


def test_bulk_and_link_routes_present():
    bugs = _read(APP / "routes" / "bugs.py")
    assert '"/bulk"' in bugs, "bulk endpoint must stay"
    assert '"/{bug_id}/links"' in bugs, "links endpoint must stay"


def test_sleuth_ingest_is_admin_only_and_conversational():
    router = _read(APP / "chatbot" / "router.py")
    assert '"/ingest"' in router and "AdminUser" in router
    # The endpoint stages a preview rather than auto-creating.
    assert "ingest_preview" in router, "upload must preview, not auto-create"
    assert "stage_ingest" in router
    ex = _read(APP / "chatbot" / "executor.py")
    assert "_maybe_handle_ingest_create" in ex, "create-on-go-ahead handler must exist"


def test_bug_modal_keeps_links_section():
    src = _read(BUGMODAL)
    assert "bugLinksSection" in src and "/links" in src


def test_list_view_keeps_bulk_bar():
    src = _read(LISTVIEW)
    assert "bulk-bar" in src and "/bugs/bulk" in src and "col-select" in src


def test_sleuth_panel_admin_upload_and_text():
    src = _read(SLEUTH)
    assert "sleuthUploadBtn" in src and "/chat/ingest" in src and "isAdmin" in src
    assert "Ask Me Anything" in src, "placeholder must be 'Ask Me Anything'"
    assert ">Online<" in src, "status must read just 'Online'"


# ---------------------------------------------------------------------------
# Removed features — watchers and labels must be gone everywhere
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("needle", [
    "bug_watchers", "bug_labels", "class Label(", "watchers:", "labels:",
])
def test_models_have_no_watchers_or_labels(needle):
    assert needle not in _read(APP / "models.py"), f"models.py still references {needle!r}"


def test_no_labels_router_file():
    assert not (APP / "routes" / "labels.py").exists(), "labels router must be deleted"


def test_no_label_modal_component():
    assert not (SRC / "modals" / "LabelModal.tsx").exists(), "LabelModal must be deleted"


def test_schemas_drop_label_and_watch():
    schemas = _read(APP / "schemas.py")
    for gone in ("class LabelOut", "class LabelIn", "watcher_count", "is_watching", "label_ids"):
        assert gone not in schemas, f"schemas.py still has {gone!r}"


def test_bugout_type_has_no_label_or_watch_fields():
    types = _read(TYPES)
    for gone in ("LabelOut", "watcher_count", "is_watching", "label_id"):
        assert gone not in types, f"types.ts still has {gone!r}"


@pytest.mark.parametrize("src_file,gone", [
    (BUGMODAL, "bugWatchBtn"), (BUGMODAL, "labelPicker"),
    (LISTVIEW, "renderRowLabels"), (LISTVIEW, "add_label"),
    (SIDEBAR, "labelList"), (APPCTX, "loadLabels"), (APPCTX, "labelModal"),
])
def test_frontend_has_no_label_or_watch_wiring(src_file, gone):
    assert gone not in _read(src_file), f"{src_file.name} still wires {gone!r}"


# ---------------------------------------------------------------------------
# Two-tier (no-collapse) layout
# ---------------------------------------------------------------------------
def test_shell_uses_frame_not_collapsible_rail():
    shell = _read(SHELL)
    assert '"frame"' in shell, "Shell must render the frame layout"
    assert "app-shell" not in shell and "collapsed" not in shell, "collapse must be gone"


def test_topchrome_carries_the_brandmark():
    chrome = _read(TOPCHROME)
    assert "brandmark" in chrome, "brand mark must live in the chrome (not the sidebar)"
    assert "HUNTER</span>" in chrome


def test_layout_css_is_frame_grid():
    css = _read(STYLES)
    assert ".frame {" in css and "grid-template-columns: 236px 1fr" in css
    assert ".brandmark" in css
    # The collapsible-rail CSS is gone.
    assert "--rail-w" not in css and ".app-shell" not in css


@pytest.mark.parametrize("cls", [
    ".bug-links-list", ".bug-link-row", ".bulk-bar", ".bulk-select", ".col-select",
])
def test_kept_css_classes_present(cls):
    assert cls in _read(STYLES), f"styles.css missing {cls!r}"


def test_dead_label_watch_css_removed():
    css = _read(STYLES)
    for gone in (".label-picker", ".bug-watch-btn", ".title-labels", ".swatch-dot"):
        assert gone not in css, f"styles.css still defines {gone!r}"


def test_chatbot_css_has_upload_button():
    assert ".sleuth-upload-btn" in _read(CHATBOT_CSS)
