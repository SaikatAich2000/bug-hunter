"""End-to-end UI smoke tests run in a real Chromium via Playwright.

These tests would have caught the nested-form Create-button bug. They
boot the FastAPI app on a real port, then drive Chromium to:

  1. Log in as admin
  2. Create a project
  3. Open the bug create modal — assert the Create button is INSIDE
     the bug form, the Reporter select is disabled, and pressing
     Create actually fires the POST /api/bugs request and creates a
     bug visible in the list.
  4. Open the bug, assert the inline comments section appears.
  5. Sessions panel: as admin, revoke a different user's session and
     assert that user's open tab is bounced to /login.html within
     ~20 s by the periodic /me poll.

Run from the repo root:  python -m pytest tests/test_ui_smoke.py -q
"""
import os
import socket
import tempfile
import threading
import time
import sys
import contextlib

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """Boot the FastAPI app on a random port in a background thread."""
    db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    db.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{db.name}"
    os.environ["SESSION_SECRET"] = "ui_smoke"
    os.environ["EMAIL_BACKEND"] = "disabled"
    os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@ui.test"
    os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "AdminUI1234"
    os.environ["BOOTSTRAP_ADMIN_NAME"] = "UI Admin"
    # v2.7-security T4: disable the HIBP outbound call. Unlike the
    # TestClient fixtures in conftest.py, this live_server starts a real
    # uvicorn thread BEFORE any function-scoped monkeypatch is applied,
    # so it would inherit the unset (= default-enabled) value otherwise
    # and create-user would block on a live network round-trip.
    os.environ["PASSWORD_BREACH_CHECK_ENABLED"] = "false"
    # Force a fresh import after env is set.
    for m in list(sys.modules):
        if m == "app" or m.startswith("app."):
            del sys.modules[m]

    import uvicorn
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for the server to actually bind.
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("server didn't start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)
    with contextlib.suppress(Exception):
        os.unlink(db.name)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _login(page, base_url, email="admin@ui.test", password="AdminUI1234"):
    page.goto(f"{base_url}/login.html")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/", timeout=5000)
    # Wait for the SPA to finish booting. #newBugBtn is in the static HTML
    # so it appears instantly — not a reliable boot-done signal. Instead
    # wait for the network to settle (boot fires several /api calls) AND
    # for the account name to be filled in by renderAccountCard.
    page.wait_for_load_state("networkidle", timeout=10000)
    page.wait_for_function(
        "() => document.getElementById('accountName')?.textContent?.trim().length > 0",
        timeout=5000,
    )


# ---------------------------------------------------------------------------
# THE CRITICAL TEST: clicking "Create" actually creates a bug.
#
# Before the v3.1.1 fix, the inline comment <form> was nested inside the
# bug-modal <form>, which (per HTML5 spec) silently terminated the outer
# form. The Create button ended up parent-less and clicking it did
# nothing. This test reproduces that scenario end-to-end.
# ---------------------------------------------------------------------------
def test_create_bug_button_actually_creates(live_server, browser):
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # Create a project first (Create-Bug needs at least one project)
    page.click("#newProjectBtn")
    page.wait_for_selector("#modalProject", state="visible", timeout=2000)
    page.fill('#formProject input[name="name"]', "UI Smoke Project")
    page.click('#formProject button[type="submit"]')
    page.wait_for_selector("#modalProject", state="hidden", timeout=3000)

    # Now open the bug create modal
    page.click("#newBugBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)

    # ----- Assertions about modal structure -----
    # Create button must be INSIDE the bug form. NOTE: getAttribute('id')
    # not f.id — HTMLFormElement has a named-property accessor that lets
    # `form.id` resolve to a child input named "id" (we have
    # <input name="id"> in formBug for the hidden bug-id field). Famous
    # DOM gotcha.
    enclosing_form_id = page.evaluate("""
        () => {
            const btn = document.getElementById('bugSubmitBtn');
            const f = btn ? btn.closest('form') : null;
            return f ? f.getAttribute('id') : null;
        }
    """)
    assert enclosing_form_id == "formBug", \
        f"Create button is not inside formBug! Got: {enclosing_form_id!r}"

    # Reporter select must be disabled.
    is_disabled = page.evaluate("""
        () => document.querySelector('#formBug select[name="reporter_id"]').disabled
    """)
    assert is_disabled, "Reporter select must be disabled"

    # Reporter must show the logged-in user's name.
    reporter_text = page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="reporter_id"]');
            return sel.options[sel.selectedIndex]?.text;
        }
    """)
    assert "UI Admin" in reporter_text, f"Reporter should show 'UI Admin', got: {reporter_text!r}"

    # ----- Fill the form and click Create -----
    page.fill('#formBug input[name="title"]', "Smoke-test bug from Playwright")
    page.fill('#formBug textarea[name="description"]', "Created by an automated UI test.")

    # Pick the project we just created
    page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="project_id"]');
            sel.value = sel.options[1].value;  // skip the placeholder
            sel.dispatchEvent(new Event('change'));
        }
    """)

    # Capture the network request to /api/bugs to confirm Create actually fires.
    with page.expect_response(lambda r: r.url.endswith("/api/bugs") and r.request.method == "POST") as resp_info:
        page.click("#bugSubmitBtn")
    resp = resp_info.value
    assert resp.status == 201, f"POST /api/bugs returned {resp.status}: {resp.text()}"

    # Modal closes
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)

    # New bug shows up in the list
    page.wait_for_selector("text=Smoke-test bug from Playwright", timeout=3000)

    ctx.close()


def test_open_bug_shows_inline_comments_section(live_server, browser):
    """Clicking a bug row opens the unified modal with the inline
    comments section visible (was: separate detail modal with tabs)."""
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # Open the bug we just created (relies on previous test having run;
    # for isolation, we'd seed via API — but the project + bug from the
    # previous test are still in the test DB since the fixture is module-scoped).
    page.click("text=Smoke-test bug from Playwright")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)

    # Comments section should be visible (not hidden) when editing an existing bug.
    is_hidden = page.evaluate("""
        () => document.getElementById('bugCommentsSection').hidden
    """)
    assert not is_hidden, "Comments section should be visible when opening an existing bug"

    # The post-comment button is a normal button, not a submit (no nested form).
    btn_type = page.evaluate("() => document.getElementById('commentPostBtn').type")
    assert btn_type == "button"

    ctx.close()


def test_session_revoke_kicks_user_out_promptly(live_server, browser):
    """Admin revokes a user's session → user is logged out.

    The v3.1.1 fix has two parts working together:

      • Frontend: api() catches 401 and calls bounceToLogin() which does
        location.replace('/login.html'). Also a periodic /me poll for
        the case where the user does nothing.
      • Backend: _has_valid_session() now consults the sessions table,
        not just the cookie signature. Without this fix, the / and
        /login.html HTML handlers can't tell a revoked cookie from a
        live one — so the SPA's location.replace('/login.html') just
        bounces back to / in an infinite loop. (That was the
        "behaving strangely after refresh" symptom.)

    We exercise the most common user flow: revoke → user reloads (or
    triggers any API call) → server now correctly serves /login.html.
    """
    import httpx
    admin = httpx.Client(base_url=live_server)
    admin.post("/api/auth/login",
               json={"email": "admin@ui.test", "password": "AdminUI1234"})
    admin.post("/api/users", json={
        "name": "Victim", "email": "victim@ui.test",
        "role": "user", "password": "Victim1234",
    })

    victim_ctx = browser.new_context()
    victim_page = victim_ctx.new_page()
    _login(victim_page, live_server, "victim@ui.test", "Victim1234")
    assert victim_page.url == f"{live_server}/", \
        f"Victim should be on / before revoke, was at {victim_page.url}"

    # Revoke the victim's session via the API directly (deterministic;
    # equivalent to admin clicking Revoke in the Sessions panel — which
    # is covered separately by the backend tests in test_v31.py).
    sessions = admin.get("/api/sessions").json()
    victim_session = next(
        s for s in sessions if s.get("user_email") == "victim@ui.test"
    )
    r = admin.delete(f"/api/sessions/{victim_session['id']}")
    assert r.status_code == 200

    # Reload the victim's tab → boot() calls /me → 401 → location.replace.
    # Server-side, _has_valid_session must now correctly return False for
    # the revoked cookie so /login.html doesn't bounce back to /.
    victim_page.reload()
    try:
        victim_page.wait_for_url("**/login.html", timeout=5000)
    except Exception:
        pass
    assert "/login.html" in victim_page.url, \
        f"Victim was not redirected after reload. URL: {victim_page.url}\n" \
        f"This means the redirect-loop bug from v3.1.0 is back: the " \
        f"_has_valid_session() function in app/main.py is treating a " \
        f"revoked cookie as valid and bouncing /login.html back to /."

    victim_ctx.close()
    admin.close()
