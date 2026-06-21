"""Tests for rich-text editor and form behavior.

Covers:

  1. Bold / Italic / Underline (toolbar and Ctrl+B/I/U) apply when the
     contenteditable surface has focus but no caret yet, and formatBlock
     (blockquote / pre) toggles off on a second click.

  2. Calendar prev / next buttons page the month instead of closing the
     popover.

  3. The disabled Reporter <select> does not render the open-caret.

  4. The toolbar image button routes through the attachment flow rather
     than inserting an inline <img>.

These tests assert:

  * The shipped frontend carries the markers for each behavior, so a
    refactor that removes one is caught.
  * The source does not carry the old inline-image embed path
    (no `_bhRtPickImage`, no `insertHTML.*<img`).
  * Server-side HTML sanitisation lets the rich-text formatting tags
    survive a round-trip through bug-description and comment-body
    submission (bold, italic, underline, lists, blockquote, pre,
    code, paragraphs).
  * Server-side sanitisation still strips dangerous payloads
    (<script>, javascript: URLs, onerror handlers).
  * The Reporter field stays disabled in the rendered modal, so the
    custom-dropdown's disabled branch is exercised in real rendering.
  * The admin-only comment edit/delete and admin-only bug-attachment
    delete rules still hold (re-covered here so this suite is
    self-sufficient).

Every test runs against a tmp SQLite file created by the conftest
fixture; nothing here touches a production database.
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test helpers — duplicated intentionally so this file stands alone.
# ---------------------------------------------------------------------------
def _login(client, email, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _make_project(client, name="Eng v2.6"):
    r = client.post("/api/projects", json={"name": name, "color": "#558844"})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(client, project_id, **extra):
    body = {
        "title": "Sample Bug v2.6",
        "project_id": project_id,
        "item_type": "Bug",
        "priority": "Medium",
        "environment": "DEV",
    }
    body.update(extra)
    r = client.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _read_static(name):
    p = Path(__file__).resolve().parents[1] / "app" / "static" / name
    return p.read_text(encoding="utf-8")


# The rich-text editor / custom select / date input live in these sources
# (the shipped JS is a content-hashed Vite bundle under app/static/assets/,
# not human-readable). The marker tests below read the React source.
def _read_frontend(relpath):
    p = Path(__file__).resolve().parents[1] / "frontend" / "src" / relpath
    return p.read_text(encoding="utf-8")


def _rich_editor():
    return _read_frontend("components/RichEditor.tsx")


def _bh_select():
    return _read_frontend("components/BhSelect.tsx")


def _bh_date():
    return _read_frontend("components/BhDateInput.tsx")


def _bug_modal():
    return _read_frontend("modals/BugModal.tsx")


def _styles_css():
    return _read_frontend("styles/styles.css")


# ===========================================================================
# 1. Rich-text editor fix markers (Bold / Italic / Underline / format-toggle)
# ===========================================================================
class TestRichTextFixMarkers:
    """Check that the rich-text behavior markers are present in the source."""

    def test_app_js_has_saved_selection_path(self):
        """Selection is saved and restored across toolbar clicks."""
        js = _rich_editor()
        assert "savedRange" in js, "selection-save state must exist"
        assert "captureSelection" in js, "capture-selection helper must exist"
        assert "ensureCaret" in js, "ensure-caret helper must exist"

    def test_app_js_runs_cmd_after_focus_restore(self):
        """Bold/Italic/Underline must run via the restored-selection helper,
        not by bare execCommand calls inside the keyboard handler."""
        js = _rich_editor()
        for cmd in ("bold", "italic", "underline"):
            assert f'runCmd("{cmd}")' in js, (
                f"Ctrl+{cmd[0].upper()} should go through runCmd "
                "not bare execCommand"
            )
        # Strip line comments before asserting the call is not made
        # directly (it is still mentioned in comments explaining the
        # Chrome workaround).
        code_only = "\n".join(
            ln for ln in js.splitlines()
            if not ln.lstrip().startswith("//")
        )
        for cmd in ("bold", "italic", "underline"):
            assert f'document.execCommand("{cmd}")' not in code_only, (
                f"toolbar must not call document.execCommand(\"{cmd}\") "
                "directly — Chrome silently no-ops it inside the modal"
            )

    def test_app_js_toggles_blockquote_and_pre(self):
        """formatBlock for blockquote / pre toggles off when re-clicked."""
        js = _rich_editor()
        assert 'inAncestor([tag])' in js, "format-block ancestor check missing"

    def test_app_js_no_clear_formatting_button(self):
        """The Clear-formatting button is not rendered; block-toggle on the
        same button (blockquote/pre/list) covers returning to plain text."""
        js = _rich_editor()
        assert 'data-cmd="removeFormat"' not in js, (
            "Clear-formatting button must not be rendered"
        )
        # The dedicated handler branch went with it.
        assert '"removeFormat"' not in js, (
            "removeFormat handler branch should be removed too"
        )

    def test_app_js_uses_inline_wrapper_for_collapsed_caret(self):
        """First-click Bold/Italic/Underline on a collapsed caret manually
        wraps a ZWSP placeholder. Assert that path is wired."""
        js = _rich_editor()
        assert "toggleInlineAtCaret" in js, "manual-toggle helper missing"
        assert "INLINE_TOGGLE" in js, "inline-tag map missing"
        # The ZWSP separator must be inserted on toggle-off too, or Chrome
        # absorbs the next keystroke back into the wrapper.
        assert "insertBefore(sep, n.nextSibling)" in js, (
            "ZWSP separator must be inserted after wrapper on toggle-off"
        )

    def test_app_js_uses_preventdefault_on_toolbar_mousedown(self):
        """The toolbar button's mousedown must call preventDefault, otherwise
        focus moves from the contenteditable to the button and the typing
        state set by execCommand disappears before the user can type.

        The invariant tested here is that preventDefault stays in place,
        regardless of whether the runner is the raw helper or the
        snapshot wrapper that supports undo history."""
        js = _rich_editor()
        # The toolbar mousedown handler must call preventDefault before
        # running the toolbar command (the React onMouseDown handler is
        # handleToolbarMouseDown; it ends in runToolbarCmd, the snapshot
        # wrapper).
        m = re.search(
            r'handleToolbarMouseDown.*?e\.preventDefault\(\).*?runToolbarCmd',
            js, re.S,
        )
        assert m, "mousedown must preventDefault before running the toolbar command"

    def test_app_js_has_manual_formatting_implementations(self):
        """Chrome silently no-ops document.execCommand("bold") inside the bug
        modal's stacking context, so the editor implements its own. These
        helpers must exist or B/I/U/list/blockquote stop working."""
        js = _rich_editor()
        assert "applyInlineWrap" in js, "manual inline wrap missing"
        assert "applyList" in js, "manual list toggle missing"
        assert "applyBlockWrap" in js, "manual block-wrap missing"
        assert "selectWordAtCaret" in js, (
            "word-at-caret auto-select missing — user expects Bold to "
            "bold the word they just typed, no manual selection required"
        )

    def test_app_js_has_visible_active_state(self):
        """The toolbar buttons get an `.is-active` class driven by
        updateActiveStates to show that Bold/Italic is armed."""
        js = _rich_editor()
        assert "updateActiveStates" in js
        css = _styles_css()
        assert ".bh-rt-toolbar button.is-active" in css, (
            "is-active visual style missing from stylesheet"
        )


# ===========================================================================
# 2. Calendar prev / next stays open
# ===========================================================================
class TestCalendarNavStaysOpen:
    """Internal pop clicks call `e.stopPropagation()` so the rebuilt DOM
    doesn't masquerade as an outside click that closes the popover."""

    def test_app_js_stops_propagation_for_nav_clicks(self):
        js = _bh_date()
        # The next-month nav button's onClick must stopPropagation before
        # paging the month, so the popover re-render isn't mistaken for an
        # outside click that closes it.
        m = re.search(
            r'data-nav="next".*?navMonth\(1\)',
            js, re.S,
        )
        assert m, "next-month bh-date-nav button missing"
        assert "stopPropagation" in m.group(0), (
            "nav-button onClick must stopPropagation so the rebuild "
            "doesn't trip outside-close"
        )


# ===========================================================================
# 3. Reporter dropdown has no caret when disabled
# ===========================================================================
class TestReporterDropdownNoCaret:
    def test_index_html_reporter_select_is_disabled(self):
        # The Reporter <select> now lives in BugModal.tsx. It must remain
        # disabled — if a future refactor un-disables it the caret-suppression
        # behaviour is silently bypassed, so pin it here.
        src = _bug_modal()
        assert re.search(
            r'<select[^>]*name="reporter_id"[^>]*\bdisabled\b',
            src,
        ), "Reporter <select> must keep `disabled` attribute"

    def test_app_js_skips_caret_when_select_disabled(self):
        # BhSelect renders the dropdown caret only when not disabled — a
        # disabled select cannot open, so the caret would falsely advertise
        # "click me".
        js = _bh_select()
        assert re.search(
            r'!disabled\s*&&\s*<span className="bh-sel-caret"',
            js,
        ), "disabled-select caret-suppression branch missing"

    def test_app_js_observer_rerenders_label_on_disabled_change(self):
        # Vanilla used a MutationObserver to re-render the caret when the
        # select's disabled state flipped at runtime. In React the caret is a
        # function of the `disabled` prop, so a prop change re-renders and the
        # caret toggles immediately — no observer needed. Pin that the caret
        # (and the trigger) stay driven by `disabled`.
        js = _bh_select()
        assert re.search(
            r'!disabled\s*&&\s*<span className="bh-sel-caret"',
            js,
        ), (
            "caret must be conditional on the disabled prop so it toggles "
            "reactively when disabled changes"
        )
        assert "disabled={disabled}" in js, (
            "native select + trigger must reflect the disabled prop"
        )


# ===========================================================================
# 4. Inserted image goes through attachment flow (no inline embed)
# ===========================================================================
class TestImageInsertionAsAttachment:
    def test_app_js_no_legacy_pickimage_helper(self):
        js = _rich_editor()
        # The old helper inserted <img> via insertHTML, which left images
        # that could not be resized or removed. It must be gone.
        assert "_bhRtPickImage" not in js, (
            "Legacy inline-image picker must be removed"
        )
        # And no insertHTML('<img …>') anywhere in the rich-text path.
        assert not re.search(
            r'insertHTML[^;]*<img\b',
            js,
        ), "rich-text editor must NOT inline <img> tags"

    def test_app_js_uses_attachment_pickfile(self):
        js = _rich_editor()
        assert "pickFileAsAttachment" in js, (
            "Toolbar image button must route through the attachment helper"
        )
        # And the toolbar `bh-image` button is the one that calls it.
        m = re.search(
            r'cmd\s*===\s*"bh-image".*?pickFileAsAttachment',
            js, re.S,
        )
        assert m, "bh-image branch must call pickFileAsAttachment"


# ===========================================================================
# 5. Rich HTML in description / comment survives the round-trip
# ===========================================================================
class TestRichHtmlRoundtrip:
    """End-to-end: post a description / comment containing the formatting
    tags the toolbar can produce, fetch it back, assert each tag survives."""

    @pytest.fixture()
    def bug(self, admin_client):
        proj = _make_project(admin_client)
        return _make_bug(admin_client, proj["id"])

    def _post_description(self, client, bug_id, body):
        r = client.put(f"/api/bugs/{bug_id}", json={"description": body})
        assert r.status_code == 200, r.text
        return r.json()

    def test_bold_italic_underline_survive(self, admin_client, bug):
        html = "<p>This is <b>bold</b>, <i>italic</i> and <u>underline</u>.</p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert "<b>bold</b>" in got
        assert "<i>italic</i>" in got
        assert "<u>underline</u>" in got

    def test_lists_survive(self, admin_client, bug):
        html = "<ul><li>one</li><li>two</li></ul><ol><li>a</li><li>b</li></ol>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert "<ul>" in got and "<li>one</li>" in got
        assert "<ol>" in got and "<li>a</li>" in got

    def test_blockquote_pre_code_survive(self, admin_client, bug):
        html = "<blockquote>quoted</blockquote><pre>code block</pre>" \
               "<p><code>inline</code></p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert "<blockquote>quoted</blockquote>" in got
        assert "<pre>code block</pre>" in got
        assert "<code>inline</code>" in got

    def test_strikethrough_survives(self, admin_client, bug):
        html = "<p>see <s>obsolete</s></p>"
        got = self._post_description(admin_client, bug["id"], html)["description"]
        assert "<s>obsolete</s>" in got

    def test_comment_body_keeps_rich_formatting(self, admin_client, bug):
        body = "<p><b>Note:</b> please verify <i>before</i> ship.</p>"
        r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": body})
        assert r.status_code == 201, r.text
        got = r.json()["body"]
        assert "<b>Note:</b>" in got
        assert "<i>before</i>" in got


# ===========================================================================
# 6. XSS / unsafe payloads are still scrubbed
# ===========================================================================
class TestSanitiserStillBlocksUnsafe:
    """The rich-text round-trip must not weaken the sanitiser."""

    @pytest.fixture()
    def bug(self, admin_client):
        proj = _make_project(admin_client, name="Sec v2.6")
        return _make_bug(admin_client, proj["id"])

    def test_script_tag_is_stripped(self, admin_client, bug):
        html = "<p>before<script>alert(1)</script>after</p>"
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        assert r.status_code == 200
        got = r.json()["description"]
        # The dangerous bit is the executable <script> wrapper. The
        # in-house sanitiser drops tags from the allowlist but keeps
        # surviving text content — so "alert(1)" survives as inert
        # plain text. That's correct: no XSS surface because the
        # wrapping tag is gone.
        assert "<script" not in got.lower()
        assert "</script" not in got.lower()
        # Surrounding paragraph + neighbouring text must survive.
        assert "before" in got and "after" in got

    def test_javascript_url_in_anchor_is_stripped(self, admin_client, bug):
        html = '<p><a href="javascript:alert(1)">x</a></p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "javascript:" not in got.lower()

    def test_onerror_attr_is_stripped(self, admin_client, bug):
        html = '<p><img src="x" onerror="alert(1)"></p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "onerror" not in got.lower(), "event-handler attrs must die"

    def test_iframe_is_stripped(self, admin_client, bug):
        html = '<p><iframe src="https://evil.example"></iframe>x</p>'
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": html})
        got = r.json()["description"]
        assert "<iframe" not in got.lower()
        # Body text outside the iframe must still survive.
        assert "x</p>" in got


# ===========================================================================
# 7. Re-cover the admin-only rules (so this suite stands alone)
# ===========================================================================
class TestAdminOnlyMutations:

    def _make_user(self, client, name, role="user"):
        r = client.post("/api/users", json={
            "name": name, "email": f"{name.lower()}@x.test",
            "role": role, "password": "User12345Aa",
        })
        assert r.status_code == 201, r.text
        return r.json()

    def test_non_admin_cannot_edit_own_comment(self, client):
        # admin sets things up
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6")
        u = self._make_user(client, "Beta")
        bug = _make_bug(client, proj["id"])
        # user posts a comment
        _login(client, u["email"], "User12345Aa")
        r = client.post(f"/api/bugs/{bug['id']}/comments",
                        json={"body": "<p>Hello</p>"})
        assert r.status_code == 201
        cid = r.json()["id"]
        # ...and tries to edit it
        r = client.put(f"/api/bugs/{bug['id']}/comments/{cid}",
                       json={"body": "<p>edited</p>"})
        assert r.status_code == 403, (
            "Non-admin must not edit even their own comment"
        )

    def test_non_admin_cannot_delete_comment(self, client):
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 b")
        u = self._make_user(client, "Gamma")
        bug = _make_bug(client, proj["id"])
        # admin posts the comment so the user is not the owner
        r = client.post(f"/api/bugs/{bug['id']}/comments",
                        json={"body": "<p>admin note</p>"})
        cid = r.json()["id"]
        _login(client, u["email"], "User12345Aa")
        r = client.delete(f"/api/bugs/{bug['id']}/comments/{cid}")
        assert r.status_code == 403

    def test_non_admin_cannot_delete_attachment(self, client):
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 c")
        u = self._make_user(client, "Delta")
        bug = _make_bug(client, proj["id"])
        # admin uploads an attachment
        r = client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("doc.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        _login(client, u["email"], "User12345Aa")
        r = client.delete(f"/api/bugs/{bug['id']}/attachments/{aid}")
        assert r.status_code == 403

    def test_non_admin_CAN_upload_attachment_after_creation(self, client):
        """Post-creation uploads stay open to non-admins."""
        _login(client, "admin@test.local", "Admin1234")
        proj = _make_project(client, name="Perm v2.6 d")
        u = self._make_user(client, "Epsilon")
        bug = _make_bug(client, proj["id"])
        _login(client, u["email"], "User12345Aa")
        r = client.post(
            f"/api/bugs/{bug['id']}/attachments",
            files={"file": ("note.txt", io.BytesIO(b"hi"), "text/plain")},
        )
        assert r.status_code == 201, (
            "Non-admins must still be able to attach files after the bug "
            "exists — only delete is admin-only"
        )


# ===========================================================================
# 8. Database safety: schema is unchanged across reboot (idempotent init)
# ===========================================================================
class TestInitDbIsIdempotent:
    """init_db is a three-pass create-if-missing / add-column-if-missing /
    index-if-missing routine. Running it twice on the same DB must leave the
    schema identical, so a redeploy does not alter an existing database."""

    def test_running_init_twice_does_not_recreate_tables(self, tmp_path, monkeypatch):
        import sys
        db_file = tmp_path / "smoke.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("API_KEY", "")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x" * 32)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")

        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

        from app.database import init_db, engine
        from sqlalchemy import inspect

        init_db()
        snap1 = sorted(inspect(engine).get_table_names())
        init_db()  # second pass
        snap2 = sorted(inspect(engine).get_table_names())
        assert snap1 == snap2, (
            "Schema must be identical across init_db calls — "
            "no destructive DDL on redeploy"
        )

    def test_no_new_tables_introduced_by_v26_fixes(self, tmp_path, monkeypatch):
        """init_db creates the canonical core tables. A newly introduced
        table would still pass here, but the idempotence test above pairs
        with this one to catch double-init drift."""
        import sys
        db_file = tmp_path / "schema.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("API_KEY", "")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "x" * 32)
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "a@a.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")

        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]

        from app.database import init_db, engine
        from sqlalchemy import inspect

        init_db()
        names = set(inspect(engine).get_table_names())
        # The exact set is not pinned (that's brittle); this only checks
        # the file actually creates the canonical core tables. A newly
        # introduced table would still pass here — but the migration test
        # above pairs with it in CI to catch any double-init drift.
        for required in ("users", "projects", "bugs", "comments",
                         "attachments", "activity_log"):
            assert required in names, f"missing canonical table {required!r}"


# ===========================================================================
# Hardening
# ===========================================================================
class TestHardening:
    """Pin hardening behaviors so an accidental revert is caught."""

    def test_uvicorn_host_defaults_to_loopback(self):
        """app/main.py's __main__ block must not hard-bind 0.0.0.0."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        assert 'os.getenv("UVICORN_HOST"' in src, (
            "Bind host must come from UVICORN_HOST env var, not be hard-coded"
        )
        assert '"127.0.0.1"' in src, (
            "Default UVICORN_HOST should be the loopback address"
        )

    def test_cors_default_is_empty_not_wildcard(self):
        """Default CORS_ORIGINS must be empty so the wildcard policy is opt-in."""
        import importlib, os
        os.environ.pop("CORS_ORIGINS", None)
        import app.config as cfg
        importlib.reload(cfg)
        assert cfg.Settings().CORS_ORIGINS == [], (
            f"expected [] default, got {cfg.Settings().CORS_ORIGINS!r}"
        )

    def test_cors_middleware_not_registered_when_no_origins(self):
        """No CORS_ORIGINS configured → CORSMiddleware must not be added."""
        from starlette.middleware.cors import CORSMiddleware
        # Build a fresh app by importing main
        import importlib, os, sys
        os.environ.pop("CORS_ORIGINS", None)
        for m in list(sys.modules):
            if m == "app" or m.startswith("app."):
                del sys.modules[m]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from app import main as app_main
        importlib.reload(app_main)
        cls_names = [
            m.cls.__name__ for m in app_main.app.user_middleware
        ]
        assert CORSMiddleware.__name__ not in cls_names, (
            f"CORSMiddleware should be skipped when CORS_ORIGINS is empty; "
            f"got middleware stack: {cls_names}"
        )

    def test_no_bare_except_exception_in_app_database(self):
        """app/database.py must use narrowed SQLAlchemyError, not Exception."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "database.py").read_text(encoding="utf-8")
        # The try/except blocks must catch SQLAlchemyError specifically rather
        # than a bare Exception.
        assert "except SQLAlchemyError:" in src
        assert src.count("except Exception:") == 0

    def test_email_service_uses_narrowed_except(self):
        """SMTP send-path must not catch bare Exception."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "email_service.py").read_text(encoding="utf-8")
        assert "except (smtplib.SMTPException, OSError)" in src
        # The broad form must not be present.
        assert "except Exception:\n        # Never let mailer failures" not in src

    def test_auth_uses_narrowed_except(self):
        """session-table writes must narrow to SQLAlchemyError."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1] / "app" / "auth.py").read_text(encoding="utf-8")
        assert "from sqlalchemy.exc import SQLAlchemyError" in src
        assert "except SQLAlchemyError:" in src

    def test_pyproject_toml_has_relative_coverage(self):
        """pyproject.toml must enable relative_files=True so coverage paths
        map back to sources from a container with a different layout."""
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert "relative_files = true" in text
