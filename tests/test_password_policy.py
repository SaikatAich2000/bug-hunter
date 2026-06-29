"""Tests for the configurable password policy and the permanent 'changeme' exception
(app/schemas._check_password_strength, driven by app/config flags).

Imports are kept inside each test rather than at the top of the module because
conftest.py's `client` fixture deletes and re-imports every `app.*` module
between tests so that env overrides take effect. A top-level import would
reference a stale module once any client-using test has run. Importing inside
each test ensures we patch the same Settings class that _check_password_strength
reads. We patch the class attribute directly so every instance sees the change
via normal attribute lookup, and monkeypatch restores it afterwards.
"""
import pytest


def _check():
    """Return _check_password_strength from the currently loaded app.schemas."""
    from app.schemas import _check_password_strength
    return _check_password_strength


def test_changeme_always_accepted_even_with_raised_minimum(monkeypatch):
    # 'changeme' is a permanent exception and must pass even when the minimum is raised above 8.
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 16)
    check = _check()
    assert check("changeme") == "changeme"
    assert check("CHANGEME") == "CHANGEME"  # case-insensitive


def test_default_rules_unchanged():
    check = _check()
    assert check("abcd1234") == "abcd1234"     # letter + digit, length 8: passes
    with pytest.raises(ValueError):
        check("short1")                        # too short
    with pytest.raises(ValueError):
        check("abcdefgh")                      # no digit
    with pytest.raises(ValueError):
        check("password123")                   # on the common-password list


def test_configurable_min_length(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 12)
    check = _check()
    with pytest.raises(ValueError):
        check("abcd1234")                      # 8 chars falls below the new minimum
    assert check("abcdefgh1234") == "abcdefgh1234"


def test_complexity_can_be_disabled(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_REQUIRE_COMPLEXITY", False)
    check = _check()
    assert check("abcdefgh") == "abcdefgh"     # letters-only accepted when complexity is off


def test_too_long_rejected_regardless():
    check = _check()
    with pytest.raises(ValueError):
        check("a1" * 200)                      # 400 chars exceeds the 200-char DoS guard
