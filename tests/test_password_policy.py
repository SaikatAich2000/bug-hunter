"""Configurable password policy and the permanent 'changeme' exception
(app/schemas._check_password_strength, driven by app/config flags).

Why everything is imported inside the tests:
The `client` fixture in conftest.py deletes and re-imports every ``app.*``
module between tests so each test can pick up its own env overrides. A module
captured at this file's top level would go stale (point at an earlier import
generation) once any client-using test has run, while
``_check_password_strength`` resolves ``from app.config import get_settings``
against the current generation at call time. Importing ``app.config`` and
``app.schemas`` inside each test guarantees we patch the same Settings class
the function reads. We patch the Settings class attribute (not a cached
instance): every instance sees it via normal attribute lookup, and monkeypatch
restores it cleanly afterwards.
"""
import pytest


def _check():
    """Return the live _check_password_strength from the current app.schemas."""
    from app.schemas import _check_password_strength
    return _check_password_strength


def test_changeme_always_accepted_even_with_raised_minimum(monkeypatch):
    # The hard constraint: 'changeme' (8 chars) must survive a raised minimum.
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 16)
    check = _check()
    assert check("changeme") == "changeme"
    assert check("CHANGEME") == "CHANGEME"  # case-insensitive


def test_default_rules_unchanged():
    check = _check()
    assert check("abcd1234") == "abcd1234"     # letter+digit, len 8 -> ok
    with pytest.raises(ValueError):
        check("short1")                        # too short
    with pytest.raises(ValueError):
        check("abcdefgh")                      # no digit
    with pytest.raises(ValueError):
        check("password123")                   # common-list


def test_configurable_min_length(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 12)
    check = _check()
    with pytest.raises(ValueError):
        check("abcd1234")                      # 8 < 12 now rejected
    assert check("abcdefgh1234") == "abcdefgh1234"


def test_complexity_can_be_disabled(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_REQUIRE_COMPLEXITY", False)
    check = _check()
    assert check("abcdefgh") == "abcdefgh"     # letters-only allowed when off


def test_too_long_rejected_regardless():
    check = _check()
    with pytest.raises(ValueError):
        check("a1" * 200)                      # 400 chars > 200 DoS guard
