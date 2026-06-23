"""Source guards for the password-field UX:

  * the reveal ("eye") toggle on every password input, and
  * the permanent 'changeme' exception in the client-side password validator.

These sniff the source wiring so a refactor can't silently drop the eye button
from a password field, nor re-introduce a client-side check that rejects the
legacy 'changeme' default the server still accepts (mirrors
app/schemas._check_password_strength and app/password_breach._ALWAYS_ALLOWED,
which are covered behaviourally elsewhere).
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"

PASSWORD_INPUT = SRC / "components" / "PasswordInput.tsx"
CONSTANTS = SRC / "lib" / "constants.ts"
STYLES = SRC / "styles" / "styles.css"

# Every form that collects a password.
LOGIN = SRC / "login" / "LoginPage.tsx"
RESET = SRC / "reset" / "ResetPage.tsx"
CHANGE_PW = SRC / "modals" / "ChangePasswordModal.tsx"
USER_MODAL = SRC / "modals" / "UserModal.tsx"

PASSWORD_FORMS = [LOGIN, RESET, CHANGE_PW, USER_MODAL]


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The reusable PasswordInput component
# ---------------------------------------------------------------------------
def test_password_input_component_exists():
    assert PASSWORD_INPUT.exists(), "PasswordInput.tsx must exist"


def test_password_input_toggles_type_and_renders_eye():
    src = _read(PASSWORD_INPUT)
    # The eye button flips the input between hidden and plain text.
    assert 'type={visible ? "text" : "password"}' in src
    # Structural class hooks the stylesheet targets.
    assert 'className="pw-wrap"' in src
    assert 'className="pw-toggle"' in src
    # Accessible name that reflects the current state.
    assert "Show password" in src and "Hide password" in src
    # The toggle must never submit the surrounding form.
    assert 'type="button"' in src
    # forwardRef so callers that need the DOM node (focus) still work.
    assert "forwardRef" in src


# ---------------------------------------------------------------------------
# Every password form uses PasswordInput (no bare type="password" left)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", PASSWORD_FORMS, ids=lambda p: p.name)
def test_password_forms_use_password_input(path):
    src = _read(path)
    assert 'from "../components/PasswordInput"' in src, (
        f"{path.name} must import PasswordInput"
    )
    assert "<PasswordInput" in src, f"{path.name} must render PasswordInput"
    # No raw password <input> should remain — they all go through the component
    # so they all get the eye toggle.
    assert 'type="password"' not in src, (
        f'{path.name} still has a bare type="password" input'
    )


def test_password_input_count_matches_fields():
    # The multi-field forms wire one PasswordInput per password field.
    assert _read(CHANGE_PW).count("<PasswordInput") == 3  # current/new/confirm
    assert _read(RESET).count("<PasswordInput") == 2  # new/confirm


# ---------------------------------------------------------------------------
# CSS hooks for the toggle
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", [".pw-wrap", ".pw-toggle"])
def test_password_toggle_css_present(cls):
    assert cls in _read(STYLES), f"styles.css missing {cls!r}"


# ---------------------------------------------------------------------------
# The permanent 'changeme' client-side exception
# ---------------------------------------------------------------------------
def test_client_validator_allows_changeme():
    # The validator must short-circuit 'changeme' (case-insensitive) before the
    # length/letter+digit rules, so the UI never rejects a password the backend
    # would store.
    assert 'toLowerCase() === "changeme"' in _read(CONSTANTS)
