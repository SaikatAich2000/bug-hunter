"""EXIF / metadata stripper for uploaded images.

Phone photos carry EXIF metadata (GPS coordinates, camera serial, capture
timestamp, original filename, sometimes the photographer's name). Without this
module that metadata would travel into Bug Hunter with any screenshot and be
downloadable by anyone with access to the bug.

Uploaded images are re-encoded through Pillow with the ``info`` dict cleared,
which drops EXIF (JPEG), tEXt/iTXt/eXIf chunks (PNG), XMP, ICC profiles, and
other ancillary metadata blocks. Pixel data is preserved exactly.

Behaviour:
  - Only runs when the upload's content_type starts with ``image/``.
  - Pillow is imported lazily so non-image deployments don't pay for it.
  - On any failure (Pillow not installed, format unsupported, corrupt file,
    decompression-bomb trip), the original bytes are returned unchanged, so
    metadata cleanup never blocks a legitimate upload.
  - SVG is excluded (it's text/XML, not raster) and moves on to the
    route's own active-content-type defenses.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("bug_hunter.image_strip")

# Raster formats Pillow can safely re-encode. Add more as needed.
_RASTER_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                    "image/bmp", "image/tiff")

# 50 MP ceiling for decoded pixels. Pillow's built-in limit is ~178 MP (warn)
# and ~357 MP (raise), both high enough to OOM a small server. A 50 MB
# compressed upload can still declare a huge raster, so we check before
# allocating the pixel buffer.
_MAX_IMAGE_PIXELS = 50_000_000


def strip_image_metadata(data: bytes, content_type: str | None) -> bytes:
    """Return a metadata-stripped copy of ``data`` if it's a recognised
    raster image, else ``data`` unchanged.

    Idempotent: a second pass over already-stripped bytes does nothing
    (modulo lossless re-encode jitter for JPEG).
    """
    if not data or not content_type:
        return data
    ct = content_type.lower().split(";")[0].strip()
    if not any(ct.startswith(p) for p in _RASTER_PREFIXES):
        return data

    try:
        from PIL import Image
    except ImportError:
        # Pillow isn't installed — operator opted out by pinning a
        # build without it. Return original.
        return data

    try:
        with io.BytesIO(data) as src:
            img = Image.open(src)
            # Check dimensions before load() allocates the pixel buffer.
            # Pillow's MAX_IMAGE_PIXELS stays a secondary backstop.
            width, height = img.size
            if width * height > _MAX_IMAGE_PIXELS:
                logger.info("EXIF strip skipped (%s): %d×%d px exceeds the decode budget",
                            ct, width, height)
                return data
            # Animated GIF/WebP and multi-page TIFF: a plain save() writes only
            # the first frame, destroying animation. Re-encoding all frames
            # while reliably dropping metadata is fragile, so leave multi-frame
            # files untouched (the route's content-type checks still apply).
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                logger.info("EXIF strip skipped (%s): preserving multi-frame image", ct)
                return data
            img.load()
            fmt = img.format
            if fmt is None:
                return data
            # Pillow exposes EXIF/ICC/XMP/text chunks through img.info;
            # clearing it before save keeps them out of the output buffer.
            img.info = {}
            out = io.BytesIO()
            # For JPEG, pass exif=b"" explicitly so a future Pillow version
            # that reads EXIF back from info still emits a blank block.
            if fmt == "JPEG":
                img.save(out, format=fmt, exif=b"")
            else:
                img.save(out, format=fmt)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        # Catch-all: corrupt file, unsupported format, or decompression-bomb.
        # The contract is to never block a legitimate upload over metadata
        # cleanup, so we fail open. Pillow raises a mix of OSError
        # (UnidentifiedImageError) and bare Exception subclasses
        # (Image.DecompressionBombError), so a narrow tuple would let some
        # escape as a 500. Info level because exotic uploads hit this routinely.
        logger.info("EXIF strip skipped (%s): %s", ct, exc)
        return data
