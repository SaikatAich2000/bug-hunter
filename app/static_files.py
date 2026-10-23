"""Static files with the gzip work done once, not on every request.

The gzip middleware compresses each response again on every request, which for a 200 KB JavaScript
bundle costs several milliseconds of CPU each time. On a small instance (a tenth of a core) a first
page load fetches a dozen such files at once. This subclass compresses a text asset the first time
it is asked for, keeps the result in memory keyed by file and modification time, and serves that.
The gzip middleware leaves a response that already has a Content-Encoding alone.
"""
from __future__ import annotations

import gzip
import mimetypes
from threading import Lock

import anyio
from starlette.datastructures import Headers
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

# Python's MIME table comes from the operating system and the slim container image has none, so these
# types would otherwise be sent as application/octet-stream. With `nosniff` that makes WebKit (Safari,
# every browser on iOS) refuse the images and fonts. Registered here, once, before any file is served.
for _ext, _mime in {
    ".webp": "image/webp", ".avif": "image/avif", ".woff2": "font/woff2", ".woff": "font/woff",
    ".vtt": "text/vtt", ".webmanifest": "application/manifest+json", ".mjs": "text/javascript",
}.items():
    mimetypes.add_type(_mime, _ext)

_COMPRESSIBLE = (".js", ".css", ".svg", ".json", ".html", ".txt", ".xml", ".map", ".webmanifest")
_MIN_BYTES = 1024
_CACHE_LIMIT_BYTES = 24 * 1024 * 1024  # the whole built frontend compresses to about 2 MB

_cache: dict[tuple[str, int, int], bytes] = {}
_cache_bytes = 0
_lock = Lock()


def _compressed(path: str, key: tuple[str, int, int]) -> bytes:
    global _cache_bytes
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit
    with open(path, "rb") as fh:
        body = gzip.compress(fh.read(), compresslevel=6, mtime=0)
    with _lock:
        if _cache_bytes + len(body) > _CACHE_LIMIT_BYTES:
            _cache.clear()
            _cache_bytes = 0
        _cache[key] = body
        _cache_bytes += len(body)
    return body


class CompressedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if (
            scope["method"] != "GET"
            or response.status_code != 200
            or not isinstance(response, FileResponse)
            or not str(response.path).endswith(_COMPRESSIBLE)
            or "gzip" not in Headers(scope=scope).get("accept-encoding", "")
        ):
            return response
        stat = response.stat_result
        if stat is None or stat.st_size < _MIN_BYTES:
            return response
        key = (str(response.path), stat.st_mtime_ns, stat.st_size)
        body = await anyio.to_thread.run_sync(_compressed, str(response.path), key)
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-type")}
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
        return Response(body, media_type=response.media_type, headers=headers)
