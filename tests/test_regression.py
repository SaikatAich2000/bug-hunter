"""
Comprehensive regression test suite for Bug Hunter.

Goal: methodically exercise every documented behavior across:
  - Auth (login, logout, reset, change password, sessions)
  - Users CRUD + permissions
  - Projects CRUD + permissions
  - Bugs CRUD + permissions + filters + pagination + can_edit
  - Comments + attachments
  - Audit trail + stats
  - Edge cases / input validation / security boundaries

Each test is documented with what it's checking. Failures indicate
regressions or latent bugs.
"""
from __future__ import annotations

import io
import time


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------
def _login_admin(c):
    r = c.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert r.status_code == 200, r.text


def _login_as(c, email, password):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text


def _logout(c):
    c.post("/api/auth/logout")


def _create_user(c, name, email, role="user", password="Password1", is_active=True):
    if len(name) < 2:                # safety: server requires name>=2
        name = name + "_user"
    r = c.post("/api/users", json={
        "name": name, "email": email, "role": role,
        "password": password, "is_active": is_active,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _create_project(c, name="P1", color="#c9764f"):
    r = c.post("/api/projects", json={"name": name, "color": color})
    assert r.status_code == 201, r.text
    return r.json()


def _create_bug(c, project_id, title="A Bug Title", **extra):
    if len(title) < 3:               # safety: server requires title>=3
        title = title + "_xx"
    body = {"project_id": project_id, "title": title, "priority": "Medium",
            "environment": "DEV"}
    body.update(extra)
    r = c.post("/api/bugs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# 1. AUTH — sessions, login, logout, /me, change-password, reset
# ===========================================================================
class TestAuth:
    def test_logout_when_already_logged_out_is_204(self, client):
        """Logout from a fresh client (no cookie) must still return 204."""
        r = client.post("/api/auth/logout")
        assert r.status_code == 204

    def test_login_with_uppercase_email_succeeds(self, client):
        """Email case-insensitivity: login with mixed case email."""
        r = client.post("/api/auth/login", json={
            "email": "Admin@Test.LOCAL", "password": "Admin1234",
        })
        assert r.status_code == 200, r.text

    def test_login_with_inactive_user_is_403(self, admin_client):
        """A deactivated user cannot log in — must get 403."""
        u = _create_user(admin_client, "Deact", "deact@x.com",
                         password="Password1", is_active=False)
        _logout(admin_client)
        r = admin_client.post("/api/auth/login", json={
            "email": "deact@x.com", "password": "Password1",
        })
        assert r.status_code == 403, f"got {r.status_code}: {r.text}"

    def test_session_cookie_is_httponly(self, client):
        """Auth cookie must be HttpOnly to prevent XSS theft."""
        r = client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        assert r.status_code == 200
        # Find the session cookie in Set-Cookie headers
        set_cookies = r.headers.get_list("set-cookie")
        bh_cookie = next((c for c in set_cookies if c.startswith("bh_session=")), None)
        assert bh_cookie is not None
        assert "HttpOnly" in bh_cookie, f"cookie missing HttpOnly: {bh_cookie}"

    def test_session_cookie_has_samesite_lax(self, client):
        """Auth cookie must be SameSite=Lax for CSRF defence."""
        r = client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "Admin1234",
        })
        set_cookies = r.headers.get_list("set-cookie")
        bh_cookie = next((c for c in set_cookies if c.startswith("bh_session=")), None)
        assert bh_cookie and "samesite=lax" in bh_cookie.lower()

    def test_change_password_with_short_new_password_fails(self, admin_client):
        """New password must be ≥ 8 chars."""
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234", "new_password": "Short1",
        })
        assert r.status_code == 422, r.text

    def test_change_password_then_old_session_still_valid(self, admin_client):
        """After changing password, current session should remain valid (no forced logout)."""
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234", "new_password": "Newpass789",
        })
        assert r.status_code == 204
        # Same client, same cookie — should still work
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200

    def test_login_with_extremely_long_password(self, client):
        """A 1000-char password should not crash bcrypt (sha256 prehash protects)."""
        r = client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "x" * 1000,
        })
        assert r.status_code in (401, 422)  # rejected, but not 500

    def test_invalid_email_format_login(self, client):
        r = client.post("/api/auth/login", json={
            "email": "notanemail", "password": "Whatever1",
        })
        assert r.status_code == 422

    def test_unauthed_change_password_is_401(self, client):
        r = client.post("/api/auth/change-password", json={
            "current_password": "Admin1234", "new_password": "Newpass1234",
        })
        assert r.status_code == 401

    def test_reset_password_with_invalid_token(self, client):
        r = client.post("/api/auth/reset-password", json={
            "token": "definitely-not-a-real-token", "new_password": "Newpass789",
        })
        assert r.status_code == 400


# ===========================================================================
# 2. USERS — CRUD, permissions, validation
# ===========================================================================
class TestUsers:
    def test_create_user_with_duplicate_email_is_409(self, admin_client):
        _create_user(admin_client, "U1", "dup@x.com")
        r = admin_client.post("/api/users", json={
            "name": "U2", "email": "dup@x.com", "role": "user",
            "password": "Password1",
        })
        assert r.status_code == 409, r.text

    def test_create_user_with_invalid_role(self, admin_client):
        r = admin_client.post("/api/users", json={
            "name": "Bad", "email": "bad@x.com", "role": "superadmin",
            "password": "Password1",
        })
        assert r.status_code == 422

    def test_create_user_short_password(self, admin_client):
        r = admin_client.post("/api/users", json={
            "name": "U", "email": "u@x.com", "role": "user", "password": "abc",
        })
        assert r.status_code == 422

    def test_create_user_empty_password(self, admin_client):
        """Empty password must not silently bypass min-length."""
        r = admin_client.post("/api/users", json={
            "name": "U", "email": "u@x.com", "role": "user", "password": "",
        })
        assert r.status_code == 422

    def test_create_user_invalid_email(self, admin_client):
        r = admin_client.post("/api/users", json={
            "name": "U", "email": "no-at-sign", "role": "user",
            "password": "Password1",
        })
        assert r.status_code == 422

    def test_create_user_with_whitespace_only_name(self, admin_client):
        """Whitespace-only name must be rejected."""
        r = admin_client.post("/api/users", json={
            "name": "    ", "email": "ws@x.com", "role": "user",
            "password": "Password1",
        })
        assert r.status_code == 422

    def test_admin_password_reset_via_admin(self, admin_client):
        """Admin updating a user's password should let the user log in with new pw."""
        u = _create_user(admin_client, "Bob", "bob@x.com", password="OldPass123")
        r = admin_client.put(f"/api/users/{u['id']}", json={"password": "BrandNew99"})
        assert r.status_code == 200, r.text
        _logout(admin_client)
        r = admin_client.post("/api/auth/login", json={
            "email": "bob@x.com", "password": "BrandNew99",
        })
        assert r.status_code == 200

    def test_user_emails_normalized_to_lowercase_on_create(self, admin_client):
        """Email is stored lowercased: must allow login with original case."""
        u = _create_user(admin_client, "Mix", "MixCase@X.COM", password="Password1")
        assert u["email"] == "mixcase@x.com", \
            f"expected lowercased email; got {u['email']!r}"

    def test_user_list_search_is_case_insensitive(self, admin_client):
        _create_user(admin_client, "Charlie", "charlie@x.com")
        r = admin_client.get("/api/users?q=CHARLIE")
        assert r.status_code == 200
        names = [u["name"] for u in r.json()]
        assert "Charlie" in names

    def test_regular_user_cannot_update_users(self, user_client):
        """A regular user must not be able to PUT another user."""
        # admin user has id=1 (bootstrap)
        r = user_client.put("/api/users/1", json={"role": "admin"})
        assert r.status_code == 403

    def test_regular_user_cannot_delete_users(self, user_client):
        r = user_client.delete("/api/users/1")
        assert r.status_code == 403

    def test_get_nonexistent_user_404(self, admin_client):
        r = admin_client.get("/api/users/999999")
        assert r.status_code == 404

    def test_email_is_unique_on_update(self, admin_client):
        """Updating user2's email to user1's email must 409."""
        u1 = _create_user(admin_client, "U1", "one@x.com")
        u2 = _create_user(admin_client, "U2", "two@x.com")
        r = admin_client.put(f"/api/users/{u2['id']}", json={"email": "one@x.com"})
        assert r.status_code == 409, r.text


# ===========================================================================
# 3. PROJECTS — CRUD, permissions, edge cases
# ===========================================================================
class TestProjects:
    def test_create_project_with_invalid_color(self, admin_client):
        """Color must match #RRGGBB."""
        r = admin_client.post("/api/projects", json={
            "name": "BadColor", "color": "red",
        })
        assert r.status_code == 422

    def test_create_project_with_3char_hex_color_rejected(self, admin_client):
        """Schema says #RRGGBB only — #fff (3-hex) should be rejected."""
        r = admin_client.post("/api/projects", json={
            "name": "ShortHex", "color": "#fff",
        })
        assert r.status_code == 422

    def test_create_project_with_duplicate_name_is_409(self, admin_client):
        _create_project(admin_client, name="Unique")
        r = admin_client.post("/api/projects", json={"name": "Unique"})
        assert r.status_code == 409

    def test_delete_project_with_bugs_is_409(self, admin_client):
        p = _create_project(admin_client, name="HasBugs")
        _create_bug(admin_client, p["id"])
        r = admin_client.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 409
        assert "bug" in r.json()["detail"].lower()

    def test_update_nonexistent_project_404(self, admin_client):
        r = admin_client.put("/api/projects/999999", json={"name": "Whatever"})
        assert r.status_code == 404

    def test_manager_can_create_project(self, admin_client):
        _create_user(admin_client, "Mgr", "mgr@x.com", role="manager",
                     password="Password1")
        _logout(admin_client)
        _login_as(admin_client, "mgr@x.com", "Password1")
        r = admin_client.post("/api/projects", json={"name": "MgrProject"})
        assert r.status_code == 201

    def test_regular_user_cannot_update_project(self, user_client):
        r = user_client.put("/api/projects/1", json={"name": "Hacked"})
        assert r.status_code == 403

    def test_project_name_too_short(self, admin_client):
        """Schema says min_length=2."""
        r = admin_client.post("/api/projects", json={"name": "A"})
        assert r.status_code == 422


# ===========================================================================
# 4. BUGS — CRUD, validation, can_edit, filters
# ===========================================================================
class TestBugs:
    def test_create_bug_with_nonexistent_project(self, admin_client):
        r = admin_client.post("/api/bugs", json={
            "project_id": 999999, "title": "Doomed",
            "priority": "Low", "environment": "DEV",
        })
        assert r.status_code == 400

    def test_create_bug_with_invalid_status_normalizes(self, admin_client):
        """Status normalization: 'new' should become 'New'."""
        p = _create_project(admin_client, name="N1")
        bug = _create_bug(admin_client, p["id"], status="new")
        assert bug["status"] == "New", \
            f"expected status normalized to 'New', got {bug['status']!r}"

    def test_create_bug_with_invalid_status(self, admin_client):
        p = _create_project(admin_client, name="N2")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "X",
            "priority": "Medium", "environment": "DEV",
            "status": "Bogus",
        })
        assert r.status_code == 422

    def test_create_bug_with_invalid_environment(self, admin_client):
        p = _create_project(admin_client, name="N3")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "X",
            "priority": "Medium", "environment": "QA",
        })
        assert r.status_code == 422

    def test_create_bug_with_invalid_due_date_format(self, admin_client):
        p = _create_project(admin_client, name="N4")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "X",
            "priority": "Medium", "environment": "DEV",
            "due_date": "31/12/2025",
        })
        assert r.status_code == 422

    def test_create_bug_with_unknown_assignee_id(self, admin_client):
        p = _create_project(admin_client, name="N5")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Long enough title",
            "priority": "Medium", "environment": "DEV",
            "assignee_ids": [999999],
        })
        assert r.status_code == 400

    def test_assignee_ids_dedup(self, admin_client):
        p = _create_project(admin_client, name="N6")
        u = _create_user(admin_client, "A", "a@x.com")
        bug = _create_bug(admin_client, p["id"], assignee_ids=[u["id"], u["id"], u["id"]])
        assert len(bug["assignees"]) == 1

    def test_regular_user_cant_set_other_user_as_reporter(self, admin_client):
        """Regular users can only file as themselves."""
        p = _create_project(admin_client, name="N7")
        _create_user(admin_client, "Other", "other@x.com")
        _create_user(admin_client, "Reg", "reg@x.com", password="Password1")
        _logout(admin_client)
        _login_as(admin_client, "reg@x.com", "Password1")
        # other id = 2 or so (bootstrap=1, other=2 if first)
        users_resp = admin_client.get("/api/users").json()
        other_id = next(u["id"] for u in users_resp if u["email"] == "other@x.com")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Sneaky",
            "priority": "Low", "environment": "DEV",
            "reporter_id": other_id,
        })
        assert r.status_code == 403

    def test_regular_user_can_self_report(self, admin_client):
        """Regular user passing their own id as reporter is fine."""
        p = _create_project(admin_client, name="N8")
        u = _create_user(admin_client, "Self", "self@x.com", password="Password1")
        _logout(admin_client)
        _login_as(admin_client, "self@x.com", "Password1")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "Mine",
            "priority": "Low", "environment": "DEV",
            "reporter_id": u["id"],
        })
        assert r.status_code == 201

    def test_get_nonexistent_bug_404(self, admin_client):
        r = admin_client.get("/api/bugs/999999")
        assert r.status_code == 404

    def test_delete_bug_returns_404_after(self, admin_client):
        p = _create_project(admin_client, name="N9")
        bug = _create_bug(admin_client, p["id"])
        admin_client.delete(f"/api/bugs/{bug['id']}")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.status_code == 404

    def test_bug_list_search_by_id_with_hash(self, admin_client):
        p = _create_project(admin_client, name="N10")
        bug = _create_bug(admin_client, p["id"], title="Findme")
        r = admin_client.get(f"/api/bugs?q=%23{bug['id']}")  # %23 = '#'
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == bug["id"]

    def test_bug_list_search_by_id_without_hash(self, admin_client):
        p = _create_project(admin_client, name="N11")
        bug = _create_bug(admin_client, p["id"], title="Findme2")
        r = admin_client.get(f"/api/bugs?q={bug['id']}")
        items = r.json()["items"]
        assert any(b["id"] == bug["id"] for b in items)

    def test_bug_list_search_text(self, admin_client):
        p = _create_project(admin_client, name="N12")
        _create_bug(admin_client, p["id"], title="UNIQUE_NEEDLE_string")
        r = admin_client.get("/api/bugs?q=needle")
        items = r.json()["items"]
        assert any("UNIQUE_NEEDLE_string" in b["title"] for b in items)

    def test_bug_list_invalid_pagination(self, admin_client):
        r = admin_client.get("/api/bugs?page=0")
        assert r.status_code == 400
        r = admin_client.get("/api/bugs?page_size=999")
        assert r.status_code == 400

    def test_bug_list_pagination_total_pages(self, admin_client):
        p = _create_project(admin_client, name="N13")
        for i in range(5):
            _create_bug(admin_client, p["id"], title=f"bug{i}")
        r = admin_client.get("/api/bugs?page=1&page_size=2")
        body = r.json()
        assert body["total"] >= 5
        assert body["pages"] == (body["total"] + 1) // 2

    def test_bug_filter_combinations(self, admin_client):
        p1 = _create_project(admin_client, name="P-A")
        p2 = _create_project(admin_client, name="P-B")
        _create_bug(admin_client, p1["id"], title="bug-one", priority="High", environment="DEV")
        _create_bug(admin_client, p1["id"], title="bug-two", priority="Low", environment="UAT")
        _create_bug(admin_client, p2["id"], title="bug-three", priority="High", environment="PROD")
        r = admin_client.get(f"/api/bugs?project_id={p1['id']}&priority=High")
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["title"] == "bug-one"

    def test_bug_update_invalid_field_value(self, admin_client):
        p = _create_project(admin_client, name="N14")
        bug = _create_bug(admin_client, p["id"])
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "Garbage"})
        assert r.status_code == 422

    def test_bug_update_to_same_reporter_should_not_fail_for_user(self, admin_client):
        """Regression: user re-submitting form with their own reporter_id should work,
        not 403 with "Only admins or managers can change the reporter"."""
        p = _create_project(admin_client, name="N15")
        u = _create_user(admin_client, "Owner", "owner@x.com", password="Password1")
        _logout(admin_client)
        _login_as(admin_client, "owner@x.com", "Password1")
        bug = _create_bug(admin_client, p["id"], title="mine")
        # Now PUT with same reporter_id
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "title": "renamed",
            "reporter_id": u["id"],
        })
        assert r.status_code == 200, r.text  # would 403 if buggy

    def test_bug_clear_due_date_with_empty_string(self, admin_client):
        """Frontend sends '' when due date is cleared. Should accept and store None."""
        p = _create_project(admin_client, name="N16")
        bug = _create_bug(admin_client, p["id"], due_date="2025-01-01")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"due_date": ""})
        assert r.status_code == 200, r.text
        assert r.json()["due_date"] is None

    def test_user_can_edit_bug_they_are_assignee_of(self, admin_client):
        """Regression: assignees should be able to edit; previously can_edit only checked reporter."""
        p = _create_project(admin_client, name="N17")
        u = _create_user(admin_client, "Helper", "helper@x.com", password="Password1")
        bug = _create_bug(admin_client, p["id"], assignee_ids=[u["id"]])
        _logout(admin_client)
        _login_as(admin_client, "helper@x.com", "Password1")
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "In Progress"})
        assert r.status_code == 200

    def test_csv_export_works(self, admin_client):
        p = _create_project(admin_client, name="N18")
        _create_bug(admin_client, p["id"], title="csv-test")
        r = admin_client.get("/api/bugs/export.csv")
        assert r.status_code == 200
        text = r.text
        assert "csv-test" in text
        assert text.startswith("id,project,title")


# ===========================================================================
# 5. COMMENTS + ATTACHMENTS
# ===========================================================================
class TestCommentsAttachments:
    def test_comment_on_nonexistent_bug(self, admin_client):
        r = admin_client.post("/api/bugs/999999/comments", json={"body": "hi"})
        assert r.status_code == 404

    def test_comment_with_empty_body_rejected(self, admin_client):
        p = _create_project(admin_client, name="C1")
        bug = _create_bug(admin_client, p["id"])
        r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": ""})
        assert r.status_code == 422

    def test_comment_with_only_whitespace_rejected(self, admin_client):
        p = _create_project(admin_client, name="C2")
        bug = _create_bug(admin_client, p["id"])
        r = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "   "})
        assert r.status_code == 422

    def test_attachment_empty_file_rejected(self, admin_client):
        p = _create_project(admin_client, name="C3")
        bug = _create_bug(admin_client, p["id"])
        files = {"file": ("empty.txt", io.BytesIO(b""), "text/plain")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        assert r.status_code == 400

    def test_attachment_for_wrong_comment_id(self, admin_client):
        p = _create_project(admin_client, name="C4")
        bug1 = _create_bug(admin_client, p["id"])
        bug2 = _create_bug(admin_client, p["id"], title="bug2")
        # Add a comment to bug2
        cr = admin_client.post(f"/api/bugs/{bug2['id']}/comments", json={"body": "x"})
        cid = cr.json()["id"]
        # Try to attach to bug1 with bug2's comment_id — should 400
        files = {"file": ("a.txt", io.BytesIO(b"x"), "text/plain")}
        r = admin_client.post(
            f"/api/bugs/{bug1['id']}/attachments",
            files=files, data={"comment_id": str(cid)},
        )
        assert r.status_code == 400

    def test_download_attachment_works(self, admin_client):
        p = _create_project(admin_client, name="C5")
        bug = _create_bug(admin_client, p["id"])
        files = {"file": ("data.bin", io.BytesIO(b"Hello World"), "application/octet-stream")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.status_code == 200
        assert d.content == b"Hello World"

    def test_download_attachment_wrong_bug(self, admin_client):
        """Cross-bug access: downloading bug2's attachment via bug1's URL must 404."""
        p = _create_project(admin_client, name="C6")
        bug1 = _create_bug(admin_client, p["id"])
        bug2 = _create_bug(admin_client, p["id"], title="b2")
        files = {"file": ("x.txt", io.BytesIO(b"secret"), "text/plain")}
        r = admin_client.post(f"/api/bugs/{bug2['id']}/attachments", files=files)
        att_id = r.json()["id"]
        d = admin_client.get(f"/api/bugs/{bug1['id']}/attachments/{att_id}/download")
        assert d.status_code == 404

    def test_attachment_filename_with_special_chars(self, admin_client):
        """Filenames with quotes can break Content-Disposition header."""
        p = _create_project(admin_client, name="C8")
        bug = _create_bug(admin_client, p["id"])
        files = {"file": ('weird"name.txt', io.BytesIO(b"x"), "text/plain")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        # Upload may succeed; downloading might produce a malformed header
        if r.status_code == 201:
            att_id = r.json()["id"]
            d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
            # Should not crash with 500
            assert d.status_code != 500, "header injection from filename crashed server"

    def test_user_can_delete_their_own_attachment_even_if_cant_edit_bug(self, admin_client):
        """Uploader rule: if I uploaded it, I can delete it (per code line 638-641)."""
        p = _create_project(admin_client, name="C9")
        # Admin creates bug
        bug = _create_bug(admin_client, p["id"])
        # User uploads to it (admin assigned them)
        u = _create_user(admin_client, "Up", "up@x.com", password="Password1")
        admin_client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [u["id"]]})
        _logout(admin_client)
        _login_as(admin_client, "up@x.com", "Password1")
        files = {"file": ("u.txt", io.BytesIO(b"u"), "text/plain")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        att_id = r.json()["id"]
        # Remove user as assignee
        _logout(admin_client)
        _login_as(admin_client, "admin@test.local", "Admin1234")
        admin_client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": []})
        _logout(admin_client)
        _login_as(admin_client, "up@x.com", "Password1")
        # Should still be able to delete own attachment
        d = admin_client.delete(f"/api/bugs/{bug['id']}/attachments/{att_id}")
        assert d.status_code == 200

    def test_attachment_count_increments(self, admin_client):
        p = _create_project(admin_client, name="C10")
        bug = _create_bug(admin_client, p["id"])
        # Initial bug list should show 0
        files = {"file": ("a.txt", io.BytesIO(b"a"), "text/plain")}
        admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        # Re-fetch
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["attachment_count"] == 2


# ===========================================================================
# 6. STATS + AUDIT
# ===========================================================================
class TestStatsAudit:
    def test_stats_includes_recent_bugs_in_timeline(self, admin_client):
        p = _create_project(admin_client, name="ST1")
        _create_bug(admin_client, p["id"])
        r = admin_client.get("/api/stats")
        body = r.json()
        assert isinstance(body["timeline"], list)
        assert len(body["timeline"]) == 14
        assert sum(d["count"] for d in body["timeline"]) >= 1

    def test_audit_filter_by_entity_type(self, admin_client):
        p = _create_project(admin_client, name="ST2")
        r = admin_client.get("/api/audit?entity_type=project")
        assert r.status_code == 200
        rows = r.json()
        assert all(row["entity_type"] == "project" for row in rows)
        assert any(row["action"] == "project_created" for row in rows)

    def test_audit_search_by_text(self, admin_client):
        p = _create_project(admin_client, name="UniqueAuditMarker_xyz")
        r = admin_client.get("/api/audit?q=UniqueAuditMarker_xyz")
        rows = r.json()
        assert len(rows) >= 1

    def test_bug_delete_creates_audit_record(self, admin_client):
        p = _create_project(admin_client, name="ST3")
        bug = _create_bug(admin_client, p["id"], title="DELETED_MARKER_777")
        admin_client.delete(f"/api/bugs/{bug['id']}")
        # Per source: audit record uses bug_id=None to survive cascade
        r = admin_client.get("/api/audit?q=DELETED_MARKER_777")
        rows = r.json()
        assert any(row["action"] == "bug_deleted" for row in rows), \
            "Bug delete didn't create a non-bug audit record"


# ===========================================================================
# 7. EDGE CASES — odd input patterns
# ===========================================================================
class TestEdgeCases:
    def test_title_with_only_whitespace_rejected(self, admin_client):
        """A title that is whitespace-only should be rejected as empty."""
        p = _create_project(admin_client, name="E1")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "         ",
            "priority": "Medium", "environment": "DEV",
        })
        assert r.status_code == 422, f"got {r.status_code}: {r.text}"

    def test_title_with_padded_whitespace_below_min_length(self, admin_client):
        """BUG-suspect: '  a  ' has length 5 (passes min_length=3) but trims to 'a'.
        Spec says title must be >= 3 chars. Server should still reject after stripping."""
        p = _create_project(admin_client, name="E2")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "  a  ",   # raw len=5, trimmed=1
            "priority": "Medium", "environment": "DEV",
        })
        # If 422, the validation chain works correctly (validator should re-check length).
        # If 201, title 'a' was created, violating the spec.
        assert r.status_code == 422, \
            f"BUG: trim-after-min_length lets 1-char title through. status={r.status_code}, body={r.text}"

    def test_bug_create_with_empty_assignees(self, admin_client):
        p = _create_project(admin_client, name="E3")
        bug = _create_bug(admin_client, p["id"], assignee_ids=[])
        assert bug["assignees"] == []

    def test_bug_update_with_empty_dict(self, admin_client):
        """No-op update — should not error."""
        p = _create_project(admin_client, name="E4")
        bug = _create_bug(admin_client, p["id"])
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={})
        assert r.status_code == 200

    def test_unicode_in_title_and_description(self, admin_client):
        p = _create_project(admin_client, name="E5")
        bug = _create_bug(admin_client, p["id"], title="🐛 émoji bug ✨",
                          description="Naïve café résumé — Ω₃")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert "🐛" in r.json()["title"]

    def test_concurrent_audit_log_rows(self, admin_client):
        """Two consecutive actions in same second should both appear in audit."""
        p = _create_project(admin_client, name="E6")
        _create_bug(admin_client, p["id"], title="x1")
        _create_bug(admin_client, p["id"], title="x2")
        r = admin_client.get("/api/audit?entity_type=bug&limit=10")
        rows = r.json()
        assert sum(1 for r in rows if r["action"] == "bug_created") >= 2

    def test_user_can_be_assignee_of_their_own_reported_bug(self, admin_client):
        p = _create_project(admin_client, name="E7")
        u = _create_user(admin_client, "Self", "self@x.com", password="Password1")
        bug = _create_bug(admin_client, p["id"], reporter_id=u["id"],
                          assignee_ids=[u["id"]])
        assert bug["reporter"]["id"] == u["id"]
        assert any(a["id"] == u["id"] for a in bug["assignees"])

    def test_negative_user_id_in_filters(self, admin_client):
        """Filter with bogus user id should return empty list, not 500."""
        r = admin_client.get("/api/bugs?assignee_id=-1")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_large_search_query(self, admin_client):
        """Search with very long string should not crash."""
        r = admin_client.get("/api/bugs?q=" + "x" * 5000)
        assert r.status_code == 200

    def test_bug_status_case_insensitive_filter(self, admin_client):
        """List-filter case sensitivity: ?status=new vs ?status=New.
        Database stores 'New' (canonical). Searching by 'new' should match — or be documented."""
        p = _create_project(admin_client, name="E8")
        _create_bug(admin_client, p["id"], status="New")
        # Test with canonical first (must work)
        r = admin_client.get("/api/bugs?status=New")
        n_canonical = r.json()["total"]
        assert n_canonical >= 1
        # Test with lowercase
        r = admin_client.get("/api/bugs?status=new")
        n_lower = r.json()["total"]
        # If lower returns 0 but canonical > 0, the filter is case-sensitive (mismatch with creation,
        # which IS case-insensitive per `_normalize_choice`).
        assert n_lower == n_canonical, \
            f"Filter is case-SENSITIVE but creation is case-INSENSITIVE: " \
            f"created with 'new'/found 'New' but filter ?status=new yields {n_lower} vs {n_canonical}"


# ===========================================================================
# 8. SECURITY-FOCUSED REGRESSION
# ===========================================================================
class TestSecurity:
    def test_login_does_not_leak_user_existence(self, client):
        """Two distinct error states (no such user / wrong password) must give same error."""
        r1 = client.post("/api/auth/login", json={
            "email": "doesnotexist@nowhere.test", "password": "Whatever1",
        })
        r2 = client.post("/api/auth/login", json={
            "email": "admin@test.local", "password": "wrongpass",
        })
        assert r1.status_code == 401 and r2.status_code == 401
        assert r1.json()["detail"] == r2.json()["detail"]

    def test_unauth_csv_export_blocked(self, client):
        r = client.get("/api/bugs/export.csv")
        assert r.status_code == 401

    def test_attachment_download_requires_auth(self, admin_client):
        """Attachment download must require an active session.
        Verify by logging out and re-attempting."""
        p = _create_project(admin_client, name="S1")
        bug = _create_bug(admin_client, p["id"])
        files = {"file": ("a.txt", io.BytesIO(b"secret"), "text/plain")}
        r = admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        att_id = r.json()["id"]
        # Logout — now the same TestClient has no valid cookie
        admin_client.post("/api/auth/logout")
        d = admin_client.get(f"/api/bugs/{bug['id']}/attachments/{att_id}/download")
        assert d.status_code == 401

    def test_audit_endpoint_is_visible_to_any_user(self, user_client):
        """Per README: audit trail is part of the app, but no role restriction in code.
        Verify the current behavior (any authenticated user can see audit)."""
        r = user_client.get("/api/audit")
        # No role check in code → any user gets 200. If you wanted admin-only, this would
        # be a finding to harden later. Not failing the test, just documenting.
        assert r.status_code == 200

    def test_xss_in_bug_title_is_stored_as_is(self, admin_client):
        """Server stores raw — frontend must escape. We just assert the API doesn't double-escape."""
        p = _create_project(admin_client, name="S2")
        bug = _create_bug(admin_client, p["id"], title="<script>alert(1)</script>")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["title"] == "<script>alert(1)</script>"  # stored verbatim


# ===========================================================================
# 9. DATA INTEGRITY across cascading deletes
# ===========================================================================
class TestCascades:
    def test_delete_user_who_is_reporter_sets_reporter_null(self, admin_client):
        p = _create_project(admin_client, name="CA1")
        u = _create_user(admin_client, "Will", "will@x.com")
        bug = _create_bug(admin_client, p["id"], reporter_id=u["id"])
        admin_client.delete(f"/api/users/{u['id']}")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["reporter"] is None

    def test_delete_user_who_is_assignee_removes_assignment(self, admin_client):
        p = _create_project(admin_client, name="CA2")
        u = _create_user(admin_client, "A", "asg@x.com")
        bug = _create_bug(admin_client, p["id"], assignee_ids=[u["id"]])
        admin_client.delete(f"/api/users/{u['id']}")
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["assignees"] == []

    def test_delete_bug_cascades_comments_and_attachments(self, admin_client):
        p = _create_project(admin_client, name="CA3")
        bug = _create_bug(admin_client, p["id"])
        admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "hi"})
        files = {"file": ("a.txt", io.BytesIO(b"a"), "text/plain")}
        admin_client.post(f"/api/bugs/{bug['id']}/attachments", files=files)
        admin_client.delete(f"/api/bugs/{bug['id']}")
        # After delete, comments / attachments are gone with their parent
        r = admin_client.get(f"/api/bugs/{bug['id']}/comments")
        assert r.status_code == 404
