"""Pure-unit coverage for app/schemas.py — the Pydantic schemas, their
field/model validators, and the in-house HTML sanitizer.

These tests import classes/helpers straight from ``app.schemas`` and exercise
them directly; no TestClient / DB is needed. Each validator branch gets one
passing case and one failing case so both the "valid" early-return path and
the "raise" path are covered.

IMPORTANT (mirrors tests/test_password_policy.py): the ``client`` fixture in
conftest.py deletes and re-imports every ``app.*`` module between tests, so a
module captured at THIS file's top level would go stale. We therefore import
``app.schemas`` / ``app.config`` INSIDE each test (via the ``_S()`` helper) so
we always patch and assert against the SAME import generation the validators
read at call time.

The permanent 'changeme' password exception MUST keep passing — see
test_cov_password_changeme_exception_always_passes.

Some branches (``not isinstance(v, str)`` guards and the ``if v is None:
return None`` early returns) can't be reached through normal Pydantic model
construction because Pydantic coerces/rejects the value before the plain
validator body runs. For those we call the validator/helper function directly,
which is exactly the code path the model would execute for an already-correct
type.
"""
import pytest
from pydantic import ValidationError


def _S():
    """Return the live app.schemas module for the current import generation."""
    import app.schemas as schemas
    return schemas


# ---------------------------------------------------------------------------
# HTML sanitizer (_HTMLAllowlistSanitizer / sanitize_html)
# ---------------------------------------------------------------------------
def test_cov_sanitize_html_none_returns_empty():
    # line 145: value is None -> ""
    s = _S()
    assert s.sanitize_html(None) == ""


def test_cov_sanitize_html_keeps_allowed_strips_disallowed():
    s = _S()
    # <b> kept, <script> dropped but its text survives.
    out = s.sanitize_html("<b>hi</b><script>alert(1)</script>x")
    assert "<b>hi</b>" in out
    assert "<script>" not in out
    assert out.endswith("alert(1)x")  # text content of script + trailing x


def test_cov_sanitize_html_idempotent():
    s = _S()
    once = s.sanitize_html("<b>hi</b><script>bad</script>")
    twice = s.sanitize_html(once)
    assert once == twice


def test_cov_sanitize_url_empty_returns_none():
    # line 69: not raw -> None (href value present but empty string)
    s = _S()
    out = s.sanitize_html('<a href="">link</a>')
    # empty href is dropped; a-tag still emitted with forced rel.
    assert 'href=' not in out
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_url_data_image_ok_and_too_big():
    # lines 76-79: valid data:image kept; oversized data:image dropped.
    s = _S()
    small = '<img src="data:image/png;base64,AAAA" alt="x">'
    out = s.sanitize_html(small)
    assert 'src="data:image/png;base64,AAAA"' in out
    # > 14 MB after the data: prefix -> src dropped (line 77-78).
    big_payload = "A" * (14 * 1024 * 1024 + 10)
    big = f'<img src="data:image/png;base64,{big_payload}" alt="x">'
    out_big = s.sanitize_html(big)
    assert "data:image" not in out_big
    assert "<img" in out_big  # tag kept, just the src stripped


def test_cov_sanitize_url_allowed_scheme_kept():
    # lines 80-82: an allowed scheme (http:) is returned as-is.
    s = _S()
    out = s.sanitize_html('<a href="https://example.com">ex</a>')
    assert 'href="https://example.com"' in out


def test_cov_sanitize_url_disallowed_scheme_dropped():
    # line 83: javascript: is not allowed -> href stripped.
    s = _S()
    out = s.sanitize_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out


def test_cov_sanitize_attr_disallowed_dropped():
    # line 93-94: an attribute not in the per-tag allowlist is skipped
    # (`onclick` is not in <a>'s {href,title,rel}).
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" onclick="evil()">y</a>')
    assert "onclick" not in out
    assert 'href="https://e.com"' in out


def test_cov_sanitize_attr_none_value_skipped():
    # lines 95-96: an allowed attribute present with no value is skipped
    # (`<a href>` -> href is allowed but valueless).
    s = _S()
    out = s.sanitize_html("<a href>link</a>")
    assert "href=" not in out
    assert "<a" in out  # tag kept, forced rel still added
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_attr_value_html_escaped():
    # lines 102-106: attribute value gets &/<>/" escaped on the way out.
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" title=\'a"b<c&d\'>x</a>')
    assert "&quot;" in out
    assert "&lt;" in out
    assert "&amp;" in out


def test_cov_sanitize_a_tag_keeps_existing_rel():
    # line 110->112: when a rel= attr is already present, no forced rel added.
    s = _S()
    out = s.sanitize_html('<a href="https://e.com" rel="author">x</a>')
    assert out.count("rel=") == 1
    assert 'rel="author"' in out
    assert "noopener" not in out


def test_cov_sanitize_a_tag_forces_rel_when_missing():
    s = _S()
    out = s.sanitize_html('<a href="https://e.com">x</a>')
    assert 'rel="noopener nofollow"' in out


def test_cov_sanitize_void_elements_no_closing_tag():
    # line 120-121: br/img end-tags produce no output.
    s = _S()
    out = s.sanitize_html("<br></br>")
    assert out == "<br>"
    out_img = s.sanitize_html("<img src=\"https://e.com/a.png\"></img>")
    assert out_img.count("<img") == 1
    assert "</img>" not in out_img


def test_cov_sanitize_endtag_disallowed_dropped():
    # handle_endtag early-return for a non-allowed tag (e.g. </script>).
    s = _S()
    out = s.sanitize_html("text</script>")
    assert "</script>" not in out
    assert out.startswith("text")


def test_cov_sanitize_startendtag_self_closing():
    # line 126: <br/> self-closing routed through handle_startendtag.
    s = _S()
    out = s.sanitize_html("a<br/>b")
    assert out == "a<br>b"


def test_cov_sanitize_entityref_and_charref_preserved():
    # lines 134 & 137: &amp; entity ref and &#169; char ref are re-emitted.
    s = _S()
    out = s.sanitize_html("A&amp;B &#169; C")
    assert "&amp;" in out
    assert "&#169;" in out


# ---------------------------------------------------------------------------
# normalize_choice (public helper)
# ---------------------------------------------------------------------------
def test_cov_normalize_choice_canonicalizes():
    s = _S()
    assert s.normalize_choice("bug", s.ALLOWED_ITEM_TYPES, "item_type") == "Bug"
    assert s.normalize_choice("  HIGH ", s.ALLOWED_PRIORITIES, "priority") == "High"


def test_cov_normalize_choice_invalid_value_raises():
    s = _S()
    with pytest.raises(ValueError):
        s.normalize_choice("nope", s.ALLOWED_ITEM_TYPES, "item_type")


def test_cov_normalize_choice_non_string_raises():
    # line 219: not isinstance(value, str) -> ValueError
    s = _S()
    with pytest.raises(ValueError):
        s.normalize_choice(123, s.ALLOWED_ITEM_TYPES, "item_type")


# ---------------------------------------------------------------------------
# _strip_and_check_min_length
# ---------------------------------------------------------------------------
def test_cov_strip_and_check_min_length_ok():
    s = _S()
    assert s._strip_and_check_min_length("  abc  ", 2, "Name") == "abc"


def test_cov_strip_and_check_min_length_non_string_raises():
    # line 246: not isinstance(v, str) -> ValueError
    s = _S()
    with pytest.raises(ValueError):
        s._strip_and_check_min_length(123, 2, "Name")


def test_cov_strip_and_check_min_length_too_short_raises():
    # line 251: below min_len (min_len != 1) -> "at least N characters"
    s = _S()
    with pytest.raises(ValueError):
        s._strip_and_check_min_length("a", 3, "Title")


def test_cov_strip_and_check_min_length_empty_when_min_one():
    # lines 249-250: min_len == 1 -> "cannot be empty"
    s = _S()
    with pytest.raises(ValueError) as exc:
        s._strip_and_check_min_length("   ", 1, "Field")
    assert "cannot be empty" in str(exc.value)


# ---------------------------------------------------------------------------
# _normalize_role
# ---------------------------------------------------------------------------
def test_cov_normalize_role_ok():
    s = _S()
    assert s._normalize_role("  ADMIN ") == "admin"


def test_cov_normalize_role_non_string_raises():
    # line 260: not isinstance(v, str) -> ValueError
    s = _S()
    with pytest.raises(ValueError):
        s._normalize_role(5)


def test_cov_normalize_role_invalid_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._normalize_role("superuser")


# ---------------------------------------------------------------------------
# _validate_email
# ---------------------------------------------------------------------------
def test_cov_validate_email_ok_lowercased():
    s = _S()
    assert s._validate_email("  USER@Example.COM ") == "user@example.com"


def test_cov_validate_email_invalid_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._validate_email("not-an-email")


# ---------------------------------------------------------------------------
# _check_password_strength  (incl. the permanent 'changeme' exception)
# ---------------------------------------------------------------------------
def test_cov_password_changeme_exception_always_passes(monkeypatch):
    # 'changeme' MUST stay valid, even with a raised minimum (lines 275-276).
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_MIN_LENGTH", 24)
    s = _S()
    assert s._check_password_strength("changeme") == "changeme"
    assert s._check_password_strength("CHANGEME") == "CHANGEME"


def test_cov_password_valid_default():
    s = _S()
    assert s._check_password_strength("abcd1234") == "abcd1234"


def test_cov_password_non_string_raises():
    # lines 268-269: not isinstance(v, str) -> ValueError
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength(12345678)


def test_cov_password_too_long_raises():
    # lines 278-279: > 200 chars DoS guard
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("a1" * 200)


def test_cov_password_too_short_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("ab12")


def test_cov_password_missing_digit_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("abcdefgh")


def test_cov_password_common_list_raises():
    s = _S()
    with pytest.raises(ValueError):
        s._check_password_strength("password123")


def test_cov_password_complexity_off_allows_letters_only(monkeypatch):
    import app.config as config
    monkeypatch.setattr(config.Settings, "PASSWORD_REQUIRE_COMPLEXITY", False)
    s = _S()
    assert s._check_password_strength("abcdefgh") == "abcdefgh"


# ---------------------------------------------------------------------------
# UserIn
# ---------------------------------------------------------------------------
def test_cov_userin_valid():
    s = _S()
    u = s.UserIn(name="  Alice ", email="A@B.com", role="ADMIN",
                 password="abcd1234")
    assert u.name == "Alice"
    assert u.email == "a@b.com"
    assert u.role == "admin"


def test_cov_userin_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="a", email="a@b.com", password="abcd1234")


def test_cov_userin_bad_email_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="bad", password="abcd1234")


def test_cov_userin_bad_role_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="a@b.com", role="root", password="abcd1234")


def test_cov_userin_weak_password_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserIn(name="Alice", email="a@b.com", password="short")


def test_cov_userin_changeme_password_passes():
    # 'changeme' must pass through the model too.
    s = _S()
    u = s.UserIn(name="Alice", email="a@b.com", password="changeme")
    assert u.password == "changeme"


# ---------------------------------------------------------------------------
# UserUpdate  (Optional fields: None -> early-return branches 343/349/355/361)
# ---------------------------------------------------------------------------
def test_cov_userupdate_all_none_passes():
    # Each optional validator hits its `if v is None: return None` branch
    # (lines 343/349/355/361 ->exit). Pydantic only runs a field validator
    # when a value is EXPLICITLY supplied, so we pass None explicitly rather
    # than letting the fields fall back to their (un-validated) defaults.
    s = _S()
    u = s.UserUpdate(name=None, email=None, role=None, password=None,
                     is_active=None)
    assert u.name is None and u.email is None and u.role is None
    assert u.password is None and u.is_active is None


def test_cov_userupdate_values_validated():
    s = _S()
    u = s.UserUpdate(name="  Bob ", email="B@C.com", role="Manager",
                     password="abcd1234")
    assert u.name == "Bob"
    assert u.email == "b@c.com"
    assert u.role == "manager"


def test_cov_userupdate_bad_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(name="x")


def test_cov_userupdate_bad_email_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(email="nope")


def test_cov_userupdate_bad_role_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(role="root")


def test_cov_userupdate_bad_password_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.UserUpdate(password="short")


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------
def test_cov_loginin_email_validated():
    s = _S()
    assert s.LoginIn(email="A@B.com", password="x").email == "a@b.com"
    with pytest.raises(ValidationError):
        s.LoginIn(email="bad", password="x")


def test_cov_changepassword_validates_new():
    s = _S()
    ok = s.ChangePasswordIn(current_password="anything", new_password="abcd1234")
    assert ok.new_password == "abcd1234"
    with pytest.raises(ValidationError):
        s.ChangePasswordIn(current_password="anything", new_password="short")
    # current_password must be non-empty (Field min_length=1).
    with pytest.raises(ValidationError):
        s.ChangePasswordIn(current_password="", new_password="abcd1234")


def test_cov_forgotpassword_email_validated():
    s = _S()
    assert s.ForgotPasswordIn(email="A@B.com").email == "a@b.com"
    with pytest.raises(ValidationError):
        s.ForgotPasswordIn(email="bad")


def test_cov_resetpassword_validates_new():
    s = _S()
    assert s.ResetPasswordIn(token="t", new_password="abcd1234").new_password == "abcd1234"
    with pytest.raises(ValidationError):
        s.ResetPasswordIn(token="t", new_password="short")


# ---------------------------------------------------------------------------
# ProjectIn
# ---------------------------------------------------------------------------
def test_cov_projectin_valid_and_trims():
    s = _S()
    p = s.ProjectIn(name="  Proj ", description="  d  ", color="#aabbcc")
    assert p.name == "Proj"
    assert p.description == "d"
    assert p.color == "#aabbcc"


def test_cov_projectin_default_color():
    s = _S()
    p = s.ProjectIn(name="Proj")
    assert p.color == "#c9764f"
    assert p.description == ""


def test_cov_projectin_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.ProjectIn(name="a")


def test_cov_projectin_bad_color_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.ProjectIn(name="Proj", color="red")
    with pytest.raises(ValidationError):
        s.ProjectIn(name="Proj", color="#fff")  # 3 hex not 6


# ---------------------------------------------------------------------------
# BugCreate — field + model validators
# ---------------------------------------------------------------------------
def test_cov_bugcreate_valid_defaults():
    s = _S()
    b = s.BugCreate(project_id=1, title="  A valid title ")
    assert b.title == "A valid title"
    assert b.item_type == "Bug"
    assert b.status == "New"
    assert b.priority == "Medium"
    assert b.environment == "DEV"


def test_cov_bugcreate_normalizes_choices():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", item_type="task",
                    status="done", priority="high", environment="prod")
    assert b.item_type == "Task"
    assert b.status == "Done"
    assert b.priority == "High"
    assert b.environment == "PROD"


def test_cov_bugcreate_short_title_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="ab")


def test_cov_bugcreate_description_sanitized():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here",
                    description="  <b>x</b><script>bad</script>  ")
    assert "<b>x</b>" in b.description
    assert "<script>" not in b.description


def test_cov_bugcreate_bad_item_type_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", item_type="Epic")


def test_cov_bugcreate_bad_status_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", status="Nonsense")


def test_cov_bugcreate_bad_priority_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", priority="Whenever")


def test_cov_bugcreate_bad_environment_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", environment="STAGING")


def test_cov_bugcreate_due_date_valid_and_empty():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", due_date="2026-01-31")
    assert b.due_date == "2026-01-31"
    # line 529->exit: empty string -> None early return
    b2 = s.BugCreate(project_id=1, title="Title here", due_date="")
    assert b2.due_date is None


def test_cov_bugcreate_due_date_bad_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", due_date="31-01-2026")


def test_cov_bugcreate_assignee_dedup():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", assignee_ids=[1, 1, 2, 2, 3])
    assert b.assignee_ids == [1, 2, 3]


def test_cov_bugcreate_status_invalid_for_type_raises():
    # model_validator (lines 544-554): 'Done' is a Task status, not a Bug status.
    s = _S()
    with pytest.raises(ValidationError):
        s.BugCreate(project_id=1, title="Title here", item_type="Bug",
                    status="Done")


def test_cov_bugcreate_status_valid_for_type_passes():
    s = _S()
    b = s.BugCreate(project_id=1, title="Title here", item_type="Requirement",
                    status="Approved")
    assert b.status == "Approved"


def test_cov_bugcreate_strip_desc_non_string_passthrough():
    # line 500->exit: validator's `if not isinstance(v, str): return v`.
    # Pydantic would reject a non-str for the model field, so call the
    # field validator directly with a non-str — that's the same body the
    # model runs for an already-correct value.
    s = _S()
    assert s.BugCreate._strip_desc(None) is None


# ---------------------------------------------------------------------------
# BugUpdate — Optional everything (None early-returns: 576/583-584/620)
# ---------------------------------------------------------------------------
def test_cov_bugupdate_all_none_passes():
    # Explicit None forces each optional validator to run its None-return
    # branch (lines 576/583-584/620 ->exit and the item_type/status/priority/
    # environment/due_date None paths).
    s = _S()
    u = s.BugUpdate(project_id=None, title=None, description=None,
                    reporter_id=None, assignee_ids=None, item_type=None,
                    status=None, priority=None, environment=None,
                    due_date=None, event_id=None)
    assert u.title is None
    assert u.description is None
    assert u.item_type is None
    assert u.status is None
    assert u.priority is None
    assert u.environment is None
    assert u.due_date is None
    assert u.assignee_ids is None


def test_cov_bugupdate_values_validated():
    s = _S()
    u = s.BugUpdate(title="  New title ", item_type="task", status="blocked",
                    priority="low", environment="uat",
                    description="<b>k</b><script>x</script>",
                    assignee_ids=[2, 2, 5], due_date="2026-02-02")
    assert u.title == "New title"
    assert u.item_type == "Task"
    assert u.status == "Blocked"
    assert u.priority == "Low"
    assert u.environment == "UAT"
    assert "<script>" not in u.description
    assert u.assignee_ids == [2, 5]
    assert u.due_date == "2026-02-02"


def test_cov_bugupdate_short_title_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(title="ab")


def test_cov_bugupdate_bad_item_type_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(item_type="Epic")


def test_cov_bugupdate_bad_status_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(status="Nope")


def test_cov_bugupdate_bad_priority_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(priority="Sometime")


def test_cov_bugupdate_bad_environment_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(environment="LOCAL")


def test_cov_bugupdate_due_date_bad_raises():
    # lines 611-615: invalid date -> ValueError
    s = _S()
    with pytest.raises(ValidationError):
        s.BugUpdate(due_date="2026/02/02")


def test_cov_bugupdate_due_date_empty_is_none():
    # line 610->exit: empty string -> None
    s = _S()
    assert s.BugUpdate(due_date="").due_date is None


def test_cov_bugupdate_strip_desc_non_string_passthrough():
    # line 584->exit: `if not isinstance(v, str): return v` (called directly).
    s = _S()
    assert s.BugUpdate._strip_desc(123) == 123


# ---------------------------------------------------------------------------
# CommentIn
# ---------------------------------------------------------------------------
def test_cov_commentin_valid_sanitized():
    s = _S()
    c = s.CommentIn(body="  <b>hello</b><script>x</script> ")
    assert "<b>hello</b>" in c.body
    assert "<script>" not in c.body


def test_cov_commentin_whitespace_only_raises():
    # lines 690-692: text-only empty and no <img> -> ValueError
    s = _S()
    with pytest.raises(ValidationError):
        s.CommentIn(body="<p>   </p>")


def test_cov_commentin_image_only_allowed():
    # The `"<img" in cleaned` branch keeps an image-only comment.
    s = _S()
    c = s.CommentIn(body='<img src="data:image/png;base64,AAAA" alt="s">')
    assert "<img" in c.body


def test_cov_commentin_empty_string_rejected_by_field():
    # Field(min_length=1) rejects the empty string before the validator.
    s = _S()
    with pytest.raises(ValidationError):
        s.CommentIn(body="")


def test_cov_commentin_non_string_body_raises():
    # line 688: validator's `if not isinstance(v, str)` (called directly,
    # since the model field would coerce/reject first).
    s = _S()
    with pytest.raises(ValueError):
        s.CommentIn._strip(123)


# ---------------------------------------------------------------------------
# EventCreate
# ---------------------------------------------------------------------------
def test_cov_eventcreate_valid_trims_desc():
    # line 797: description strip path.
    s = _S()
    e = s.EventCreate(name="  Standup ", description="  notes  ",
                      scheduled_for="2026-03-03", manager_ids=[1, 1, 2])
    assert e.name == "Standup"
    assert e.description == "notes"
    assert e.scheduled_for == "2026-03-03"
    assert e.manager_ids == [1, 2]


def test_cov_eventcreate_short_name_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.EventCreate(name="a")


def test_cov_eventcreate_scheduled_empty_is_none():
    # line 802->exit: empty -> None
    s = _S()
    assert s.EventCreate(name="Standup", scheduled_for="").scheduled_for is None
    assert s.EventCreate(name="Standup").scheduled_for is None


def test_cov_eventcreate_scheduled_bad_raises():
    s = _S()
    with pytest.raises(ValidationError):
        s.EventCreate(name="Standup", scheduled_for="03-03-2026")


def test_cov_eventcreate_strip_desc_non_string_passthrough():
    # line 797 false branch: non-str returns v unchanged (called directly).
    s = _S()
    assert s.EventCreate._strip_desc(None) is None


# ---------------------------------------------------------------------------
# EventUpdate  (Optional: None early-returns 827/838 + dedup 848-852)
# ---------------------------------------------------------------------------
def test_cov_eventupdate_all_none_passes():
    # Explicit None so the name/scheduled_for/manager_ids validators run their
    # None-return branches (lines 827/838/848 ->exit).
    s = _S()
    u = s.EventUpdate(name=None, description=None, scheduled_for=None,
                      manager_ids=None)
    assert u.name is None
    assert u.scheduled_for is None
    assert u.manager_ids is None


def test_cov_eventupdate_values_validated():
    # line 833 (desc strip) + 845-852 (dedup with a list).
    s = _S()
    u = s.EventUpdate(name="  Sprint ", description="  d  ",
                      scheduled_for="2026-04-04", manager_ids=[3, 3, 4])
    assert u.name == "Sprint"
    assert u.description == "d"
    assert u.scheduled_for == "2026-04-04"
    assert u.manager_ids == [3, 4]


def test_cov_eventupdate_short_name_raises():
    # line 827->exit is the None pass; here we hit the raise via a short name.
    s = _S()
    with pytest.raises(ValidationError):
        s.EventUpdate(name="a")


def test_cov_eventupdate_scheduled_bad_raises():
    # lines 841-842: invalid date -> ValueError
    s = _S()
    with pytest.raises(ValidationError):
        s.EventUpdate(scheduled_for="04/04/2026")


def test_cov_eventupdate_scheduled_empty_is_none():
    # line 838->exit: empty string -> None
    s = _S()
    assert s.EventUpdate(scheduled_for="").scheduled_for is None


def test_cov_eventupdate_strip_desc_non_string_passthrough():
    # line 833 false branch: non-str returns v unchanged.
    s = _S()
    assert s.EventUpdate._strip_desc(123) == 123


# ---------------------------------------------------------------------------
# Push schemas (simple Field constraints)
# ---------------------------------------------------------------------------
def test_cov_push_subscribe_valid_and_invalid():
    s = _S()
    ok = s.PushSubscribeIn(token="abc")
    assert ok.platform == "web"
    with pytest.raises(ValidationError):
        s.PushSubscribeIn(token="")  # min_length=1


def test_cov_push_unsubscribe_valid_and_invalid():
    s = _S()
    assert s.PushUnsubscribeIn(token="abc").token == "abc"
    with pytest.raises(ValidationError):
        s.PushUnsubscribeIn(token="")


# ---------------------------------------------------------------------------
# statuses_for_type helper
# ---------------------------------------------------------------------------
def test_cov_statuses_for_type_known_and_unknown():
    s = _S()
    assert s.statuses_for_type("Task") == s.STATUSES_BY_TYPE["Task"]
    # unknown type falls back to the Bug list.
    assert s.statuses_for_type("Mystery") == s.STATUSES_BY_TYPE["Bug"]
    assert s.statuses_for_type("") == s.STATUSES_BY_TYPE["Bug"]
