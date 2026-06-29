"""Tests for app/fcm_transport.py — the optional Firebase Cloud Messaging transport.

firebase_admin is imported lazily and every failure is caught and logged rather
than raised. A fake firebase_admin (with credentials and messaging submodules)
is injected so all branches — init success/failure, dead-token pruning, the
https-only webpush link, never-raises guarantees — run without a live Firebase
project. app.push_service mocks send() wholesale in its own tests; this file
exercises the transport layer directly.
"""
from __future__ import annotations

import sys
import types

import pytest

from app import fcm_transport


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, success: bool, exception=None):
        self.success = success
        self.exception = exception


class _Multicast:
    def __init__(self, responses):
        self.responses = responses


class UnregisteredError(Exception):
    """Name-matches fcm_transport._DEAD_TOKEN_ERRORS."""


class SenderIdMismatchError(Exception):
    """Name-matches fcm_transport._DEAD_TOKEN_ERRORS."""


def _make_messaging(*, multicast=None, send_raises=False):
    m = types.ModuleType("firebase_admin.messaging")
    m.WebpushConfig = lambda **kw: ("WebpushConfig", kw)
    m.WebpushFCMOptions = lambda **kw: ("WebpushFCMOptions", kw)
    m.Notification = lambda **kw: ("Notification", kw)
    m.MulticastMessage = lambda **kw: ("MulticastMessage", kw)

    captured = {}

    def send_each_for_multicast(message, app=None):
        captured["message"] = message
        captured["app"] = app
        if send_raises:
            raise RuntimeError("send boom")
        return multicast

    m.send_each_for_multicast = send_each_for_multicast
    m._captured = captured
    return m


def _install_fake_firebase(monkeypatch, *, init_raises=False, messaging=None):
    """Install a fake firebase_admin with credentials and optional messaging submodule."""
    fa = types.ModuleType("firebase_admin")
    creds = types.ModuleType("firebase_admin.credentials")
    creds.Certificate = lambda path: ("cert", path)
    sentinel_app = object()

    def initialize_app(cred, name=None):
        if init_raises:
            raise RuntimeError("init boom")
        return sentinel_app

    fa.initialize_app = initialize_app
    fa.credentials = creds
    if messaging is not None:
        fa.messaging = messaging
    monkeypatch.setitem(sys.modules, "firebase_admin", fa)
    monkeypatch.setitem(sys.modules, "firebase_admin.credentials", creds)
    if messaging is not None:
        monkeypatch.setitem(sys.modules, "firebase_admin.messaging", messaging)
    return fa, sentinel_app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset the module's init-latch before each test."""
    monkeypatch.setattr(fcm_transport, "_state", {"app": None, "init_failed": False})
    yield


def _fake_settings(monkeypatch, cred="/path/sa.json"):
    monkeypatch.setattr(
        fcm_transport, "get_settings",
        lambda: types.SimpleNamespace(FCM_CREDENTIALS_FILE=cred),
    )


# ---------------------------------------------------------------------------
# _ensure_app
# ---------------------------------------------------------------------------
def test_ensure_app_returns_cached(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_state", {"app": sentinel, "init_failed": False})
    assert fcm_transport._ensure_app() is sentinel


def test_ensure_app_init_failed_latched(monkeypatch):
    monkeypatch.setattr(fcm_transport, "_state", {"app": None, "init_failed": True})
    assert fcm_transport._ensure_app() is None


def test_ensure_app_empty_creds(monkeypatch):
    _fake_settings(monkeypatch, cred="")
    assert fcm_transport._ensure_app() is None
    assert fcm_transport._state["init_failed"] is True


def test_ensure_app_success(monkeypatch):
    _fake_settings(monkeypatch)
    _, sentinel_app = _install_fake_firebase(monkeypatch)
    assert fcm_transport._ensure_app() is sentinel_app
    assert fcm_transport._state["app"] is sentinel_app


def test_ensure_app_init_exception_latches(monkeypatch):
    _fake_settings(monkeypatch)
    _install_fake_firebase(monkeypatch, init_raises=True)
    assert fcm_transport._ensure_app() is None
    assert fcm_transport._state["init_failed"] is True


def test_ensure_app_double_check_app_inside_lock(monkeypatch):
    """Simulate another thread setting _state['app'] between the outer check and lock
    acquisition; the inner re-check inside the lock should return it."""
    _fake_settings(monkeypatch)
    sentinel = object()

    class _LockSetsApp:
        def __enter__(self):
            fcm_transport._state["app"] = sentinel
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fcm_transport, "_init_lock", _LockSetsApp())
    assert fcm_transport._ensure_app() is sentinel


def test_ensure_app_double_check_init_failed_inside_lock(monkeypatch):
    _fake_settings(monkeypatch)

    class _LockSetsFailed:
        def __enter__(self):
            fcm_transport._state["init_failed"] = True
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fcm_transport, "_init_lock", _LockSetsFailed())
    assert fcm_transport._ensure_app() is None


# ---------------------------------------------------------------------------
# _is_dead_token
# ---------------------------------------------------------------------------
def test_is_dead_token_none():
    assert fcm_transport._is_dead_token(None) is False


def test_is_dead_token_by_class_name():
    assert fcm_transport._is_dead_token(UnregisteredError("x")) is True
    assert fcm_transport._is_dead_token(SenderIdMismatchError("x")) is True


def test_is_dead_token_by_message():
    assert fcm_transport._is_dead_token(RuntimeError("Requested entity not-registered")) is True


def test_is_dead_token_other():
    assert fcm_transport._is_dead_token(RuntimeError("quota exceeded")) is False


# ---------------------------------------------------------------------------
# _webpush_config
# ---------------------------------------------------------------------------
def test_webpush_config_https():
    m = _make_messaging()
    cfg = fcm_transport._webpush_config(m, "https://app.example/bug/1")
    assert cfg[0] == "WebpushConfig"


def test_webpush_config_non_https():
    m = _make_messaging()
    assert fcm_transport._webpush_config(m, "http://insecure/bug/1") is None
    assert fcm_transport._webpush_config(m, "") is None


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------
def test_send_empty_tokens_returns_empty():
    assert fcm_transport.send([], title="t", body="b") == []
    assert fcm_transport.send(None, title="t", body="b") == []


def test_send_app_none_returns_empty(monkeypatch):
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: None)
    assert fcm_transport.send(["tok"], title="t", body="b") == []


def test_send_messaging_import_fails_returns_empty(monkeypatch):
    # _ensure_app succeeds, but firebase_admin.messaging is not importable,
    # so the lazy import raises. send() must return [] and not propagate.
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    fa = types.ModuleType("firebase_admin")  # no .messaging, not a package
    monkeypatch.setitem(sys.modules, "firebase_admin", fa)
    monkeypatch.delitem(sys.modules, "firebase_admin.messaging", raising=False)
    assert fcm_transport.send(["tok"], title="t", body="b") == []


def test_send_success_attaches_data_and_https_webpush(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    messaging = _make_messaging(multicast=_Multicast([_Resp(True)]))
    _install_fake_firebase(monkeypatch, messaging=messaging)
    dead = fcm_transport.send(
        ["tok1"], title="t", body="b",
        url="https://app.example/bug/9", data={"bug_id": 9},
    )
    assert dead == []
    msg_kwargs = messaging._captured["message"][1]
    assert msg_kwargs["data"]["url"] == "https://app.example/bug/9"
    assert msg_kwargs["data"]["bug_id"] == "9"  # stringified
    assert msg_kwargs["webpush"] is not None    # https → webpush link


def test_send_non_https_url_no_webpush(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    messaging = _make_messaging(multicast=_Multicast([_Resp(True)]))
    _install_fake_firebase(monkeypatch, messaging=messaging)
    fcm_transport.send(["tok1"], title="t", body="b", url="http://insecure")
    assert messaging._captured["message"][1]["webpush"] is None


def test_send_none_url_normalized(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    messaging = _make_messaging(multicast=_Multicast([_Resp(True)]))
    _install_fake_firebase(monkeypatch, messaging=messaging)
    # url=None would cause AttributeError on startswith if not guarded first.
    fcm_transport.send(["tok1"], title="t", body="b", url=None)
    assert messaging._captured["message"][1]["data"]["url"] == "/"


def test_send_prunes_dead_tokens(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    responses = [
        _Resp(True),                                       # ok
        _Resp(False, UnregisteredError("gone")),           # dead (class)
        _Resp(False, RuntimeError("not-registered")),      # dead (message)
        _Resp(False, RuntimeError("transient quota")),     # logged, not dead
    ]
    messaging = _make_messaging(multicast=_Multicast(responses))
    _install_fake_firebase(monkeypatch, messaging=messaging)
    dead = fcm_transport.send(["ok", "d1", "d2", "live"], title="t", body="b")
    assert dead == ["d1", "d2"]


def test_send_multicast_raises_returns_empty(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(fcm_transport, "_ensure_app", lambda: sentinel)
    messaging = _make_messaging(send_raises=True)
    _install_fake_firebase(monkeypatch, messaging=messaging)
    assert fcm_transport.send(["tok"], title="t", body="b") == []
