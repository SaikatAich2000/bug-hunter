"""Property-based tests (Hypothesis): the parsers and guards that take untrusted input must hold
their invariants for any input, not just the examples written by hand."""
from __future__ import annotations

import ipaddress

import pytest

hyp = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

SETTINGS = settings(max_examples=300, deadline=None)
_PROBLEMS = {None, "a link-local or cloud metadata address", "a reserved address", "a private network address"}


@SETTINGS
@given(st.one_of(st.ip_addresses(v=4), st.ip_addresses(v=6)), st.booleans())
def test_address_guard_invariants(ip, allow_private):
    from app import net_guard

    result = net_guard.address_problem(ip, allow_private)
    assert result in _PROBLEMS
    inner = net_guard._embedded_ipv4(ip)
    if any(inner in net for net in net_guard._METADATA if net.version == inner.version):
        assert result == "a link-local or cloud metadata address"  # never reachable, whatever is allowed
    if allow_private:
        assert result != "a private network address"
    if net_guard.address_problem(ip, False) is None:
        assert result is None  # allowing private ranges never makes a public address worse


@SETTINGS
@given(st.ip_addresses(v=4))
def test_ipv4_mapped_ipv6_is_judged_like_its_ipv4(v4):
    from app import net_guard

    mapped = ipaddress.IPv6Address(f"::ffff:{v4}")
    for allow in (True, False):
        assert net_guard.address_problem(mapped, allow) == net_guard.address_problem(v4, allow)


@SETTINGS
@given(st.text(max_size=80))
def test_upload_extension_is_lowercase_and_never_contains_separators(name):
    from app.upload_policy import upload_extension

    ext = upload_extension(name)
    assert ext == ext.lower()
    assert "." not in ext and ext == ext.strip()


@SETTINGS
@given(st.sampled_from(["evil.exe", "a.PNG", "report.pdf", "x.tar", "noext", "photo.jpg"]), st.text(alphabet=". \t", max_size=6))
def test_trailing_dots_and_spaces_never_change_the_verdict(name, junk):
    from app.upload_policy import check_extension

    assert (check_extension(name + junk) is None) == (check_extension(name) is None)


@SETTINGS
@given(st.text(max_size=40), st.binary(max_size=64))
def test_verify_upload_only_ever_raises_value_error(name, data):
    from app.upload_policy import verify_upload

    try:
        mime = verify_upload(name, data)
    except ValueError:
        return
    assert "/" in mime


@SETTINGS
@given(st.sampled_from(["exe", "bat", "js", "html", "svg", "php", "sh", "dll"]), st.binary(max_size=64))
def test_dangerous_extensions_are_refused_whatever_the_content(ext, data):
    from app.upload_policy import verify_upload

    with pytest.raises(ValueError):
        verify_upload(f"file.{ext}", b"\x89PNG\r\n\x1a\n" + data)


@SETTINGS
@given(st.text(max_size=60))
def test_defanged_text_never_starts_a_formula(s):
    from app.util import defang_formula

    out = defang_formula(s)
    stripped = out.lstrip()
    assert not out or out[0] not in "=+-@\t\r" or out[0] == "'" or out == s and not stripped
    assert not stripped or stripped[0] not in "=+-@" or out.startswith("'")


@SETTINGS
@given(st.text(max_size=30))
def test_xlsx_text_has_no_illegal_xml_characters_and_no_formula(s):
    from app.reports.xlsx import _defang_formula_text

    out = _defang_formula_text(s)
    assert not any(ord(c) < 32 and c not in "\t\n\r" for c in out)
    assert not out.lstrip() or out.lstrip()[0] not in "=+-@" or out.startswith("'")


@SETTINGS
@given(st.binary(max_size=2000))
def test_csv_import_reads_any_bytes_or_refuses_cleanly(data):
    from app.bulk_import import BulkImportError, _rows_from_csv

    try:
        rows = _rows_from_csv(data)
    except BulkImportError:
        return
    assert all(isinstance(r, list) and all(isinstance(c, str) for c in r) for r in rows)
