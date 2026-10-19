"""Infra tests: app.main middleware/lifespan, scheduler, push/email transports, DB migrations.
Client fixture reimports app.* per test — import app.<mod> inside test bodies; network is always mocked.
"""
from __future__ import annotations

import asyncio
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import account_lockout, image_strip, scheduler
from app.auth import COOKIE_NAME, trusted_forwarded_ip
from tests.conftest import BOOTSTRAP_EMAIL


# Shared helpers
def _session():
    from app.database import SessionLocal
    return SessionLocal()


def _uid(db, email: str) -> int:
    from sqlalchemy import select

    from app.models import User
    return db.scalar(select(User).where(User.email == email)).id


def _fresh_app(monkeypatch, tmp_path, **env):
    """Reimport app.main hermetically with env overrides, for module-import-time branches."""
    db_file = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("SESSION_SECRET", "test_secret_for_tests_only")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_NAME", "Test Admin")
    monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
    monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
    # Production-mode helpers (COOKIE_SECURE=true) must present the https base
    # URL the fail-closed lifespan guard requires; tests may override it.
    monkeypatch.setenv("APP_BASE_URL", "https://test.local")
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    old_database = sys.modules.get("app.database")
    if old_database is not None:
        old_database.engine.dispose()

    for mod in list(sys.modules):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

    from app.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    import app.main as main
    return main


# app/main.py — asset version hashing
def test_cov_main_asset_version_missing_dir_is_dev():
    # Non-existent static dir returns the sentinel "dev".
    from pathlib import Path

    from app.main import _compute_asset_version
    assert _compute_asset_version(Path("c:/no/such/static/dir/xyz")) == "dev"


def test_cov_main_asset_version_hashes_files(tmp_path):
    (tmp_path / "a.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / ".hidden").write_text("ignored", encoding="utf-8")
    from app.main import _compute_asset_version
    v = _compute_asset_version(tmp_path)
    assert isinstance(v, str)
    assert len(v) == 12


def test_cov_main_asset_version_skips_unreadable_dir_entry(tmp_path, monkeypatch):
    # A path whose read_bytes raises OSError is skipped rather than crashing.
    (tmp_path / "good.js").write_text("ok", encoding="utf-8")
    bad = tmp_path / "bad.js"
    bad.write_text("boom", encoding="utf-8")

    from pathlib import Path
    real_read = Path.read_bytes

    def flaky_read(self):
        if self.name == "bad.js":
            raise OSError("simulated unreadable file")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read)
    from app.main import _compute_asset_version
    v = _compute_asset_version(tmp_path)
    assert isinstance(v, str)
    assert len(v) == 12


# app/main.py — health / meta endpoints
def test_cov_main_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert "version" in j
    assert "asset_version" in j


def test_cov_main_meta(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    j = r.json()
    for key in ("statuses", "statuses_by_type", "priorities",
                "environments", "item_types"):
        assert key in j


# app/main.py — HTML serving + __APP_VERSION__ substitution
def test_cov_main_login_html_substitutes_app_version(client):
    r = client.get("/login.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "__APP_VERSION__" not in r.text  # placeholder was substituted
    from app.config import get_settings
    assert f"Version {get_settings().APP_VERSION}" in r.text


def test_cov_main_login_alias_path(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "__APP_VERSION__" not in r.text


def test_cov_main_reset_html_served(client):
    r = client.get("/reset.html")
    assert r.status_code == 200
    assert "__ASSET_VERSION__" not in r.text
    assert client.get("/reset").status_code == 200


def test_cov_main_home_redirects_to_login_when_anonymous(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login.html"


def test_cov_main_home_serves_index_when_authed(admin_client):
    r = admin_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


def test_cov_main_login_redirects_to_home_when_authed(admin_client):
    # Already-authenticated users hitting /login.html are bounced to /.
    r = admin_client.get("/login.html", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


# app/main.py — _auto_login (AUTO_LOGIN_ENABLED convenience switch)
def test_cov_main_home_auto_login_when_enabled(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "AUTO_LOGIN_ENABLED", True)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "set-cookie" in {k.lower() for k in r.headers.keys()}
    db = _session()
    try:
        from sqlalchemy import select

        from app.models import Activity
        assert db.scalar(
            select(Activity).where(Activity.action == "login")
        ) is not None
    finally:
        db.close()


def test_cov_main_login_page_auto_login_when_enabled(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "AUTO_LOGIN_ENABLED", True)
    r = client.get("/login.html", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert "set-cookie" in {k.lower() for k in r.headers.keys()}


def test_cov_main_auto_login_noop_when_bootstrap_missing(client, monkeypatch):
    # Flag on but the configured account doesn't exist: falls back to the normal flow.
    import app.main as main
    monkeypatch.setattr(main.settings, "AUTO_LOGIN_ENABLED", True)
    monkeypatch.setattr(main.settings, "BOOTSTRAP_ADMIN_EMAIL", "nobody@test.local")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login.html"


def test_cov_main_auto_login_noop_when_bootstrap_inactive(client, monkeypatch):
    from sqlalchemy import update

    import app.main as main
    from app.models import User
    db = _session()
    try:
        db.execute(update(User).where(User.email == "admin@test.local").values(is_active=False))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(main.settings, "AUTO_LOGIN_ENABLED", True)
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login.html"


def test_cov_main_html_cache_reuse(client):
    # Second fetch should return the cached render.
    a = client.get("/login.html").text
    b = client.get("/login.html").text
    assert a == b


# app/main.py — _has_valid_session: legacy / jti / expired / DB error
def test_cov_main_has_valid_session_no_cookie(client):
    from app.main import _has_valid_session

    class _Req:
        cookies: dict = {}

    assert _has_valid_session(_Req()) is False


def test_cov_main_has_valid_session_legacy_token(client, monkeypatch):
    # jti=None (legacy) is accepted on signature alone, no sessions-table lookup.
    import app.main as main

    class _Req:
        cookies = {COOKIE_NAME: "legacy-cookie"}

    monkeypatch.setattr(main, "parse_session_token", lambda tok: (42, 0, None))
    assert main._has_valid_session(_Req()) is True


def test_cov_main_has_valid_session_expired_row(admin_client, monkeypatch):
    # A session row whose expires_at is in the past should be rejected.
    import app.main as main
    from app.models import Session as SessionRow

    fake = SessionRow(
        jti="jti-x", user_id=7,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )

    class _Scalar:
        def scalar(self, *_a, **_k):
            return fake

        def close(self):
            pass

    monkeypatch.setattr(main, "parse_session_token", lambda tok: (7, 0, "jti-x"))
    monkeypatch.setattr(main, "SessionLocal", lambda: _Scalar())

    class _Req:
        cookies = {COOKIE_NAME: "cookie"}

    assert main._has_valid_session(_Req()) is False


def test_cov_main_has_valid_session_naive_expiry_is_valid(admin_client, monkeypatch):
    # A naive (tz-less) expires_at in the future is treated as UTC and accepted.
    import app.main as main
    from app.models import Session as SessionRow

    fake = SessionRow(
        jti="jti-y", user_id=7,
        expires_at=datetime.utcnow() + timedelta(hours=1),  # naive, future
    )

    class _DB:
        def scalar(self, *_a, **_k):
            return fake

        def close(self):
            pass

    monkeypatch.setattr(main, "parse_session_token", lambda tok: (7, 0, "jti-y"))
    monkeypatch.setattr(main, "SessionLocal", lambda: _DB())

    class _Req:
        cookies = {COOKIE_NAME: "cookie"}

    assert main._has_valid_session(_Req()) is True


def test_cov_main_has_valid_session_db_error_returns_false(client, monkeypatch):
    # SQLAlchemyError during lookup is swallowed → False, not a 500.
    from sqlalchemy.exc import SQLAlchemyError

    import app.main as main

    class _DB:
        def scalar(self, *_a, **_k):
            raise SQLAlchemyError("db down")

        def close(self):
            pass

    monkeypatch.setattr(main, "parse_session_token", lambda tok: (7, 0, "jti-z"))
    monkeypatch.setattr(main, "SessionLocal", lambda: _DB())

    class _Req:
        cookies = {COOKIE_NAME: "cookie"}

    assert main._has_valid_session(_Req()) is False


# app/main.py — middleware edges (security headers, rate limit, CSRF)
def test_cov_main_security_headers_present(client):
    r = client.get("/api/health")
    assert r.headers.get("Content-Security-Policy")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "server" not in {k.lower() for k in r.headers}


def test_cov_main_hsts_emitted_when_cookie_secure(monkeypatch, tmp_path):
    # HSTS needs COOKIE_SECURE; 32+ char secret so lifespan doesn't fail-closed first.
    main = _fresh_app(
        monkeypatch, tmp_path,
        COOKIE_SECURE="true",
        SESSION_SECRET="x" * 40,
    )
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        r = c.get("/api/health")
        assert "max-age=" in r.headers.get("Strict-Transport-Security", "")


def test_cov_main_rate_limit_returns_429(client):
    # forgot-password is capped at 3 requests per 60s; the 4th is throttled.
    last = None
    for _ in range(5):
        last = client.post("/api/auth/forgot-password", json={"email": "x@y.local"})
    assert last is not None
    assert last.status_code == 429
    assert last.headers.get("Retry-After")


def test_cov_main_rate_limit_trusts_xff_when_configured(monkeypatch, tmp_path):
    # With TRUST_PROXY_FORWARDED_FOR, buckets key on the left-most XFF entry.
    main = _fresh_app(monkeypatch, tmp_path, TRUST_PROXY_FORWARDED_FOR="true")
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        r1 = c.post("/api/auth/forgot-password", json={"email": "a@b.local"},
                    headers={"X-Forwarded-For": "10.0.0.1, 192.168.0.1"})
        r2 = c.post("/api/auth/forgot-password", json={"email": "a@b.local"},
                    headers={"X-Forwarded-For": "10.0.0.2"})
        assert r1.status_code != 429
        assert r2.status_code != 429


def test_cov_main_csrf_blocks_foreign_origin(admin_client):
    # An Origin that isn't in the allowed list is rejected 403.
    r = admin_client.post(
        "/api/projects",
        json={"name": "csrf"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert r.status_code == 403
    assert "Cross-origin" in r.json()["detail"]


def test_cov_main_csrf_allows_matching_host_origin(admin_client):
    # Origin matching the request Host is allowed.
    r = admin_client.post(
        "/api/projects",
        json={"name": "ok-origin"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code in (200, 201)


def test_cov_main_csrf_allows_matching_referer(admin_client):
    # No Origin but a host-matching Referer passes the prefix check.
    r = admin_client.post(
        "/api/projects",
        json={"name": "ok-referer"},
        headers={"Referer": "http://testserver/index.html"},
    )
    assert r.status_code in (200, 201)


def test_cov_main_csrf_skips_non_browser(admin_client):
    # No Origin or Referer means a non-browser client; CSRF check is skipped.
    r = admin_client.post("/api/projects", json={"name": "curl-like"})
    assert r.status_code in (200, 201)


# app/main.py — CORS module-import branches
def test_cov_main_cors_wildcard_disables_credentials(monkeypatch, tmp_path):
    # CORS_ORIGINS="*" must disable credentials (spec) yet still pass preflights.
    main = _fresh_app(monkeypatch, tmp_path, CORS_ORIGINS="*")
    assert main._allow_credentials is False
    assert "*" in main._origins
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        r = c.options(
            "/api/health",
            headers={
                "Origin": "https://anything.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert r.status_code in (200, 204)


def test_cov_main_cors_concrete_origin_registers(monkeypatch, tmp_path):
    # A concrete origin list enables credentials (no wildcard).
    main = _fresh_app(monkeypatch, tmp_path,
                      CORS_ORIGINS="https://bugs.example.com")
    assert main._allow_credentials is True
    assert main._origins == ["https://bugs.example.com"]


# app/main.py — exception handler + 404 / validation
def test_cov_main_http_exception_handler_preserves_headers(client):
    # Hitting an auth-required endpoint unauthenticated exercises the custom 401 handler.
    r = client.get("/api/users")
    assert r.status_code == 401
    assert "detail" in r.json()


def test_cov_main_404_unknown_path(client):
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404


def test_cov_main_validation_error(admin_client):
    # Missing required field triggers the 422 handler.
    r = admin_client.post("/api/projects", json={})
    assert r.status_code == 422


def test_cov_main_body_size_limit_rejects_huge_content_length(client):
    # Oversized Content-Length rejected before reading; rejection code varies by stack.
    big = str(200 * 1024 * 1024)
    r = client.post(
        "/api/auth/login",
        headers={"Content-Length": big, "Content-Type": "application/json"},
        content=b"{}",
    )
    assert r.status_code in (413, 400, 422, 401)


# app/scheduler.py — cron helpers + lifecycle + loop/tick dispatch
def test_cov_scheduler_parse_and_matches():
    from app.scheduler import CronSchedule, _parse_field
    assert _parse_field("*/20", 0, 59) == {0, 20, 40}
    c = CronSchedule("0 7 * * *")
    assert c.matches(datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc))
    assert not c.matches(datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc))


def test_cov_scheduler_resolve_tz():
    from app.scheduler import _resolve_tz
    assert _resolve_tz("") is timezone.utc
    assert _resolve_tz("Not/AReal/Zone") is timezone.utc
    assert _resolve_tz("Asia/Kolkata") is not None


def test_cov_scheduler_run_digest_once_calls_run_digest(monkeypatch):
    # _run_digest_once opens a session, calls run_digest, and closes the session
    # in its finally block. We patch the source attributes because the function
    # imports them locally.
    from app import scheduler

    closed = {"v": False}

    class _DB:
        def close(self):
            closed["v"] = True

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _DB(), raising=False)
    import app.database as database
    import app.jobs.email_digest as digest
    monkeypatch.setattr(database, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(digest, "run_digest",
                        lambda db: {"emails_sent": 1, "operations": 1, "users": 1})

    stats = scheduler._run_digest_once()
    assert stats["emails_sent"] == 1
    assert closed["v"] is True


def test_cov_scheduler_tick_runs_and_logs(monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "_run_digest_once",
                        lambda: {"emails_sent": 3, "operations": 9, "users": 3})
    c = scheduler.CronSchedule("* * * * *")
    stats = asyncio.run(scheduler._tick(
        c, timezone.utc, now=datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)))
    assert stats["emails_sent"] == 3


def test_cov_scheduler_loop_invokes_tick_once(monkeypatch):
    # Drive one iteration of the otherwise-infinite loop: fake_sleep raises
    # after the first tick so the loop body executes exactly once.
    from app import scheduler

    ticked = {"n": 0}

    async def fake_tick(schedule, tz, now=None):
        ticked["n"] += 1

    class _StopLoop(Exception):
        pass

    async def fake_sleep(_secs):
        if ticked["n"] >= 1:
            raise _StopLoop()
        return None

    monkeypatch.setattr(scheduler, "_tick", fake_tick)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fake_sleep)

    c = scheduler.CronSchedule("* * * * *")
    coro = scheduler._loop(c, timezone.utc)
    with pytest.raises(_StopLoop):
        asyncio.run(coro)
    assert ticked["n"] == 1


def test_cov_scheduler_start_creates_and_stop_cancels_task(monkeypatch):
    # start() must be driven inside a running event loop. The loop body is
    # replaced so the task parks quietly until stop() cancels it.
    from app import scheduler

    async def driver():
        monkeypatch.setattr(scheduler, "get_settings", lambda: type(
            "S", (), {"EMAIL_DIGEST_CRON": "* * * * *",
                      "EMAIL_DIGEST_ENABLED": True,
                      "EMAIL_DIGEST_TIMEZONE": "UTC"})())
        async def quiet_loop(schedule, tz):
            await asyncio.Event().wait()
        monkeypatch.setattr(scheduler, "_loop", quiet_loop)

        scheduler._task = None
        scheduler.start()
        assert scheduler._task is not None
        await scheduler.stop()
        assert scheduler._task is None

    asyncio.run(driver())


def test_cov_scheduler_start_noop_when_disabled(monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "get_settings", lambda: type(
        "S", (), {"EMAIL_DIGEST_CRON": "0 7 * * *",
                  "EMAIL_DIGEST_ENABLED": False,
                  "EMAIL_DIGEST_TIMEZONE": ""})())
    scheduler._task = None
    scheduler.start()
    assert scheduler._task is None


# app/push_service.py
def test_cov_push_deep_link_all_branches():
    from app import push_service
    assert push_service._deep_link(5, None) == "/#bug=5"
    assert push_service._deep_link(None, 9) == "/#event=9"
    assert push_service._deep_link(None, None) == "/"


def test_cov_push_schedule_noop_when_disabled(client, monkeypatch):
    from app import push_service

    class _BG:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn, *a, **k):
            self.tasks.append((fn, a, k))

    bg = _BG()
    push_service.schedule(bg, [1, 2], title="t", body="b")
    assert bg.tasks == []


def test_cov_push_schedule_noop_when_no_ids(admin_client, monkeypatch):
    # Enabled but all-None ids → nothing scheduled.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    from app import push_service

    class _BG:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn, *a, **k):
            self.tasks.append((fn, a, k))

    bg = _BG()
    push_service.schedule(bg, [None, None], title="t", body="b")
    assert bg.tasks == []


def test_cov_push_schedule_adds_task_when_enabled(admin_client, monkeypatch):
    # Enabled with real ids → exactly one task queued.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    from app import push_service

    class _BG:
        def __init__(self):
            self.tasks = []

        def add_task(self, fn, *a, **k):
            self.tasks.append((fn, a, k))

    bg = _BG()
    push_service.schedule(bg, [1, None, 2], title="t", body="b", bug_id=3)
    assert len(bg.tasks) == 1
    fn, args, kwargs = bg.tasks[0]
    assert fn is push_service.push_to_users
    assert args[0] == [1, 2]
    assert kwargs["url"] == "/#bug=3"


def test_cov_push_register_new_then_refresh_and_cross_user_conflict(admin_client):
    # Register a fresh token, re-register for the same user (refreshes metadata),
    # then verify that a different user claiming the same token raises
    # PushTokenConflict (hijack defense).
    import pytest

    from app import push_service
    from app.push_service import PushTokenConflict
    db = _session()
    admin_id = _uid(db, "admin@test.local")
    sub = push_service.register(db, user_id=admin_id, token="reg-tok",
                                platform="web", user_agent="UA")
    db.commit()
    assert sub.user_id == admin_id
    assert sub.platform == "web"
    sub_again = push_service.register(db, user_id=admin_id, token="reg-tok",
                                      platform="android", user_agent="UA2")
    db.commit()
    assert sub_again.user_id == admin_id
    assert sub_again.platform == "android"

    db.close()
    r = admin_client.post("/api/users", json={
        "name": "Re Home", "email": "rehome@test.local",
        "role": "user", "password": "User12345",
    })
    assert r.status_code == 201
    other_id = r.json()["id"]

    db = _session()
    with pytest.raises(PushTokenConflict):
        push_service.register(db, user_id=other_id, token="reg-tok")
    db.rollback()
    from sqlalchemy import func, select

    from app.models import PushSubscription
    row = db.scalar(select(PushSubscription).where(PushSubscription.token == "reg-tok"))
    n = db.scalar(select(func.count()).select_from(PushSubscription)
                  .where(PushSubscription.token == "reg-tok"))
    db.close()
    assert n == 1
    assert row.user_id == admin_id


def test_cov_push_remove_found_notfound_and_user_scoped(admin_client):
    from app import push_service
    db = _session()
    admin_id = _uid(db, "admin@test.local")
    push_service.register(db, user_id=admin_id, token="rm-tok")
    db.commit()

    assert push_service.remove(db, token="nope") is False
    assert push_service.remove(db, token="rm-tok", user_id=admin_id + 999) is False
    assert push_service.remove(db, token="rm-tok", user_id=admin_id) is True
    db.commit()
    db.close()


def test_cov_push_push_to_users_disabled_returns_zero(admin_client, monkeypatch):
    # Disabled → returns 0 without touching the transport.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", False)
    sent = []
    monkeypatch.setattr("app.fcm_transport.send",
                        lambda *a, **k: sent.append(1) or [])
    from app import push_service
    assert push_service.push_to_users([1], title="t", body="b") == 0
    assert sent == []


def test_cov_push_push_to_users_no_ids_returns_zero(admin_client, monkeypatch):
    # Enabled but all ids are None → returns 0.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    from app import push_service
    assert push_service.push_to_users([None], title="t", body="b") == 0


def test_cov_push_push_to_users_no_subs_returns_zero(admin_client, monkeypatch):
    # Enabled with a real user id but no subscriptions registered → returns 0.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    from app import push_service
    db = _session()
    admin_id = _uid(db, "admin@test.local")
    db.close()
    assert push_service.push_to_users([admin_id], title="t", body="b") == 0


def test_cov_push_push_to_users_prunes_dead_tokens(admin_client, monkeypatch):
    # Dead tokens returned by the transport are pruned from the DB.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)
    monkeypatch.setattr("app.fcm_transport.send",
                        lambda tokens, **k: ["dead"] if "dead" in tokens else [])
    from app import push_service
    db = _session()
    admin_id = _uid(db, "admin@test.local")
    push_service.register(db, user_id=admin_id, token="alive")
    push_service.register(db, user_id=admin_id, token="dead")
    db.commit()
    db.close()

    delivered = push_service.push_to_users([admin_id], title="t", body="b")
    assert delivered == 1

    from sqlalchemy import select

    from app.models import PushSubscription
    db = _session()
    assert db.scalar(select(PushSubscription).where(
        PushSubscription.token == "dead")) is None
    assert db.scalar(select(PushSubscription).where(
        PushSubscription.token == "alive")) is not None
    db.close()


def test_cov_push_push_to_users_swallows_transport_exception(admin_client, monkeypatch, caplog):
    # A transport exception is caught and rolled back; push must never break the
    # request flow.
    import logging

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "WEB_PUSH_ENABLED", True)

    def boom(*a, **k):
        raise RuntimeError("fcm exploded")

    monkeypatch.setattr("app.fcm_transport.send", boom)
    from app import push_service
    db = _session()
    admin_id = _uid(db, "admin@test.local")
    push_service.register(db, user_id=admin_id, token="will-fail")
    db.commit()
    db.close()

    with caplog.at_level(logging.ERROR, logger="bug_hunter.push"):
        result = push_service.push_to_users([admin_id], title="t", body="b")
    assert result == 0
    assert any("push_to_users failed" in (r.message or "") for r in caplog.records)


# app/database.py
def test_cov_database_get_db_yields_and_closes(client, monkeypatch):
    # get_db yields a session and closes it in its finally block.
    import app.database as database

    closed = {"v": False}
    real_session = database.SessionLocal

    class _Tracked:
        def __init__(self):
            self._s = real_session()

        def close(self):
            closed["v"] = True
            self._s.close()

        def __getattr__(self, name):
            return getattr(self._s, name)

    monkeypatch.setattr(database, "SessionLocal", _Tracked)
    import asyncio

    async def _drive():
        gen = database.get_db()
        s = await gen.__anext__()
        assert s is not None
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    asyncio.run(_drive())
    assert closed["v"] is True


def test_cov_database_column_names_missing_table(client):
    # _column_names on a non-existent table returns an empty set.
    from sqlalchemy import inspect

    import app.database as database
    insp = inspect(database.engine)
    assert database._column_names(insp, "no_such_table_xyz") == set()


def test_cov_database_add_missing_columns_idempotent(client):
    # Running _add_missing_columns on an already-migrated DB is a clean no-op.
    import app.database as database
    with database.engine.begin() as conn:
        database._add_missing_columns(conn)
    from sqlalchemy import inspect
    insp = inspect(database.engine)
    notif_cols = {c["name"] for c in insp.get_columns("notifications")}
    assert "emailed_at" in notif_cols


def test_cov_database_add_missing_indexes(client):
    import app.database as database
    with database.engine.begin() as conn:
        database._add_missing_indexes(conn)


def test_cov_database_init_db_idempotent(client):
    import app.database as database
    database.init_db()
    database.init_db()


# app/notification_service.py
def test_cov_notification_emailed_at_branch(admin_client, monkeypatch):
    # When the digest is on, new notifications are born un-emailed (emailed_at
    # is None) so the digest job can pick them up. When the digest is off, rows
    # are stamped immediately so they're never re-sent.
    from sqlalchemy import select

    from app import notification_service
    from app.config import get_settings
    from app.models import Notification

    db = _session()
    admin_id = _uid(db, "admin@test.local")

    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", True)
    notification_service.notify(db, [admin_id], kind="updated", title="digest-on")
    db.commit()
    on_row = db.scalar(select(Notification)
                       .where(Notification.title == "digest-on"))
    assert on_row.emailed_at is None

    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", False)
    notification_service.notify(db, [admin_id], kind="updated", title="digest-off")
    db.commit()
    off_row = db.scalar(select(Notification)
                        .where(Notification.title == "digest-off"))
    assert off_row.emailed_at is not None
    db.close()


# app/email_service.py


def _email_cfg(backend="smtp", **over):
    cfg = SimpleNamespace(
        EMAIL_BACKEND=backend,
        EMAIL_FROM="Test Tracker <bot@bh.local>",
        APP_NAME="Test Tracker",
        APP_BASE_URL="http://bh.local:8765",
        SMTP_HOST="mail.bh.local",
        SMTP_PORT=587,
        SMTP_USERNAME="",
        SMTP_PASSWORD="",
        SMTP_USE_TLS=False,
        SMTP_USE_SSL=False,
        SMTP_TIMEOUT=10,
        EMAIL_DIGEST_ENABLED=False,
        EMAIL_DIGEST_LOOKBACK_HOURS=26,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_cov_email_smtp_plain_no_auth(monkeypatch):
    # Plain SMTP with no username and no TLS: login() and starttls() must not be called.
    from app import email_service as es

    captured = {}

    class _Plain:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            captured["ehlo"] = captured.get("ehlo", 0) + 1

        def starttls(self, context=None):
            captured["starttls"] = True

        def login(self, u, p):
            captured["login"] = True

        def send_message(self, msg):
            captured["sent"] = True

    monkeypatch.setattr(es.smtplib, "SMTP", _Plain)
    monkeypatch.setattr(es, "get_settings",
                        lambda: _email_cfg(SMTP_USE_TLS=False, SMTP_USERNAME=""))
    es.deliver("Hi", ["a@b.local"], "body")
    assert captured.get("sent") is True
    assert "starttls" not in captured  # USE_TLS False
    assert "login" not in captured     # no username


def test_cov_email_smtp_ssl_no_auth(monkeypatch):
    # SMTP_SSL with no username: login() is skipped.
    from app import email_service as es

    captured = {}

    class _SSL:
        def __init__(self, host, port, timeout=None, context=None):
            captured["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, u, p):
            captured["login"] = True

        def send_message(self, msg):
            captured["sent"] = True

    monkeypatch.setattr(es.smtplib, "SMTP_SSL", _SSL)
    monkeypatch.setattr(
        es, "get_settings",
        lambda: _email_cfg(SMTP_USE_SSL=True, SMTP_PORT=465, SMTP_USERNAME=""),
    )
    es.deliver("Hi", ["a@b.local"], "body")
    assert captured.get("sent") is True
    assert "login" not in captured


def test_cov_email_smtp_missing_host_dropped(monkeypatch, caplog):
    import logging

    from app import email_service as es
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(SMTP_HOST=""))
    with caplog.at_level(logging.WARNING, logger="bug_hunter.email"):
        es.deliver("Hi", ["a@b.local"], "body")
    assert any("SMTP_HOST is empty" in (r.message or "") for r in caplog.records)


def test_cov_email_smtp_swallows_transport_error(monkeypatch, caplog):
    import logging
    import smtplib

    from app import email_service as es

    def boom(*a, **k):
        raise smtplib.SMTPException("nope")

    monkeypatch.setattr(es.smtplib, "SMTP", boom)
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(SMTP_USE_SSL=False))
    with caplog.at_level(logging.ERROR, logger="bug_hunter.email"):
        es.deliver("Hi", ["a@b.local"], "body")  # must not raise
    assert any("Failed to send email" in (r.message or "") for r in caplog.records)


def test_cov_email_notify_event_created_and_updated(monkeypatch):
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver",
                        lambda subject, to, body: sent.append((subject, to, body)))
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(backend="console"))

    ev = es.EventSnapshot(
        id=4, name="Standup", description="Daily",
        scheduled_for="2026-06-13",
        managers=(es.UserSnapshot(2, "Meg", "meg@bh.local"),),
    )
    es.notify_event_created(ev, "Alice", actor_user_id=None)
    es.notify_event_updated(ev, [], "Alice", actor_user_id=None)
    es.notify_event_updated(ev, [("name", "Old", "New")], "Alice", actor_user_id=None)
    subjects = [s for (s, _t, _b) in sent]
    assert any("New event #4" in s for s in subjects)
    assert any("Event #4 updated" in s for s in subjects)
    # The empty-changes call should not produce a second email.
    assert sum("updated" in s for s in subjects) == 1


def test_cov_email_notify_password_reset_empty_email_noop(monkeypatch):
    # An empty email address should short-circuit with no deliver call.
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver",
                        lambda *a, **k: sent.append(1))
    es.notify_password_reset("", "Nobody", "http://x/reset")
    assert sent == []


def test_cov_email_notify_event_deleted(monkeypatch):
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver",
                        lambda subject, to, body: sent.append((subject, to, body)))
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(backend="console"))
    ev = es.EventSnapshot(
        id=7, name="Gone", description="",
        scheduled_for=None,
        managers=(es.UserSnapshot(2, "Meg", "meg@bh.local"),),
    )
    es.notify_event_deleted(ev, "Alice", actor_user_id=None)
    assert any("Event #7 deleted" in s for (s, _t, _b) in sent)


def test_cov_email_bug_updated_suppressed_when_digest_on(monkeypatch):
    # notify_bug_updated returns early when the digest owns work-item emails.
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver", lambda *a, **k: sent.append(1))
    monkeypatch.setattr(es, "get_settings",
                        lambda: _email_cfg(backend="console", EMAIL_DIGEST_ENABLED=True))
    bug = es.BugSnapshot(
        id=1, title="B", project_name="P", status="New", priority="Low",
        environment="DEV", description="",
        reporter=es.UserSnapshot(1, "R", "r@bh.local"), assignees=(),
    )
    es.notify_bug_updated(bug, [("status", "New", "Done")], "Alice", actor_user_id=None)
    assert sent == []


def test_cov_email_assignment_skips_emailless_assignee(monkeypatch):
    # An assignee with an empty email is skipped; only the one with an address gets the mail.
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver",
                        lambda subject, to, body: sent.append((subject, to, body)))
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(backend="console"))
    bug = es.BugSnapshot(
        id=3, title="Assign me", project_name="P", status="New", priority="Low",
        environment="DEV", description="",
        reporter=None, assignees=(),
    )
    es.notify_assignment(
        bug,
        [es.UserSnapshot(8, "No Mail", ""),
         es.UserSnapshot(9, "Has Mail", "has@bh.local")],
        "Alice",
    )
    assert len(sent) == 1
    assert sent[0][1] == ["has@bh.local"]


def test_cov_email_event_updated_no_recipients_returns(monkeypatch):
    # When the only manager is the actor, the recipient list is empty and
    # the function returns early without sending.
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver", lambda *a, **k: sent.append(1))
    monkeypatch.setattr(es, "get_settings", lambda: _email_cfg(backend="console"))
    ev = es.EventSnapshot(
        id=5, name="Solo", description="", scheduled_for=None,
        managers=(es.UserSnapshot(2, "Only Mgr", "mgr@bh.local"),),
    )
    es.notify_event_updated(ev, [("name", "a", "b")], "Mgr", actor_user_id=2)
    assert sent == []


def test_cov_email_event_suppressed_when_digest_on(monkeypatch):
    # All notify_event_* functions share the same digest-on guard; none should
    # call deliver when EMAIL_DIGEST_ENABLED is true.
    from app import email_service as es
    sent = []
    monkeypatch.setattr(es, "deliver", lambda *a, **k: sent.append(1))
    monkeypatch.setattr(es, "get_settings",
                        lambda: _email_cfg(backend="console", EMAIL_DIGEST_ENABLED=True))
    ev = es.EventSnapshot(id=1, name="X", description="", scheduled_for=None,
                          managers=(es.UserSnapshot(2, "M", "m@bh.local"),))
    bug = es.BugSnapshot(
        id=1, title="B", project_name="P", status="New", priority="Low",
        environment="DEV", description="",
        reporter=es.UserSnapshot(1, "R", "r@bh.local"), assignees=(),
    )
    es.notify_event_created(ev, "A", None)
    es.notify_event_updated(ev, [("n", "a", "b")], "A", None)
    es.notify_event_deleted(ev, "A", None)
    es.notify_assignment(bug, [es.UserSnapshot(3, "D", "d@bh.local")], "A")
    es.notify_comment_added(bug, "Bob", 9, "hi")
    assert sent == []


# app/jobs/email_digest.py
def test_cov_digest_main_returns_zero_when_disabled(admin_client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", False)
    sent = []
    monkeypatch.setattr("app.email_service.deliver",
                        lambda *a, **k: sent.append(1))
    from app.jobs.email_digest import main
    assert main() == 0
    assert sent == []


def test_cov_digest_main_returns_zero_on_success(admin_client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", True)
    sent = []
    monkeypatch.setattr("app.email_service.deliver",
                        lambda subject, to, body: sent.append((subject, to, body)))

    db = _session()
    admin_id = _uid(db, "admin@test.local")
    from app.models import Notification
    from app.util import utcnow as _utcnow
    n = Notification(user_id=admin_id, kind="assigned",
                     title="Assigned to Bug #1", body="x assigned you",
                     actor_name="Actor")
    n.created_at = _utcnow() - timedelta(hours=1)
    db.add(n)
    db.commit()
    db.close()

    from app.jobs.email_digest import main
    assert main() == 0
    assert len(sent) == 1


def test_cov_digest_main_returns_one_on_failure(admin_client, monkeypatch):
    # Any exception from run_digest should be caught and main() should return 1.
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "EMAIL_DIGEST_ENABLED", True)
    import app.jobs.email_digest as digest

    def boom(*a, **k):
        raise RuntimeError("digest blew up")

    monkeypatch.setattr(digest, "run_digest", boom)
    assert digest.main() == 1


def test_cov_digest_render_links_and_other_category(admin_client):
    # Bug and event deep-links must appear, and an unknown kind falls into "Other".
    from app.jobs.email_digest import _link, render_digest
    from app.models import Notification, User

    db = _session()
    admin = db.get(User, _uid(db, "admin@test.local"))

    rows = [
        Notification(user_id=admin.id, kind="assigned",
                     title="Bug link", body="b", bug_id=11, actor_name="A"),
        Notification(user_id=admin.id, kind="event",
                     title="Event link", body="b", event_id=22, actor_name="A"),
        Notification(user_id=admin.id, kind="weird-unknown-kind",
                     title="Other thing", body="b", actor_name="A"),
    ]
    _subject, body = render_digest(admin, rows)
    db.close()

    assert "#bug=11" in body
    assert "#event=22" in body
    assert "Other" in body

    # _link with neither bug_id nor event_id falls back to the base URL.
    plain = Notification(user_id=1, kind="x", title="t", actor_name="A")
    assert _link("http://base", plain) == "http://base"


def test_cov_digest_run_digest_empty_returns_zeroes(admin_client):
    from app.jobs.email_digest import run_digest
    from app.util import utcnow as _utcnow
    db = _session()
    stats = run_digest(db, now=_utcnow(), lookback_hours=1)
    db.close()
    assert stats == {"users": 0, "emails_sent": 0, "operations": 0, "failed": 0}


# Branch edges that can't be reached deterministically through HTTP requests:
# rate-limit eviction, CSRF host-absent / referer-miss, bootstrap/lifespan
# fail-closed RuntimeErrors, DB additive-migration ALTER paths, and a few
# scheduler.matches edge cases. Drive middleware and helpers directly.

# Tiny ASGI Request/Response doubles for direct middleware dispatch.
class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeClient:
    def __init__(self, host):
        self.host = host


class _FakeRequest:
    def __init__(self, path="/api/x", method="POST", headers=None, client_host="1.2.3.4"):
        self.url = _FakeURL(path)
        self.method = method
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host else None
        self.cookies = {}


def test_cov_main_security_headers_strip_server(client):
    from starlette.responses import Response

    import app.main as main

    async def inner(scope, receive, send):
        resp = Response("ok")
        resp.headers["server"] = "uvicorn"
        await resp(scope, receive, send)

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/api/health", "headers": []}
    asyncio.run(main.ResponseHeadersMiddleware(inner)(scope, receive, send))
    names = {k.decode().lower() for k, _ in sent[0]["headers"]}
    assert "server" not in names
    assert {"content-security-policy", "x-frame-options", "cache-control"} <= names


def test_cov_main_client_ip_trust_proxy_without_xff(monkeypatch):
    # TRUST_PROXY on but no XFF header: falls back to request.client.host,
    # and returns "unknown" when client is None.
    import app.main as main
    monkeypatch.setattr(main.settings, "TRUST_PROXY_FORWARDED_FOR", True)
    req = _FakeRequest(headers={}, client_host="9.9.9.9")
    assert main._client_ip(req) == "9.9.9.9"
    req2 = _FakeRequest(headers={}, client_host=None)
    assert main._client_ip(req2) == "unknown"


def test_cov_main_rate_limit_evicts_oldest_bucket_at_cap(monkeypatch):
    # When the bucket dict is at capacity, the oldest entry is evicted before
    # inserting the new one so the dict stays bounded.
    import time as _time

    import app.main as main

    monkeypatch.setattr(main, "_RATE_BUCKETS_MAX", 1)
    main._rate_buckets.clear()
    main._rate_buckets[("/seed", "ip0")] = main.deque([_time.monotonic()])

    req = _FakeRequest(path="/api/auth/login", method="POST",
                       headers={}, client_host="5.5.5.5")
    assert main.rate_limit_check(req) is None
    assert len(main._rate_buckets) <= 2
    main._rate_buckets.clear()


def test_cov_main_rate_limit_popleft_expired(monkeypatch):
    # A stale timestamp in a reused bucket is popped by the eviction loop so
    # only the current request remains.
    import time as _time

    import app.main as main

    main._rate_buckets.clear()
    stale = _time.monotonic() - 10_000
    key = ("/api/auth/login", "7.7.7.7")
    main._rate_buckets[key] = main.deque([stale])

    req = _FakeRequest(path="/api/auth/login", method="POST",
                       headers={}, client_host="7.7.7.7")
    assert main.rate_limit_check(req) is None
    assert len(main._rate_buckets[key]) == 1
    main._rate_buckets.clear()


def test_cov_main_csrf_no_host_header_blocks(monkeypatch):
    # Origin present but no Host header: the host-based allowed-set is skipped
    # and, with no configured CORS origins, the request is rejected 403.
    import app.main as main
    monkeypatch.setattr(main, "_allowed_origins", lambda: set())
    req = _FakeRequest(
        path="/api/projects", method="POST",
        headers={"origin": "https://evil.example.com"},  # no "host"
    )
    assert main.csrf_check(req).status_code == 403


def test_cov_main_csrf_referer_mismatch_blocks(monkeypatch):
    # Referer present but not starting with any allowed origin prefix → 403.
    import app.main as main
    monkeypatch.setattr(main, "_allowed_origins", lambda: set())
    req = _FakeRequest(
        path="/api/projects", method="POST",
        headers={"referer": "https://evil.example.com/page", "host": "testserver"},
    )
    assert main.csrf_check(req).status_code == 403


def test_cov_main_csrf_safe_method_skips(client):
    # GET is a safe method; the CSRF check is skipped entirely.
    import app.main as main
    req = _FakeRequest(path="/api/projects", method="GET",
                       headers={"origin": "https://evil.example.com"})
    assert main.csrf_check(req) is None


def test_cov_main_serve_html_cache_overflow_clears(client):
    # When the cache exceeds 32 entries it is cleared before inserting, so
    # only the freshly-rendered page remains.
    import app.main as main
    main._html_cache.clear()
    for i in range(40):
        main._html_cache[(f"junk{i}.html", "v")] = "x"
    assert len(main._html_cache) > 32
    main._serve_html("login.html")
    assert len(main._html_cache) == 1


def test_cov_main_bootstrap_idempotent_when_users_exist(admin_client):
    # A second _bootstrap() call takes the users-already-exist branch and
    # must not raise or create a duplicate admin.
    import app.main as main
    main._bootstrap()
    from sqlalchemy import func, select

    from app.models import User
    db = _session()
    n = db.scalar(select(func.count()).select_from(User))
    db.close()
    assert n >= 1


def test_cov_main_bootstrap_refuses_default_admin_in_prod(monkeypatch, tmp_path):
    # An empty production database must refuse bootstrap without explicit admin
    # credentials. Called directly to avoid also triggering the lifespan check.
    main = _fresh_app(
        monkeypatch, tmp_path,
        COOKIE_SECURE="true",
        BOOTSTRAP_ADMIN_EMAIL="",
        BOOTSTRAP_ADMIN_PASSWORD="",
        SESSION_SECRET="x" * 40,
    )
    settings = main.get_settings()
    settings.BOOTSTRAP_ADMIN_EMAIL = ""
    settings.BOOTSTRAP_ADMIN_PASSWORD = ""
    main.init_db()  # create the (empty) schema
    with pytest.raises(RuntimeError, match="Bootstrap admin not configured"):
        main._bootstrap()


def test_cov_main_lifespan_requires_strong_secret_in_prod(monkeypatch, tmp_path):
    # In production mode a SESSION_SECRET shorter than 32 chars causes lifespan
    # startup to raise so the app never begins serving.
    main = _fresh_app(
        monkeypatch, tmp_path,
        COOKIE_SECURE="true",
        SESSION_SECRET="short",
        BOOTSTRAP_ADMIN_PASSWORD="StrongPw0rd!",
    )
    from fastapi.testclient import TestClient
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        with TestClient(main.app):
            pass


def test_cov_main_lifespan_warns_on_empty_secret(monkeypatch, tmp_path, caplog):
    # Empty SESSION_SECRET in non-prod mode emits a warning but does not abort startup.
    import logging
    main = _fresh_app(
        monkeypatch, tmp_path,
        COOKIE_SECURE="false",
        SESSION_SECRET="",
    )
    from fastapi.testclient import TestClient
    with caplog.at_level(logging.WARNING, logger="bug_hunter"):
        with TestClient(main.app) as c:
            assert c.get("/api/health").status_code == 200
    assert any("SESSION_SECRET is not set" in (r.message or "")
               for r in caplog.records)


def test_cov_scheduler_matches_month_miss():
    from app.scheduler import CronSchedule
    c = CronSchedule("0 0 * 12 *")
    assert not c.matches(datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc))
    assert c.matches(datetime(2026, 12, 15, 0, 0, tzinfo=timezone.utc))


def test_cov_scheduler_matches_dom_only():
    # Day-of-month restricted with wildcard dow: match falls through to dom check.
    from app.scheduler import CronSchedule
    c = CronSchedule("0 0 15 * *")
    assert c.matches(datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc))
    assert not c.matches(datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc))


def test_cov_push_subs_for_users_empty_ids(client):
    from app import push_service
    db = _session()
    assert push_service._subs_for_users(db, [None, None]) == []
    assert push_service._subs_for_users(db, []) == []
    db.close()


def test_cov_database_build_engine_postgres_branch(monkeypatch):
    # Cover the Postgres pool branch without a real server by stubbing
    # create_engine and event.listens_for so no connection is ever opened.
    import app.database as database

    captured = {}

    def fake_create_engine(url, **kw):
        captured["url"] = url
        captured["kw"] = kw
        return object()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)
    monkeypatch.setattr(database.event, "listens_for", lambda *a, **k: (lambda f: f))
    eng = database._build_engine("postgresql+psycopg://u:p@db/bh")
    assert eng is not None
    assert captured["kw"].get("pool_pre_ping") is True
    assert captured["kw"].get("pool_size") == 5
    assert captured["kw"].get("max_overflow") == 10


def test_cov_database_add_missing_columns_alters_legacy_db(monkeypatch, tmp_path):
    # Build a minimal SQLite DB that lacks the new columns, then verify
    # _add_missing_columns adds them via ALTER TABLE.
    from sqlalchemy import create_engine, inspect, text

    import app.database as database

    legacy = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with legacy.begin() as conn:
        conn.execute(text("CREATE TABLE bugs (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE notifications (id INTEGER PRIMARY KEY)"))

    # _add_missing_columns reads the module-global `engine` for introspection
    # and executes DDL on the passed-in connection — point both at the legacy engine.
    monkeypatch.setattr(database, "engine", legacy)
    with legacy.begin() as conn:
        database._add_missing_columns(conn)

    insp = inspect(legacy)
    bug_cols = {c["name"] for c in insp.get_columns("bugs")}
    notif_cols = {c["name"] for c in insp.get_columns("notifications")}
    assert "item_type" in bug_cols
    assert "event_id" in bug_cols
    assert "emailed_at" in notif_cols
    legacy.dispose()


def test_cov_database_add_missing_indexes_handles_introspection_error(monkeypatch):
    # get_indexes raising for every table exercises the except-continue path;
    # the helper must complete without propagating. Patched on the sqlalchemy
    # package because _add_missing_indexes imports inspect locally.
    import sqlalchemy
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _BoomInspector:
        def get_indexes(self, _table):
            raise SQLAlchemyError("introspection failed")

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _engine: _BoomInspector())
    with database.engine.begin() as conn:
        database._add_missing_indexes(conn)


# account_lockout._reclaim_buckets
def test_reclaim_deletes_idle_bucket():
    account_lockout._buckets.clear()
    # Idle: unlocked (locked_until 0) and no in-window failures → reclaimed.
    account_lockout._buckets["idle@x"] = account_lockout._Bucket(
        fails=deque(), locked_until=0.0)
    account_lockout._reclaim_buckets(1000.0)
    assert "idle@x" not in account_lockout._buckets
    account_lockout._buckets.clear()


def test_reclaim_evicts_soonest_expiring_when_all_locked(monkeypatch):
    account_lockout._buckets.clear()
    monkeypatch.setattr(account_lockout, "_LOCKOUT_BUCKETS_MAX", 2)
    # Two locked buckets (locked_until in the future, recent fails so neither is idle).
    account_lockout._buckets["a@x"] = account_lockout._Bucket(
        fails=deque([999.0]), locked_until=5000.0)
    account_lockout._buckets["b@x"] = account_lockout._Bucket(
        fails=deque([999.0]), locked_until=6000.0)
    account_lockout._reclaim_buckets(1000.0)
    # No idle buckets: reclaim falls back to evicting the soonest-to-expire (a@x).
    assert "a@x" not in account_lockout._buckets
    assert "b@x" in account_lockout._buckets
    account_lockout._buckets.clear()


# auth.trusted_forwarded_ip — empty input guard (line 79)
def test_trusted_forwarded_ip_empty_returns_none():
    assert trusted_forwarded_ip("", 1) is None
    assert trusted_forwarded_ip("   ,  ,", 1) is None
    assert trusted_forwarded_ip(None, 1) is None


# image_strip — animated/multi-frame images are preserved untouched (85-86)
def test_strip_preserves_animated_image(monkeypatch):
    from PIL import Image

    class _FakeAnimated:
        size = (4, 4)
        is_animated = True
        n_frames = 3

    # Saving multi-frame would drop all but the first frame; must return original bytes.
    monkeypatch.setattr(Image, "open", lambda src: _FakeAnimated())
    data = b"GIF89a-fake-animated-bytes"
    assert image_strip.strip_image_metadata(data, "image/gif") == data


# scheduler.start() re-entry guard (193-194)
def test_scheduler_start_reentry_is_noop(monkeypatch):
    class _FakeTask:
        def done(self):
            return False

    fake = _FakeTask()
    monkeypatch.setattr(scheduler, "_task", fake)
    scheduler.start()  # already running: should log and return without replacing _task
    assert scheduler._task is fake


# app/main.py - asset hash, body-limit ASGI, cache headers, rate buckets, IP, CSRF.
class _FakeURL_misc_branches:
    def __init__(self, path):
        self.path = path


class _FakeReq:
    def __init__(self, path="/api/x", method="POST", headers=None, client_host="1.2.3.4"):
        self.url = _FakeURL_misc_branches(path)
        self.method = method
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host else None
        self.cookies = {}


def test_main_asset_version_large_file_uses_size(tmp_path, monkeypatch):
    # Over-cap files are hashed by size (not read); shrink the cap to trip the branch.
    import app.main as main
    monkeypatch.setattr(main, "_MAX_ASSET_FILE_BYTES", 4)
    big = tmp_path / "big.js"
    big.write_text("0123456789")  # 10 bytes > 4-byte cap
    v = main._compute_asset_version(tmp_path)
    assert v
    assert v != "dev"


def test_main_cache_control_static_non_fingerprinted(monkeypatch):
    # Non-assets /static/ paths get the 1-hour header, not the immutable one.
    from starlette.datastructures import MutableHeaders

    import app.main as main

    headers = MutableHeaders()
    main._apply_cache_headers(headers, "/static/firebase-messaging-sw.js")
    assert headers["Cache-Control"] == "public, max-age=3600"
    kept = MutableHeaders({"Cache-Control": "private"})
    main._apply_cache_headers(kept, "/api/x")
    assert kept["Cache-Control"] == "private"


def test_main_body_limit_skips_non_request_messages():
    # Non-http.request messages (http.disconnect) pass through without the byte counter.
    import app.main as main

    received = {"msgs": []}

    async def app_inner(_scope, receive, _send):
        msg = await receive()
        received["msgs"].append(msg)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_msg):
        return None

    mw = main.BodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    asyncio.run(mw(scope, receive, send))
    assert received["msgs"] == [{"type": "http.disconnect"}]


def test_main_body_limit_413_on_oversized_body(monkeypatch):
    # Oversized body triggers 413 when the response hasn't started.
    import app.main as main
    monkeypatch.setattr(main.settings, "MAX_JSON_BODY_BYTES", 8)

    async def app_inner(_scope, receive, _send):
        await receive()

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    sent = []

    async def send(msg):
        sent.append(msg)

    mw = main.BodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    asyncio.run(mw(scope, receive, send))
    starts = [m for m in sent if m.get("type") == "http.response.start"]
    assert starts
    assert starts[0]["status"] == 413


def test_main_body_limit_reraises_when_response_started(monkeypatch):
    # Response already started: no clean 413, so _RequestBodyTooLarge is re-raised.
    import app.main as main
    monkeypatch.setattr(main.settings, "MAX_JSON_BODY_BYTES", 8)

    async def app_inner(_scope, receive, send):
        # Start the response before reading the oversized body.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()  # this read overflows, raising _RequestBodyTooLarge

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def send(_msg):
        return None

    mw = main.BodyLimitMiddleware(app_inner)
    scope = {"type": "http", "path": "/api/x"}
    coro = mw(scope, receive, send)
    with pytest.raises(main._RequestBodyTooLarge):
        asyncio.run(coro)


def test_main_evict_dead_rate_bucket(monkeypatch):
    # Buckets past the eviction horizon and empty buckets are deleted; under cap, nothing more.
    import time as _time

    import app.main as main
    main._rate_buckets.clear()
    old = _time.monotonic() - 10_000  # older than _MAX_RATE_WINDOW
    main._rate_buckets[("/old", "ipX")] = main.deque([old])
    main._rate_buckets[("/empty", "ipY")] = main.deque()
    main._evict_one_rate_bucket(_time.monotonic())
    assert ("/old", "ipX") not in main._rate_buckets
    assert ("/empty", "ipY") not in main._rate_buckets
    main._rate_buckets.clear()


def test_main_client_ip_trusted_xff_none_falls_through(monkeypatch):
    # TRUST_PROXY on but trusted_forwarded_ip None: fall back to request.client.host.
    import app.main as main
    monkeypatch.setattr(main.settings, "TRUST_PROXY_FORWARDED_FOR", True)
    monkeypatch.setattr(main.settings, "TRUST_PROXY_HOP_COUNT", 5)
    monkeypatch.setattr(main, "trusted_forwarded_ip", lambda _xff, _hops: None)
    req = _FakeReq(headers={"x-forwarded-for": "1.1.1.1"}, client_host="9.9.9.9")
    assert main._client_ip(req) == "9.9.9.9"


def test_main_csrf_origin_present_not_allowed_blocks(monkeypatch):
    # Unknown Origin is 403. ('elif referer:' false is unreachable: the earlier guard returns first.)
    import app.main as main
    monkeypatch.setattr(main, "_allowed_origins", lambda: set())
    req = _FakeReq(path="/api/projects", method="POST",
                   headers={"origin": "https://evil.example.com"})  # no host/referer
    assert main.csrf_check(req).status_code == 403


def test_main_has_valid_session_missing_row_returns_false(client):
    # A valid-looking token whose jti was never persisted must be rejected.
    from sqlalchemy import select

    import app.main as main
    from app.auth import make_session_token, new_jti
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        admin = db.scalar(select(User).where(User.email == BOOTSTRAP_EMAIL))
        token = make_session_token(admin.id, admin.session_version or 0, jti=new_jti())
    finally:
        db.close()

    req = _FakeReq(method="GET", headers={})
    req.cookies = {COOKIE_NAME: token}
    assert main._has_valid_session(req) is False


# --- database reconciliation helpers ------------------------------------------------------

def test_column_names_survive_an_introspection_error(caplog):
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _Broken:
        def get_columns(self, _table):
            raise SQLAlchemyError("cannot introspect")

    with caplog.at_level("WARNING", logger="bug_hunter"):
        assert database._column_names(_Broken(), "bugs") == set()
    assert "Could not read columns of bugs" in caplog.text


def test_ddl_type_falls_back_when_the_dialect_cannot_render_the_type():
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _Type:
        def compile(self, dialect):
            raise SQLAlchemyError("no dialect rendering")

        def __str__(self):
            return "datetime"

    conn = SimpleNamespace(dialect=None)
    assert database._ddl_type_for(conn, _Type()) == "TIMESTAMP"


def test_safe_scalar_default_sql_renders_only_literal_defaults():
    import app.database as database

    def col(arg):
        return SimpleNamespace(default=SimpleNamespace(arg=arg))

    assert database._safe_scalar_default_sql(col(True)) == "TRUE"
    assert database._safe_scalar_default_sql(col(False)) == "FALSE"
    assert database._safe_scalar_default_sql(col(7)) == "7"
    assert database._safe_scalar_default_sql(col(1.5)) == "1.5"
    assert database._safe_scalar_default_sql(col("it's")) == "'it''s'"
    assert database._safe_scalar_default_sql(col(lambda: 1)) is None
    assert database._safe_scalar_default_sql(SimpleNamespace(default=None)) is None


def test_legacy_sprint_epic_and_git_tables_gain_their_new_columns(monkeypatch, tmp_path):
    from sqlalchemy import create_engine, inspect, text

    import app.database as database

    legacy = create_engine(f"sqlite:///{tmp_path / 'older.db'}", future=True)
    with legacy.begin() as conn:
        for table in ("sprints", "epic_details", "project_git_configs"):
            conn.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", legacy)
    with legacy.begin() as conn:
        database._add_missing_columns(conn)

    insp = inspect(legacy)
    assert "cadence" in {c["name"] for c in insp.get_columns("sprints")}
    assert "sprint_id" in {c["name"] for c in insp.get_columns("epic_details")}
    assert "credential_encrypted" in {c["name"] for c in insp.get_columns("project_git_configs")}
    legacy.dispose()


def test_display_id_backfill_tolerates_a_missing_table(caplog):
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _Conn:
        def execute(self, *_a, **_k):
            raise SQLAlchemyError("no such table: bugs")

    with caplog.at_level("WARNING", logger="bug_hunter"):
        database._backfill_bug_display_id(_Conn())
    assert "display_id backfill skipped" in caplog.text


def test_legacy_description_copy_logs_instead_of_aborting(caplog):
    from contextlib import nullcontext

    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _Conn:
        def begin_nested(self):
            return nullcontext()

        def execute(self, *_a, **_k):
            raise SQLAlchemyError("boom")

    with caplog.at_level("ERROR", logger="bug_hunter"):
        database._preserve_legacy_html_descriptions(_Conn())
    assert "legacy description preservation failed" in caplog.text


def test_run_once_records_success_skips_repeats_and_retries_failures(db_session, caplog):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    conn = db_session.connection()
    calls = []
    database._run_once(conn, "unit_test_once", lambda c: calls.append("ran"))
    database._run_once(conn, "unit_test_once", lambda c: calls.append("again"))
    assert calls == ["ran"]

    def _fail(_conn):
        raise SQLAlchemyError("migration blew up")

    with caplog.at_level("ERROR", logger="bug_hunter"):
        database._run_once(conn, "unit_test_failing", _fail)
    assert "data migration unit_test_failing failed" in caplog.text
    recorded = {r[0] for r in conn.execute(text("SELECT name FROM app_migrations"))}
    assert "unit_test_once" in recorded
    assert "unit_test_failing" not in recorded


def test_plain_text_migration_only_rewrites_entities_that_change(db_session):
    from sqlalchemy import text

    import app.database as database
    from app.models import Board, Project, Sprint

    project = Project(name="Entities", color="#123456", agile_enabled=True)
    db_session.add(project)
    db_session.flush()
    board = Board(project_id=project.id, name="B", name_normalized="b")
    db_session.add(board)
    db_session.flush()
    escaped = Sprint(board_id=board.id, project_id=project.id, name="S1", goal="a &gt; b", state="future",
                     sequence_number=1, name_normalized="s1")
    literal = Sprint(board_id=board.id, project_id=project.id, name="S2", goal="fish &chips", state="future",
                     sequence_number=2, name_normalized="s2")
    db_session.add_all([escaped, literal])
    db_session.flush()

    database._unescape_plain_text_fields(db_session.connection())
    goals = dict(db_session.connection().execute(text("SELECT name, goal FROM sprints")).all())
    assert goals == {"S1": "a > b", "S2": "fish &chips"}


class _FakeNested:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakePgConn:
    """Records the SQL a PostgreSQL-only reconciler sends; ``fail`` makes matching statements raise."""

    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, pending=(), fail=()):
        self.sql = []
        self.pending = pending
        self.fail = fail
        self.committed = 0
        self.rolled_back = 0

    def begin_nested(self):
        return _FakeNested(self)

    def execute(self, stmt, params=None):
        from sqlalchemy.exc import SQLAlchemyError

        text_ = str(stmt)
        if any(marker in text_ for marker in self.fail):
            raise SQLAlchemyError("refused")
        self.sql.append(text_)
        if "pg_constraint" in text_:
            return [(name,) for name in self.pending]
        return SimpleNamespace(scalar=lambda: True)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _fake_inspector(existing_checks, broken=False):
    from sqlalchemy.exc import SQLAlchemyError

    def get_check_constraints(_table):
        if broken:
            raise SQLAlchemyError("cannot read constraints")
        return [{"name": n} for n in existing_checks]

    return SimpleNamespace(get_check_constraints=get_check_constraints)


def test_check_constraints_are_added_not_valid_then_validated(monkeypatch):
    import sqlalchemy

    import app.database as database

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _conn: _fake_inspector([]))
    conn = _FakePgConn()
    database._add_missing_check_constraints(conn)
    added = [s for s in conn.sql if "NOT VALID" in s]
    validated = [s for s in conn.sql if "VALIDATE CONSTRAINT" in s]
    assert added
    assert len(validated) == len(added)


def test_check_constraints_revalidate_pending_ones_and_tolerate_failures(monkeypatch, caplog):
    import sqlalchemy

    import app.database as database

    names = [c.name for t in database.Base.metadata.sorted_tables for c in t.constraints
             if c.__class__.__name__ == "CheckConstraint" and c.name]
    assert names
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _conn: _fake_inspector(names))

    conn = _FakePgConn(pending=[names[0]])
    database._add_missing_check_constraints(conn)
    assert [s for s in conn.sql if "VALIDATE CONSTRAINT" in s] == [
        s for s in conn.sql if f'VALIDATE CONSTRAINT "{names[0]}"' in s
    ]
    assert not [s for s in conn.sql if "NOT VALID" in s]

    refusing = _FakePgConn(pending=[names[0]], fail=("VALIDATE CONSTRAINT",))
    with caplog.at_level("WARNING", logger="bug_hunter"):
        database._add_missing_check_constraints(refusing)
    assert "stays NOT VALID" in caplog.text

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _conn: _fake_inspector([]))
    rejecting = _FakePgConn(fail=("NOT VALID",))
    with caplog.at_level("ERROR", logger="bug_hunter"):
        database._add_missing_check_constraints(rejecting)
    assert "could not add constraint" in caplog.text
    assert not [s for s in rejecting.sql if "VALIDATE CONSTRAINT" in s]

    monkeypatch.setattr(sqlalchemy, "inspect", lambda _conn: _fake_inspector([], broken=True))
    unreadable = _FakePgConn()
    database._add_missing_check_constraints(unreadable)
    assert not [s for s in unreadable.sql if "NOT VALID" in s]


def test_check_constraints_are_left_alone_off_postgresql():
    import app.database as database

    conn = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    assert database._add_missing_check_constraints(conn) is None


class _PgEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        from contextlib import nullcontext

        return nullcontext(self._conn)


def _stub_migration_steps(monkeypatch, database, order):
    monkeypatch.setattr(database._settings, "DATABASE_URL", "postgresql+psycopg://u:p@db/bh")
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda bind: order.append("create_all"))
    for step in ("_add_missing_columns", "_retire_legacy_branch_index", "_add_missing_indexes",
                 "_seed_global_workflow_statuses", "_backfill_bug_display_id", "_align_agile_hierarchy",
                 "_add_missing_check_constraints"):
        monkeypatch.setattr(database, step, lambda conn, _n=step: order.append(_n))
    monkeypatch.setattr(database, "_run_once", lambda conn, name, fn: order.append(name))


def test_postgres_init_runs_every_step_under_the_advisory_lock(monkeypatch):
    import app.database as database

    order = []
    conn = _FakePgConn()
    _stub_migration_steps(monkeypatch, database, order)
    monkeypatch.setattr(database, "engine", _PgEngine(conn))
    seeded = []

    assert database.init_db(on_migrated=lambda: seeded.append(True)) is True
    assert order[0] == "create_all"
    assert order[-1] == "_add_missing_check_constraints"
    assert "plain_text_storage_v1" in order
    assert seeded == [True]
    assert any("pg_advisory_unlock" in s for s in conn.sql)


def test_postgres_init_skips_migration_when_another_replica_holds_the_lock(monkeypatch, caplog):
    import app.database as database

    class _Busy(_FakePgConn):
        def execute(self, stmt, params=None):
            self.sql.append(str(stmt))
            return SimpleNamespace(scalar=lambda: False)

    order = []
    _stub_migration_steps(monkeypatch, database, order)
    monkeypatch.setattr(database, "engine", _PgEngine(_Busy()))
    with caplog.at_level("INFO", logger="bug_hunter"):
        assert database.init_db() is False
    assert order == []
    assert "another replica holds the migration lock" in caplog.text


def test_postgres_init_rolls_back_a_failed_step_and_still_unlocks(monkeypatch):
    import pytest
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    order = []
    conn = _FakePgConn()
    _stub_migration_steps(monkeypatch, database, order)

    def _boom(_conn):
        raise RuntimeError("step failed")

    monkeypatch.setattr(database, "_add_missing_indexes", _boom)
    monkeypatch.setattr(database, "engine", _PgEngine(conn))
    with pytest.raises(RuntimeError, match="step failed"):
        database.init_db()
    assert conn.rolled_back == 1
    assert any("pg_advisory_unlock" in s for s in conn.sql)

    class _LockError(_FakePgConn):
        def execute(self, stmt, params=None):
            raise SQLAlchemyError("lock query failed")

    monkeypatch.setattr(database, "_add_missing_indexes", lambda _c: order.append("indexes"))
    flaky = _LockError()
    monkeypatch.setattr(database, "engine", _PgEngine(flaky))
    assert database.init_db() is True
    assert flaky.rolled_back >= 2


def test_workflow_seed_is_idempotent_and_reports_drift(db_session, caplog):
    from sqlalchemy import func, select

    import app.database as database
    from app.models import WorkflowStatus
    from app.schemas import STATUSES_BY_TYPE

    conn = db_session.connection()
    before = conn.execute(select(func.count(WorkflowStatus.id))).scalar()
    database._seed_global_workflow_statuses(conn)  # already seeded at startup: nothing new
    assert conn.execute(select(func.count(WorkflowStatus.id))).scalar() == before

    original = STATUSES_BY_TYPE["Task"]
    STATUSES_BY_TYPE["Task"] = [*original, "Invented"]
    try:
        with caplog.at_level("ERROR", logger="bug_hunter"):
            database._seed_global_workflow_statuses(conn)
    finally:
        STATUSES_BY_TYPE["Task"] = original
    assert "Workflow-status seed drift for item_type=Task" in caplog.text


def test_workflow_seed_tolerates_a_missing_table_and_a_refused_insert(caplog):
    from sqlalchemy.exc import SQLAlchemyError

    import app.database as database

    class _NoTable:
        def execute(self, *_a, **_k):
            raise SQLAlchemyError("no such table: workflow_statuses")

    database._seed_global_workflow_statuses(_NoTable())

    class _Refusing:
        def execute(self, stmt, *_a, **_k):
            return SimpleNamespace(all=lambda: [])

        def begin_nested(self):
            raise SQLAlchemyError("duplicate key")

    with caplog.at_level("WARNING", logger="bug_hunter"):
        database._seed_global_workflow_statuses(_Refusing())
    assert "Could not seed global workflow status" in caplog.text


def test_display_id_indexes_are_rebuilt_non_unique(monkeypatch):
    from sqlalchemy import Column, Index, Integer, MetaData, String, Table, create_engine, inspect

    import app.database as database

    meta = MetaData()
    table = Table("boards", meta, Column("id", Integer, primary_key=True), Column("display_id", String(32)))
    Index("ix_boards_display_id", table.c.display_id, unique=True)
    monkeypatch.setattr(database.Base, "metadata", SimpleNamespace(sorted_tables=[table]))

    probe = create_engine("sqlite://")
    with probe.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE boards (id INTEGER PRIMARY KEY, display_id VARCHAR(32))")
        database._add_missing_indexes(conn)
        indexes = inspect(conn).get_indexes("boards")
    assert [(i["name"], bool(i["unique"])) for i in indexes] == [("ix_boards_display_id", False)]
    probe.dispose()


def test_meta_serves_poll_intervals_from_settings(client, monkeypatch):
    from app.main import settings
    j = client.get("/api/meta").json()
    assert (j["data_poll_seconds"], j["session_poll_seconds"], j["notification_poll_seconds"]) == (
        settings.DATA_POLL_SECONDS, settings.SESSION_POLL_SECONDS, settings.NOTIFICATION_POLL_SECONDS)
    monkeypatch.setattr(settings, "DATA_POLL_SECONDS", 99)
    assert client.get("/api/meta").json()["data_poll_seconds"] == 99
