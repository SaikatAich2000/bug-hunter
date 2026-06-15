"""Regression: the Sleuth chat write-path must enforce the SAME v2.3 per-type
edit rules as the REST API.

Before the fix, app/chatbot/actions._check_can_edit_bug called can_edit_bug
WITHOUT item_type, so it defaulted to 'Bug' and returned True for everyone —
letting a regular user edit Tasks/Requirements via chat that PUT /api/bugs/{id}
forbids with 403 (a real broken-access-control / IDOR escalation). These tests
lock chat/REST parity at the permission-helper level, where the fix lives, so
they're independent of NLU / confirm-flow parsing.

Imports are done INSIDE each test because conftest's `client` fixture re-imports
all app.* modules between tests; importing at call time always binds the current
generation of the helper.
"""
from __future__ import annotations

from types import SimpleNamespace


def _bug(item_type, reporter_id=999, assignees=()):
    # _check_can_edit_bug reads .item_type, .reporter_id and .assignees (each
    # assignee needs an .id). can_edit_bug itself del's reporter/assignees.
    return SimpleNamespace(
        item_type=item_type,
        reporter_id=reporter_id,
        assignees=[SimpleNamespace(id=a) for a in assignees],
    )


def _actor(role, uid=1):
    return SimpleNamespace(role=role, id=uid)


def test_chat_blocks_regular_user_on_task_and_requirement():
    from app.chatbot.actions import _check_can_edit_bug
    user = _actor("user")
    task_err = _check_can_edit_bug(user, _bug("Task"))
    req_err = _check_can_edit_bug(user, _bug("Requirement"))
    assert task_err is not None and req_err is not None
    # The denial names the item type the user tried to edit.
    assert "task" in task_err.lower()
    assert "requirement" in req_err.lower()


def test_chat_still_allows_regular_user_on_bug():
    from app.chatbot.actions import _check_can_edit_bug
    # Bugs stay editable by any authenticated user via chat (unchanged).
    assert _check_can_edit_bug(_actor("user"), _bug("Bug")) is None
    # Legacy rows without an item_type default to 'Bug' and remain editable.
    assert _check_can_edit_bug(_actor("user"), _bug(None)) is None


def test_chat_allows_manager_and_admin_on_every_type():
    from app.chatbot.actions import _check_can_edit_bug
    for role in ("admin", "manager"):
        actor = _actor(role)
        for itype in ("Bug", "Task", "Requirement"):
            assert _check_can_edit_bug(actor, _bug(itype)) is None
