"""Tests for the screenshot PNG encoder, the screen overlay guard,
and the hosts-file domain block (v12.3).

No Windows desktop is needed here: the PNG encoder is pure stdlib,
and the Windows-only paths are tested for their safe failure on
Linux. The real capture + overlay run under Wine in the frozen-exe
smoke test.
"""

import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit import screenshot as shot  # noqa: E402
from deskaudit import screen_blank  # noqa: E402
from deskaudit.telegram import block_domain, unblock_domain  # noqa: E402


def test_encode_png_roundtrip():
    # 2x1 image: pixel 0 = red, pixel 1 = green (BGRA input).
    bgra = bytes((0, 0, 255, 255, 0, 255, 0, 255))
    data = shot.encode_png(2, 1, bgra)
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG signature"
    pos = 8
    chunks = {}
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        chunks[kind] = data[pos + 8:pos + 8 + length]
        pos += 12 + length
    assert set(chunks) == {b"IHDR", b"IDAT", b"IEND"}
    w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (w, h, depth, ctype) == (2, 1, 8, 2)
    raw = zlib.decompress(chunks[b"IDAT"])
    # One scanline: filter byte 0 + RGB RGB.
    assert raw == b"\x00" + bytes((255, 0, 0, 0, 255, 0))
    try:
        shot.encode_png(0, 1, bgra)
    except ValueError:
        pass
    else:
        raise AssertionError("zero width must raise ValueError")
    print("encode_png roundtrip passed")


def test_capture_windows_only():
    if sys.platform == "win32":
        print("capture windows-only skipped (on Windows)")
        return
    try:
        shot.capture_screen("/tmp/shot-test.png")
    except RuntimeError as exc:
        assert "Windows" in str(exc)
    else:
        raise AssertionError("capture must fail on non-Windows")
    print("capture windows-only passed")


def test_screen_blank_windows_only():
    if sys.platform == "win32":
        print("screen blank windows-only skipped (on Windows)")
        return
    ok, msg = screen_blank.start_blank("white")
    assert ok is False and msg == "windows_only"
    ok, msg = screen_blank.stop_blank()
    assert ok is False and msg == "windows_only"
    assert screen_blank.blank_status() is None
    print("screen blank windows-only passed")


def test_hosts_block_unblock():
    with tempfile.TemporaryDirectory() as tmp:
        hosts = Path(tmp) / "hosts"
        hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        out = block_domain("facebook.com", hosts_file=hosts)
        assert "ব্লক করা হয়েছে" in out
        text = hosts.read_text(encoding="utf-8")
        assert "127.0.0.1 facebook.com  # DeskAudit" in text
        assert "127.0.0.1 www.facebook.com  # DeskAudit" in text
        out = block_domain("https://www.facebook.com/feed", hosts_file=hosts)
        assert "আগেই ব্লক" in out
        out = unblock_domain("facebook.com", hosts_file=hosts)
        assert "ব্লক তুলে" in out
        text = hosts.read_text(encoding="utf-8")
        assert "facebook.com" not in text
        assert "localhost" in text  # unrelated lines untouched
        out = unblock_domain("facebook.com", hosts_file=hosts)
        assert "ব্লক করা নেই" in out
        try:
            block_domain("notadomain", hosts_file=hosts)
        except ValueError:
            pass
        else:
            raise AssertionError("bad domain must raise ValueError")
    print("hosts block/unblock passed")


def test_bind_native_noop_on_linux():
    """v12.14.3: declaring the native GDI signatures must be a
    harmless no-op off Windows (there is no windll there)."""
    import sys

    from deskaudit import screenshot as shot

    if sys.platform != "win32":
        shot._bind_native()
        assert shot._NATIVE_BOUND is False
    print("bind native noop on linux passed")


if __name__ == "__main__":
    test_encode_png_roundtrip()
    test_capture_windows_only()
    test_screen_blank_windows_only()
    test_hosts_block_unblock()
    test_bind_native_noop_on_linux()
    print("ALL SCREENSHOT TESTS PASSED")
