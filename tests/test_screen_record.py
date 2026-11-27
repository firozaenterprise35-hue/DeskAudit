"""Screen recording tests (v12.14) — validation, MJPEG AVI writer,
recorder start/stop with fake capture/encode, and the ffmpeg MP4 path
(skipped when ffmpeg is not installed). No real screen is captured."""

import shutil
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from deskaudit.screen_record import (  # noqa: E402
    ScreenRecorder,
    encode_mp4,
    frame_interval_seconds,
    validate_minutes,
    write_mjpeg_avi,
)
from deskaudit.screenshot import encode_png  # noqa: E402


def _fake_jpeg(width=96, height=54, size=200):
    """Minimal JPEG bytes with a readable SOF0 (dimensions only)."""
    body = bytearray(b"\xff\xd8")  # SOI
    app0 = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    body += b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    sof = (struct.pack(">H", 17) + bytes([8])
           + struct.pack(">HH", height, width)
           + bytes([3, 1, 0x11, 0, 2, 0x11, 1, 3, 0x11, 1]))
    body += b"\xff\xc0" + sof
    body += b"\x00" * max(0, size - len(body))
    return bytes(body)


def test_validate_minutes():
    assert validate_minutes(5) == 5
    assert validate_minutes("10") == 10
    assert validate_minutes("৫") == 5  # Bengali digit
    assert validate_minutes(" ৩০ ") == 30
    for bad in (0, 61, "abc", "", None):
        with pytest.raises(ValueError):
            validate_minutes(bad)
    print("validate_minutes passed")


def test_frame_interval_seconds():
    # v12.14.5: short recordings run at 5 fps (0.2 s); the
    # 700-frame cap takes over beyond 2 minutes.
    assert frame_interval_seconds(1) == 0.2
    assert frame_interval_seconds(2) == 0.2
    assert frame_interval_seconds(3) == pytest.approx(180 / 700)
    assert frame_interval_seconds(30) == pytest.approx(1800 / 700)
    assert frame_interval_seconds(60) == pytest.approx(3600 / 700)
    print("frame_interval_seconds passed")


def test_write_mjpeg_avi_structure():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        frames = []
        for i in range(3):
            p = root / f"frame{i:04d}.jpg"
            p.write_bytes(_fake_jpeg(size=200 + i))
            frames.append(p)
        out = write_mjpeg_avi(frames, root / "out.avi", fps=0.5)
        data = out.read_bytes()
        assert data[0:4] == b"RIFF"
        assert struct.unpack("<I", data[4:8])[0] == len(data) - 8
        assert data[8:12] == b"AVI "
        assert b"movi" in data and b"idx1" in data
        # idx1: 3 entries, offsets point at '00db' fourccs relative
        # to the 'movi' fourcc.
        movi_pos = data.index(b"movi")
        idx_pos = data.index(b"idx1")
        idx_size = struct.unpack("<I", data[idx_pos + 4:idx_pos + 8])[0]
        assert idx_size == 3 * 16
        for i in range(3):
            off = idx_pos + 8 + i * 16
            chunk_id, flags, chunk_off, size = struct.unpack("<4sIII", data[off:off + 16])
            assert chunk_id == b"00db" and flags == 0x10
            assert data[movi_pos + chunk_off:movi_pos + chunk_off + 4] == b"00db"
            assert size == 200 + i
        # The header claims 3 frames, 96x54.
        assert struct.unpack("<I", data[data.index(b"avih") + 8:data.index(b"avih") + 12])[0] > 0
    print("write_mjpeg_avi structure passed")


def test_write_mjpeg_avi_needs_two_frames():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        one = root / "frame0000.jpg"
        one.write_bytes(_fake_jpeg())
        with pytest.raises(ValueError):
            write_mjpeg_avi([one], root / "out.avi", fps=0.5)
    print("write_mjpeg_avi needs two frames passed")


def test_encode_mp4_with_ffmpeg():
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # PNG frames (named .png so ffmpeg picks the PNG decoder —
        # the recorder's real frames are JPEGs named .jpg).
        for i in range(3):
            bgra = bytes([i * 40, 100, 150, 255]) * (16 * 12)
            (root / f"frame{i:04d}.png").write_bytes(encode_png(16, 12, bgra))
        out = encode_mp4([root / f"frame{i:04d}.png" for i in range(3)],
                         root / "out.mp4", fps=0.5)
        assert out is not None and out.exists() and out.stat().st_size > 0
        assert b"ftyp" in out.read_bytes()[:12]
    print("encode_mp4 with ffmpeg passed")


class _FakeCapture:
    def __init__(self):
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        Path(path).write_bytes(_fake_jpeg())
        return Path(path)


def test_recorder_start_stop_sends_video():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = threading.Event()
        got = {}

        def fake_encode(frames, out_dir, stem, fps):
            video = Path(out_dir) / f"{stem}.mp4"
            video.write_bytes(b"fake-video")
            return video, "mp4"

        def on_done(video, kind, minutes, frames):
            got.update(video=video, kind=kind, minutes=minutes, frames=frames)
            done.set()

        rec = ScreenRecorder(root, on_done=on_done,
                             capture_fn=_FakeCapture(),
                             encode_fn=fake_encode)
        assert rec.start(0) == (False, "রেকর্ড ১ থেকে ৬০ মিনিট পর্যন্ত করা যায়।")
        ok, msg = rec.start(1)
        assert ok and "রেকর্ড শুরু হলো" in msg
        ok, msg = rec.start(3)
        assert not ok and "ইতিমধ্যেই চলছে" in msg
        assert rec.status()["running"] is True
        time.sleep(3.0)  # v12.14.5: frames every 0.2 s, so ~15 by now
        ok, msg = rec.stop()
        assert ok and "থামানো হলো" in msg
        assert done.wait(6), "recording did not stop in time"
        assert got["kind"] == "mp4" and got["minutes"] == 1
        assert got["frames"] >= 2 and got["video"] is not None
        assert rec.status()["running"] is False
    print("recorder start/stop sends video passed")


def test_recorder_too_short_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = threading.Event()
        got = {}

        def on_done(video, kind, minutes, frames):
            got.update(video=video, kind=kind, minutes=minutes, frames=frames)
            done.set()

        rec = ScreenRecorder(root, on_done=on_done,
                             capture_fn=_FakeCapture(),
                             encode_fn=lambda f, d, s, fps: (Path(d) / "x.mp4", "mp4"))
        ok, _msg = rec.start(1)
        assert ok
        # Wait until frame 0 exists, then stop at once — only one
        # frame, so no video can be made. Poll fast: at 5 fps the
        # next frame is due 0.2 s after the first.
        deadline = time.time() + 3
        while rec.status()["frames"] < 1 and time.time() < deadline:
            time.sleep(0.01)
        ok, msg = rec.stop()
        assert ok and "থামানো হলো" in msg
        assert done.wait(6)
        assert got["video"] is None and got["frames"] == 1
    print("recorder too short sends nothing passed")


def test_recorder_fail_fast_reports_error():
    # v12.14.1: when every frame fails (locked screen is the usual
    # cause) the recorder must stop within seconds and say why —
    # not wait out the whole duration and then say only "too short".
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = threading.Event()
        got = {}

        def always_fail(path):
            raise RuntimeError("screen copy failed")

        def on_done(video, kind, minutes, frames):
            got.update(video=video, kind=kind, minutes=minutes, frames=frames)
            done.set()

        rec = ScreenRecorder(root, on_done=on_done,
                             capture_fn=always_fail,
                             encode_fn=lambda f, d, s, fps: (Path(d) / "x.mp4", "mp4"))
        started = time.time()
        ok, _msg = rec.start(1)
        assert ok
        assert done.wait(20), "failing recorder did not finish fast"
        assert time.time() - started < 20, "recorder waited out the full minute"
        assert got["video"] is None and got["frames"] == 0
        assert rec.last_error == "screen copy failed"
        assert rec.status()["running"] is False
        assert rec.status()["last_error"] == "screen copy failed"
    print("recorder fail fast reports error passed")


def test_recorder_transient_failure_recovers():
    # Two failed frames, then capturing works: the recording still
    # completes, and the stale error is forgotten.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = threading.Event()
        got = {}
        calls = {"n": 0}

        def flaky(path):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("screen copy failed")
            Path(path).write_bytes(_fake_jpeg())
            return Path(path)

        def fake_encode(frames, out_dir, stem, fps):
            video = Path(out_dir) / f"{stem}.mp4"
            video.write_bytes(b"fake-video")
            return video, "mp4"

        def on_done(video, kind, minutes, frames):
            got.update(video=video, kind=kind, minutes=minutes, frames=frames)
            done.set()

        rec = ScreenRecorder(root, on_done=on_done,
                             capture_fn=flaky,
                             encode_fn=fake_encode)
        ok, _msg = rec.start(1)
        assert ok
        time.sleep(7.0)  # v12.14.5: failures at ~0 s and ~0.2 s, good frames from ~0.4 s
        ok, _msg = rec.stop()
        assert ok
        assert done.wait(6)
        assert got["video"] is not None and got["frames"] >= 2
        assert rec.last_error is None
    print("recorder transient failure recovers passed")


def test_recorder_encode_failure_still_reports():
    # v12.14.1: an encoder that claims success but leaves no file
    # (or raises) must not kill the worker thread silently — the
    # owner still gets a message instead of waiting forever.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        done = threading.Event()
        got = {}

        def on_done(video, kind, minutes, frames):
            got.update(video=video, kind=kind, minutes=minutes, frames=frames)
            done.set()

        rec = ScreenRecorder(root, on_done=on_done,
                             capture_fn=_FakeCapture(),
                             encode_fn=lambda f, d, s, fps: (Path(d) / "ghost.mp4", "mp4"))
        ok, _msg = rec.start(1)
        assert ok
        time.sleep(5.0)  # v12.14.5: frames every 0.2 s, so ~25 by now
        ok, _msg = rec.stop()
        assert ok
        assert done.wait(6), "encode failure left the recording hanging"
        assert got["video"] is None and got["frames"] >= 2
        assert rec.status()["running"] is False
    print("recorder encode failure still reports passed")


def test_screen_locked_reason_none_on_linux():
    # v12.14.2: the lock check is Windows-only; off Windows it must
    # say nothing is wrong (and never raise).
    import sys

    from deskaudit.screen_record import screen_locked_reason

    if sys.platform != "win32":
        assert screen_locked_reason() is None
    print("screen locked reason none on linux passed")


def test_native_struct_sizes():
    """v12.14.3: the GDI+ structs must match the native layout —
    EncoderParameter 32 bytes, EncoderParameters 40, StartupInput
    24, ImageCodecInfo 104. A wrong size means GDI+ reads garbage
    (the same class of bug as the token crash)."""
    import ctypes

    from deskaudit.screen_record import (
        EncoderParameter,
        EncoderParameters,
        GdiplusStartupInput,
        ImageCodecInfo,
    )

    assert ctypes.sizeof(EncoderParameter) == 32
    assert ctypes.sizeof(EncoderParameters) == 40
    assert ctypes.sizeof(GdiplusStartupInput) == 24
    assert ctypes.sizeof(ImageCodecInfo) == 104
    print("native struct sizes passed")


def test_token_is_pointer_sized():
    """v12.14.3 regression test for the access violation:
    the GDI+ token is a ULONG_PTR, so its type must be
    pointer-sized (c_void_p) — never c_ulong. The old 4-byte
    c_ulong let GdiplusStartup write 8 bytes into it on 64-bit
    Windows, corrupting the heap and crashing the next GDI+ call
    with 'access violation reading 0x...'. Wine returns a fake
    32-bit token, so the bug never showed up in tests here."""
    import ctypes

    from deskaudit.screen_record import _TOKEN_TYPE

    assert _TOKEN_TYPE is ctypes.c_void_p
    assert ctypes.sizeof(_TOKEN_TYPE) == ctypes.sizeof(ctypes.c_void_p)
    print("token is pointer sized passed")


def test_bind_native_noop_on_linux():
    """v12.14.3: declaring the native signatures must be a harmless
    no-op off Windows (there is no windll there)."""
    import sys

    from deskaudit import screen_record as sr

    if sys.platform != "win32":
        sr._bind_native()
        assert sr._NATIVE_BOUND is False
    print("bind native noop on linux passed")


def test_call_labels_native_crash():
    """v12.14.4: when a native call crashes, _call names the call
    instead of leaving a bare address in the message."""
    from deskaudit.screen_record import _call

    def boom():
        raise OSError(
            "exception: access violation reading 0x000002046D4A1F28")

    try:
        _call("GdipSaveImageToFile", boom)
    except RuntimeError as exc:
        assert "GdipSaveImageToFile" in str(exc)
        assert "access violation" in str(exc)
    else:
        raise AssertionError("_call did not raise")
    assert _call("ok", lambda: 7) == 7
    print("call labels native crash passed")


def test_capture_disposes_before_shutdown():
    """v12.14.4: GDI+ objects MUST be disposed before GdiplusShutdown.
    The old code shut GDI+ down first and disposed the bitmaps after —
    disposing after shutdown dereferences freed GDI+ state and crashes
    with an access violation on real Windows (Wine tolerates it, which
    is why no test here saw it, and why the v12.14.3 token fix alone
    did not stop the crash). Fake the native DLLs and assert the
    call order."""
    import ctypes
    import sys
    from types import SimpleNamespace

    from deskaudit import screen_record as sr

    calls = []

    def _set_out(arg, value):
        # arg is a byref() to a c_void_p: fill the out-param.
        try:
            arg._obj.value = value
        except Exception:
            ctypes.cast(arg,
                        ctypes.POINTER(ctypes.c_void_p)).contents.value = value

    def _rec(name, result=0):
        def fn(*args):
            calls.append(name)
            return result
        return fn

    def _metrics(i):
        calls.append("GetSystemMetrics")
        return {76: 0, 77: 0, 78: 1280, 79: 800}[i]

    def _from_hbitmap(hbm, pal, bmp_p):
        calls.append("GdipCreateBitmapFromHBITMAP")
        _set_out(bmp_p, 21)
        return 0

    def _from_scan0(w, h, stride, fmt, scan0, bmp_p):
        calls.append("GdipCreateBitmapFromScan0")
        _set_out(bmp_p, 22)
        return 0

    def _get_graphics(bmp, g_p):
        calls.append("GdipGetImageGraphicsContext")
        _set_out(g_p, 23)
        return 0

    fake = SimpleNamespace(
        user32=SimpleNamespace(
            GetSystemMetrics=_metrics,
            GetDC=_rec("GetDC", 11),
            ReleaseDC=_rec("ReleaseDC", 1)),
        gdi32=SimpleNamespace(
            CreateCompatibleDC=_rec("CreateCompatibleDC", 12),
            CreateCompatibleBitmap=_rec("CreateCompatibleBitmap", 13),
            SelectObject=_rec("SelectObject", 14),
            BitBlt=_rec("BitBlt", 1),
            DeleteObject=_rec("DeleteObject", 1),
            DeleteDC=_rec("DeleteDC", 1)),
        gdiplus=SimpleNamespace(
            GdiplusStartup=_rec("GdiplusStartup", 0),
            GdiplusShutdown=_rec("GdiplusShutdown", 0),
            GdipCreateBitmapFromHBITMAP=_from_hbitmap,
            GdipCreateBitmapFromScan0=_from_scan0,
            GdipGetImageGraphicsContext=_get_graphics,
            GdipDrawImageRectI=_rec("GdipDrawImageRectI", 0),
            GdipDeleteGraphics=_rec("GdipDeleteGraphics", 0),
            GdipSaveImageToFile=_rec("GdipSaveImageToFile", 0),
            GdipGetImageEncodersSize=_rec("GdipGetImageEncodersSize", 0),
            GdipGetImageEncoders=_rec("GdipGetImageEncoders", 0),
            GdipDisposeImage=_rec("GdipDisposeImage", 0)),
    )

    old_platform = sys.platform
    had_windll = hasattr(ctypes, "windll")
    old_windll = getattr(ctypes, "windll", None)
    old_clsid = sr._jpeg_encoder_clsid
    old_bound = sr._NATIVE_BOUND
    sr._jpeg_encoder_clsid = lambda gdiplus: b"\x01" * 16
    try:
        sys.platform = "win32"
        ctypes.windll = fake
        sr._NATIVE_BOUND = False
        with tempfile.TemporaryDirectory() as tmp:
            out = sr.capture_jpeg_frame(
                str(Path(tmp) / "frame.jpg"))
            assert out.name == "frame.jpg"
        assert "GdipSaveImageToFile" in calls
        assert "GdiplusShutdown" in calls
        disposes = [i for i, c in enumerate(calls)
                    if c == "GdipDisposeImage"]
        assert len(disposes) == 2, calls  # scaled + bitmap
        shut = calls.index("GdiplusShutdown")
        assert all(i < shut for i in disposes), calls
    finally:
        sys.platform = old_platform
        if had_windll:
            ctypes.windll = old_windll
        else:
            del ctypes.windll
        sr._jpeg_encoder_clsid = old_clsid
        sr._NATIVE_BOUND = old_bound
    print("capture disposes before shutdown passed")


def test_cursor_struct_sizes():
    """v12.14.5: the cursor structs must match the native layout —
    CURSORINFO 24 bytes, ICONINFO 32, POINT 8. A wrong size means
    GetCursorInfo writes past the buffer (the same class of bug
    as the token crash)."""
    import ctypes

    from deskaudit.screen_record import _CursorInfo, _IconInfo, _Point

    assert ctypes.sizeof(_Point) == 8
    assert ctypes.sizeof(_CursorInfo) == 24
    assert ctypes.sizeof(_IconInfo) == 32
    print("cursor struct sizes passed")


def _fake_cursor_dlls(showing=True, hotspot=(3, 5), pos=(100, 50),
                      fail=None, hcursor=99, load_cursor=55):
    """Fake user32/gdi32 for _draw_cursor: GetCursorInfo fills a
    visible pointer at `pos` with handle `hcursor`, GetIconInfo
    fills `hotspot` and two bitmaps, LoadCursorW returns
    `load_cursor`. `fail` names a call that raises instead."""
    import ctypes
    from types import SimpleNamespace

    from deskaudit import screen_record as sr

    calls = []

    def _struct(arg, cls):
        try:
            return arg._obj
        except Exception:
            return ctypes.cast(arg, ctypes.POINTER(cls)).contents

    def _cursor_info(arg):
        calls.append("GetCursorInfo")
        if fail == "GetCursorInfo":
            raise OSError("boom")
        info = _struct(arg, sr._CursorInfo)
        info.cbSize = ctypes.sizeof(sr._CursorInfo)
        info.flags = 1 if showing else 0
        info.hCursor = hcursor
        info.ptScreenPos.x, info.ptScreenPos.y = pos
        return 1

    def _icon_info(hcur, arg):
        calls.append("GetIconInfo")
        icon = _struct(arg, sr._IconInfo)
        icon.fIcon = 0
        icon.xHotspot, icon.yHotspot = hotspot
        icon.hbmMask, icon.hbmColor = 7, 8
        return 1

    def _load_cursor(hinst, name):
        calls.append(("LoadCursorW", hinst, name))
        return load_cursor

    def _draw(hdc, x, y, hcur, cw, ch, step, brush, flags):
        calls.append(("DrawIconEx", hdc, x, y, hcur, cw, ch, step,
                      brush, flags))
        return 1

    def _delete(obj):
        calls.append(("DeleteObject", obj))
        return 1

    user32 = SimpleNamespace(GetCursorInfo=_cursor_info,
                             GetIconInfo=_icon_info,
                             LoadCursorW=_load_cursor, DrawIconEx=_draw)
    gdi32 = SimpleNamespace(DeleteObject=_delete)
    return user32, gdi32, calls


def test_draw_cursor_visible():
    """v12.14.5: a visible pointer is drawn at its position minus
    the hotspot (so the arrow tip sits where the pointer is), and
    the two bitmaps GetIconInfo hands back are freed."""
    from deskaudit import screen_record as sr

    user32, gdi32, calls = _fake_cursor_dlls(
        showing=True, hotspot=(3, 5), pos=(100, 50))
    sr._draw_cursor(user32, gdi32, hdc_mem=12, x0=10, y0=20)
    draws = [c for c in calls if isinstance(c, tuple)
             and c[0] == "DrawIconEx"]
    assert len(draws) == 1, calls
    # hdc 12, x = 100 - 10 - 3, y = 50 - 20 - 5, cursor 99,
    # default size, DI_NORMAL (3).
    assert draws[0] == ("DrawIconEx", 12, 87, 25, 99, 0, 0, 0, None, 3), calls
    assert ("DeleteObject", 7) in calls, calls
    assert ("DeleteObject", 8) in calls, calls
    print("draw cursor visible passed")


def test_draw_cursor_no_handle_falls_back_to_arrow():
    """v12.14.5: a showing cursor with no handle (Wine reports
    exactly this under Xvfb: flags=1, hCursor=0) falls back to the
    shared system arrow, hotspot (0, 0) — the position still shows
    instead of the frame shipping with no pointer at all."""
    from deskaudit import screen_record as sr

    user32, gdi32, calls = _fake_cursor_dlls(
        showing=True, hcursor=0, load_cursor=55, pos=(100, 50))
    sr._draw_cursor(user32, gdi32, hdc_mem=12, x0=10, y0=20)
    assert ("LoadCursorW", None, 32512) in calls, calls
    draws = [c for c in calls if isinstance(c, tuple)
             and c[0] == "DrawIconEx"]
    assert len(draws) == 1, calls
    # Arrow hotspot is (0, 0): x = 100 - 10, y = 50 - 20.
    assert draws[0] == ("DrawIconEx", 12, 90, 30, 55, 0, 0, 0, None, 3), calls
    assert not [c for c in calls if isinstance(c, tuple)
                and c[0] == "DeleteObject"], calls
    print("draw cursor no handle falls back to arrow passed")


def test_draw_cursor_hidden():
    """v12.14.5: a hidden pointer (or no cursor at all) draws
    nothing and frees nothing."""
    from deskaudit import screen_record as sr

    user32, gdi32, calls = _fake_cursor_dlls(showing=False)
    sr._draw_cursor(user32, gdi32, hdc_mem=12, x0=0, y0=0)
    assert not [c for c in calls if isinstance(c, tuple)], calls
    print("draw cursor hidden passed")


def test_draw_cursor_failure_never_raises():
    """v12.14.5: a crashing GetCursorInfo — or a DLL without the
    cursor functions at all — must never lose a frame: _draw_cursor
    returns silently."""
    from deskaudit import screen_record as sr
    from types import SimpleNamespace

    user32, gdi32, calls = _fake_cursor_dlls(fail="GetCursorInfo")
    sr._draw_cursor(user32, gdi32, hdc_mem=12, x0=0, y0=0)
    assert calls == ["GetCursorInfo"], calls
    sr._draw_cursor(SimpleNamespace(), SimpleNamespace(),
                    hdc_mem=12, x0=0, y0=0)
    print("draw cursor failure never raises passed")


if __name__ == "__main__":
    test_validate_minutes()
    test_frame_interval_seconds()
    test_write_mjpeg_avi_structure()
    test_write_mjpeg_avi_needs_two_frames()
    test_encode_mp4_with_ffmpeg()
    test_recorder_start_stop_sends_video()
    test_recorder_too_short_sends_nothing()
    test_recorder_fail_fast_reports_error()
    test_recorder_transient_failure_recovers()
    test_recorder_encode_failure_still_reports()
    test_screen_locked_reason_none_on_linux()
    test_native_struct_sizes()
    test_token_is_pointer_sized()
    test_bind_native_noop_on_linux()
    test_call_labels_native_crash()
    test_capture_disposes_before_shutdown()
    test_cursor_struct_sizes()
    test_draw_cursor_visible()
    test_draw_cursor_no_handle_falls_back_to_arrow()
    test_draw_cursor_hidden()
    test_draw_cursor_failure_never_raises()
    print("ALL SCREEN RECORD TESTS PASSED")
