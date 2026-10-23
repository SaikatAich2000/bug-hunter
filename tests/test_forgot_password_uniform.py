"""Forgot-password does the same work in the request for a known and an unknown address: one
lookup, then everything else after the response, so timing cannot reveal which accounts exist."""
from __future__ import annotations


def test_known_and_unknown_addresses_take_the_same_request_path(client, monkeypatch):
    import app.routes.auth as auth_routes
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    scheduled = []
    monkeypatch.setattr(auth_routes, "_issue_password_reset", lambda user_id, email: scheduled.append((user_id, email)))

    known = client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
    unknown = client.post("/api/auth/forgot-password", json={"email": "ghost@test.local"})

    assert known.status_code == unknown.status_code == 204
    assert known.content == unknown.content == b""
    assert [e for _, e in scheduled] == ["admin@test.local", "ghost@test.local"]
    assert scheduled[0][0] is not None and scheduled[1][0] is None
    with SessionLocal() as db:
        assert db.query(PasswordResetToken).count() == 0  # the request itself wrote nothing


def test_the_real_account_still_gets_its_token_and_the_unknown_one_none(client):
    from app.database import SessionLocal
    from app.models import PasswordResetToken

    client.post("/api/auth/forgot-password", json={"email": "ghost@test.local"})
    with SessionLocal() as db:
        assert db.query(PasswordResetToken).count() == 0
    client.post("/api/auth/forgot-password", json={"email": "admin@test.local"})
    with SessionLocal() as db:
        assert db.query(PasswordResetToken).count() == 1
