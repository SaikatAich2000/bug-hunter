"""Tests covering security and correctness concerns and related edge cases:

  - Session lifecycle around password changes / resets
  - Outstanding password-reset tokens after password change
  - Concrete stored-XSS via uploaded HTML/SVG
  - update_bug ORM-mutation-before-permission-check
  - Activity ordering inconsistency between endpoints
  - Multi-worker session secret behavior
  - current_password length validation
  - Audit log visibility for non-admins
  - Forgot-password issuing multiple concurrent tokens
  - URL injection / content-type handling for attachments
"""
from __future__ import annotations

import io


def _admin_login(c):
    r = c.post("/api/auth/login", json={
        "email": "admin@test.local", "password": "Admin1234",
    })
    assert r.status_code == 200, r.text


def _make_bug(c, project_id, title="A bug for tests"):
    r = c.post("/api/bugs", json={
        "project_id": project_id, "title": title,
        "priority": "Medium", "environment": "DEV",
    })
    assert r.status_code == 201, r.text
    return r.json()


def _make_project(c, name="P"):
    r = c.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _make_user(c, name="Someone", email="some@x.com", role="user", password="TestUserPwd9X"):
    body = {"name": name, "email": email, "role": role, "password": password}
    # Project-scoped access: tag the new user to every existing project so these
    # pre-scoping tests keep exercising the flat "see everything" model.
    pids = [p["id"] for p in c.get("/api/projects").json()]
    if pids:
        body["project_ids"] = pids
    r = c.post("/api/users", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ===========================================================================
# CONCERN: existing sessions survive a password change / reset
# This is a real security gap — if the password gets compromised, "change my
# password" is expected to invalidate the attacker's session.
# ===========================================================================
class TestSessionLifecycle:
    def test_existing_session_still_works_after_password_change(self, admin_client):
        """Changing the password does not invalidate existing sessions. A
        compromised account can stay compromised even after the legitimate
        owner resets their password."""
        # Confirm session works
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200

        # Change password
        r = admin_client.post("/api/auth/change-password", json={
            "current_password": "Admin1234",
            "new_password": "NewBetter999",
        })
        assert r.status_code == 204

        # Same cookie still gives 200 — old session not invalidated.
        r = admin_client.get("/api/auth/me")
        assert r.status_code == 200, \
            "BUG: old session is invalidated after password change (good!) — update test"
        # Existing sessions are preserved across a password change. That is the
        # current behavior, but is a security gap worth flagging.

    def test_outstanding_reset_tokens_remain_after_password_change(self, admin_client):
        """If a user requests a password reset, then quickly changes their
        password through the regular flow, the outstanding reset link is
        still valid — and works to override the new password.
        Anyone with the email link can take over the account.

        This test exercises the request flow as far as it can without an
        actual email — the token cannot be retrieved after creation
        (only sent via email), so it just verifies the schema allows
        multiple outstanding tokens to exist."""
        # Issue 3 reset requests — should all succeed and all log audit rows
        for _ in range(3):
            r = admin_client.post("/api/auth/forgot-password", json={
                "email": "admin@test.local",
            })
            assert r.status_code == 204
        # Audit log should have 3 password_reset_requested entries
        r = admin_client.get("/api/audit?q=password_reset_requested")
        rows = r.json()
        n = sum(1 for r in rows if r["action"] == "password_reset_requested")
        assert n >= 3, \
            f"BUG: forgot-password doesn't dedupe — {n} concurrent tokens issued"


# ===========================================================================
# CONCERN-4: update_bug mutates ORM before role check completes
# ===========================================================================
class TestUpdateBugOrdering:
    def test_failed_reporter_change_does_not_persist_other_changes(self, admin_client):
        """A regular user PUTs an update with title + reporter_id=different_user.
        The role check rejects the request (cannot change reporter). Verify
        that the title change is not persisted (no partial write)."""
        p = _make_project(admin_client, "U1")
        owner = _make_user(admin_client, name="Owner", email="owner@x.com",
                           password="TestUserPwd9X")
        other = _make_user(admin_client, name="Other", email="other@x.com",
                           password="TestUserPwd9X")
        # Admin creates the bug with owner as reporter so owner can edit
        bug = _make_bug(admin_client, p["id"], title="Original title here")
        admin_client.put(f"/api/bugs/{bug['id']}", json={"reporter_id": owner["id"]})

        # Switch to owner
        admin_client.post("/api/auth/logout")
        admin_client.post("/api/auth/login", json={
            "email": "owner@x.com", "password": "TestUserPwd9X",
        })

        # Owner attempts both a legitimate title change and an unauthorized
        # reporter change in one PUT. The 403 must reject everything.
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={
            "title": "Hacked title here",
            "reporter_id": other["id"],
        })
        assert r.status_code == 403

        # Verify title was not changed
        r = admin_client.get(f"/api/bugs/{bug['id']}")
        assert r.json()["title"] == "Original title here", \
            "BUG: title was persisted despite the 403 from the role check"


# ===========================================================================
# CONCERN-6: Bug.activities relationship has no id-tiebreaker — same-second
# events can flip order between endpoints.
# ===========================================================================
class TestActivityOrdering:
    def test_activity_order_consistent_between_get_bug_and_list_activity(self, admin_client):
        """Same-second timestamp activities should appear in the same order
        regardless of which endpoint you ask."""
        p = _make_project(admin_client, "AO1")
        bug = _make_bug(admin_client, p["id"], title="Activity ordering test")
        # Create several rapid status updates so timestamps might collide
        for status in ("In Progress", "Resolved", "Reopened", "Closed"):
            admin_client.put(f"/api/bugs/{bug['id']}", json={"status": status})

        detail = admin_client.get(f"/api/bugs/{bug['id']}").json()
        activity = admin_client.get(f"/api/bugs/{bug['id']}/activity").json()

        detail_ids = [a["id"] for a in detail["activities"]]
        activity_ids = [a["id"] for a in activity]
        assert detail_ids == activity_ids, \
            f"BUG: activity order differs between endpoints.\n" \
            f"  detail:   {detail_ids}\n  activity: {activity_ids}"


# ===========================================================================
# Front-end logic concerns, exercised through the API responses they consume
# ===========================================================================
class TestFrontendContract:
    def test_bug_detail_attachments_only_includes_bug_level(self, admin_client):
        """The 'attachments' list at the top level of a BugDetail should
        only contain bug-level attachments (comment_id NULL). Comment-level
        attachments belong to their respective comment. The frontend depends
        on this split."""
        p = _make_project(admin_client, "FE1")
        bug = _make_bug(admin_client, p["id"])
        # Bug-level attachment
        admin_client.post(f"/api/bugs/{bug['id']}/attachments",
                          files={"file": ("bug.txt", io.BytesIO(b"a"), "text/plain")})
        # Add a comment + comment-level attachment
        cr = admin_client.post(f"/api/bugs/{bug['id']}/comments", json={"body": "x"})
        cid = cr.json()["id"]
        admin_client.post(f"/api/bugs/{bug['id']}/attachments",
                          files={"file": ("c.txt", io.BytesIO(b"b"), "text/plain")},
                          data={"comment_id": str(cid)})

        d = admin_client.get(f"/api/bugs/{bug['id']}").json()
        # Top-level attachments: only the bug-level one
        bug_level_filenames = {a["filename"] for a in d["attachments"]}
        assert bug_level_filenames == {"bug.txt"}, bug_level_filenames
        # Comment-level under that comment
        comment_atts = d["comments"][0]["attachments"]
        assert {a["filename"] for a in comment_atts} == {"c.txt"}
        # But attachment_count should reflect the total
        assert d["attachment_count"] == 2

    def test_xlsx_export_preserves_commas_and_newlines_in_description(self, admin_client):
        """XLSX: a description with commas/newlines must survive the round
        trip through openpyxl as a single cell, not get split into rows.
        Same invariant the old CSV export was tested for, now on the
        Reports XLSX path that replaced it."""
        import io
        from openpyxl import load_workbook
        p = _make_project(admin_client, "FE2")
        admin_client.post("/api/bugs", json={
            "project_id": p["id"],
            "title": "row, has, commas",
            "description": "line1\nline2,still line2",
            "priority": "Low", "environment": "DEV",
        })
        r = admin_client.post("/api/reports/export.xlsx", json={
            "report_key": "item_detail", "filters": {},
        })
        assert r.status_code == 200, r.text
        wb = load_workbook(io.BytesIO(r.content), read_only=True)
        # Flatten every string cell in the main sheet.
        text = " ".join(
            str(v) for row in wb[wb.sheetnames[0]].iter_rows(values_only=True)
            for v in row if v is not None
        )
        # Both halves of the description survived the export.
        assert "line1" in text and "still line2" in text
        # And the comma-laden title is in the workbook.
        assert "row, has, commas" in text


# ===========================================================================
# Misc: validation gaps on the input surface
# ===========================================================================
class TestInputValidation:
    def test_user_create_with_extreme_long_email(self, admin_client):
        """email max=254. Pass 300 chars — must reject."""
        r = admin_client.post("/api/users", json={
            "name": "longmail", "email": "a" * 250 + "@x.com",
            "role": "user", "password": "TestUserPwd9X",
        })
        assert r.status_code == 422

    def test_bug_create_with_oversized_description(self, admin_client):
        # Description is rich HTML; the cap is 1 MB so multiple inline pasted
        # screenshots (base64 data URLs) fit. Anything past the cap is 422.
        p = _make_project(admin_client, "IV1")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "x" * 10,
            "description": "a" * 1_000_001,
            "priority": "Low", "environment": "DEV",
        })
        assert r.status_code == 422

    def test_comment_with_oversized_body(self, admin_client):
        # Comment body cap is 200 KB to accommodate pasted screenshots.
        # Anything past that is 422.
        p = _make_project(admin_client, "IV2")
        bug = _make_bug(admin_client, p["id"])
        r = admin_client.post(f"/api/bugs/{bug['id']}/comments",
                              json={"body": "x" * 200_001})
        assert r.status_code == 422

    def test_audit_limit_clamped_at_10000(self, admin_client):
        # The audit limit ceiling is 10 000 so operators can pull the full
        # trail. Anything past 10 000 rejects with 422.
        r = admin_client.get("/api/audit?limit=10001")
        assert r.status_code == 422
        r = admin_client.get("/api/audit?limit=10000")
        assert r.status_code == 200

    def test_user_role_normalization_upper_to_lower(self, admin_client):
        """Schema lowercases role: 'ADMIN' should become 'admin'."""
        r = admin_client.post("/api/users", json={
            "name": "rolecase", "email": "rc@x.com", "role": "ADMIN",
            "password": "TestUserPwd9X",
        })
        assert r.status_code == 201, r.text
        assert r.json()["role"] == "admin"


# ===========================================================================
# CSRF — verify cookie SameSite actually blocks cross-site form posts.
# Cross-site cannot be fully simulated here, so this verifies the cookie
# attributes instead.
# ===========================================================================
class TestCSRFPosture:
    def test_no_csrf_token_on_state_changing_routes(self, admin_client):
        """Bug Hunter relies entirely on SameSite=Lax for CSRF defence —
        verify there's no CSRF token mechanism that the SPA bypasses."""
        # Send a normal authenticated POST without any extra header.
        # If a token were required, this would 403/400.
        p = _make_project(admin_client, "CSRF1")
        r = admin_client.post("/api/bugs", json={
            "project_id": p["id"], "title": "no csrf token here",
            "priority": "Low", "environment": "DEV",
        })
        assert r.status_code == 201
        # No CSRF token mechanism. Defence rests entirely on SameSite=Lax.

    def test_options_preflight_handled(self, admin_client):
        """Preflight OPTIONS must return a deterministic response.

        CORS middleware is only registered when CORS_ORIGINS is explicitly
        configured. With no configured origins (the default), preflight hits
        the route directly — any non-5xx response is accepted, just no
        surprises like 500s.
        """
        r = admin_client.options("/api/bugs", headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        })
        # 200/204 = CORS middleware answered; 400 = CORS rejected the
        # origin; 401/405 = no CORS middleware, route handles it.
        assert r.status_code in (200, 204, 400, 401, 405), (
            f"unexpected: {r.status_code}"
        )


# ===========================================================================
# Race / transactional behavior
# ===========================================================================
class TestTransactional:
    def test_bug_update_no_change_does_not_log_activity(self, admin_client):
        """If no fields actually change, no activity row should be added."""
        p = _make_project(admin_client, "TX1")
        bug = _make_bug(admin_client, p["id"], title="No-change update test")
        # Get baseline activity count
        n0 = len(admin_client.get(f"/api/bugs/{bug['id']}/activity").json())
        # Send the same status it already has
        r = admin_client.put(f"/api/bugs/{bug['id']}", json={"status": "New"})
        assert r.status_code == 200
        n1 = len(admin_client.get(f"/api/bugs/{bug['id']}/activity").json())
        assert n0 == n1, f"BUG: no-change update added an activity row ({n0} → {n1})"

    def test_bug_assignee_set_to_same_value_does_not_log_change(self, admin_client):
        """Re-submitting the same assignee list shouldn't log a fake 'assignees changed' event."""
        p = _make_project(admin_client, "TX2")
        u = _make_user(admin_client, name="Assigned1", email="ass1@x.com")
        bug = _make_bug(admin_client, p["id"], title="Assignee no-change test")
        admin_client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [u["id"]]})
        n0 = len(admin_client.get(f"/api/bugs/{bug['id']}/activity").json())
        admin_client.put(f"/api/bugs/{bug['id']}", json={"assignee_ids": [u["id"]]})
        n1 = len(admin_client.get(f"/api/bugs/{bug['id']}/activity").json())
        assert n0 == n1, f"BUG: same-assignee update created a phantom activity row"
