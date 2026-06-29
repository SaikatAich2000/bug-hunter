"""Chat write-path authorization parity tests.

Verifies that app/chatbot/actions._check_can_edit_bug enforces per-type edit
rules matching the REST API. Without the item_type argument to can_edit_bug,
the check defaults to 'Bug' and passes for everyone, letting regular users
edit Tasks/Requirements via chat while PUT /api/bugs/{id} returns 403.

Imports are deferred into each test because the client fixture re-imports all
app.* modules between tests; importing at call time binds the right generation
of the helper.
"""
from __future__ import annotations

from types import SimpleNamespace


def _bug(item_type, reporter_id=999, assignees=()):
    # _check_can_edit_bug reads .item_type, .reporter_id, and .assignees
    # (each assignee needs .id); can_edit_bug deletes those attrs internally.
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
    # Error message should name the type the user tried to edit.
    assert "task" in task_err.lower()
    assert "requirement" in req_err.lower()


def test_chat_still_allows_regular_user_on_bug():
    from app.chatbot.actions import _check_can_edit_bug
    # Bugs remain editable by any authenticated user via chat.
    assert _check_can_edit_bug(_actor("user"), _bug("Bug")) is None
    # Legacy rows with no item_type default to 'Bug' and stay editable.
    assert _check_can_edit_bug(_actor("user"), _bug(None)) is None


def test_chat_allows_manager_and_admin_on_every_type():
    from app.chatbot.actions import _check_can_edit_bug
    for role in ("admin", "manager"):
        actor = _actor(role)
        for itype in ("Bug", "Task", "Requirement"):
            assert _check_can_edit_bug(actor, _bug(itype)) is None
