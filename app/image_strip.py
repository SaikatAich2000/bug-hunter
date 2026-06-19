"""EXIF / metadata stripper for uploaded images.

Photos taken with a phone include EXIF metadata — GPS coordinates, camera
serial number, capture timestamp, original filename, sometimes the
photographer's name. When a bug report is filed with a screenshot snapped on a
phone, all of that metadata travels into Bug Hunter and is downloadable by
anyone with access to the bug.

This module re-encodes uploaded images through Pillow with the ``info`` dict
cleared, which drops EXIF (JPEG), tEXt/iTXt/eXIf chunks (PNG), XMP, ICC
profiles, and any other ancillary metadata blocks. Pixel data is preserved
exactly.

Behaviour:
  - Only runs when the upload's content_type starts with ``image/``.
  - Pillow is imported lazily so non-image deployments don't pay for it.
  - On ANY failure (Pillow not installed, format unsupported, corrupt
    file, decompression-bomb-protection trip), the original bytes are
    returned unchanged — fail-open, never block a legitimate upload
    because of metadata cleanup.
  - SVG is excluded (it's text/XML, not raster) and falls through to
    the route's own active-content-type defenses.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("bug_hunter.image_strip")

# Formats we recognise as raster images Pillow can safely re-encode.
# Extending this set is fine — anything Pillow can read it can write.
_RASTER_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                    "image/bmp", "image/tiff")

# Decoded-pixel ceiling (50 MP). Pillow's own default only WARNS at ~178 MP and
# raises only at 2× that (~357 MP ≈ 1.4 GB RGBA) — enough to OOM the small
# target box. The 50 MB upload cap bounds COMPRESSED bytes, so a highly
# compressible "decompression bomb" image can still declare an enormous raster;
# this bounds the actual decode. 50 MP comfortably covers real phone/camera
# screenshots while rejecting bombs.
_MAX_IMAGE_PIXELS = 50_000_000


def strip_image_metadata(data: bytes, content_type: str | None) -> bytes:
    """Return a metadata-stripped copy of ``data`` if it's a recognised
    raster image, else ``data`` unchanged.

    Idempotent: a second pass over already-stripped bytes is a no-op
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
            # Reject oversized rasters from the (cheap) header read BEFORE load()
            # ever allocates the pixel buffer. The 50 MB upload cap bounds
            # COMPRESSED bytes, so a highly-compressible "decompression bomb" can
            # still declare an enormous raster; this bounds the actual decode.
            # Pillow's own MAX_IMAGE_PIXELS stays a secondary backstop.
            width, height = img.size
            if width * height > _MAX_IMAGE_PIXELS:
                logger.info("EXIF strip skipped (%s): %d×%d px exceeds the decode budget",
                            ct, width, height)
                return data
            img.load()
            fmt = img.format
            if fmt is None:
                return data
            # info is where Pillow exposes EXIF/ICC/XMP/text chunks.
            # Clearing it BEFORE save guarantees none of them ride into
            # the output buffer.
            img.info = {}
            out = io.BytesIO()
            # Pass exif=b"" explicitly for JPEG so even a future Pillow
            # version that starts preserving EXIF from info.get('exif')
            # still emits a blank EXIF block.
            if fmt == "JPEG":
                img.save(out, format=fmt, exif=b"")
            else:
                img.save(out, format=fmt)
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open
        # Corrupt image, a format Pillow can't decode, or a decompression-bomb
        # trip — leave the file alone. We catch broadly ON PURPOSE: the contract
        # is "never block a legitimate upload over metadata cleanup", and Pillow
        # raises a mix of OSError (UnidentifiedImageError) AND bare-Exception
        # subclasses — notably Image.DecompressionBombError(Exception) — that a
        # narrow tuple would let escape as a 500. Logged at info level (not warn)
        # because it's an expected outcome for exotic uploads.
        logger.info("EXIF strip skipped (%s): %s", ct, exc)
        return data
