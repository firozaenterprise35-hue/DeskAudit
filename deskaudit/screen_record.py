"""Anonymous on-demand screen recording (Windows only, v12.14).

The owner picks a duration from Telegram (1 / 3 / 5 / 10 / 15 / 20 /
30 minutes, or any 1–60 minutes typed as /record <minutes>). The agent
then records the screen by itself — silently, with no window, sound or
indicator on the PC — and sends the finished video to the owner's
Telegram chat. Nothing is recorded automatically and nothing is kept
after sending (frames and the video file are deleted once Telegram
accepts it).

Like /screenshot, a recording shows whatever is on the screen,
including typed text and message contents. That is beyond DeskAudit's
usual metadata-only rule, so it happens ONLY when the owner commands
it from Telegram, and the video goes only to the owner's chat. Staff
must be told the PCs are monitored.

How a video is made (no new dependency):
  * Frames are JPEGs captured with ctypes + GDI+ (BitBlt, then a
    native scaled JPEG save — fast, no Pillow). Frames are capped at
    960 px wide and at most 700 frames per recording, so the file
    stays under Telegram's 50 MB limit.
  * If ffmpeg is on the PC, the frames become a small H.264 MP4
    (sent as a video). Otherwise the frames are written as an MJPEG
    AVI with a pure-Python RIFF writer (sent as a document — plays
    in VLC / Windows Media Player).
"""

from __future__ import annotations

import ctypes
import shutil
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PRESET_MINUTES = (1, 3, 5, 10, 15, 20, 30)
MIN_MINUTES = 1
MAX_MINUTES = 60
MAX_FRAMES = 700
FRAME_WIDTH = 960
JPEG_QUALITY = 55
MAX_FILE_MB = 48  # Telegram Bot API limit is 50 MB per file
# Give up early when the screen cannot be captured at all (locked
# screen, no display, GDI+ failure): 3 failed frames in a row means
# the rest will fail too. Without this the owner waited the whole
# duration and only then got a reason-less "too short" message.
MAX_CONSECUTIVE_FAILURES = 3

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_TO_BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


# ---------------------------------------------------------------------------
# Native GDI / GDI+ signatures (v12.14.3 — the access-violation fix)
#
# Without argtypes/restype ctypes assumes every function returns a
# 32-bit int and passes Python ints as 32-bit values. That was wrong
# for two things here, and it is the reason recording crashed on
# Firoza's real PC while every test on this machine passed:
#
#   * The GDI+ token is a ULONG_PTR — 8 bytes on 64-bit Windows.
#     The old code kept it in a 4-byte c_ulong, so GdiplusStartup
#     wrote 8 bytes into a 4-byte buffer and corrupted the heap;
#     the next GDI+ call then died with
#     "access violation reading 0x000002046D4A1F28". Wine returns a
#     fake 32-bit token (0xDEADBEEF), which is why the bug never
#     showed up in any test here.
#   * HDC / HBITMAP / GpImage* handles are pointer-sized. Passing
#     them as plain ints truncates them to 32 bits on 64-bit Windows.
#
# So every native call below goes through _bind_native(), which
# declares the real signatures once. The token is _TOKEN_TYPE —
# c_void_p, pointer-sized on every platform, never c_ulong.
# ---------------------------------------------------------------------------

_TOKEN_TYPE = ctypes.c_void_p  # ULONG_PTR — pointer-sized, never c_ulong


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int),
                ("SuppressExternalCodecs", ctypes.c_int)]


class ImageCodecInfo(ctypes.Structure):
    _fields_ = [("Clsid", ctypes.c_ubyte * 16),
                ("FormatID", ctypes.c_ubyte * 16),
                ("CodecName", ctypes.c_void_p),
                ("DllName", ctypes.c_void_p),
                ("FormatDescription", ctypes.c_void_p),
                ("FilenameExtension", ctypes.c_void_p),
                ("MimeType", ctypes.c_void_p),
                ("Flags", ctypes.c_uint32),
                ("Version", ctypes.c_uint32),
                ("SigCount", ctypes.c_uint32),
                ("SigSize", ctypes.c_uint32),
                ("SigPattern", ctypes.c_void_p),
                ("SigMask", ctypes.c_void_p)]


class EncoderParameter(ctypes.Structure):
    # NOTE: ULONG on Windows is always 4 bytes — use c_uint32, never
    # c_ulong, which is 8 bytes on Linux (LP64) and would change the
    # struct layout on this machine vs Windows.
    _fields_ = [("Guid", ctypes.c_ubyte * 16),
                ("NumberOfValues", ctypes.c_uint32),
                ("Type", ctypes.c_uint32),
                ("Value", ctypes.c_void_p)]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint),
                ("Parameter", EncoderParameter * 1)]


class _Point(ctypes.Structure):
    # NOTE: Windows LONG is always 4 bytes — use c_int32, never
    # c_long, which is 8 bytes on Linux (LP64) and would make the
    # struct 16 bytes here but 8 on Windows (the same class of bug
    # as the 12.14.3 token crash).
    _fields_ = [("x", ctypes.c_int32),
                ("y", ctypes.c_int32)]


class _CursorInfo(ctypes.Structure):
    """CURSORINFO (24 bytes on 64-bit): cbSize, flags (bit 0 =
    CURSOR_SHOWING), hCursor, ptScreenPos."""
    _fields_ = [("cbSize", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("hCursor", ctypes.c_void_p),
                ("ptScreenPos", _Point)]


class _IconInfo(ctypes.Structure):
    """ICONINFO (32 bytes on 64-bit): fIcon, hotspot x/y, the mask
    and colour bitmaps GetIconInfo hands back — the caller must
    DeleteObject both, or every frame leaks two bitmaps."""
    _fields_ = [("fIcon", ctypes.c_int),
                ("xHotspot", ctypes.c_uint32),
                ("yHotspot", ctypes.c_uint32),
                ("hbmMask", ctypes.c_void_p),
                ("hbmColor", ctypes.c_void_p)]


_NATIVE_BOUND = False


def _bind_native() -> None:
    """Declare the ctypes signatures for the GDI / GDI+ calls used
    to capture a frame. Idempotent; a no-op off Windows (there is no
    windll there). Must run before any of those calls."""
    global _NATIVE_BOUND
    if _NATIVE_BOUND or sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    gdiplus = ctypes.windll.gdiplus
    _P = ctypes.c_void_p
    _I = ctypes.c_int
    _U = ctypes.c_uint
    user32.GetSystemMetrics.argtypes = [_I]
    user32.GetSystemMetrics.restype = _I
    user32.GetDC.argtypes = [_P]
    user32.GetDC.restype = _P
    user32.ReleaseDC.argtypes = [_P, _P]
    user32.ReleaseDC.restype = _I
    gdi32.CreateCompatibleDC.argtypes = [_P]
    gdi32.CreateCompatibleDC.restype = _P
    gdi32.CreateCompatibleBitmap.argtypes = [_P, _I, _I]
    gdi32.CreateCompatibleBitmap.restype = _P
    gdi32.SelectObject.argtypes = [_P, _P]
    gdi32.SelectObject.restype = _P
    gdi32.BitBlt.argtypes = [_P, _I, _I, _I, _I, _P, _I, _I, _U]
    gdi32.BitBlt.restype = _I
    gdi32.DeleteObject.argtypes = [_P]
    gdi32.DeleteObject.restype = _I
    gdi32.DeleteDC.argtypes = [_P]
    gdi32.DeleteDC.restype = _I
    gdiplus.GdiplusStartup.argtypes = [ctypes.POINTER(_TOKEN_TYPE),
                                       ctypes.POINTER(GdiplusStartupInput),
                                       _P]
    gdiplus.GdiplusStartup.restype = _I
    gdiplus.GdiplusShutdown.argtypes = [_TOKEN_TYPE]
    gdiplus.GdiplusShutdown.restype = None
    gdiplus.GdipCreateBitmapFromHBITMAP.argtypes = [_P, _P,
                                                    ctypes.POINTER(_P)]
    gdiplus.GdipCreateBitmapFromHBITMAP.restype = _I
    gdiplus.GdipCreateBitmapFromScan0.argtypes = [_I, _I, _I, _I, _P,
                                                 ctypes.POINTER(_P)]
    gdiplus.GdipCreateBitmapFromScan0.restype = _I
    gdiplus.GdipGetImageGraphicsContext.argtypes = [_P, ctypes.POINTER(_P)]
    gdiplus.GdipGetImageGraphicsContext.restype = _I
    gdiplus.GdipDrawImageRectI.argtypes = [_P, _P, _I, _I, _I, _I]
    gdiplus.GdipDrawImageRectI.restype = _I
    gdiplus.GdipDeleteGraphics.argtypes = [_P]
    gdiplus.GdipDeleteGraphics.restype = _I
    gdiplus.GdipSaveImageToFile.argtypes = [_P, ctypes.c_wchar_p, _P, _P]
    gdiplus.GdipSaveImageToFile.restype = _I
    gdiplus.GdipGetImageEncodersSize.argtypes = [ctypes.POINTER(_U),
                                                ctypes.POINTER(_U)]
    gdiplus.GdipGetImageEncodersSize.restype = _I
    gdiplus.GdipGetImageEncoders.argtypes = [_U, _U, _P]
    gdiplus.GdipGetImageEncoders.restype = _I
    gdiplus.GdipDisposeImage.argtypes = [_P]
    gdiplus.GdipDisposeImage.restype = _I
    # Cursor drawing (v12.14.5): bound defensively — the functions
    # exist on every Windows, but a fake DLL in tests may not have
    # them, and _draw_cursor must never fail a frame for that.
    _cursor = getattr(user32, "GetCursorInfo", None)
    if _cursor is not None:
        _cursor.argtypes = [ctypes.POINTER(_CursorInfo)]
        _cursor.restype = _I
    _icon = getattr(user32, "GetIconInfo", None)
    if _icon is not None:
        _icon.argtypes = [_P, ctypes.POINTER(_IconInfo)]
        _icon.restype = _I
    _draw = getattr(user32, "DrawIconEx", None)
    if _draw is not None:
        _draw.argtypes = [_P, _I, _I, _P, _I, _I, _U, _P, _U]
        _draw.restype = _I
    _load = getattr(user32, "LoadCursorW", None)
    if _load is not None:
        _load.argtypes = [_P, _P]
        _load.restype = _P
    _NATIVE_BOUND = True


def validate_minutes(value) -> int:
    """Parse a duration (int, or a string with ASCII/Bengali digits).
    Returns the minutes (1–60) or raises ValueError with a Bengali
    message the bot can send back as-is."""
    if isinstance(value, str):
        value = value.strip().translate(_BN_DIGITS)
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ValueError("সময় মিনিটে লিখুন — যেমন /record 5 (১ থেকে ৬০ মিনিট)।")
    if minutes < MIN_MINUTES or minutes > MAX_MINUTES:
        raise ValueError(
            f"রেকর্ড ১ থেকে {str(MAX_MINUTES).translate(_TO_BN_DIGITS)} মিনিট পর্যন্ত করা যায়।")
    return minutes


def frame_interval_seconds(minutes: int) -> float:
    """Seconds between frames. Short recordings run at 5 frames a
    second, so mouse movement looks smooth instead of a slideshow
    (the old 2 s gap was 0.5 fps — the owner reported the video
    was not smooth); longer ones spread the MAX_FRAMES cap over
    the whole duration, so the file stays small no matter how long
    the recording runs. A 1-minute recording is 300 frames, 2
    minutes is 600; beyond that the cap takes over (3 min ≈ 0.26 s,
    30 min ≈ 2.6 s per frame)."""
    return max(0.2, minutes * 60.0 / MAX_FRAMES)


def find_ffmpeg() -> str | None:
    """ffmpeg on the PATH, if the PC has it (needed for MP4)."""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def _call(label: str, func, *args):
    """Call one native function; if it crashes (an access violation
    reaches Python as OSError), say WHICH call it was. A bare address
    tells the owner nothing; 'GdipSaveImageToFile: exception: access
    violation reading 0x...' tells us exactly where to look next."""
    try:
        return func(*args)
    except Exception as exc:
        raise RuntimeError(f"{label}: {exc}") from exc


_CURSOR_SHOWING = 0x00000001
_DI_NORMAL = 0x0003
_IDC_ARROW = 32512


def _draw_cursor(user32, gdi32, hdc_mem, x0, y0) -> None:
    """Paint the mouse pointer onto the captured bitmap, at the
    position GetCursorInfo reports, corrected by the cursor's
    hotspot (so the arrow tip — not the bitmap's corner — sits
    where the pointer is). BitBlt never copies the pointer:
    Windows draws it as a separate hardware overlay, so a
    recording without this shows no mouse at all — the owner
    reported exactly that. Never raises and never fails a frame:
    a hidden pointer, a failed call, or a missing function all
    just mean the frame ships without a pointer."""
    try:
        info = _CursorInfo()
        info.cbSize = ctypes.sizeof(_CursorInfo)
        if not _call("GetCursorInfo", user32.GetCursorInfo,
                     ctypes.byref(info)):
            return
        if not (info.flags & _CURSOR_SHOWING):
            return
        hcursor = info.hCursor
        if hcursor:
            icon = _IconInfo()
            if _call("GetIconInfo", user32.GetIconInfo, hcursor,
                     ctypes.byref(icon)):
                hx, hy = icon.xHotspot, icon.yHotspot
                # GetIconInfo hands back two bitmaps we own: free both,
                # or every frame leaks them.
                if icon.hbmMask:
                    _call("DeleteObject", gdi32.DeleteObject, icon.hbmMask)
                if icon.hbmColor:
                    _call("DeleteObject", gdi32.DeleteObject, icon.hbmColor)
            else:
                hx, hy = 0, 0
        else:
            # Wine (and some bare desktops) report a showing cursor
            # with no handle — fall back to the shared system arrow
            # so the position still shows. Its hotspot is (0, 0).
            hcursor = _call("LoadCursorW", user32.LoadCursorW, None,
                            _IDC_ARROW)
            if not hcursor:
                return
            hx, hy = 0, 0
        _call("DrawIconEx", user32.DrawIconEx, hdc_mem,
              info.ptScreenPos.x - x0 - hx,
              info.ptScreenPos.y - y0 - hy,
              hcursor, 0, 0, 0, None, _DI_NORMAL)
    except Exception:
        return


def screen_locked_reason() -> str | None:
    """Why the screen cannot be captured right now, or None when it
    can (or when we cannot tell). Windows only; never raises.

    On a locked workstation the desktop receiving input is "Winlogon",
    not "Default" — BitBlt from the screen then fails for every frame.
    The recorder used to learn that only after running the whole
    duration; checking first turns it into an instant, exact message.
    A screensaver still runs on "Default", so it does not trip this.
    When unsure (the check itself fails) this returns None and the
    recording tries anyway — the fail-fast path then reports the
    real error."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_int,
                                            ctypes.c_uint]
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.GetUserObjectInformationW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
        user32.GetUserObjectInformationW.restype = ctypes.c_bool
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        user32.CloseDesktop.restype = ctypes.c_bool
        hdesk = user32.OpenInputDesktop(0, 0, 0x0001)  # DESKTOP_READOBJECTS
        if not hdesk:
            return None
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_uint(0)
            ok = user32.GetUserObjectInformationW(
                hdesk, 2, ctypes.byref(buf), ctypes.sizeof(buf),
                ctypes.byref(needed))  # UOI_NAME = 2
            name = buf.value if ok else ""
        finally:
            user32.CloseDesktop(hdesk)
        if name and name != "Default":
            return ("🎥 স্ক্রিন এখন ধরা যাচ্ছে না — লক করা থাকলে খুলে নিন, "
                    "তারপর আবার চেষ্টা করুন।")
        return None
    except Exception:
        return None


def capture_jpeg_frame(path: str | Path, width: int = FRAME_WIDTH,
                       quality: int = JPEG_QUALITY) -> Path:
    """Capture the whole virtual screen as one JPEG file (Windows
    only). BitBlt into a bitmap, then GDI+ saves it scaled to at most
    `width` px wide at the given JPEG quality — all native code, so a
    frame takes well under a second. Raises RuntimeError otherwise."""
    if sys.platform != "win32":
        raise RuntimeError("screen recording works on Windows only")
    _bind_native()

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    gdiplus = ctypes.windll.gdiplus
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    SRCCOPY = 0x00CC0020
    x0 = _call("GetSystemMetrics", user32.GetSystemMetrics, SM_XVIRTUALSCREEN)
    y0 = _call("GetSystemMetrics", user32.GetSystemMetrics, SM_YVIRTUALSCREEN)
    w = _call("GetSystemMetrics", user32.GetSystemMetrics, SM_CXVIRTUALSCREEN)
    h = _call("GetSystemMetrics", user32.GetSystemMetrics, SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not read screen size")
    hdc_screen = _call("GetDC", user32.GetDC, 0)
    if not hdc_screen:
        raise RuntimeError("could not open the screen device")
    hdc_mem = _call("CreateCompatibleDC", gdi32.CreateCompatibleDC, hdc_screen)
    hbm = _call("CreateCompatibleBitmap", gdi32.CreateCompatibleBitmap,
                hdc_screen, w, h)
    # v12.14.3: the token MUST be pointer-sized. The old code used
    # ctypes.c_ulong (4 bytes); GdiplusStartup writes an 8-byte
    # ULONG_PTR into it on 64-bit Windows, corrupting the heap and
    # crashing the next GDI+ call with an access violation.
    token = _TOKEN_TYPE(0)
    bitmap = ctypes.c_void_p(0)
    scaled = ctypes.c_void_p(0)
    try:
        if not hdc_mem or not hbm:
            raise RuntimeError("could not allocate the capture bitmap")
        _call("SelectObject", gdi32.SelectObject, hdc_mem, hbm)
        if not _call("BitBlt", gdi32.BitBlt, hdc_mem, 0, 0, w, h,
                     hdc_screen, x0, y0, SRCCOPY):
            raise RuntimeError("screen copy failed")
        # v12.14.5: BitBlt never copies the mouse pointer (Windows
        # draws it as a separate overlay), so paint it onto the
        # bitmap now — otherwise a recording shows no mouse at all.
        _draw_cursor(user32, gdi32, hdc_mem, x0, y0)

        inp = GdiplusStartupInput(1, None, 0, 0)
        if _call("GdiplusStartup", gdiplus.GdiplusStartup,
                 ctypes.byref(token), ctypes.byref(inp), None) != 0:
            raise RuntimeError("GDI+ could not start")
        try:
            if _call("GdipCreateBitmapFromHBITMAP",
                     gdiplus.GdipCreateBitmapFromHBITMAP,
                     hbm, None, ctypes.byref(bitmap)) != 0:
                raise RuntimeError("GDI+ could not read the capture")
            save_bitmap = bitmap
            if w > width:
                tw = width
                th = max(1, round(h * width / w))
                if _call("GdipCreateBitmapFromScan0",
                         gdiplus.GdipCreateBitmapFromScan0,
                         tw, th, 0, 0x26200A, None,
                         ctypes.byref(scaled)) != 0:
                    raise RuntimeError("GDI+ could not make the scaled bitmap")
                graphics = ctypes.c_void_p(0)
                if _call("GdipGetImageGraphicsContext",
                         gdiplus.GdipGetImageGraphicsContext,
                         scaled, ctypes.byref(graphics)) != 0:
                    raise RuntimeError("GDI+ could not draw the scaled frame")
                try:
                    if _call("GdipDrawImageRectI",
                             gdiplus.GdipDrawImageRectI,
                             graphics, bitmap, 0, 0, tw, th) != 0:
                        raise RuntimeError("GDI+ could not scale the frame")
                finally:
                    _call("GdipDeleteGraphics", gdiplus.GdipDeleteGraphics,
                          graphics)
                save_bitmap = scaled
            clsid = _jpeg_encoder_clsid(gdiplus)
            if clsid is None:
                raise RuntimeError("no JPEG encoder on this PC")
            # Keep the 16 CLSID bytes in a live buffer for the call.
            clsid_buf = (ctypes.c_ubyte * 16).from_buffer_copy(clsid)
            params = _quality_params(quality)
            status = _call("GdipSaveImageToFile", gdiplus.GdipSaveImageToFile,
                           save_bitmap, str(out), ctypes.byref(clsid_buf),
                           ctypes.byref(params) if params is not None else None)
            if status != 0:
                raise RuntimeError(f"GDI+ could not save the JPEG (status {status})")
        finally:
            # v12.14.4: dispose EVERY GDI+ object BEFORE shutting GDI+
            # down. The old code shut down first and disposed the
            # bitmaps after — disposing after shutdown dereferences
            # freed GDI+ state and crashes with an access violation
            # on real Windows (Wine tolerates it, which is why no
            # test here ever saw it). This was the remaining cause
            # after the v12.14.3 token fix.
            if scaled:
                _call("GdipDisposeImage", gdiplus.GdipDisposeImage, scaled)
            if bitmap:
                _call("GdipDisposeImage", gdiplus.GdipDisposeImage, bitmap)
            _call("GdiplusShutdown", gdiplus.GdiplusShutdown, token)
    finally:
        # Plain GDI cleanup only — the GDI+ objects are already gone.
        if hbm:
            _call("DeleteObject", gdi32.DeleteObject, hbm)
        if hdc_mem:
            _call("DeleteDC", gdi32.DeleteDC, hdc_mem)
        _call("ReleaseDC", user32.ReleaseDC, 0, hdc_screen)
    return out


def _jpeg_encoder_clsid(gdiplus):
    """The CLSID of the image/jpeg encoder, or None."""
    _bind_native()
    num = ctypes.c_uint(0)
    size = ctypes.c_uint(0)
    _call("GdipGetImageEncodersSize", gdiplus.GdipGetImageEncodersSize,
          ctypes.byref(num), ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)

    _call("GdipGetImageEncoders", gdiplus.GdipGetImageEncoders,
          num, size, buf)
    for i in range(num.value):
        info = ImageCodecInfo.from_address(
            ctypes.addressof(buf) + i * ctypes.sizeof(ImageCodecInfo))
        mime = ctypes.wstring_at(info.MimeType) if info.MimeType else ""
        if mime == "image/jpeg":
            return bytes(info.Clsid)
    return None


def _quality_params(quality: int):
    """EncoderParameters for JPEG quality (1–100), or None to use the
    encoder default. The quality GUID is {1D5BE4B5-FA4A-452D-9CDD-
    5DB35105E7EB}; value type 4 is a 32-bit long."""
    guid_bytes = bytes.fromhex("B5E45B1D4AFA2D459CDD5DB35105E7EB")
    # The quality value is a 32-bit long (type 4) — c_uint32, not
    # c_ulong, for the same platform-width reason as above.
    value = ctypes.c_uint32(max(1, min(100, quality)))
    params = EncoderParameters()
    params.Count = 1
    params.Parameter[0].Guid = (ctypes.c_ubyte * 16).from_buffer_copy(guid_bytes)
    params.Parameter[0].NumberOfValues = 1
    params.Parameter[0].Type = 4
    params.Parameter[0].Value = ctypes.cast(
        ctypes.byref(value), ctypes.c_void_p)
    # Keep the value alive as long as the params live.
    params._value = value  # type: ignore[attr-defined]
    return params


def write_mjpeg_avi(frame_paths, out_path: str | Path, fps: float) -> Path:
    """Write JPEG frames as an MJPEG AVI (pure Python RIFF writer).
    Plays in VLC / Windows Media Player. `fps` may be fractional.
    Raises ValueError when fewer than 2 frames are given."""
    frames = [Path(p) for p in frame_paths]
    if len(frames) < 2:
        raise ValueError("need at least 2 frames for a video")
    sizes = [p.stat().st_size for p in frames]
    # First frame's size sets the stream dimensions; read the JPEG
    # header (SOF0/SOF2) for width/height.
    width, height = _jpeg_size(frames[0])
    max_size = max(sizes)
    rate = 100
    scale = max(1, round(100 / fps))  # fps = rate / scale
    # Chunk sizes, all known up front (frame sizes are on disk).
    # A LIST's size field includes its 4-byte type ('movi' / 'hdrl').
    movi_size = 4 + sum(8 + s + (s % 2) for s in sizes)
    idx_size = 16 * len(frames)
    hdrl = _avi_header(width, height, len(frames), max_size, rate, scale)
    riff_size = 4 + (8 + len(hdrl)) + (8 + movi_size) + (8 + idx_size)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Offsets in idx1 are relative to the 'movi' FOURCC and point at
    # each chunk's FOURCC (the convention ffmpeg and WMP accept).
    movi_fourcc_pos = 12 + (8 + len(hdrl)) + 8
    offsets = []
    pos = movi_fourcc_pos + 4  # first chunk starts after 'movi'
    for s in sizes:
        offsets.append(pos - movi_fourcc_pos)
        pos += 8 + s + (s % 2)
    with out.open("wb") as f:
        f.write(b"RIFF" + struct.pack("<I", riff_size) + b"AVI ")
        f.write(b"LIST" + struct.pack("<I", len(hdrl)) + hdrl)
        f.write(b"LIST" + struct.pack("<I", movi_size) + b"movi")
        for frame, size, off in zip(frames, sizes, offsets):
            f.write(b"00db" + struct.pack("<I", size))
            with frame.open("rb") as src:
                shutil.copyfileobj(src, f)
            if size % 2:
                f.write(b"\x00")
        f.write(b"idx1" + struct.pack("<I", idx_size))
        for size, off in zip(sizes, offsets):
            f.write(b"00db" + struct.pack("<III", 0x10, off, size))
    return out


def _jpeg_size(path: Path) -> tuple[int, int]:
    """Width/height from a JPEG's first SOF marker (pure stdlib)."""
    data = path.read_bytes()
    i = 2  # skip SOI
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):  # SOF markers
            h = struct.unpack(">H", data[i + 5:i + 7])[0]
            w = struct.unpack(">H", data[i + 7:i + 9])[0]
            return w, h
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + length
    raise ValueError("could not read JPEG size")


def _avi_header(width: int, height: int, frames: int, max_size: int,
                rate: int, scale: int) -> bytes:
    """The LIST 'hdrl' contents (avih + strl/strh + strf)."""
    micro_per_frame = max(1, round(1_000_000 * scale / rate))
    avih = struct.pack(
        "<14I", micro_per_frame, max_size, 0, 0x10, frames, 0, 1,
        max_size, width, height, 0, 0, 0, 0)
    strh = struct.pack(
        "<4s4sIHHIIIIIIII4i",
        b"vids", b"MJPG", 0, 0, 0, 0, scale, rate, 0, frames,
        max_size, 0xFFFFFFFF, 0, 0, 0, width, height)
    strf = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24,
        struct.unpack("<I", b"MJPG")[0], max_size, 0, 0, 0, 0)
    strl = (b"strh" + struct.pack("<I", len(strh)) + strh
            + b"strf" + struct.pack("<I", len(strf)) + strf)
    hdrl = (b"avih" + struct.pack("<I", len(avih)) + avih
            + b"LIST" + struct.pack("<I", len(strl) + 4) + b"strl" + strl)
    return b"hdrl" + hdrl


def encode_mp4(frame_paths, out_path: str | Path, fps: float,
               ffmpeg: str | None = None) -> Path | None:
    """Encode frames as H.264 MP4 with ffmpeg. Returns the path, or
    None when ffmpeg is missing or the encode fails. Never raises."""
    frames = [Path(p) for p in frame_paths]
    if len(frames) < 2:
        return None
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return None
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # The pattern follows the frames' own suffix (.jpg from the GDI+
    # capture; .png in tests) so ffmpeg picks the right decoder.
    pattern = str(frames[0].parent / ("frame%04d" + frames[0].suffix))
    cmd = [exe, "-y", "-v", "error", "-framerate", f"{fps:.4f}",
           "-i", pattern, "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    try:
        from deskaudit.utils import run_hidden

        result = run_hidden(cmd, capture_output=True, timeout=600,
                            check=False)
        if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    except Exception:
        pass
    try:
        out.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def encode_video(frame_paths, out_dir: str | Path, stem: str,
                 fps: float) -> tuple[Path, str] | None:
    """Best video for the frames: MP4 via ffmpeg when available,
    MJPEG AVI otherwise. Returns (path, kind) with kind "mp4" or
    "avi", or None when nothing could be encoded. Never raises."""
    out_dir = Path(out_dir)
    try:
        mp4 = encode_mp4(frame_paths, out_dir / f"{stem}.mp4", fps)
        if mp4 is not None:
            return mp4, "mp4"
    except Exception:
        pass
    try:
        avi = write_mjpeg_avi(frame_paths, out_dir / f"{stem}.avi", fps)
        return avi, "avi"
    except Exception:
        return None


class ScreenRecorder:
    """Records the screen in a background thread for a chosen number
    of minutes, then hands the finished video to `on_done`. One
    recording at a time; `capture_fn` / `encode_fn` are injectable so
    tests can run without a real screen or ffmpeg."""

    def __init__(self, project_root, log=None, on_done=None,
                 capture_fn=None, encode_fn=None):
        self.project_root = Path(project_root)
        self.log = log
        self.on_done = on_done
        self._capture = capture_fn or capture_jpeg_frame
        self._encode = encode_fn or encode_video
        # RLock (not Lock): _begin_run holds it while minutes_left()
        # -> status() acquires it again. A plain Lock deadlocked both
        # threads there (bot stuck, recording frozen) — caught by the
        # double-start test in test_screen_record.py.
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started_at: float | None = None
        self._minutes = 0
        self._duration_seconds = 0
        self._frames = 0
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._run_dir: Path | None = None

    # -- public API --------------------------------------------------
    def start(self, minutes) -> tuple[bool, str]:
        """Start a recording. Returns (ok, Bengali message)."""
        try:
            minutes = validate_minutes(minutes)
        except ValueError as exc:
            return False, str(exc)
        return self._begin_run(minutes, minutes * 60,
                               f"🎥 রেকর্ড শুরু হলো — {minutes} মিনিট। "
                               "শেষ হলে ভিডিও এখানেই পাঠিয়ে দেওয়া হবে।")

    def start_test(self, seconds: int) -> tuple[bool, str]:
        """Start a short test recording (3–120 s, CLI test only)."""
        if not isinstance(seconds, int) or not 3 <= seconds <= 120:
            return False, "record-test needs 3–120 seconds"
        return self._begin_run(0, seconds,
                               f"🎥 টেস্ট রেকর্ড শুরু — {seconds} সেকেন্ড।")

    def _begin_run(self, minutes: int, duration_seconds: int,
                   started_msg: str) -> tuple[bool, str]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                left = self.minutes_left()
                return False, (f"🎥 রেকর্ড ইতিমধ্যেই চলছে — "
                               f"আরও {left} মিনিট বাকি। থামাতে /record stop লিখুন।")
            self._cleanup_stale_runs()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            self._run_dir = (self.project_root / "data" / "recordings"
                             / stamp)
            self._run_dir.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._started_at = time.time()
            self._minutes = minutes
            self._duration_seconds = duration_seconds
            self._frames = 0
            self._last_error = None
            self._consecutive_failures = 0
            self._thread = threading.Thread(target=self._run,
                                            name="deskaudit-record",
                                            daemon=True)
            self._thread.start()
            return True, started_msg

    def stop(self) -> tuple[bool, str]:
        """Stop the running recording early (the video so far is still
        sent). Returns (ok, Bengali message)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False, "🎥 এখন কোনো রেকর্ড চলছে না।"
            self._stop.set()
            return True, "🎥 রেকর্ড থামানো হলো — ভিডিও পাঠানো হচ্ছে…"

    def status(self) -> dict:
        """Current state: running, minutes, elapsed, frames, last error."""
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            elapsed = (time.time() - self._started_at) if self._started_at else 0
            return {"running": running, "minutes": self._minutes,
                    "elapsed_seconds": int(elapsed), "frames": self._frames,
                    "last_error": self._last_error}

    @property
    def last_error(self) -> str | None:
        """The last frame-capture error, or None when capturing works."""
        with self._lock:
            return self._last_error

    def minutes_left(self) -> int:
        st = self.status()
        if not st["running"]:
            return 0
        return max(0, st["minutes"] - st["elapsed_seconds"] // 60)

    # -- internals ---------------------------------------------------
    def _run(self):
        frames: list[Path] = []
        # The run dir is local: a new recording may start while this
        # thread is still cleaning up, and it must never write frames
        # into (or delete) the new run's folder.
        run_dir = self._run_dir
        try:
            interval = frame_interval_seconds(self._minutes)
            end = (self._started_at or time.time()) + self._duration_seconds
            index = 0
            while not self._stop.is_set():
                now = time.time()
                if now >= end:
                    break
                try:
                    frame = self._capture(
                        run_dir / f"frame{index:04d}.jpg")
                    frames.append(Path(frame))
                    with self._lock:
                        self._frames = len(frames)
                        # Capturing works again — forget the earlier error.
                        self._last_error = None
                        self._consecutive_failures = 0
                except Exception as exc:
                    with self._lock:
                        self._last_error = str(exc)
                        self._consecutive_failures += 1
                        failed = self._consecutive_failures
                    if self.log is not None:
                        # v12.14.4: the traceback too — the agent log
                        # on the PC then holds the exact failing call.
                        self.log.warning("Screen record frame failed: %s",
                                         exc, exc_info=True)
                    if failed >= MAX_CONSECUTIVE_FAILURES:
                        # The screen cannot be captured at all (locked
                        # screen is the usual cause) — stop now and
                        # report the reason instead of waiting out the
                        # whole duration and then saying "too short".
                        break
                index += 1
                # Sleep until the next frame is due (capture time counts).
                due = (self._started_at or now) + index * interval
                remaining = due - time.time()
                if remaining > 0:
                    self._stop.wait(remaining)
            if len(frames) < 2:
                self._finish(None, None, len(frames))
                return
            fps = 1.0 / interval
            try:
                result = self._encode(frames, run_dir,
                                      "screen-record", fps)
            except Exception as exc:
                # An encoder must never kill the thread silently: the
                # owner would wait forever with no message at all.
                if self.log is not None:
                    self.log.warning("Screen record encode failed: %s", exc)
                result = None
            if result is None:
                self._finish(None, None, len(frames))
                return
            video, kind = result
            try:
                size_mb = video.stat().st_size / (1024 * 1024)
            except Exception:
                # Encoder claimed success but left no file behind.
                self._finish(None, None, len(frames))
                return
            if size_mb > MAX_FILE_MB and self.log is not None:
                self.log.warning("Recorded video is %.1f MB (over the "
                                 "%d MB Telegram limit).", size_mb, MAX_FILE_MB)
            self._finish(video, kind, len(frames))
        finally:
            self._cleanup_frames(frames, run_dir)

    def _finish(self, video, kind, frames: int):
        # Mark the recording finished BEFORE notifying: status() must
        # already say "not running" when on_done fires, and a new
        # /record may start at once.
        with self._lock:
            self._thread = None
        if self.on_done is not None:
            try:
                self.on_done(video, kind, self._minutes, frames)
            except Exception as exc:
                if self.log is not None:
                    self.log.warning("Screen record send failed: %s", exc)

    def _cleanup_frames(self, frames, run_dir=None):
        for frame in frames:
            try:
                Path(frame).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            if run_dir is None:
                run_dir = self._run_dir
            if run_dir is not None:
                run_dir.rmdir()
        except Exception:
            pass

    def _cleanup_stale_runs(self):
        """Leftover run folders from a previous process (crash, agent
        stopped mid-recording) — no recording can be running now."""
        base = self.project_root / "data" / "recordings"
        if not base.exists():
            return
        for child in base.iterdir():
            if not child.is_dir():
                continue
            try:
                for f in child.iterdir():
                    f.unlink(missing_ok=True)
                child.rmdir()
            except Exception:
                pass


def cleanup_stale_recordings(project_root) -> None:
    """Delete leftover recording folders (used at agent start)."""
    try:
        ScreenRecorder(project_root)._cleanup_stale_runs()
    except Exception:
        pass
