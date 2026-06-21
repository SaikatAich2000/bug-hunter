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
    decompression-bomb trip), the original bytes are returned unchanged. This
    is fail-open: metadata cleanup never blocks a legitimate upload.
  - SVG is excluded (it's text/XML, not raster) and falls through to the
    route's own active-content-type defenses.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("bug_hunter.image_strip")

# Formats we recognise as raster images Pillow can safely re-encode.
# Extending this set is fine — anything Pillow can read it can write.
_RASTER_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp",
                    "image/bmp", "image/tiff")

# Decoded-pixel ceiling (50 MP). Pillow's own default only warns at ~178 MP and
# raises only at 2x that (~357 MP, ~1.4 GB RGBA), enough to OOM a small box. The
# 50 MB upload cap bounds compressed bytes, so a highly compressible
# "decompression bomb" image can still declare an enormous raster; this bounds
# the actual decode. 50 MP covers real phone/camera screenshots while rejecting
# bombs.
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
            # Reject oversized rasters from the cheap header read before load()
            # allocates the pixel buffer. The 50 MB upload cap bounds compressed
            # bytes, so a highly compressible "decompression bomb" can still
            # declare an enormous raster; this bounds the actual decode. Pillow's
            # own MAX_IMAGE_PIXELS stays a secondary backstop.
            width, height = img.size
            if width * height > _MAX_IMAGE_PIXELS:
                logger.info("EXIF strip skipped (%s): %d×%d px exceeds the decode budget",
                            ct, width, height)
                return data
            # Animated GIF/WebP and multi-page TIFF: a plain save() writes only
            # the first frame, destroying the animation/extra pages. Re-encoding
            # all frames while reliably dropping metadata is fragile across
            # formats, so leave multi-frame images untouched (the route's
            # content-type defenses still apply). Single-frame images, the common
            # phone-screenshot case the GPS/EXIF strip targets, proceed.
            if getattr(img, "is_animated", False) or getattr(img, "n_frames", 1) > 1:
                logger.info("EXIF strip skipped (%s): preserving multi-frame image", ct)
                return data
            img.load()
            fmt = img.format
            if fmt is None:
                return data
            # info is where Pillow exposes EXIF/ICC/XMP/text chunks. Clearing
            # it before save keeps any of them out of the output buffer.
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
        # trip: leave the file alone. The catch is broad on purpose. The
        # contract is "never block a legitimate upload over metadata cleanup",
        # and Pillow raises a mix of OSError (UnidentifiedImageError) and
        # bare-Exception subclasses (notably Image.DecompressionBombError) that a
        # narrow tuple would let escape as a 500. Logged at info level because
        # it's an expected outcome for exotic uploads.
        logger.info("EXIF strip skipped (%s): %s", ct, exc)
        return data
