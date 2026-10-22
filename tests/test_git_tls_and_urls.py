"""TLS trust, transport classification and API-URL normalization for the Git provider.

Two failure modes this guards against, both observed in the field:

* Corporate TLS interception: ``curl``/Windows trust the interception CA, but
  httpx's bundled ``certifi`` roots do not, so every Git call died with
  ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``. The
  provider must verify against the OS trust store (app/git/tls.py), and must
  still verify, never fall back to an unverified connection.
* URL normalization: a stored ``https://github.com`` or an enterprise
  ``https://ghe.example.com/api/v3`` must map to the exact REST base, and a
  value carrying credentials, a query or a fragment must be refused.

Nothing here touches the network: the transport is stubbed.
"""
from __future__ import annotations

import ssl
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

# A realistic-looking PAT. Only used to prove it never leaks.
TOKEN = "ghp_" + "a" * 30


def _github():
    """The currently loaded app.git.github module (conftest re-imports app.*)."""
    from app.git import github

    return github


class _StubResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self._payload = {"login": "acme"} if payload is None else payload

    def json(self):
        return self._payload


def _install_transport(monkeypatch, github, *, response=None, raiser=None):
    """Replace httpx.Client with a recorder; returns the recorded calls."""
    calls: dict[str, list] = {"verify": [], "count": 0}

    class _Recorder:
        def __init__(self, **kwargs):
            calls["count"] += 1
            calls["verify"].append(kwargs.get("verify"))
            assert "verify" in kwargs, "TLS verification must always be passed"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            if raiser is not None:
                raise raiser
            return response if response is not None else _StubResponse()

    monkeypatch.setattr(github.httpx, "Client", _Recorder)
    return calls


def _provider(github, base_url="https://ghe.example.com", token=TOKEN):
    return github.GitHubEnterpriseProvider(
        base_url=base_url, organization="acme", token=token
    )


# A. TLS trust

def test_system_trust_context_is_handed_to_httpx(monkeypatch):
    """The OS-trust context: not the certifi default, is what httpx verifies with."""
    github = _github()
    sentinel = object()
    monkeypatch.setattr(github, "system_trust_context", lambda: sentinel)
    calls = _install_transport(monkeypatch, github)

    assert _provider(github).validate_connection() == "acme"
    assert calls["verify"] == [sentinel]


def test_verification_stays_on_with_the_system_trust_store():
    """The real context enforces verification: required + hostname checked."""
    from app.git.tls import system_trust_context

    context = system_trust_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.parametrize("relative", ["app/git/github.py", "app/git/tls.py"])
def test_source_never_disables_verification(relative):
    """No insecure bypass exists, and none can be switched on by configuration."""
    source = (ROOT / relative).read_text(encoding="utf-8")
    assert "verify=False" not in source
    assert "CERT_NONE" not in source
    assert "check_hostname = False" not in source
    assert "_create_unverified_context" not in source


def test_the_provider_passes_the_context_as_verify():
    source = (ROOT / "app" / "git" / "github.py").read_text(encoding="utf-8")
    assert "verify=ssl_context" in source, "httpx.Client must receive verify=<context>"


def test_certificate_failure_is_classified_with_a_safe_message(monkeypatch, caplog):
    """A trust failure is its own class, and the token never appears anywhere."""
    github = _github()
    cause = ssl.SSLCertVerificationError(
        1, "certificate verify failed: unable to get local issuer certificate"
    )
    error = httpx.ConnectError(f"request failed for token {TOKEN}")
    error.__cause__ = cause
    _install_transport(monkeypatch, github, raiser=error)

    with caplog.at_level("DEBUG"):
        provider = _provider(github)
        with pytest.raises(github.GitProviderError) as excinfo:
            provider.validate_connection()

    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert excinfo.value.message == (
        "GitHub TLS certificate could not be verified using the system trust store."
    )
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in caplog.text
    assert "unable to get local issuer certificate" not in caplog.text


def test_certificate_failure_is_not_retried(monkeypatch):
    """Retrying an untrusted chain cannot help, so it fails on the first attempt."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 5)
    get_settings.cache_clear()
    try:
        error = httpx.ConnectError("boom")
        error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
        calls = _install_transport(monkeypatch, github, raiser=error)
        provider = _provider(github)
        with pytest.raises(github.GitProviderError) as excinfo:
            provider.validate_connection()
        assert excinfo.value.code == github.TLS_TRUST_ERROR
        assert calls["count"] == 1
    finally:
        get_settings.cache_clear()


def test_proxy_dns_and_generic_failures_are_classified_separately(monkeypatch):
    """TLS, proxy, DNS and a plain outage must be distinguishable."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        cases = [
            (httpx.ProxyError("proxy connect failed"), github.PROXY_ERROR),
            (httpx.ConnectError("getaddrinfo failed"), github.DNS_ERROR),
            (httpx.ConnectError("connection refused"), github.UNAVAILABLE),
        ]
        for exc, expected in cases:
            _install_transport(monkeypatch, github, raiser=exc)
            provider = _provider(github)
            with pytest.raises(github.GitProviderError) as excinfo:
                provider.validate_connection()
            assert excinfo.value.code == expected, type(exc).__name__
            assert excinfo.value.code != github.TLS_TRUST_ERROR
    finally:
        get_settings.cache_clear()


def test_timeout_is_still_its_own_class(monkeypatch):
    github = _github()
    _install_transport(monkeypatch, github, raiser=httpx.ReadTimeout("took too long"))
    provider = _provider(github)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.TIMEOUT


def test_trust_store_init_failure_fails_closed_before_any_request(monkeypatch):
    """No trust store means no request at all, never an unverified one."""
    github = _github()

    def _boom():
        raise github.GitProviderError(github.TLS_TRUST_ERROR, github.TLS_TRUST_MESSAGE)

    monkeypatch.setattr(github, "system_trust_context", _boom)
    calls = _install_transport(monkeypatch, github)
    provider = _provider(github)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert calls["count"] == 0, "no connection may be attempted without a trust store"


def test_tls_helper_fails_closed_when_truststore_cannot_be_imported(monkeypatch):
    """A missing/unhealthy trust store is a sanitized configuration error."""
    from app.git import tls
    from app.git.provider import GitProviderError

    monkeypatch.setattr(tls, "_TRUSTSTORE_AVAILABLE", False)
    monkeypatch.setattr(tls, "_TRUSTSTORE_IMPORT_ERROR", "ImportError")
    tls.reset_tls_context_cache()
    try:
        with pytest.raises(GitProviderError) as excinfo:
            tls.system_trust_context()
        assert excinfo.value.code == "tls_trust_error"
        assert excinfo.value.message == tls.TLS_TRUST_MESSAGE
    finally:
        tls.reset_tls_context_cache()


def test_tls_helper_context_is_cached_and_resettable():
    from app.git import tls

    first = tls.system_trust_context()
    assert tls.system_trust_context() is first
    tls.reset_tls_context_cache()
    assert tls.system_trust_context() is not first


def test_provider_repr_and_str_never_render_the_token():
    github = _github()
    provider = _provider(github)
    assert TOKEN not in repr(provider)
    assert TOKEN not in str(provider)
    assert "token=<set>" in repr(provider)
    assert "token=None" in repr(_provider(github, token=None))


def test_requests_are_sent_to_the_normalized_rest_base(monkeypatch):
    """The URL actually used is the /api/v3 enterprise root, not the web root."""
    github = _github()
    seen: list[str] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append(url)
            return _StubResponse()

    monkeypatch.setattr(github.httpx, "Client", _Recorder)
    _provider(github).validate_connection()
    assert seen == ["https://ghe.example.com/api/v3/orgs/acme"]


@pytest.mark.parametrize("ref", ["", "main", "heads/main", "refs/heads/", "refs/heads/*"])
def test_delete_branch_refuses_a_non_exact_ref_without_calling_the_api(monkeypatch, ref):
    """Only an exact ``refs/heads/<branch>`` may ever be deleted.

    Regression guard: the guard clause referenced an error code that was never
    imported, so a malformed stored ref raised ``NameError`` instead of a
    classified provider error. A malformed ref must also never reach the API, so
    the recorder is asserted to have made zero calls.
    """
    github = _github()
    calls = _install_transport(monkeypatch, github)

    provider = _provider(github)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.delete_branch("acme", "widgets", ref)

    assert excinfo.value.code == github.INVALID_BASE_BRANCH
    assert calls["count"] == 0, "an invalid ref must be rejected before any request"


def test_delete_branch_sends_only_the_url_encoded_short_ref(monkeypatch):
    """The stored full ref is stripped to ``refs/heads/<branch>`` exactly once."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=204, payload={})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one") is True
    assert seen == [
        ("DELETE", "https://ghe.example.com/api/v3/repos/acme/widgets/git/refs/heads/feature%2Fone")
    ]


def test_delete_branch_returns_false_only_for_an_exact_404(monkeypatch):
    """A 404 of the exact ref is "already absent"; success stays True."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=404, payload={"message": "Not Found"})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch("acme", "widgets", "refs/heads/feature/one") is False
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (401, "auth_failed"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (502, "unavailable"),
    ],
)
def test_delete_branch_never_treats_http_failures_as_absent(monkeypatch, status_code, expected_code):
    """Auth/permission/rate-limit/server failures raise; they never return False."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        _install_transport(
            monkeypatch, github,
            response=_StubResponse(status_code=status_code, payload={"message": "boom"}),
        )
        provider = _provider(github)
        with pytest.raises(github.GitProviderError) as excinfo:
            provider.delete_branch("acme", "widgets", "refs/heads/feature/one")
        assert excinfo.value.code == expected_code
    finally:
        get_settings.cache_clear()


def test_delete_branch_never_treats_transport_failures_as_absent(monkeypatch):
    """Timeout and TLS failures raise; they never mean "already absent"."""
    github = _github()
    from app.config import Settings, get_settings

    monkeypatch.setattr(Settings, "GITHUB_MAX_RETRIES", 0)
    get_settings.cache_clear()
    try:
        failures = [
            (httpx.ReadTimeout("took too long"), github.TIMEOUT),
            (tls_failure(), github.TLS_TRUST_ERROR),
        ]
        for raiser, expected_code in failures:
            _install_transport(monkeypatch, github, raiser=raiser)
            provider = _provider(github)
            with pytest.raises(github.GitProviderError) as excinfo:
                provider.delete_branch("acme", "widgets", "refs/heads/feature/one")
            assert excinfo.value.code == expected_code
    finally:
        get_settings.cache_clear()


def tls_failure():
    """A transport failure whose cause is a certificate-verification error."""
    error = httpx.ConnectError("request failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    return error


def test_delete_branch_only_uses_the_exact_heads_ref_endpoint(monkeypatch):
    """Only ``/git/refs/heads/<encoded ref>`` is called; never repo or tag."""
    github = _github()
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def request(self, method, url, **kwargs):
            seen.append((method, url))
            return _StubResponse(status_code=204, payload={})

    monkeypatch.setattr(github.httpx, "Client", _Recorder)

    assert _provider(github).delete_branch(
        "acme", "widgets", "refs/heads/feature/spaced name"
    ) is True
    assert seen == [
        (
            "DELETE",
            "https://ghe.example.com/api/v3/repos/acme/widgets"
            "/git/refs/heads/feature%2Fspaced%20name",
        )
    ]
    for _method, url in seen:
        assert "/git/refs/heads/" in url
        assert url.rstrip("/").endswith("/git/refs/heads/feature%2Fspaced%20name")
        assert "/git/tags" not in url
        assert not url.endswith("/repos/acme/widgets")



# B. API URL normalization

@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # blank -> deployment default
        ("", "https://api.github.com"),
        ("   ", "https://api.github.com"),
        # public github.com variants -> the public REST root
        ("https://github.com", "https://api.github.com"),
        ("https://github.com/", "https://api.github.com"),
        ("https://www.github.com", "https://api.github.com"),
        ("https://WWW.GitHub.com", "https://api.github.com"),
        # the API host itself, with or without a trailing slash -> unchanged
        ("https://api.github.com", "https://api.github.com"),
        ("https://api.github.com/", "https://api.github.com"),
        # enterprise -> exactly one /api/v3
        ("https://ghe.example.com", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com/api/v3"),
        ("https://ghe.example.com/api/v3/", "https://ghe.example.com/api/v3"),
        # a valid enterprise port is preserved
        ("https://ghe.example.com:8443", "https://ghe.example.com:8443/api/v3"),
        ("https://ghe.example.com:8443/", "https://ghe.example.com:8443/api/v3"),
        ("https://ghe.example.com:8443/api/v3", "https://ghe.example.com:8443/api/v3"),
        # loopback http stays usable for local smoke testing
        ("http://localhost:3000", "http://localhost:3000/api/v3"),
    ],
)
def test_api_base_normalization(stored, expected):
    github = _github()
    assert github.normalize_api_base(stored) == expected


@pytest.mark.parametrize(
    "stored",
    [
        "https://ghe.example.com/api/v3/api/v3",  # never double the suffix
        "https://ghe.example.com/some/path",
        "https://user:pass@ghe.example.com",  # credentials in a URL
        "https://user@ghe.example.com",  # userinfo without a password
        "https://ghe.example.com?redirect=evil",  # query string
        "https://ghe.example.com#fragment",
        "file:///etc/passwd",  # unsafe scheme
        "ftp://ghe.example.com",
        "javascript:alert(1)",
        "https://",  # no host
    ],
)
def test_api_base_rejects_unsafe_values(stored):
    github = _github()
    with pytest.raises(github.GitBaseUrlError):
        github.normalize_api_base(stored)


def test_api_base_is_idempotent():
    """Re-normalizing a normalized value must not append /api/v3 twice."""
    github = _github()
    once = github.normalize_api_base("https://ghe.example.com")
    assert github.normalize_api_base(once) == once
    assert once.count("/api/v3") == 1


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("", "https://github.com"),
        ("https://github.com", "https://github.com"),
        ("https://api.github.com", "https://github.com"),
        ("https://ghe.example.com", "https://ghe.example.com"),
        ("https://ghe.example.com/api/v3", "https://ghe.example.com"),
        ("https://ghe.example.com:8443", "https://ghe.example.com:8443"),
    ],
)
def test_web_base_normalization(stored, expected):
    github = _github()
    assert github.normalize_web_base(stored) == expected


def test_branch_url_uses_the_web_base_not_the_api_base():
    github = _github()
    provider = github.GitHubEnterpriseProvider(
        base_url="https://ghe.example.com", organization="acme", token=TOKEN
    )
    assert provider.build_branch_url("acme", "web", "bug/123-fix") == (
        "https://ghe.example.com/acme/web/tree/bug/123-fix"
    )


def test_an_unusable_stored_base_url_is_a_safe_configuration_error(monkeypatch):
    """A bad saved URL must not 500 and must not echo the value back."""
    github = _github()
    monkeypatch.setattr(github.httpx, "Client", _NeverCalled)
    provider = github.GitHubEnterpriseProvider(
        base_url="https://user:secret@ghe.example.com", organization="acme", token=TOKEN
    )
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_BASE_URL
    assert "secret" not in excinfo.value.message
    with pytest.raises(github.GitProviderError) as url_excinfo:
        provider.build_branch_url("acme", "web", "dev")
    assert url_excinfo.value.code == github.INVALID_BASE_URL


class _NeverCalled:
    """A client factory that fails the test if a request is ever attempted."""

    def __init__(self, **kwargs):
        raise AssertionError("no request may be attempted for an unusable base URL")


# F. Strict draft semantics: explicit blank is never replaced

def test_draft_explicit_blank_base_url_is_tested_as_blank(monkeypatch):
    """An explicit "" draft must reach the provider unchanged (then fail safe)."""
    github = _github()
    seen: dict[str, object] = {}

    class _Capture:
        def __init__(self, **kwargs):
            seen["base_url"] = kwargs.get("base_url", "")

        def validate_connection(self):
            raise github.GitProviderError(github.INVALID_BASE_URL, "bad test value")

    monkeypatch.setattr(github, "GitHubEnterpriseProvider", _Capture)
    provider = github.GitHubEnterpriseProvider(base_url="", organization="acme")
    assert seen["base_url"] == ""
    with pytest.raises(github.GitProviderError):
        provider.validate_connection()


def test_build_provider_none_vs_blank_resolution(monkeypatch):
    """None inherits saved-then-env; "" stays an explicit blank draft value."""
    from app.config import get_settings
    from app.routes import git as routes

    settings = get_settings()
    saved = SimpleNamespace(
        base_url="https://saved.example.com",
        organization="saved-org",
        credential_encrypted="",
    )
    monkeypatch.setattr(
        __import__("app.git.credentials", fromlist=["x"]), "resolve_token", lambda config, base_url=None: "global-fallback"
    )
    monkeypatch.setattr(
        routes, "GitHubEnterpriseProvider",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    inherited = routes.build_provider(saved)
    assert inherited.base_url == "https://saved.example.com"
    assert inherited.organization == "saved-org"
    blank = routes.build_provider(saved, base_url="", organization="")
    assert blank.base_url == ""
    assert blank.organization == ""
    monkeypatch.setattr(
        routes, "GitHubEnterpriseProvider",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    env_only = routes.build_provider(None)
    assert env_only.base_url == (settings.GITHUB_API_URL or "").strip()


def test_require_https_allows_loopback_http_but_rejects_remote_http():
    github = _github()
    assert github.require_https_provider_url(
        "http://localhost:8080"
    ) == "http://localhost:8080/api/v3"
    assert github.require_https_provider_url(
        "http://127.0.0.1:9000"
    ) == "http://127.0.0.1:9000/api/v3"
    with pytest.raises(github.GitBaseUrlError):
        github.require_https_provider_url("http://ghe.example.com")
    with pytest.raises(github.GitBaseUrlError):
        github.require_https_provider_url("http://192.168.1.10")


def test_require_https_rejects_userinfo_query_fragment_and_schemes():
    github = _github()
    for bad in (
        "https://user:pass@ghe.example.com",
        "https://user@ghe.example.com",
        "https://ghe.example.com?x=1",
        "https://ghe.example.com#frag",
        "ftp://ghe.example.com",
        "file:///etc/passwd",
        "https://ghe.example.com/extra/path",
    ):
        with pytest.raises(github.GitBaseUrlError):
            github.require_https_provider_url(bad)


def test_tls_ca_bundle_missing_file_fails_closed(monkeypatch, tmp_path):
    from app.git import tls as tls_mod

    tls_mod.reset_tls_context_cache()
    monkeypatch.setattr(
        tls_mod, "ca_bundle_configured",
        lambda: str(tmp_path / "missing-ca.pem"),
    )
    with pytest.raises(tls_mod.GitProviderError) as excinfo:
        tls_mod.system_trust_context()
    assert excinfo.value.code == tls_mod.TLS_TRUST_ERROR
    tls_mod.reset_tls_context_cache()


def test_tls_ca_bundle_invalid_file_fails_closed(monkeypatch, tmp_path):
    from app.git import tls as tls_mod

    bad = tmp_path / "bad-ca.pem"
    bad.write_text("not a pem bundle", encoding="utf-8")
    tls_mod.reset_tls_context_cache()
    monkeypatch.setattr(tls_mod, "ca_bundle_configured", lambda: str(bad))
    with pytest.raises(tls_mod.GitProviderError) as excinfo:
        tls_mod.system_trust_context()
    assert excinfo.value.code == tls_mod.TLS_TRUST_ERROR
    tls_mod.reset_tls_context_cache()


def test_tls_error_message_never_contains_pat(monkeypatch):
    github = _github()
    token = "super-secret-project-pat-value-12345"
    calls = _install_transport(
        monkeypatch, github,
        raiser=ssl.SSLCertVerificationError("certificate verify failed"),
    )
    provider = github.GitHubEnterpriseProvider(
        base_url="https://ghe.example.com", organization="acme", token=token
    )
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.TLS_TRUST_ERROR
    assert token not in excinfo.value.message
    assert calls["count"] >= 1


# Provider API surface: repositories, branches, error mapping

class _Reply:
    """A scripted httpx response: ``payload`` is returned by json(); ``bad_json`` makes json() raise."""

    def __init__(self, status_code=200, payload=None, *, bad_json=False, headers=None, content=b"{}"):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def _script(monkeypatch, github, *replies):
    """Serve ``replies`` in order, one per request; returns the (method, url, params) log."""
    log: list[tuple] = []
    queue = list(replies)

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def request(self, method, url, **kwargs):
            log.append((method, url, kwargs.get("params")))
            return queue.pop(0)

    monkeypatch.setattr(github.httpx, "Client", _Client)
    monkeypatch.setattr(github.time, "sleep", lambda _s: None)
    return log


def _repo_row(index, **extra):
    return {"id": index, "name": f"repo-{index}", "owner": {"login": "acme"},
            "html_url": f"https://ghe.example.com/acme/repo-{index}", "default_branch": "main", **extra}


def test_repositories_are_paged_until_the_limit(monkeypatch):
    github = _github()
    first = [_repo_row(i) for i in range(100)]
    second = [_repo_row(i) for i in range(100, 150)]
    log = _script(monkeypatch, github, _Reply(payload=first), _Reply(payload=second))

    repos = _provider(github).list_repositories(limit=150)
    assert [r.name for r in repos][:2] == ["repo-0", "repo-1"]
    assert len(repos) == 150
    assert [p["page"] for _m, _u, p in log] == [1, 2]
    assert log[0][1].endswith("/orgs/acme/repos")
    assert log[0][2]["type"] == "all"


def test_repositories_without_an_organization_list_the_users_own(monkeypatch):
    github = _github()
    log = _script(monkeypatch, github, _Reply(payload=[_repo_row(1)]))
    provider = github.GitHubEnterpriseProvider(base_url="https://ghe.example.com", organization="", token=TOKEN)

    assert [r.name for r in provider.list_repositories(limit=0)] == ["repo-1"]
    assert log[0][1].endswith("/user/repos")
    assert "type" not in log[0][2]
    assert log[0][2]["per_page"] == 1


def test_repository_rows_are_mapped_defensively(monkeypatch):
    github = _github()
    rows = [
        "not a mapping",
        {"id": 1, "name": "  "},
        {"id": 2, "name": "from-full-name", "full_name": "octo/from-full-name"},
        {"id": 3, "name": "no-owner", "owner": {}},
        _repo_row(4),
    ]
    _script(monkeypatch, github, _Reply(payload=rows), _Reply(payload={"message": "ignored"}))

    repos = _provider(github).list_repositories(limit=10)
    assert [(r.name, r.owner) for r in repos] == [("from-full-name", "octo"), ("no-owner", ""), ("repo-4", "acme")]
    assert repos[0].provider_repo_id == "2"

    _script(monkeypatch, github, _Reply(payload={"unexpected": True}))
    assert _provider(github).list_repositories() == []


def test_connection_label_falls_back_to_the_organization_or_api_base(monkeypatch):
    github = _github()
    _script(monkeypatch, github, _Reply(payload={}))
    assert _provider(github).validate_connection() == "acme"

    anonymous = github.GitHubEnterpriseProvider(base_url="https://ghe.example.com", organization="", token=TOKEN)
    _script(monkeypatch, github, _Reply(payload={"login": "octocat"}))
    assert anonymous.validate_connection() == "octocat"
    _script(monkeypatch, github, _Reply(payload=[]))
    assert anonymous.validate_connection() == "https://ghe.example.com/api/v3"


def test_branch_lookup_distinguishes_missing_present_and_unresolvable(monkeypatch):
    github = _github()
    provider = _provider(github)

    _script(monkeypatch, github, _Reply(payload={"commit": {"sha": "abc123"}}))
    assert provider.get_branch_sha("acme", "repo", "main") == "abc123"
    _script(monkeypatch, github, _Reply(404, {"message": "Branch not found"}))
    assert provider.get_branch_sha("acme", "repo", "gone") is None
    _script(monkeypatch, github, _Reply(payload={"commit": {}}))
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.get_branch_sha("acme", "repo", "odd")
    assert excinfo.value.code == github.BASE_BRANCH_MISSING

    _script(monkeypatch, github, _Reply(payload={"name": "main"}))
    assert provider.branch_exists("acme", "repo", "main") is True
    _script(monkeypatch, github, _Reply(404, {"message": "Branch not found"}))
    assert provider.branch_exists("acme", "repo", "gone") is False


def test_create_branch_returns_the_new_ref_sha_or_nothing(monkeypatch):
    github = _github()
    provider = _provider(github)

    log = _script(monkeypatch, github, _Reply(201, {"object": {"sha": "def456"}}))
    assert provider.create_branch("acme", "repo", "feature/x", "abc123") == "def456"
    assert log[0][0] == "POST"
    _script(monkeypatch, github, _Reply(201, ["unexpected"]))
    assert provider.create_branch("acme", "repo", "feature/y", "abc123") == ""
    _script(monkeypatch, github, _Reply(201, {"object": "odd"}))
    assert provider.create_branch("acme", "repo", "feature/z", "abc123") == ""


def test_error_mapping_covers_unparseable_bodies_and_transient_outages(monkeypatch):
    github = _github()
    provider = _provider(github)

    _script(monkeypatch, github, _Reply(418, bad_json=True))
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_RESPONSE
    assert "HTTP 418" in str(excinfo.value)

    _script(monkeypatch, github, _Reply(418, ["not", "a", "mapping"]))
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert "HTTP 418" in str(excinfo.value)

    outage = [_Reply(503, {"message": "down"})] * 10
    log = _script(monkeypatch, github, *outage)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.UNAVAILABLE
    assert excinfo.value.retryable
    assert len(log) == github.get_settings().GITHUB_MAX_RETRIES + 1


def test_a_success_body_that_is_not_json_is_an_invalid_response(monkeypatch):
    github = _github()
    _script(monkeypatch, github, _Reply(200, bad_json=True))
    provider = _provider(github)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_RESPONSE


def test_a_missing_token_is_reported_before_any_request(monkeypatch):
    github = _github()
    log = _script(monkeypatch, github)
    tokenless = github.GitHubEnterpriseProvider(base_url="https://ghe.example.com", organization="acme", token="")
    with pytest.raises(github.GitProviderError) as excinfo:
        tokenless.validate_connection()
    assert excinfo.value.code == github.NOT_CONFIGURED
    assert log == []


def test_private_hosts_are_refused_before_the_request(monkeypatch):
    github = _github()
    log = _script(monkeypatch, github)
    # Only a proxied request is checked up front; a direct one is guarded at connect time.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setattr(github.get_settings(), "GIT_ALLOW_PRIVATE_HOSTS", False)
    provider = github.GitHubEnterpriseProvider(base_url="https://10.1.2.3", organization="acme", token=TOKEN)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.validate_connection()
    assert excinfo.value.code == github.INVALID_BASE_URL
    assert log == []


def test_name_resolution_failures_are_recognised_through_the_exception_chain():
    import socket

    github = _github()
    try:
        try:
            raise socket.gaierror(-2, "Name or service not known")
        except socket.gaierror as inner:
            raise httpx.ConnectError("connection failed") from inner
    except httpx.ConnectError as outer:
        assert github._is_dns_failure(outer) is True
    assert github._is_dns_failure(httpx.ConnectError("refused")) is False


def test_base_urls_with_userinfo_or_blank_hosts_are_handled():
    github = _github()
    with pytest.raises(github.GitBaseUrlError):
        github.require_https_provider_url("https://user:secret@ghe.example.com")
    assert github.require_https_provider_url("") == "https://api.github.com"


def test_url_validation_helpers_and_the_cache_shim():
    from urllib.parse import urlparse

    github = _github()
    parts = urlparse("https://user:secret@ghe.example.com")
    with pytest.raises(github.GitBaseUrlError, match="credentials"):
        github._check_url_parts(parts, "https")
    assert github.reset_token_cache() is None


def test_requests_without_a_token_carry_no_authorization_header(monkeypatch):
    github = _github()
    sent = {}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def request(self, method, url, **kwargs):
            sent.update(kwargs["headers"])
            return _Reply(payload={"login": "anon"})

    monkeypatch.setattr(github.httpx, "Client", _Client)
    assert _provider(github)._request("GET", "/user", token="") == {"login": "anon"}
    assert "Authorization" not in sent


def test_a_branch_whose_commit_is_not_an_object_has_no_sha(monkeypatch):
    github = _github()
    _script(monkeypatch, github, _Reply(payload={"commit": "abc"}))
    provider = _provider(github)
    with pytest.raises(github.GitProviderError) as excinfo:
        provider.get_branch_sha("acme", "repo", "main")
    assert excinfo.value.code == github.BASE_BRANCH_MISSING


# trust-store helpers and message sanitising

def test_trust_store_availability_is_reported():
    from app.git import tls

    assert tls.trust_store_available() is True


def test_ca_bundle_context_fails_closed_without_a_usable_bundle(monkeypatch):
    import ssl

    from app.git import tls
    from app.git.provider import TLS_TRUST_ERROR, GitProviderError

    with pytest.raises(GitProviderError) as excinfo:
        tls._load_verified_bundle_context("  ")
    assert excinfo.value.code == TLS_TRUST_ERROR

    class _Lax(ssl.SSLContext):
        verify_mode = property(lambda self: ssl.CERT_NONE)

        def load_verify_locations(self, *args, **kwargs):
            return None

    bundle = ROOT / "pyproject.toml"  # any existing file: the lax context never parses it
    monkeypatch.setattr(tls.ssl, "SSLContext", _Lax)
    with pytest.raises(GitProviderError) as excinfo:
        tls._load_verified_bundle_context(str(bundle))
    assert excinfo.value.code == TLS_TRUST_ERROR


def test_certificate_failures_are_recognised_by_type_name_or_message():
    from app.git import tls

    class CertificateVerifyError(Exception):
        pass

    assert tls.is_certificate_verification_error(CertificateVerifyError("boom")) is True
    assert tls.is_certificate_verification_error(OSError("self-signed certificate in certificate chain")) is True
    assert tls.is_certificate_verification_error(OSError("connection reset")) is False


def test_provider_messages_are_collapsed_redacted_and_capped(monkeypatch):
    from app.git import provider

    assert provider.sanitize_provider_message("   ") == "Git provider request failed"
    assert provider.sanitize_provider_message("", fallback="custom") == "custom"
    assert provider.sanitize_provider_message("two\n  lines\there") == "two lines here"
    long = provider.sanitize_provider_message("word " * 400)
    assert long.endswith("...")
    assert len(long) <= provider._MAX_MESSAGE_CHARS + 3

    monkeypatch.setattr(provider, "_configured_secret_values", lambda extra=(): ("hunter2-hunter2",))
    assert "hunter2" not in provider.sanitize_provider_message("the token hunter2-hunter2 leaked")
    assert provider.sanitize_provider_message("the token hunter2-hunter2 leaked").count("[redacted]") >= 0
    assert "hunter2" not in provider.redact_secrets("Bearer hunter2-hunter2 rejected")


def test_only_credential_length_values_are_collected_as_secrets():
    from app.git import provider

    values = provider._configured_secret_values(("short", "  a-long-enough-secret ", ""))
    assert "a-long-enough-secret" in values
    assert "short" not in values


def test_redact_secrets_erases_caller_supplied_values():
    from app.git import provider

    assert "plain-secret-value" not in provider.redact_secrets(
        "rejected plain-secret-value", extra_secrets=("plain-secret-value",))
