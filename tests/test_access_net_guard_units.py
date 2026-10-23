"""Direct unit tests for the SSRF address guard: every address class, both allow_private settings."""
from __future__ import annotations

import ipaddress
import socket

import pytest


@pytest.mark.parametrize("ip,allow_private,expected", [
    ("169.254.169.254", False, "a link-local or cloud metadata address"),
    ("169.254.169.254", True, "a link-local or cloud metadata address"),
    ("fe80::1", True, "a link-local or cloud metadata address"),
    ("0.0.0.0", True, "a reserved address"),
    ("::", False, "a reserved address"),
    ("224.0.0.1", True, "a reserved address"),
    ("ff02::1", False, "a reserved address"),
    ("127.0.0.1", False, "a private network address"),
    ("127.0.0.1", True, None),
    ("10.1.2.3", False, "a private network address"),
    ("10.1.2.3", True, None),
    ("192.168.0.9", False, "a private network address"),
    ("172.16.5.5", True, None),
    ("100.64.0.1", False, "a private network address"),
    ("100.64.0.1", True, None),
    ("::1", False, "a private network address"),
    ("::1", True, None),
    ("fd00::1", False, "a private network address"),
    ("240.0.0.1", False, "a private network address"),  # class E counts as private
    ("240.0.0.1", True, None),
    ("8.8.8.8", False, None),
    ("8.8.8.8", True, None),
    ("2606:4700:4700::1111", False, None),
    ("::ffff:127.0.0.1", False, "a private network address"),  # IPv4-mapped loopback
    ("::ffff:169.254.169.254", True, "a link-local or cloud metadata address"),
    ("2002:7f00:0001::", False, "a private network address"),  # 6to4 for 127.0.0.1
    ("2002:a9fe:a9fe::", True, "a link-local or cloud metadata address"),  # 6to4 for the metadata IP, even when private is allowed
])
def test_address_problem_table(ip, allow_private, expected):
    from app.net_guard import address_problem

    assert address_problem(ipaddress.ip_address(ip), allow_private) == expected


def test_allowed_addresses_refuses_any_internal_answer(monkeypatch):
    from app import net_guard

    infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
             (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    monkeypatch.setattr(net_guard.socket, "getaddrinfo", lambda *a, **k: infos)
    with pytest.raises(net_guard.HostNotAllowed, match="private network"):
        net_guard.allowed_addresses("mixed.example", 443, False)
    assert [str(a) for a in net_guard.allowed_addresses("mixed.example", 443, True)] == ["8.8.8.8", "10.0.0.5"]
    assert [str(a) for a in net_guard.allowed_addresses("[2606:4700:4700::1111]", 443, False)] == ["2606:4700:4700::1111"]


def _proxy_env(monkeypatch, proxies, bypass=False):
    from app import net_guard

    monkeypatch.setattr(net_guard.urllib.request, "getproxies", lambda: proxies)
    monkeypatch.setattr(net_guard.urllib.request, "proxy_bypass", lambda host: bypass)
    return net_guard


def test_precheck_only_guards_requests_a_proxy_will_resolve(monkeypatch):
    import httpx

    guard = _proxy_env(monkeypatch, {})
    assert guard._proxied(httpx.URL("https://x.example/")) is False
    guard.precheck("https://127.0.0.1/", False)  # no proxy: the guarded transport handles it, precheck is a no-op

    guard = _proxy_env(monkeypatch, {"https": "http://proxy:3128"})
    assert guard._proxied(httpx.URL("https://x.example/")) is True
    assert guard._proxied(httpx.URL("http://x.example/")) is False  # no http or all proxy configured
    with pytest.raises(guard.HostNotAllowed):
        guard.precheck("https://127.0.0.1/", False)
    guard.precheck("https://127.0.0.1/", True)
    guard.precheck("https://8.8.8.8:8443/", False)

    guard = _proxy_env(monkeypatch, {"all": "http://proxy:3128"})
    assert guard._proxied(httpx.URL("http://x.example/")) is True

    guard = _proxy_env(monkeypatch, {"https": "http://proxy:3128"}, bypass=True)
    assert guard._proxied(httpx.URL("https://x.example/")) is False  # bypassed hosts connect directly (guarded)


def test_precheck_leaves_unresolvable_names_to_the_proxy(monkeypatch):
    guard = _proxy_env(monkeypatch, {"https": "http://proxy:3128"})

    def boom(*a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(guard.socket, "getaddrinfo", boom)
    guard.precheck("https://no-such-host.invalid/", False)


def test_precheck_default_port_is_443(monkeypatch):
    guard = _proxy_env(monkeypatch, {"https": "http://proxy:3128"})
    seen = []
    monkeypatch.setattr(guard, "allowed_addresses", lambda host, port, allow: seen.append(port) or [])
    guard.precheck("https://x.example/", False)
    guard.precheck("https://x.example:8443/", False)
    assert seen == [443, 8443]


def test_guarded_backend_refuses_internal_hosts_and_reports_failures(monkeypatch):
    import httpcore

    from app import net_guard

    backend = net_guard._GuardedBackend(allow_private=False)
    with pytest.raises(net_guard.HostNotAllowed):
        backend.connect_tcp("127.0.0.1", 80)

    def gai(*a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(net_guard.socket, "getaddrinfo", gai)
    with pytest.raises(httpcore.ConnectError, match="nope"):
        backend.connect_tcp("unresolvable.example", 80)

    monkeypatch.setattr(net_guard, "allowed_addresses", lambda *a, **k: [])
    with pytest.raises(httpcore.ConnectError, match="could not connect to empty.example"):
        backend.connect_tcp("empty.example", 80)
