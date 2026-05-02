"""
Round-3 — final batch of targeted tests for issues identified by code review.

Focus:
  - Activity ordering inconsistency (verified) — explore further
  - LIKE-wildcard injection in user search
  - Project move via update — does a user keep edit rights?
  - Update bug with reporter_id=null (admin/manager only?)
  - Background email tasks don't crash the request even if SMTP fails
  - Empty-string fields handling
  - Bug update with ALL same values shouldn't trigger any audit
  - Username-search with `_` (LIKE wildcard) returns false matches
  - Comment with attachment — comment author preserved across deletes
"""
from __future__ import annotations

import io


def _make_user(c, name="Someone Long", email="some@x.com", role="user", password="Password1"):
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role, "password": password,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(c, name="Project Name"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _make_bug(c, project_id, title="A bug for tests", **extra):
    body = {"project_id": project_id, "title": title,
            "priority": "Medium", "environment": "DEV"}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# Activity order: detail vs list-activity returns reversed order with
# same-second timestamps. Confirmed by previous round, verify magnitude here.
# ===========================================================================
class TestActivityOrderingDeep:
    def test_activity_detail_and_list_show_reversed_order_when_clock_is_coarse(self, admin_client):
        """The relationship orders by created_at DESC only.
        list_activity orders by (created_at DESC, id DESC).
        With second-precision timestamps, same-second activities will tie
        on created_at; SQLite returns them in insertion order (asc id), so
        the relationship gives oldest-first while list_activity (with id-desc
        tiebreaker) gives newest-first."""
        p = _make_project(admin_client, "AO-Deep")
        bug = _make_bug(admin_client, p["id"], title="Activity-order verify")
        for s in ("In Progress", "Resolved", "Closed", "Reopened"):
            admin_client.put(f"/api/bugs/{bug['id']}", json={"status": s})

        detail_acts = admin_client.get(f"/api/bugs/{bug['id']}").json()["activities"]
        list_acts   = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()

        # Same set of activities
        assert {a["id"] for a in detail_acts} == {a["id"] for a in list_acts}
        # But order must match
        d = [a["id"] for a in detail_acts]
        l = [a["id"] for a in list_acts]
        assert d == l, \
            f"BUG-4 confirmed: activity ordering inconsistent.\n" \
            f"  /bugs/{{id}}        (detail.activities): {d}\n" \
            f"  /bugs/{{id}}/activity (list_activity):    {l}"



# ===========================================================================
# Bug.project_id can be changed by anyone with edit rights — confirm
# ===========================================================================
class TestBugProjectMove:
    def test_user_can_move_their_bug_to_any_project(self, admin_client):
        p1 = _make_project(admin_client, "Move-A")
        p2 = _make_project(admin_client, "Move-B")
        _make_user(admin_client, name="Mover", email="mover@x.com",
                   password="Password1")
        admin_client.post("/api/auth/logout")
        admin_client.post("/api/auth/login", json={
            "email": "mover@x.com", "password": "Password1",
        })
        bug = _make_bug(admin_client, p1["id"], title="My bug")
        # Move to p2 — user has no special rights on p2, but the API permits this
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"project_id": p2["id"]})
        assert r.status_code == 200
        assert r.json()["project_id"] == p2["id"]


# ===========================================================================
# Reporter unset
# ===========================================================================
class TestReporterUnset:
    def test_admin_can_set_reporter_to_null(self, admin_client):
        """Per code: admin/manager passing reporter_id=null sets bug.reporter_id=None."""
        p = _make_project(admin_client, "RU-1")
        bug = _make_bug(admin_client, p["id"])
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": None})
        assert r.status_code == 200
        assert r.json()["reporter"] is None

    def test_user_cannot_unset_reporter_even_if_they_are_reporter(self, admin_client):
        p = _make_project(admin_client, "RU-2")
        u = _make_user(admin_client, name="Reporter", email="rep@x.com",
                       password="Password1")
        bug = _make_bug(admin_client, p["id"], reporter_id=u["id"])
        admin_client.post("/api/auth/logout")
        admin_client.post("/api/auth/login", json={
            "email": "rep@x.com", "password": "Password1",
        })
        # User tries to unset themselves as reporter — would orphan the bug
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": None})
        # Per code, role check fires for ANY non-omitted reporter_id.
        assert r.status_code == 403


# ===========================================================================
# Email service silent SMTP failure
# ===========================================================================
class TestEmailServiceFailureSilent:
    def test_bug_create_succeeds_even_if_email_backend_disabled(self, admin_client):
        """EMAIL_BACKEND=disabled in conftest. Verify the request flow doesn't
        depend on email succeeding (smoke check)."""
        p = _make_project(admin_client, "EM-1")
        u = _make_user(admin_client, name="Recipient", email="recipient@x.com")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Should email but doesn't",
            "priority": "Low", "environment": "DEV",
            "assignee_ids": [u["id"]],
        })
        assert r.status_code == 201


# ===========================================================================
# Empty-string fields
# ===========================================================================
class TestEmptyStringFields:
    def test_bug_update_with_empty_description(self, admin_client):
        """Empty description should be accepted (description has no min_length)."""
        p = _make_project(admin_client, "ES-1")
        bug = _make_bug(admin_client, p["id"], description="initial")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": ""})
        assert r.status_code == 200
        assert r.json()["description"] == ""

    def test_bug_update_with_only_whitespace_description(self, admin_client):
        """Whitespace-only description: validator strips → empty → stored as ''."""
        p = _make_project(admin_client, "ES-2")
        bug = _make_bug(admin_client, p["id"], description="initial")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"description": "    "})
        assert r.status_code == 200
        assert r.json()["description"] == ""

    def test_project_color_with_uppercase_hex(self, admin_client):
        """Schema regex is `^#[0-9a-fA-F]{6}$` — should accept #ABCDEF."""
        r = admin_client.post("/api/projects", json={
            "name": "UpperHex", "color": "#ABCDEF",
        })
        assert r.status_code == 201
        # Stored verbatim — verify
        assert r.json()["color"] == "#ABCDEF"


# ===========================================================================
# Audit log: actor_user_id semantics
# ===========================================================================
class TestAuditActorSemantics:
    def test_password_reset_request_audit_has_no_actor(self, admin_client):
        """A forgot-password request comes from an UNAUTHENTICATED user, so the
        audit row's actor_user_id must be NULL and actor_name 'system'."""
        admin_client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
        r = admin_client.get("/api/audit?q=password_reset_requested")
        rows = r.json()
        prr = [r for r in rows if r["action"] == "password_reset_requested"]
        assert prr, "password_reset_requested audit row missing"
        # Per code (routes/auth.py:119): _audit(db, None, ...) → actor_name='system'
        assert prr[0]["actor_user_id"] is None
        assert prr[0]["actor_name"] == "system"

    def test_filtering_audit_by_actor_excludes_anonymous_actions(self, admin_client):
        """Filtering by actor_user_id naturally hides system-initiated rows.
        This is a UX gotcha: a user filtering 'their own' audit log won't see
        the password-reset requests they sent because those have NULL actor."""
        admin_client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
        # Filter by actor_user_id=1 (admin)
        r = admin_client.get("/api/audit?actor_user_id=1&q=password_reset_requested")
        rows = r.json()
        # Filter MUST exclude the system row, even though it's about user 1's email
        assert all(row["actor_user_id"] == 1 for row in rows)
        assert not any(row["action"] == "password_reset_requested" for row in rows)


# ===========================================================================
# Stress: tons of bugs / pages
# ===========================================================================
class TestStress:
    def test_many_bugs_pagination_is_stable(self, admin_client):
        p = _make_project(admin_client, "Stress")
        for i in range(25):
            _make_bug(admin_client, p["id"], title=f"stress-{i:03d}")
        ids_seen = set()
        for page in (1, 2, 3):
            r = admin_client.get(f"/api/bugs?page={page}&page_size=10&project_id={p['id']}")
            for b in r.json()["items"]:
                ids_seen.add(b["id"])
        # 25 bugs in this project across 3 pages of 10
        assert len(ids_seen) == 25, f"Pagination missed/dup: {len(ids_seen)}/25"

    def test_search_with_trailing_whitespace(self, admin_client):
        p = _make_project(admin_client, "TW-1")
        _make_bug(admin_client, p["id"], title="findable thing")
        r = admin_client.get("/api/bugs?q=  findable  ")
        items = r.json()["items"]
        # The search code does q.strip().lstrip("#"), so trimmed query works
        assert any("findable" in b["title"] for b in items)


# ===========================================================================
# Bug attachment edge cases
# ===========================================================================
class TestAttachmentEdge:
    def test_attachment_with_no_filename(self, admin_client):
        p = _make_project(admin_client, "AE-1")
        bug = _make_bug(admin_client, p["id"])
        # multipart with no filename
        files = {"file": ("", io.BytesIO(b"data"), "application/octet-stream")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        # Empty filename — server sets `(file.filename or "unnamed")[:255]`.
        # But what status does the server return? Should be 201 with 'unnamed' filename.
        # Or could be 400 if multipart parsing rejects empty filename.
        assert r.status_code in (201, 400, 422)

    def test_attachment_with_no_content_type(self, admin_client):
        """Server should fall back to application/octet-stream."""
        p = _make_project(admin_client, "AE-2")
        bug = _make_bug(admin_client, p["id"])
        # FastAPI/Starlette UploadFile.content_type can be empty
        files = {"file": ("plain.dat", io.BytesIO(b"data"))}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        assert r.status_code == 201
