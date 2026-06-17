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

# Every test here drives a real Chromium, so mark the module `ui`. CI can skip
# the slow browser suite with `-m "not ui"`.
pytestmark = pytest.mark.ui


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

    # Pick the project we just created. bubbles:true is required: real
    # change events bubble, and the React frontend receives delegated
    # events at the root — a non-bubbling synthetic event would never
    # reach its onChange handler (the old vanilla app read the value at
    # submit time, which masked this).
    page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="project_id"]');
            sel.value = sel.options[1].value;  // skip the placeholder
            sel.dispatchEvent(new Event('change', { bubbles: true }));
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


def test_v3_shell_account_menu_and_bell_top_right(live_server, browser):
    """v3.0 shell redesign regression guard.

    Locks in the three chrome bugs fixed in the redesign:
      1. The notification panel actually renders when the bell is clicked.
      2. The bell sits in the SAME (right-hand) place on every view — it used
         to snap to the left next to the title on non-list views because a
         hidden search box still matched a `~` sibling rule.
      3. Account controls (change-password / theme / log out) live in a
         top-right profile menu, NOT in the sidebar.
    """
    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)

    # The account name now lives in the top-right profile button, and the old
    # sidebar account card / logout button are gone.
    assert page.locator("#profileBtn #accountName").count() == 1, \
        "Account name should be inside the top-right profile button"
    assert page.locator(".sidebar #logoutBtn").count() == 0, \
        "Log out must no longer live in the sidebar"

    # The bell + profile share one right-hand cluster, so the bell's left edge
    # must be well into the right half of the viewport (it used to be ~0 on
    # non-list views). Switch to a NON-LIST view first (the original bug).
    # v3.1: the view nav lives in TWO surfaces — the desktop chrome bar and the
    # mobile drawer (Sidebar). Target the chrome one explicitly so this desktop
    # test doesn't resolve to the hidden drawer copy that's first in the DOM.
    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents, .view", timeout=3000)
    box = page.locator("#notifBtn").bounding_box()
    vw = page.evaluate("() => window.innerWidth")
    assert box and box["x"] > vw / 2, \
        f"Bell should be right-aligned on the Events view (x={box and box['x']}, vw={vw})"

    # Clicking the bell opens the panel (bug #1: it didn't render before).
    page.click("#notifBtn")
    page.wait_for_selector(".notif-panel", state="visible", timeout=2000)
    assert page.locator(".notif-panel .notif-panel-title").inner_text().strip() == "Notifications"
    page.keyboard.press("Escape")

    # The profile menu exposes change-password / theme / log out.
    page.click("#profileBtn")
    for ctrl in ("#changePasswordBtn", "#themeBtn", "#logoutBtn"):
        page.wait_for_selector(f".profile-menu {ctrl}", state="visible", timeout=2000)

    # Theme toggle flips the document theme attribute (moved off the sidebar).
    before = page.evaluate("() => document.documentElement.dataset.theme || 'dark'")
    page.click("#themeBtn")
    after = page.evaluate("() => document.documentElement.dataset.theme || 'dark'")
    assert before != after, f"Theme toggle should flip the theme (was {before}, now {after})"

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
    # is covered separately by the backend tests in test_role_policy.py).
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


# ---------------------------------------------------------------------------
# Navigation regression guards (v3.3.1)
#
# A class of "surprise redirect" bugs: saving an item or clicking a KPI from a
# non-list view would force-navigate the user to the Work Items list. The most
# visible symptom was reported against events — "after creating a task inside an
# event, saving redirects to the all-work-items screen instead of staying in the
# event." The same forced setView("list") lived in BugModal (create + edit),
# ProjectModal (save) and KpiStrip (click). These tests pin the fix: a save /
# KPI click keeps the user exactly where they were.
#
# Detection trick: Shell mounts ONLY the active view (`{view === "x" && <X/>}`),
# so when the app is on Events the #viewList element is absent from the DOM
# entirely. `#viewList` count == 0 is therefore a precise "did not redirect to
# the list" assertion, and #viewEvents/#viewAnalytics visibility confirms we
# stayed put.
# ---------------------------------------------------------------------------
def _api_admin(live_server):
    """Logged-in httpx client for seeding fixtures via the API. Credentials are
    read from the bootstrap env vars live_server set, so they aren't repeated as
    literals here."""
    import httpx
    creds = {
        "email": os.environ["BOOTSTRAP_ADMIN_EMAIL"],
        "password": os.environ["BOOTSTRAP_ADMIN_PASSWORD"],
    }
    c = httpx.Client(base_url=live_server)
    c.post("/api/auth/login", json=creds)
    return c


@pytest.fixture(scope="module")
def nav_page(live_server, browser):
    """One logged-in admin page shared across the navigation-regression tests.

    Login is rate-limited (8 / 60s per IP); the rest of the module already
    spends most of that budget, so each of these tests can't afford its own
    fresh login. They never revoke sessions and always re-enter through the view
    nav, so a single shared page is both correct and rate-limit friendly. The
    project + event they drive against are seeded once via a single API login."""
    admin = _api_admin(live_server)
    admin.post("/api/projects", json={"name": "Event Task Project", "color": "#3366ff"})
    admin.post("/api/events", json={"name": "Sprint Planning Regression"})
    admin.close()

    ctx = browser.new_context()
    page = ctx.new_page()
    _login(page, live_server)
    yield page
    ctx.close()


def test_create_task_inside_event_stays_in_event(nav_page):
    """THE reported bug: '+ Add Task' inside an event, then Save, must keep the
    user inside that event's detail panel — never bounce to Work Items."""
    page = nav_page

    # Open the Events view and drill into our event. Scope the card click to the
    # visible grid: the event name also renders in the (hidden) detail header, so
    # an unscoped text= match can resolve to the wrong, invisible node.
    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents", state="visible", timeout=3000)
    page.click("#eventsGrid >> text=Sprint Planning Regression")
    page.wait_for_selector("#eventsDetailMode", state="visible", timeout=3000)

    # "+ Add Task" opens the shared bug modal seeded with this event + Task type.
    page.click("#addItemToEventBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)
    page.fill('#formBug input[name="title"]', "Task created inside the event")
    page.evaluate("""
        () => {
            const sel = document.querySelector('#formBug select[name="project_id"]');
            const opt = [...sel.options].find(o => o.text === 'Event Task Project');
            sel.value = opt.value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """)
    with page.expect_response(
        lambda r: r.url.endswith("/api/bugs") and r.request.method == "POST"
    ) as ri:
        page.click("#bugSubmitBtn")
    assert ri.value.status == 201, f"create task failed: {ri.value.text()}"

    # Modal closes — and we must STILL be inside the event, not on the list.
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
    assert page.locator("#viewList").count() == 0, (
        "BUG: creating a task inside an event redirected to the Work Items list. "
        "The forced setView('list') in BugModal.onSubmit is back."
    )
    assert page.locator("#viewEvents").is_visible(), \
        "Should still be on the Events view after adding a task"
    assert page.locator("#eventsDetailMode").is_visible(), \
        "Should still be in the event's detail panel (not the event card grid)"
    # And the new task is visible in the event's own item table.
    page.wait_for_selector(
        "#eventDetailItems >> text=Task created inside the event", timeout=4000
    )


def test_edit_task_from_event_stays_in_event(nav_page):
    """Editing a task opened FROM the event table and saving must also keep the
    user in the event (the edit path force-navigated too)."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="events"]')
    page.wait_for_selector("#viewEvents", state="visible", timeout=3000)
    # The shared page may already be inside an event detail (the create-task test
    # ran first); return to the card grid so the event card is visible/clickable.
    if page.locator("#eventsDetailMode").is_visible():
        page.click("#eventBackBtn")
        page.wait_for_selector("#eventsListMode", state="visible", timeout=3000)
    page.click("#eventsGrid >> text=Sprint Planning Regression")
    page.wait_for_selector("#eventsDetailMode", state="visible", timeout=3000)

    # Open the task created by the previous test from the event's item table.
    page.click("#eventDetailItems >> text=Task created inside the event")
    page.wait_for_selector("#modalBug", state="visible", timeout=2000)
    page.fill('#formBug input[name="title"]', "Task edited inside the event")
    with page.expect_response(
        lambda r: "/api/bugs/" in r.url and r.request.method == "PUT"
    ) as ri:
        page.click("#bugSubmitBtn")
    assert ri.value.status == 200, f"edit task failed: {ri.value.text()}"

    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
    assert page.locator("#viewList").count() == 0, \
        "BUG: editing a task from an event redirected to the Work Items list"
    assert page.locator("#eventsDetailMode").is_visible(), \
        "Should still be in the event's detail panel after an edit"


def test_kpi_click_on_analytics_stays_on_analytics(nav_page):
    """Clicking a KPI tile on Analytics filters in place — it must not redirect
    to Work Items."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="analytics"]')
    page.wait_for_selector("#viewAnalytics", state="visible", timeout=3000)

    page.click('.kpi[data-kpi="open"]')
    # No surprise jump — still on Analytics, and the tile reflects the filter.
    assert page.locator("#viewList").count() == 0, \
        "BUG: a KPI click on Analytics redirected to the Work Items list"
    assert page.locator("#viewAnalytics").is_visible(), \
        "Clicking a KPI on Analytics must keep the user on Analytics"
    page.wait_for_selector('.kpi[data-kpi="open"].active', timeout=2000)


def test_project_save_keeps_current_view(nav_page):
    """Saving a project from a non-list view keeps the user on that view."""
    page = nav_page

    page.click('.chrome .nav-btn[data-view="analytics"]')
    page.wait_for_selector("#viewAnalytics", state="visible", timeout=3000)

    page.click("#newProjectBtn")
    page.wait_for_selector("#modalProject", state="visible", timeout=2000)
    page.fill('#formProject input[name="name"]', "Stay-Put Project")
    page.click('#formProject button[type="submit"]')
    page.wait_for_selector("#modalProject", state="hidden", timeout=3000)

    assert page.locator("#viewList").count() == 0, \
        "BUG: saving a project redirected to the Work Items list"
    assert page.locator("#viewAnalytics").is_visible(), \
        "Saving a project should keep the user on the Analytics view"


# ---------------------------------------------------------------------------
# Feature regressions (v3.3.2): linked-items Jira-style picker + the unified
# square checkbox. Both reuse the shared nav_page login (rate-limit budget).
# ---------------------------------------------------------------------------
def _seed_bug_via_page(page, title: str) -> int:
    """Create a Bug straight through the authenticated browser session (no extra
    login) and return its id. Reuses the first existing project."""
    return page.evaluate(
        """async (title) => {
            const projs = await (await fetch('/api/projects', {credentials:'include'})).json();
            const r = await fetch('/api/bugs', {
                method: 'POST', credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    project_id: projs[0].id, title,
                    priority: 'Medium', environment: 'DEV', item_type: 'Bug',
                }),
            });
            return (await r.json()).id;
        }""",
        title,
    )


def test_user_active_checkbox_is_square(nav_page):
    """The user-create "Active" checkbox must render as a SQUARE (custom themed
    control), not the old stretched rectangle."""
    page = nav_page
    page.click("#newUserBtn")
    page.wait_for_selector("#modalUser", state="visible", timeout=3000)

    box = page.locator('#modalUser input[name="is_active"]').bounding_box()
    assert box is not None, "Active checkbox not found"
    # Square: width == height (allow 1px sub-pixel rounding).
    assert abs(box["width"] - box["height"]) <= 1, \
        f"Active checkbox is not square (rectangle bug): {box}"
    # And it's the custom-drawn control (appearance:none), so it's themed, not
    # the browser's native box.
    appearance = page.evaluate(
        """() => getComputedStyle(
            document.querySelector('#modalUser input[name="is_active"]')
        ).webkitAppearance"""
    )
    assert appearance == "none", f"checkbox is still a native control: {appearance!r}"
    page.keyboard.press("Escape")


def test_linked_items_picker_multiselect_and_link(nav_page):
    """The Jira-style linked-items picker: filter by type, multi-select two
    results, and Link both at once. Guards the new multi-select UX, the type
    filter, and that the underlying POST /links works end-to-end."""
    page = nav_page
    src_id = _seed_bug_via_page(page, "ZZ Picker Source Bug")
    tgt1 = _seed_bug_via_page(page, "ZZ Picker Target Alpha")
    tgt2 = _seed_bug_via_page(page, "ZZ Picker Target Bravo")

    # Open the source item's modal directly (no list-filter coupling).
    page.evaluate(
        "(id) => document.dispatchEvent(new CustomEvent('sleuth:open-bug', {detail: {bugId: id}}))",
        src_id,
    )
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)

    # Open the picker, narrow to the Bugs tab, search, and tick TWO results.
    page.click(".item-picker-trigger")
    page.click('.item-picker-tab:has-text("Bugs")')
    page.fill(".item-picker-search", "ZZ Picker Target")
    page.click('.item-picker-row:has-text("ZZ Picker Target Alpha")')
    page.click('.item-picker-row:has-text("ZZ Picker Target Bravo")')
    # Both selections show as chips (multi-select keeps the dropdown open).
    assert page.locator(".item-picker-chips .item-picker-chip").count() == 2

    # Link both — one POST per selected item.
    page.click("#bugLinkAdd .btn.primary")

    # Both link rows appear in the list.
    page.wait_for_selector(f'.bug-link-row:has-text("#{tgt1}")', timeout=4000)
    page.wait_for_selector(f'.bug-link-row:has-text("#{tgt2}")', timeout=4000)
    assert page.locator('.bug-link-row:has-text("ZZ Picker Target Alpha")').count() >= 1
    assert page.locator('.bug-link-row:has-text("ZZ Picker Target Bravo")').count() >= 1

    page.keyboard.press("Escape")


def test_reports_type_dropdown_anchors_to_trigger(nav_page):
    """The Report-type dropdown must open anchored to its trigger — it used to
    land hundreds of px to the right because the position:fixed popover was
    contained by an ancestor. Now portaled to <body>, it aligns to the trigger."""
    page = nav_page
    page.click('.chrome .nav-btn[data-view="reports"]')
    page.wait_for_selector("#reportTypeSelect", timeout=5000)

    trigger = page.locator('.bh-sel-wrap:has(#reportTypeSelect) .bh-sel-btn')
    trigger.click()
    page.wait_for_selector(".bh-sel-pop", state="visible", timeout=2000)

    tb = trigger.bounding_box()
    pb = page.locator(".bh-sel-pop").bounding_box()
    assert tb and pb
    # Left edges align (the misplacement bug shifted it ~sidebar-width right).
    assert abs(pb["x"] - tb["x"]) <= 8, f"dropdown not left-aligned: trigger={tb}, pop={pb}"
    # And it sits directly below the trigger (or just above if flipped).
    below = abs(pb["y"] - (tb["y"] + tb["height"])) <= 14
    above = abs((pb["y"] + pb["height"]) - tb["y"]) <= 14
    assert below or above, f"dropdown not anchored vertically: trigger={tb}, pop={pb}"
    page.keyboard.press("Escape")


def test_bulk_bar_uses_themed_selects(nav_page):
    """The bulk action bar must use the themed BhSelect controls, not the old
    unstyled native <select> boxes."""
    page = nav_page
    _seed_bug_via_page(page, "ZZ Bulk Bar Item")
    page.click('.chrome .nav-btn[data-view="list"]')
    page.wait_for_selector("#viewList", state="visible", timeout=3000)

    page.check('th.col-select input[type="checkbox"]')  # select all on page
    page.wait_for_selector("#bulkBar", state="visible", timeout=3000)

    assert page.locator("#bulkBar select.bulk-select").count() == 0, \
        "bulk bar still uses unthemed native <select>"
    assert page.locator("#bulkBar .bulk-select-wrap .bh-sel-btn").count() == 3, \
        "bulk bar should have 3 themed BhSelect controls (status/priority/env)"
    # Clear the selection so the shared page is clean for later tests.
    page.click("#bulkBar .bulk-clear")


def test_video_attachment_opens_custom_player_in_lightbox(nav_page):
    """A video attachment shows a compact poster thumbnail in the card; clicking
    "View" opens the custom player in a themed lightbox (NOT the browser's native
    player in a new tab). The control bar must be visible while paused AND while
    playing — the seek bar is always grabbable, including fullscreen."""
    page = nav_page
    bug_id = _seed_bug_via_page(page, "ZZ Video Bug")
    # Upload a tiny fake mp4 so the card is treated as a video by content-type.
    page.evaluate(
        """async (id) => {
            const fd = new FormData();
            const blob = new Blob([new Uint8Array([0, 0, 0, 24])], {type: 'video/mp4'});
            fd.append('file', new File([blob], 'clip.mp4', {type: 'video/mp4'}));
            await fetch(`/api/bugs/${id}/attachments`, {method: 'POST', credentials: 'include', body: fd});
        }""",
        bug_id,
    )
    page.evaluate(
        "(id) => document.dispatchEvent(new CustomEvent('sleuth:open-bug', {detail: {bugId: id}}))",
        bug_id,
    )
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)
    # Card shows a poster thumbnail, not an inline player.
    page.wait_for_selector(".attach-video-thumb", timeout=4000)
    assert page.locator(".vplayer").count() == 0, "player should not render inline (use the lightbox)"

    # "View" for a video opens the lightbox player (not a raw new-tab download).
    view_btn = page.locator('.attach-actions [data-act="view-video"]').first
    assert view_btn.count() == 1, "video View action missing"
    view_btn.click()
    page.wait_for_selector(".video-lightbox .vplayer", timeout=3000)
    assert page.locator(".video-lightbox .vplayer-seek-input").count() == 1, "no seek bar"
    assert page.locator(".video-lightbox .vplayer-volume").count() == 1, "no volume control"

    # Control bar visible while paused.
    bar_opacity = page.eval_on_selector(".vplayer-bar", "el => getComputedStyle(el).opacity")
    assert float(bar_opacity) > 0.9, f"control bar not visible while paused (opacity {bar_opacity})"

    # Regression for "control bar not visible while the video runs / can't drag
    # the seek bar": it stays at full opacity once playback starts (no auto-hide).
    page.evaluate(
        """() => {
            const v = document.querySelector('.video-lightbox .vplayer-video');
            Object.defineProperty(v, 'paused', {configurable: true, get: () => false});
            v.dispatchEvent(new Event('play'));
        }"""
    )
    page.wait_for_timeout(300)
    play_opacity = page.eval_on_selector(".vplayer-bar", "el => getComputedStyle(el).opacity")
    assert float(play_opacity) > 0.9, f"control bar faded while playing (opacity {play_opacity})"

    # Backdrop click closes the lightbox.
    page.locator(".video-lightbox-close").click()
    page.wait_for_selector(".video-lightbox", state="detached", timeout=3000)

    page.click("#modalBug .modal-close")
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)


def test_attachment_drag_and_drop_stages_file(nav_page):
    """Dropping a file on the create-mode attachment zone stages it for upload."""
    page = nav_page
    page.click("#newBugBtn")
    page.wait_for_selector("#modalBug", state="visible", timeout=3000)

    # Synthesize a native file-drop on the create dropzone.
    page.evaluate(
        """() => {
            const zone = document.querySelector('#bugCreateAttachSection .attach-dropzone');
            const dt = new DataTransfer();
            dt.items.add(new File(['hello'], 'dropped.txt', {type: 'text/plain'}));
            for (const t of ['dragenter', 'dragover', 'drop']) {
                const ev = new DragEvent(t, {bubbles: true, cancelable: true});
                Object.defineProperty(ev, 'dataTransfer', {value: dt});
                zone.dispatchEvent(ev);
            }
        }"""
    )
    page.wait_for_selector('#createFilePreview .attach-staged', timeout=3000)
    assert page.locator('.attach-staged-name:has-text("dropped.txt")').count() == 1, \
        "dropped file was not staged"
    page.click("#modalBug .modal-close")
    page.wait_for_selector("#modalBug", state="hidden", timeout=3000)
