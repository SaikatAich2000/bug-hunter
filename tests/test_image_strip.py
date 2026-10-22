"""Lossless metadata stripping for uploads (app/image_strip.py).

JPEG, PNG and WebP are cleaned at the byte level: metadata goes, pixels stay identical, and a
huge image is never decoded. GIF/BMP/TIFF go through Pillow within a memory budget; one over it
is refused with a clear 413 instead of being stored with its metadata.
"""
from __future__ import annotations

import io
import struct

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from app.image_strip import ImageTooLarge, strip_image_metadata

SECRET = b"GPS-51.5N-0.12W"


def _jpeg(orientation: int | None = None, progressive: bool = False) -> bytes:
    img = Image.new("RGB", (64, 48), (200, 100, 50))
    exif = img.getexif()
    exif[270] = SECRET.decode()
    if orientation:
        exif[0x0112] = orientation
    out = io.BytesIO()
    img.save(out, format="JPEG", exif=exif.tobytes(), comment=SECRET, icc_profile=b"ICC" * 20,
             progressive=progressive)
    return out.getvalue()


def _pixels(data: bytes) -> bytes:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.tobytes()


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_metadata_goes_and_pixels_stay_identical(progressive):
    raw = _jpeg(progressive=progressive)
    clean = strip_image_metadata(raw, "image/jpeg")
    assert SECRET not in clean
    assert _pixels(clean) == _pixels(raw)
    assert Image.open(io.BytesIO(clean)).info.get("icc_profile") == b"ICC" * 20
    assert strip_image_metadata(clean, "image/jpeg") == clean  # idempotent


@pytest.mark.parametrize("orientation", [3, 6, 8])
def test_jpeg_orientation_survives_so_photos_stay_upright(orientation):
    clean = strip_image_metadata(_jpeg(orientation), "image/jpeg")
    assert SECRET not in clean
    exif = Image.open(io.BytesIO(clean)).getexif()
    assert dict(exif) == {0x0112: orientation}


def test_png_and_apng_text_and_exif_chunks_go():
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    meta.add_itxt("Where", SECRET.decode())
    frames = [Image.new("RGBA", (12, 12), (i * 60, 0, 0, 200)) for i in range(3)]
    out = io.BytesIO()
    frames[0].save(out, format="PNG", save_all=True, append_images=frames[1:], pnginfo=meta)
    raw = out.getvalue()
    clean = strip_image_metadata(raw, "image/png")
    assert SECRET not in clean
    assert Image.open(io.BytesIO(clean)).n_frames == 3
    assert _pixels(clean) == _pixels(raw)


def test_webp_exif_and_xmp_go():
    img = Image.new("RGB", (20, 20), (9, 9, 9))
    exif = img.getexif()
    exif[270] = SECRET.decode()
    out = io.BytesIO()
    img.save(out, format="WEBP", exif=exif.tobytes(), xmp=b"<x>" + SECRET + b"</x>", lossless=True)
    clean = strip_image_metadata(out.getvalue(), "image/webp")
    assert SECRET not in clean
    reopened = Image.open(io.BytesIO(clean))
    assert "exif" not in reopened.info
    assert "xmp" not in reopened.info
    assert _pixels(clean) == _pixels(out.getvalue())


def test_a_huge_png_is_cleaned_without_decoding(monkeypatch):
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    out = io.BytesIO()
    Image.new("L", (7000, 7000)).save(out, format="PNG", pnginfo=meta)
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("PNG was decoded"))
    clean = strip_image_metadata(out.getvalue(), "image/png")
    assert SECRET not in clean


def test_declared_type_does_not_decide_the_format():
    meta = PngInfo()
    meta.add_text("Source", SECRET.decode())
    out = io.BytesIO()
    Image.new("RGB", (4, 4)).save(out, format="PNG", pnginfo=meta)
    assert SECRET not in strip_image_metadata(out.getvalue(), "image/jpeg")


def test_truncated_jpeg_is_returned_unchanged():
    raw = _jpeg()[:40]
    assert strip_image_metadata(raw, "image/jpeg") == raw


def test_an_oversized_bmp_is_refused():
    out = io.BytesIO()
    Image.new("RGB", (5000, 5000)).save(out, format="BMP")
    data = out.getvalue()
    with pytest.raises(ImageTooLarge):
        strip_image_metadata(data, "image/bmp")


def test_upload_of_an_oversized_bmp_is_a_clear_413(admin_client, monkeypatch):
    from app import image_strip

    monkeypatch.setattr(image_strip, "_MAX_DECODE_BYTES", 100 * 100 * 4)
    pid = admin_client.get("/api/projects").json()[0]["id"]
    bug = admin_client.post("/api/bugs", json={"project_id": pid, "title": "Huge screenshot"}).json()
    out = io.BytesIO()
    Image.new("RGB", (200, 200)).save(out, format="BMP")
    res = admin_client.post(f"/api/bugs/{bug['id']}/attachments",
                            files={"file": ("big.bmp", out.getvalue(), "image/bmp")})
    assert res.status_code == 413, res.text
    assert "16 megapixels" in res.json()["detail"]
    assert admin_client.get(f"/api/bugs/{bug['id']}").json()["attachments"] == []


# Pillow-backed formats and malformed input

def _encoded(img, fmt, **save):
    out = io.BytesIO()
    img.save(out, format=fmt, **save)
    return out.getvalue()


def test_gif_keeps_its_pixels_and_transparency_but_loses_its_comment():
    img = Image.new("P", (16, 16), 1)
    img.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0] + [0] * 759)
    raw = _encoded(img, "GIF", transparency=1, comment=SECRET)
    assert SECRET in raw

    cleaned = strip_image_metadata(raw, "image/gif")
    assert SECRET not in cleaned
    reread = Image.open(io.BytesIO(cleaned))
    assert reread.info.get("transparency") is not None
    assert _pixels(cleaned) == _pixels(raw)


def test_bmp_and_tiff_are_re_encoded_without_their_tags():
    bmp = _encoded(Image.new("RGB", (8, 8), (1, 2, 3)), "BMP")
    assert _pixels(strip_image_metadata(bmp, "image/bmp")) == _pixels(bmp)

    tagged = Image.new("RGB", (8, 8), (9, 8, 7))
    tiff = _encoded(tagged, "TIFF", tiffinfo={315: SECRET.decode()})
    assert SECRET in tiff
    cleaned = strip_image_metadata(tiff, "image/tiff")
    assert SECRET not in cleaned
    assert _pixels(cleaned) == _pixels(tiff)


def test_an_animated_gif_is_left_alone_rather_than_losing_frames():
    frames = []
    for n in (0, 1, 2):
        frame = Image.new("P", (8, 8), 0)
        frame.putpixel((n, n), 1)
        frames.append(frame)
    raw = _encoded(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0, optimize=False)
    assert Image.open(io.BytesIO(raw)).n_frames == 3
    assert strip_image_metadata(raw, "image/gif") == raw


def test_unreadable_pillow_formats_and_a_missing_pillow_fail_open(monkeypatch):
    import sys

    broken = b"GIF89a" + b"\x00" * 4
    assert strip_image_metadata(broken, "image/gif") == broken

    raw = _encoded(Image.new("RGB", (4, 4)), "BMP")
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert strip_image_metadata(raw, "image/bmp") == raw


def test_a_busy_decoder_pool_refuses_the_image(monkeypatch):
    from app import image_strip

    class _Full:
        def acquire(self, timeout=None):
            return False

    monkeypatch.setattr(image_strip, "_DECODES", _Full())
    raw = _encoded(Image.new("RGB", (4, 4)), "BMP")
    with pytest.raises(ImageTooLarge, match="busy"):
        strip_image_metadata(raw, "image/bmp")


def test_malformed_jpegs_are_returned_unchanged():
    for broken in (
        b"\xff\xd8\x00\x01",                      # a segment that does not start with 0xFF
        b"\xff\xd8\xff\xff",                      # only fill bytes, then the data ends
        b"\xff\xd8\xff",                          # marker without a length
        b"\xff\xd8\xff\xe1\xff\xff",              # a segment longer than the file
        b"\xff\xd8\xff\xd0\xff\xd9",              # restart and end markers but no scan
    ):
        assert strip_image_metadata(broken, "image/jpeg") == broken


def test_exif_orientation_is_read_only_from_well_formed_payloads():
    from app.image_strip import _jpeg_orientation

    assert _jpeg_orientation(b"not exif at all") == 1
    assert _jpeg_orientation(b"Exif\x00\x00short") == 1
    assert _jpeg_orientation(b"Exif\x00\x00" + b"XX*\x00\x08\x00\x00\x00" + b"\x00" * 8) == 1
    assert _jpeg_orientation(b"Exif\x00\x00" + b"II*\x00" + b"\xff\xff\xff\x7f" + b"\x00" * 8) == 1
    wild = b"Exif\x00\x00" + b"MM\x00*\x00\x00\x00\x08" + b"\x00\x01" + struct.pack(">HHIHH", 0x0112, 3, 1, 9, 0)
    assert _jpeg_orientation(wild) == 1
    other_tag = b"Exif\x00\x00" + b"MM\x00*\x00\x00\x00\x08" + b"\x00\x01" + struct.pack(">HHIHH", 0x010F, 3, 1, 6, 0)
    assert _jpeg_orientation(other_tag) == 1


def test_orientation_is_inserted_first_when_there_is_no_jfif_header():
    from app.image_strip import _insert_orientation

    out = bytearray(b"\xff\xd8\xff\xdb\x00\x02")
    _insert_orientation(out, 6)
    assert out[2:4] == b"\xff\xe1"
    assert out.endswith(b"\xff\xdb\x00\x02")


def test_malformed_png_and_webp_are_returned_unchanged():
    png_sig = b"\x89PNG\r\n\x1a\n"
    overrun = png_sig + struct.pack(">I", 500) + b"IDAT" + b"\x00" * 8
    no_end = png_sig + struct.pack(">I", 0) + b"IDAT" + b"\x00" * 4
    assert strip_image_metadata(overrun, "image/png") == overrun
    assert strip_image_metadata(no_end, "image/png") == no_end

    header = b"RIFF" + struct.pack("<I", 0) + b"WEBP"
    overrun = header + b"VP8 " + struct.pack("<I", 999) + b"\x00" * 4
    trailing = header + b"VP8 " + struct.pack("<I", 0) + b"\x01" * 5
    assert strip_image_metadata(overrun, "image/webp") == overrun
    assert strip_image_metadata(trailing, "image/webp") == trailing
