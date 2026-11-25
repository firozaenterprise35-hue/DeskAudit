"""Screen capture for Telegram /screenshot (Windows only).

Captures the whole virtual screen (all monitors) with ctypes +
gdi32 BitBlt and encodes it as PNG with stdlib zlib — no Pillow or
any other dependency, so the frozen exe can do it too.

A screenshot shows whatever is on the screen, including typed text
and message contents. That is beyond DeskAudit's usual metadata-only
rule, so it happens ONLY when the owner commands it from Telegram,
and the image goes only to the owner's chat. Nothing is captured
automatically and nothing is stored after sending (the file is
deleted once Telegram accepts it).
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    import zlib as _zlib

    crc = _zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def encode_png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode a top-down BGRA buffer (4 bytes per pixel) as a PNG
    (8-bit RGB, no interlace). Pure stdlib."""
    if width <= 0 or height <= 0:
        raise ValueError("bad image size")
    if len(bgra) < width * height * 4:
        raise ValueError("pixel buffer too small")
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        row = y * width * 4
        for x in range(width):
            off = row + x * 4
            raw += bytes((bgra[off + 2], bgra[off + 1], bgra[off]))  # BGR -> RGB
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (_PNG_SIG + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _chunk(b"IEND", b""))


def _capture_bgra() -> tuple[int, int, bytes]:
    """Capture the virtual screen (all monitors) as top-down BGRA.
    Windows only; raises RuntimeError otherwise or on failure."""
    if sys.platform != "win32":
        raise RuntimeError("screenshot works on Windows only")
    import ctypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    SRCCOPY = 0x00CC0020
    x0 = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y0 = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not read screen size")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_int32),
            ("biHeight", ctypes.c_int32),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    hdc_screen = user32.GetDC(0)
    if not hdc_screen:
        raise RuntimeError("could not open the screen device")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    try:
        if not hdc_mem or not hbm:
            raise RuntimeError("could not allocate the capture bitmap")
        gdi32.SelectObject(hdc_mem, hbm)
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x0, y0, SRCCOPY):
            raise RuntimeError("screen copy failed")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = w
        header.biHeight = -h  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        lines = gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(header), 0)
        if lines != h:
            raise RuntimeError("could not read the captured pixels")
        return w, h, bytes(buf.raw)
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def capture_screen(path: str | Path) -> Path:
    """Capture the screen and save it as a PNG at `path`.
    Returns the path. Raises RuntimeError on failure."""
    w, h, pixels = _capture_bgra()
    data = encode_png(w, h, pixels)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out
