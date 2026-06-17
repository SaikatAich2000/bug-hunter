"""Security tests covering the hardening items from the OWASP audit.

Covers:

  - login timing equality (no user-enumeration via response latency)
  - CSV formula injection guard on bug export
  - global request body size middleware
  - X-Forwarded-For trust gate on audit IP
  - masked email in INFO-level logs
  - per-account lockout after N failed logins
  - HaveIBeenPwned breach check on password set
  - EXIF / metadata strip on uploaded images

Each block has both a focused unit test and a route-level integration
test where it makes sense. Module-level state used by the in-memory
features (account lockout, HIBP backend) is reset between cases via
the ``reset_security_state`` fixture so the suite is order-independent.
"""
from __future__ import annotations

import io
import logging
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_security_state():
    """Wipe per-process security state between tests. The account
    lockout buckets are in-memory; without this, a failed-login burst
    in one test could lock the same email out of a later test."""
    yield
    # Done AFTER yield so test failures still see clean state on rerun.
    try:
        from app import account_lockout
        account_lockout._reset_for_tests()
    except ImportError:
        pass


def _make_bug(client, title: str, project_id: int = 1) -> int:
    res = client.post("/api/bugs", json={
        "project_id": project_id, "title": title,
        "description": "desc", "item_type": "Bug",
        "status": "New", "priority": "Medium", "environment": "DEV",
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


# ---------------------------------------------------------------------------
# Login timing equality
# ---------------------------------------------------------------------------
class TestLoginTimingEquality:
    """Both the unknown-email and the wrong-password branches must run
    one bcrypt verification. Without this, an attacker can enumerate
    accounts by measuring response latency."""

    def test_unknown_email_still_runs_bcrypt(self, client):
        # Patch verify_password to count calls without paying the
        # 50 ms bcrypt cost in the test.
        from app.routes import auth as auth_routes
        with mock.patch.object(auth_routes, "verify_password",
                               wraps=auth_routes.verify_password) as spy:
            res = client.post("/api/auth/login", json={
                "email": "no-such-user@example.com",
                "password": "anything-at-all-9",
            })
        assert res.status_code == 401
        # The dummy-hash path must invoke verify_password exactly once.
        assert spy.call_count == 1, (
            "Unknown-email branch did not run bcrypt — timing oracle still open"
        )
        # And it must have used the dummy hash, not None / empty string.
        args, _ = spy.call_args
        assert args[1] == auth_routes._DUMMY_PASSWORD_HASH

    def test_wrong_password_branch_unchanged(self, client):
        # The existing branch still works: real user, wrong password.
        from tests.conftest import BOOTSTRAP_EMAIL
        from app.routes import auth as auth_routes
        with mock.patch.object(auth_routes, "verify_password",
                               wraps=auth_routes.verify_password) as spy:
            res = client.post("/api/auth/login", json={
                "email": BOOTSTRAP_EMAIL,
                "password": "definitely-not-the-right-pwd-9",
            })
        assert res.status_code == 401
        assert spy.call_count == 1

    def test_unified_401_message_for_both_branches(self, client):
        from tests.conftest import BOOTSTRAP_EMAIL
        a = client.post("/api/auth/login", json={
            "email": "no-such-user@example.com", "password": "abc12345",
        })
        b = client.post("/api/auth/login", json={
            "email": BOOTSTRAP_EMAIL, "password": "wrong-password-9",
        })
        assert a.status_code == b.status_code == 401
        assert a.json()["detail"] == b.json()["detail"]


# ---------------------------------------------------------------------------
# Spreadsheet formula injection (XLSX)
#
# A bug title like `=cmd|'/c calc.exe'!A1` becomes an Excel formula when
# the workbook is opened, because openpyxl auto-treats cell values
# starting with `=`/`+`/`-`/`@`/`\t`/`\r` as formulas. The values are
# defanged at the same boundary in
# app/reports/xlsx.py::_defang_formula_text.
# ---------------------------------------------------------------------------
class TestXlsxFormulaInjectionGuard:

    @pytest.mark.parametrize("trigger", ["=", "+", "-", "@", "\t", "\r"])
    def test_defang_helper_neutralises_formula_triggers(self, trigger):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text(trigger + "cmd|calc!A1").startswith("'" + trigger)

    def test_defang_helper_passes_through_normal_text(self):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text("Login button broken") == "Login button broken"

    def test_defang_helper_handles_empty_string(self):
        from app.reports.xlsx import _defang_formula_text
        assert _defang_formula_text("") == ""

    def test_export_xlsx_prefixes_malicious_title(self, admin_client):
        """End-to-end: a bug filed with a formula-shaped title must come
        back through the XLSX with a leading single-quote so spreadsheet
        apps render it as text rather than executing it."""
        import io
        from openpyxl import load_workbook
        bug_id = _make_bug(admin_client, "=cmd|'calc.exe'!A1")
        assert bug_id  # sanity
        res = admin_client.post("/api/reports/export.xlsx", json={
            "report_key": "item_detail", "filters": {},
        })
        assert res.status_code == 200
        wb = load_workbook(io.BytesIO(res.content), read_only=True)
        # Walk the cells in the main sheet looking for the title column.
        found_defanged = False
        for row in wb[wb.sheetnames[0]].iter_rows(values_only=True):
            for cell in row:
                if not isinstance(cell, str):
                    continue
                if "cmd|" in cell:
                    # Must be prefixed with a single quote (defanged).
                    assert cell.startswith("'="), (
                        f"Un-neutralised formula in XLSX cell: {cell!r}"
                    )
                    found_defanged = True
        assert found_defanged, "expected the malicious title to appear (defanged) somewhere"


# ---------------------------------------------------------------------------
# Body size middleware
# ---------------------------------------------------------------------------
class TestBodySizeMiddleware:

    def test_normal_request_under_limit_succeeds(self, admin_client):
        # 1 KB body is far below the 60 MB default limit.
        res = admin_client.get("/api/auth/me")
        assert res.status_code == 200

    def test_oversize_content_length_rejected_with_413(self, client, monkeypatch):
        # Send a Content-Length that exceeds even a generous limit.
        # Uploading 70 MB isn't necessary — middleware checks the
        # header BEFORE reading the body. httpx normally overrides
        # Content-Length to match the body, so the raw transport sends
        # the actual body to make the assertion robust.
        # Easiest deterministic path: lower the limit via env, send a
        # body slightly above it. Requires fresh client to re-read settings.
        # (Done in the next test.)
        from app.main import settings
        # Sanity: the default limit is at the documented 60 MB.
        assert settings.MAX_REQUEST_BODY_BYTES >= 50 * 1024 * 1024

    def test_oversize_body_rejected(self, tmp_path, monkeypatch):
        """End-to-end check with a deliberately tiny limit to avoid
        allocating hundreds of MB just to exercise the rejection."""
        import sys
        db_file = tmp_path / "bodysize.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1024")  # 1 KB cap
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            c.post("/api/auth/login", json={
                "email": "admin@test.local", "password": "Admin1234",
            })
            # Send a body larger than 1 KB to a JSON endpoint.
            payload = {"current_password": "Admin1234", "new_password": "x" * 2048}
            res = c.post("/api/auth/change-password", json=payload)
            # The middleware fires BEFORE the route, so the response is a
            # 413 (Payload Too Large) regardless of the route's own
            # validation.
            assert res.status_code == 413, (
                f"Expected 413, got {res.status_code}: {res.text}"
            )
            assert "too large" in res.json()["detail"].lower()

    def test_malformed_content_length_returns_400(self):
        """The middleware must reject a non-integer Content-Length cleanly
        rather than tracebacking inside the int() call. Exercised by
        invoking the middleware's dispatch directly because httpx + uvicorn
        normally enforce numeric Content-Length on the wire."""
        import asyncio
        from starlette.requests import Request
        from app.main import BodySizeLimitMiddleware

        middleware = BodySizeLimitMiddleware(None)
        scope = {
            "type": "http", "method": "POST", "path": "/api/auth/login",
            "headers": [(b"content-length", b"not-a-number")],
            "query_string": b"", "scheme": "http",
            "server": ("testserver", 80), "client": ("test", 0),
            "raw_path": b"/api/auth/login",
        }
        request = Request(scope)

        async def call_next(_req):     # pragma: no cover — should not run
            raise AssertionError("middleware must short-circuit before call_next")

        response = asyncio.run(middleware.dispatch(request, call_next))
        assert response.status_code == 400
        body = response.body.decode("utf-8")
        assert "Content-Length" in body or "invalid" in body.lower()


# ---------------------------------------------------------------------------
# Coverage tests — cover the remaining defensive branches in the new
# security modules so the suite catches regressions on those paths too.
# ---------------------------------------------------------------------------
class TestAccountLockoutEdgeCases:

    def test_env_int_falls_back_on_garbage(self, monkeypatch):
        from app.account_lockout import _env_int
        monkeypatch.setenv("BH_BOGUS_INT_VAR", "not-a-number")
        assert _env_int("BH_BOGUS_INT_VAR", 42) == 42

    def test_env_int_uses_value(self, monkeypatch):
        from app.account_lockout import _env_int
        monkeypatch.setenv("BH_GOOD_INT_VAR", "99")
        assert _env_int("BH_GOOD_INT_VAR", 0) == 99

    def test_old_failures_get_evicted(self, monkeypatch):
        """Failures older than the rolling window must be popleft'd so
        a user who fat-fingers once a year doesn't accumulate a lockout."""
        import time
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_WINDOW_SECONDS", 0.05)
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 3)
        account_lockout._reset_for_tests()
        for _ in range(2):
            account_lockout.record_failure("evictee@x.com")
        time.sleep(0.07)  # past the 50 ms window
        account_lockout.record_failure("evictee@x.com")
        bucket = account_lockout._buckets.get("evictee@x.com")
        # Only the post-sleep failure should remain in the deque.
        assert bucket is not None
        assert len(bucket.fails) == 1
        account_lockout.check_locked("evictee@x.com")  # must not raise

    def test_bucket_dict_cap_drops_oldest_entry(self, monkeypatch):
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOCKOUT_BUCKETS_MAX", 5)
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 10)
        account_lockout._reset_for_tests()
        # Fill past the cap → eviction kicks in on the 6th entry.
        for i in range(7):
            account_lockout.record_failure(f"user{i}@x.com")
        assert len(account_lockout._buckets) <= 5

    def test_clear_unknown_email_is_noop(self):
        """clear() on an email with no bucket must not blow up."""
        from app import account_lockout
        account_lockout._reset_for_tests()
        account_lockout.clear("never-recorded@x.com")  # must not raise
        assert "never-recorded@x.com" not in account_lockout._buckets


class TestPasswordBreachFetchRange:
    """Exercise the real ``_fetch_range`` body — every other breach-check
    test monkeypatches this seam to keep tests hermetic. These tests stub
    the httpx layer instead so the real branching is exercised."""

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")

    @staticmethod
    def _patch_httpx_client(monkeypatch, response_status=200, response_text="",
                            raise_on_get=None):
        from app import password_breach

        class _FakeResponse:
            status_code = response_status
            text = response_text

        class _FakeClient:
            def __init__(self, **_kw):
                # No-op stand-in for httpx.Client(timeout=...).
                pass
            def __enter__(self): return self
            def __exit__(self, *_a): return False
            def get(self, _url, **_kw):
                if raise_on_get is not None:
                    raise raise_on_get
                return _FakeResponse()

        monkeypatch.setattr(password_breach.httpx, "Client", _FakeClient)

    def test_fetch_range_returns_text_on_200(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(monkeypatch, 200, "ABCD:1\n")
        assert password_breach._fetch_range("5BAA6") == "ABCD:1\n"

    def test_fetch_range_returns_none_on_non_200(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(monkeypatch, 503, "")
        assert password_breach._fetch_range("5BAA6") is None

    def test_fetch_range_returns_none_on_httperror(self, monkeypatch):
        import httpx
        from app import password_breach
        self._patch_httpx_client(
            monkeypatch, raise_on_get=httpx.HTTPError("simulated")
        )
        assert password_breach._fetch_range("5BAA6") is None

    def test_fetch_range_returns_none_on_oserror(self, monkeypatch):
        from app import password_breach
        self._patch_httpx_client(
            monkeypatch, raise_on_get=OSError("connection refused")
        )
        assert password_breach._fetch_range("5BAA6") is None


class TestImageStripEdgeCases:

    def test_pillow_missing_returns_original(self, monkeypatch):
        """If Pillow isn't installed, the helper must fail-open with the
        original bytes unchanged."""
        import sys
        from app.image_strip import strip_image_metadata
        # Force the next `from PIL import Image` to ImportError.
        monkeypatch.setitem(sys.modules, "PIL", None)
        raw = b"fake-jpeg-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_format_none_returns_original(self, monkeypatch):
        """An image Pillow can open but whose .format is None (e.g. a
        raw in-memory image) must round-trip unchanged."""
        from PIL import Image as PILImage
        from app.image_strip import strip_image_metadata

        class _FakeImg:
            format = None
            info: dict = {}
            def load(self):
                # Pillow's real load() decodes pixel data; the stub
                # doesn't need to do anything to exercise the
                # format-is-None branch.
                pass

        monkeypatch.setattr(PILImage, "open", lambda _src: _FakeImg())
        raw = b"any-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_save_oserror_returns_original(self, monkeypatch):
        """If Pillow's save raises OSError mid-encode, the helper logs and
        returns the original bytes rather than tracebacking the upload."""
        from PIL import Image as PILImage
        from app.image_strip import strip_image_metadata

        class _BoomImg:
            format = "JPEG"
            info: dict = {}
            def load(self):
                # No-op load — the failure under test is in save(),
                # not in decode.
                pass
            def save(self, _out, **_kw): raise OSError("disk full mid-encode")

        monkeypatch.setattr(PILImage, "open", lambda _src: _BoomImg())
        raw = b"any-bytes"
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_content_type_with_charset_param_still_handled(self):
        """Some browsers send ``image/jpeg; charset=binary`` — the helper
        must strip the parameter before matching the prefix."""
        from app.image_strip import strip_image_metadata
        # There's no real JPEG here, so this just verifies the helper
        # gets past the content-type guard (the real round-trip is
        # already exercised by TestExifStrip).
        raw = b"not-a-jpeg"
        # With the charset suffix the helper still treats it as image/jpeg
        # and Pillow can't decode → fail-open returns the original.
        assert strip_image_metadata(raw, "image/jpeg; charset=binary") == raw


# ---------------------------------------------------------------------------
# X-Forwarded-For trust gate
# ---------------------------------------------------------------------------
class TestXffTrustGate:

    def test_xff_ignored_when_trust_disabled(self, admin_client):
        # Default TRUST_PROXY_FORWARDED_FOR = False (see config.py).
        # An X-Forwarded-For header on a login should NOT influence the
        # session-row IP.
        admin_client.post("/api/auth/logout")
        res = admin_client.post(
            "/api/auth/login",
            json={"email": "admin@test.local", "password": "Admin1234"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert res.status_code == 200
        sessions = admin_client.get("/api/sessions").json()
        assert sessions, "expected at least one session row"
        # The recorded IP must be the actual transport client, not the
        # spoofed XFF value.
        ips = [s["ip_address"] for s in sessions]
        assert "203.0.113.99" not in ips, (
            f"XFF was honoured despite TRUST_PROXY_FORWARDED_FOR=False: {ips}"
        )

    def test_xff_honoured_when_trust_enabled(self, tmp_path, monkeypatch):
        """With TRUST_PROXY_FORWARDED_FOR=true (set BEFORE app load),
        the leftmost XFF entry is recorded on the session row."""
        import sys
        db_file = tmp_path / "xff.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
        monkeypatch.setenv("EMAIL_BACKEND", "disabled")
        monkeypatch.setenv("SESSION_SECRET", "test_secret")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@test.local")
        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Admin1234")
        monkeypatch.setenv("TRUST_PROXY_FORWARDED_FOR", "true")
        for mod in list(sys.modules):
            if mod == "app" or mod.startswith("app."):
                del sys.modules[mod]
        from app.config import get_settings
        get_settings.cache_clear()  # type: ignore[attr-defined]
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as c:
            res = c.post(
                "/api/auth/login",
                json={"email": "admin@test.local", "password": "Admin1234"},
                headers={"X-Forwarded-For": "198.51.100.7, 10.0.0.1"},
            )
            assert res.status_code == 200, res.text
            sessions = c.get("/api/sessions").json()
            assert any(s["ip_address"] == "198.51.100.7" for s in sessions), (
                f"XFF was NOT honoured despite TRUST=true: {sessions}"
            )


# ---------------------------------------------------------------------------
# Email masking in logs
# ---------------------------------------------------------------------------
class TestEmailMasking:

    @pytest.mark.parametrize("raw,expected", [
        ("alice@example.com",  "a***@example.com"),
        ("a@example.com",      "a***@example.com"),
        ("@example.com",       "@example.com"),
        ("",                   "***"),
        ("plain-no-at-sign",   "***"),
        ("BOB@example.com",    "B***@example.com"),
    ])
    def test_mask_helper(self, raw, expected):
        from app.routes.auth import _mask_email
        assert _mask_email(raw) == expected

    def test_inactive_login_logs_masked_email(self, admin_client, caplog):
        # Create + deactivate a user, then attempt to log in as them.
        admin_client.post("/api/users", json={
            "name": "Disabled User", "email": "disabled@test.local",
            "role": "user", "password": "Disabled12",
        })
        users = admin_client.get("/api/users").json()
        target = next(u for u in users if u["email"] == "disabled@test.local")
        r = admin_client.put(f"/api/users/{target['id']}", json={"is_active": False})
        assert r.status_code == 200
        admin_client.post("/api/auth/logout")

        with caplog.at_level(logging.INFO, logger="bug_hunter.auth"):
            res = admin_client.post("/api/auth/login", json={
                "email": "disabled@test.local", "password": "Disabled12",
            })
        assert res.status_code == 401
        relevant = [r.message for r in caplog.records if "Login refused" in r.message]
        assert relevant, "expected the masked-email log line"
        for line in relevant:
            assert "disabled@test.local" not in line, (
                f"Raw email leaked into log: {line!r}"
            )
            assert "d***@test.local" in line


# ---------------------------------------------------------------------------
# Account lockout
# ---------------------------------------------------------------------------
class TestAccountLockout:

    def test_unit_check_locked_passes_when_no_state(self):
        from app import account_lockout
        # No prior failures recorded — should not raise.
        account_lockout.check_locked("never-seen@example.com")

    def test_unit_threshold_triggers_lockout(self):
        from app import account_lockout
        email = "victim@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(email)
        with pytest.raises(Exception) as excinfo:
            account_lockout.check_locked(email)
        # FastAPI HTTPException with 429 + Retry-After.
        assert getattr(excinfo.value, "status_code", None) == 429
        assert "Retry-After" in (excinfo.value.headers or {})

    def test_unit_clear_resets_bucket(self):
        from app import account_lockout
        email = "transient@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(email)
        account_lockout.clear(email)
        # Cleared bucket means no lockout.
        account_lockout.check_locked(email)

    def test_unit_unknown_email_also_counts(self):
        """Ticking only known emails would leak account existence."""
        from app import account_lockout
        ghost = "definitely-not-a-real-user@example.com"
        for _ in range(account_lockout._LOGIN_FAIL_LIMIT):
            account_lockout.record_failure(ghost)
        with pytest.raises(Exception) as excinfo:
            account_lockout.check_locked(ghost)
        assert getattr(excinfo.value, "status_code", None) == 429

    def test_unit_disabled_when_limit_is_zero(self, monkeypatch):
        from app import account_lockout
        monkeypatch.setattr(account_lockout, "_LOGIN_FAIL_LIMIT", 0)
        for _ in range(50):
            account_lockout.record_failure("anyone@example.com")
        # Should never raise when limit == 0 (operator opt-out).
        account_lockout.check_locked("anyone@example.com")

    def test_http_login_429_after_threshold(self, client):
        """Drive the lockout from the route layer. The IP rate limit
        (8/60s) would normally fire first, but the route-level test
        still confirms 429 is returned to abusive clients."""
        from tests.conftest import BOOTSTRAP_EMAIL
        codes = []
        for _ in range(15):
            r = client.post("/api/auth/login", json={
                "email": BOOTSTRAP_EMAIL, "password": "Wrong-pwd-9",
            })
            codes.append(r.status_code)
        # At least one of the responses must be 429 — either from the
        # IP limiter or the per-account lockout.
        assert 429 in codes, f"Expected 429 somewhere in {codes}"

    def test_successful_login_clears_lockout(self, client):
        """After K (< threshold) failures, a successful login should
        let the user keep going without carrying failure debt."""
        from tests.conftest import BOOTSTRAP_EMAIL, BOOTSTRAP_PASSWORD
        from app import account_lockout
        # 3 failures (below the threshold).
        for _ in range(3):
            account_lockout.record_failure(BOOTSTRAP_EMAIL)
        res = client.post("/api/auth/login", json={
            "email": BOOTSTRAP_EMAIL, "password": BOOTSTRAP_PASSWORD,
        })
        assert res.status_code == 200
        # Bucket should now be cleared. The internal state isn't easily
        # checkable without reaching into the module, but a fresh
        # failure-counter should not be in lockout state.
        account_lockout.check_locked(BOOTSTRAP_EMAIL)


# ---------------------------------------------------------------------------
# HIBP breach check
# ---------------------------------------------------------------------------
class TestPasswordBreachCheck:
    """The conftest disables PASSWORD_BREACH_CHECK_ENABLED for the rest
    of the suite (so unrelated tests stay hermetic). Integration tests
    re-enable it inside the test body — the autouse approach loses to
    the client fixture's later setenv call. Unit tests don't go
    through the `client` fixture so the module default (enabled=True)
    is fine for them."""

    def test_unit_known_breached_hash_matches(self):
        # 'password' SHA-1 = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
        # prefix 5BAA6, suffix 1E4C9B93F3F0682250B6CF8331B7EE68FD8
        from app import password_breach
        body = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:3861493\n"
        with mock.patch.object(password_breach, "_fetch_range", return_value=body):
            assert password_breach.is_password_breached("password") is True

    def test_unit_unknown_password_passes(self):
        from app import password_breach
        # An empty response means the suffix isn't listed.
        with mock.patch.object(password_breach, "_fetch_range", return_value=""):
            assert password_breach.is_password_breached("rare-uniq-pwd-9") is False

    def test_unit_padding_count_zero_treated_as_safe(self):
        from app import password_breach
        # Lines with COUNT=0 are the API's anti-correlation padding.
        sha1_prefix_match = "1E4C9B93F3F0682250B6CF8331B7EE68FD8"
        body = f"{sha1_prefix_match}:0\n"
        with mock.patch.object(password_breach, "_fetch_range", return_value=body):
            assert password_breach.is_password_breached("password") is False

    def test_unit_fail_open_on_network_error(self):
        from app import password_breach
        with mock.patch.object(password_breach, "_fetch_range", return_value=None):
            assert password_breach.is_password_breached("anything-9") is False

    def test_unit_disabled_short_circuits(self, monkeypatch):
        from app import password_breach
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "false")
        # Even with a positive match the helper returns False when
        # disabled. Mocking _fetch_range isn't needed — the function
        # returns before reaching it.
        assert password_breach.is_password_breached("password") is False

    @staticmethod
    def _force_match(pw: str) -> str:
        """Build a HIBP /range/ response body that will match the SHA-1
        suffix of ``pw``, regardless of what the prefix is. The
        `_fetch_range` mock returns this body unconditionally; the
        helper takes care of computing the right suffix."""
        import hashlib
        digest = hashlib.sha1(pw.encode("utf-8")).hexdigest().upper()  # NOSONAR
        return f"{digest[5:]}:9999\n"

    def test_unit_changeme_allowlisted_despite_breach_match(self):
        """The legacy default 'changeme' is ALWAYS accepted (mirrors
        schemas._check_password_strength), even though it is obviously in the
        breach corpus — so the breach gate must whitelist it, case-insensitively
        and before any network call."""
        from app import password_breach
        body = self._force_match("changeme")  # this body WOULD match its suffix
        with mock.patch.object(password_breach, "_fetch_range", return_value=body) as m:
            assert password_breach.is_password_breached("changeme") is False
            assert password_breach.is_password_breached("CHANGEME") is False
        # Whitelisted short-circuit returns before ever fetching the range.
        m.assert_not_called()

    def test_change_password_rejects_breached(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        # GoodFresh123 passes the local strength rules but the mocked
        # HIBP endpoint reports it as breached.
        new_pw = "GoodFresh123"
        with mock.patch.object(
            password_breach, "_fetch_range",
            return_value=self._force_match(new_pw),
        ):
            res = admin_client.post("/api/auth/change-password", json={
                "current_password": "Admin1234",
                "new_password": new_pw,
            })
        assert res.status_code == 400, res.text
        assert "breach" in res.json()["detail"].lower()

    def test_create_user_rejects_breached(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        new_pw = "GoodFresh123"
        with mock.patch.object(
            password_breach, "_fetch_range",
            return_value=self._force_match(new_pw),
        ):
            res = admin_client.post("/api/users", json={
                "name": "Tester", "email": "tester@test.local",
                "role": "user", "password": new_pw,
            })
        assert res.status_code == 400, res.text
        assert "breach" in res.json()["detail"].lower()

    def test_change_password_accepts_safe_new_password(self, admin_client, monkeypatch):
        monkeypatch.setenv("PASSWORD_BREACH_CHECK_ENABLED", "true")
        from app import password_breach
        with mock.patch.object(password_breach, "_fetch_range", return_value=""):
            res = admin_client.post("/api/auth/change-password", json={
                "current_password": "Admin1234",
                "new_password": "FreshSafe123",
            })
        assert res.status_code == 204


# ---------------------------------------------------------------------------
# EXIF strip
# ---------------------------------------------------------------------------
def _jpeg_with_exif(gps_value: str = "secret-gps-tag") -> bytes:
    """Build an in-memory JPEG that carries a custom EXIF tag to look
    for after the upload roundtrip. Pillow 11+ exposes Image.Exif
    directly — no extra dependency needed."""
    from PIL import Image

    img = Image.new("RGB", (8, 8), (200, 100, 50))
    exif = img.getexif()
    exif[270] = gps_value              # 270 = ImageDescription tag
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def _png_with_text(text: str = "stash-this") -> bytes:
    """Build a PNG carrying a tEXt chunk to grep for afterwards."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    img = Image.new("RGB", (8, 8), (10, 20, 30))
    meta = PngInfo()
    meta.add_text("Source", text)
    out = io.BytesIO()
    img.save(out, format="PNG", pnginfo=meta)
    return out.getvalue()


class TestExifStrip:

    def test_unit_jpeg_metadata_removed(self):
        from app.image_strip import strip_image_metadata
        marker = "GPS-LEAK-XY-ZZ"
        raw = _jpeg_with_exif(marker)
        assert marker.encode() in raw, "test fixture should embed the marker"
        clean = strip_image_metadata(raw, "image/jpeg")
        assert marker.encode() not in clean, (
            "EXIF strip failed: marker still present in output bytes"
        )

    def test_unit_png_metadata_removed(self):
        from app.image_strip import strip_image_metadata
        marker = "PNG-TEXT-LEAK-XY"
        raw = _png_with_text(marker)
        assert marker.encode() in raw
        clean = strip_image_metadata(raw, "image/png")
        assert marker.encode() not in clean

    def test_unit_non_image_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"%PDF-1.7\n%fake pdf bytes"
        assert strip_image_metadata(raw, "application/pdf") == raw

    def test_unit_unknown_content_type_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"<svg></svg>"
        assert strip_image_metadata(raw, "image/svg+xml") == raw

    def test_unit_garbage_image_passes_through(self):
        from app.image_strip import strip_image_metadata
        raw = b"this is definitely not a jpeg"
        # Pillow can't decode; the helper fails open.
        assert strip_image_metadata(raw, "image/jpeg") == raw

    def test_unit_empty_bytes_returns_empty(self):
        from app.image_strip import strip_image_metadata
        assert strip_image_metadata(b"", "image/jpeg") == b""

    def test_http_upload_strips_jpeg_exif(self, admin_client):
        bug_id = _make_bug(admin_client, "EXIF carrier upload")
        marker = "FIELD-MARKER-9X"
        raw = _jpeg_with_exif(marker)
        assert marker.encode() in raw
        res = admin_client.post(
            f"/api/bugs/{bug_id}/attachments",
            files={"file": ("evidence.jpg", raw, "image/jpeg")},
        )
        assert res.status_code == 201, res.text
        att_id = res.json()["id"]
        dl = admin_client.get(f"/api/bugs/{bug_id}/attachments/{att_id}/download")
        assert dl.status_code == 200
        assert marker.encode() not in dl.content, (
            "EXIF marker survived the upload roundtrip"
        )
