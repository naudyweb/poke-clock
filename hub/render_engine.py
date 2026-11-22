"""
Fast SVG-to-JPEG rendering engine for Poké-Hub (240x240 full + 240x96 top strip).

Primary backend ("in_memory"):
    Uses pycairo + librsvg (Rsvg 2.0) + GdkPixbuf 2.0 directly in memory without
    temporary files or external subprocesses.

Fallback backend ("subprocess"):
    Uses `rsvg-convert` + ImageMagick `magick` when GI/pycairo bindings are
    unavailable in the active Python environment.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

FRAME_WIDTH = 240
FRAME_HEIGHT = 240
ANIM_STRIP_H = 96
MAX_JPEG_BYTES = 23500
JPEG_QUALITIES = (88, 80, 70, 60)
DEFAULT_BG_HEX = "#F8F8F0"

try:
    import cairo
    import gi

    gi.require_version("Rsvg", "2.0")
    gi.require_version("GdkPixbuf", "2.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import GdkPixbuf, GLib, Rsvg

    BACKEND_NAME: str = "in_memory"
except (ImportError, ValueError):
    cairo = None  # type: ignore[assignment]
    GdkPixbuf = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]
    Rsvg = None  # type: ignore[assignment]
    BACKEND_NAME = "subprocess"


def _normalize_bg_hex(bg_hex: str) -> str:
    """Validate and normalize a hex color string to '#RRGGBB'."""
    raw = (bg_hex or DEFAULT_BG_HEX).strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return DEFAULT_BG_HEX
    try:
        int(raw, 16)
    except ValueError:
        return DEFAULT_BG_HEX
    return f"#{raw.upper()}"


def _parse_bg_rgb(bg_hex: str) -> tuple[float, float, float]:
    """Convert a hex color string into normalized (r, g, b) floats in 0.0..1.0."""
    norm = _normalize_bg_hex(bg_hex)
    r = int(norm[1:3], 16) / 255.0
    g = int(norm[3:5], 16) / 255.0
    b = int(norm[5:7], 16) / 255.0
    return r, g, b


def _clamp_strip_h(strip_h: int) -> int:
    return max(1, min(FRAME_HEIGHT, int(strip_h)))


def _svg_to_rgb_bytes_in_memory(svg_text: str, bg_hex: str) -> bytes:
    """Rasterize a 240x240 SVG onto an opaque Cairo surface and return RGB24 bytes."""
    r, g, b = _parse_bg_rgb(bg_hex)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, FRAME_WIDTH, FRAME_HEIGHT)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(r, g, b)
    ctx.paint()

    handle = Rsvg.Handle.new_from_data(svg_text.encode("utf-8"))
    if hasattr(handle, "render_document"):
        viewport = Rsvg.Rectangle()
        viewport.x = 0.0
        viewport.y = 0.0
        viewport.width = float(FRAME_WIDTH)
        viewport.height = float(FRAME_HEIGHT)
        handle.render_document(ctx, viewport)
    else:
        handle.render_cairo(ctx)

    surface.flush()
    raw = memoryview(surface.get_data())
    stride = surface.get_stride()
    expected_stride = FRAME_WIDTH * 4
    if stride != expected_stride:
        packed = bytearray(FRAME_HEIGHT * expected_stride)
        for row in range(FRAME_HEIGHT):
            src_off = row * stride
            dst_off = row * expected_stride
            packed[dst_off : dst_off + expected_stride] = raw[src_off : src_off + expected_stride]
        raw = memoryview(packed)

    rgb = bytearray(FRAME_WIDTH * FRAME_HEIGHT * 3)
    if sys.byteorder == "little":
        # Cairo ARGB32 on little-endian stores bytes in B, G, R, A order.
        rgb[0::3] = raw[2::4]
        rgb[1::3] = raw[1::4]
        rgb[2::3] = raw[0::4]
    else:
        rgb[0::3] = raw[1::4]
        rgb[1::3] = raw[2::4]
        rgb[2::3] = raw[3::4]
    return bytes(rgb)


def _encode_rgb_to_jpeg(rgb_bytes: bytes, width: int, height: int, max_bytes: int) -> bytes:
    """Encode packed RGB24 bytes as baseline JPEG within max_bytes."""
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(rgb_bytes),
        GdkPixbuf.Colorspace.RGB,
        False,
        8,
        width,
        height,
        width * 3,
    )
    last_jpg = b""
    for quality in JPEG_QUALITIES:
        ok, buf = pixbuf.save_to_bufferv("jpeg", ["quality"], [str(quality)])
        if not ok:
            raise RuntimeError(f"GdkPixbuf failed to encode JPEG at quality={quality}")
        last_jpg = bytes(buf)
        if len(last_jpg) <= max_bytes:
            return last_jpg
    return last_jpg


def _encode_png_to_jpeg_magick(
    png_path: Path,
    out_path: Path,
    bg_hex: str,
    max_bytes: int,
    crop_height: int | None = None,
) -> bytes:
    """Encode a PNG (optionally top-cropped) into a JPEG within max_bytes via ImageMagick."""
    last_jpg = b""
    for quality in JPEG_QUALITIES:
        cmd = [
            "magick",
            str(png_path),
            "-background",
            bg_hex,
            "-flatten",
        ]
        if crop_height is not None:
            cmd.extend(["-crop", f"{FRAME_WIDTH}x{crop_height}+0+0", "+repage"])
        cmd.extend([
            "-interlace",
            "none",
            "-sampling-factor",
            "4:2:0",
            "-strip",
            "-quality",
            str(quality),
            str(out_path),
        ])
        subprocess.run(cmd, check=True)
        last_jpg = out_path.read_bytes()
        if len(last_jpg) <= max_bytes:
            return last_jpg
    return last_jpg


def _render_subprocess(
    svg_text: str,
    strip_h: int,
    bg_hex: str,
    max_bytes: int,
    include_strip: bool,
) -> tuple[bytes, bytes]:
    """Fallback SVG-to-JPEG renderer using rsvg-convert and ImageMagick."""
    norm_bg = _normalize_bg_hex(bg_hex)
    with tempfile.TemporaryDirectory(prefix="poke-hub-") as tmpdir:
        tmp = Path(tmpdir)
        svg_path = tmp / "card.svg"
        png_path = tmp / "card.png"
        full_path = tmp / "card.jpg"
        strip_path = tmp / "strip.jpg"
        svg_path.write_text(svg_text, encoding="utf-8")

        subprocess.run(
            ["rsvg-convert", "-w", str(FRAME_WIDTH), "-h", str(FRAME_HEIGHT), str(svg_path), "-o", str(png_path)],
            check=True,
        )

        full_bytes = _encode_png_to_jpeg_magick(png_path, full_path, norm_bg, max_bytes)
        if not include_strip:
            return full_bytes, b""
        strip_bytes = _encode_png_to_jpeg_magick(
            png_path, strip_path, norm_bg, max_bytes, crop_height=strip_h
        )
        return full_bytes, strip_bytes


def render_svg_to_full_and_strip(
    svg_text: str,
    strip_h: int = ANIM_STRIP_H,
    bg_hex: str = DEFAULT_BG_HEX,
    max_bytes: int = MAX_JPEG_BYTES,
) -> tuple[bytes, bytes]:
    """Render a 240x240 SVG into both a full baseline JPEG and a top-strip JPEG."""
    clamped_strip_h = _clamp_strip_h(strip_h)
    if BACKEND_NAME == "in_memory":
        rgb = _svg_to_rgb_bytes_in_memory(svg_text, bg_hex)
        full_jpg = _encode_rgb_to_jpeg(rgb, FRAME_WIDTH, FRAME_HEIGHT, max_bytes)
        strip_rgb = rgb[: FRAME_WIDTH * clamped_strip_h * 3]
        strip_jpg = _encode_rgb_to_jpeg(strip_rgb, FRAME_WIDTH, clamped_strip_h, max_bytes)
        return full_jpg, strip_jpg
    return _render_subprocess(svg_text, clamped_strip_h, bg_hex, max_bytes, include_strip=True)


def render_svg_to_jpg(
    svg_text: str,
    bg_hex: str = DEFAULT_BG_HEX,
    max_bytes: int = MAX_JPEG_BYTES,
) -> bytes:
    """Render a 240x240 SVG into a single full baseline JPEG within max_bytes."""
    if BACKEND_NAME == "in_memory":
        rgb = _svg_to_rgb_bytes_in_memory(svg_text, bg_hex)
        return _encode_rgb_to_jpeg(rgb, FRAME_WIDTH, FRAME_HEIGHT, max_bytes)
    full_jpg, _ = _render_subprocess(svg_text, ANIM_STRIP_H, bg_hex, max_bytes, include_strip=False)
    return full_jpg
