"""Large text assets are gzip-compressed once and served from memory; nothing else changes."""
from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from app import static_files

ASSETS = Path(__file__).resolve().parents[1] / "app" / "static" / "assets"


def _big_js() -> str:
    return max((p for p in ASSETS.glob("*.js")), key=lambda p: p.stat().st_size).name


@pytest.fixture(autouse=True)
def _fresh_cache():
    static_files._cache.clear()
    static_files._cache_bytes = 0
    yield
    static_files._cache.clear()
    static_files._cache_bytes = 0


def test_a_large_asset_is_served_gzipped_once_and_decodes_to_the_original(client, monkeypatch):
    name = _big_js()
    calls = []
    real = gzip.compress
    monkeypatch.setattr(static_files.gzip, "compress", lambda *a, **k: calls.append(1) or real(*a, **k))

    first = client.get(f"/static/assets/{name}", headers={"Accept-Encoding": "gzip"})
    second = client.get(f"/static/assets/{name}", headers={"Accept-Encoding": "gzip"})

    assert first.status_code == second.status_code == 200
    assert first.headers["content-encoding"] == "gzip" and first.headers["vary"] == "Accept-Encoding"
    assert "immutable" in first.headers["cache-control"]
    assert first.content == (ASSETS / name).read_bytes()  # one layer of gzip: the client decoded it back to the file
    assert len(calls) == 1  # compressed on the first request, reused on the second


def test_the_compressed_copy_is_smaller_on_the_wire(client):
    name = _big_js()
    body = static_files._compressed(str(ASSETS / name), (str(ASSETS / name), (ASSETS / name).stat().st_mtime_ns, (ASSETS / name).stat().st_size))
    assert len(body) < (ASSETS / name).stat().st_size * 0.6
    assert gzip.decompress(body) == (ASSETS / name).read_bytes()


def test_clients_that_do_not_accept_gzip_get_the_plain_file(client):
    res = client.get(f"/static/assets/{_big_js()}", headers={"Accept-Encoding": "identity"})
    assert res.status_code == 200
    assert "content-encoding" not in res.headers
    assert res.content == (ASSETS / _big_js()).read_bytes()


def test_conditional_requests_still_answer_304(client):
    name = _big_js()
    first = client.get(f"/static/assets/{name}", headers={"Accept-Encoding": "gzip"})
    etag = first.headers.get("etag")
    assert etag
    again = client.get(f"/static/assets/{name}", headers={"Accept-Encoding": "gzip", "If-None-Match": etag})
    assert again.status_code == 304


def test_a_changed_file_is_compressed_again(tmp_path):
    target = tmp_path / "a.js"
    target.write_text("x" * 5000)
    st = target.stat()
    key1 = (str(target), st.st_mtime_ns, st.st_size)
    first = static_files._compressed(str(target), key1)
    target.write_text("y" * 5000)
    import os
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    st2 = target.stat()
    second = static_files._compressed(str(target), (str(target), st2.st_mtime_ns, st2.st_size))
    assert gzip.decompress(first) != gzip.decompress(second)


def test_the_cache_is_bounded(monkeypatch, tmp_path):
    monkeypatch.setattr(static_files, "_CACHE_LIMIT_BYTES", 150)
    for i in range(6):
        f = tmp_path / f"{i}.js"
        f.write_text(str(i) * 4000 + "".join(chr(65 + (j * 7 + i) % 26) for j in range(800)))
        st = f.stat()
        static_files._compressed(str(f), (str(f), st.st_mtime_ns, st.st_size))
    assert static_files._cache_bytes <= 150 and len(static_files._cache) < 6


def test_every_file_type_shipped_in_static_has_a_real_mime_type(client):
    """The slim image has no /etc/mime.types; an untyped image or font is refused by WebKit under nosniff."""
    import mimetypes

    static = ASSETS.parent
    suffixes = {p.suffix.lower() for p in static.rglob("*") if p.is_file() and p.suffix}
    untyped = sorted(s for s in suffixes if mimetypes.guess_type(f"x{s}")[0] is None)
    assert untyped == [], f"served as application/octet-stream: {untyped}"


def test_images_and_fonts_are_served_with_their_own_content_type(client):
    static = ASSETS.parent
    for path, expected in ((next(static.rglob("*.webp"), None), "image/webp"), (next(static.rglob("*.woff2"), None), "font/woff2")):
        if path is None:
            continue
        rel = path.relative_to(static).as_posix()
        assert client.get(f"/static/{rel}").headers["content-type"].startswith(expected), rel
