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

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_TO_BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")


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
    """Seconds between frames. At least 2 s apart, and at most
    MAX_FRAMES frames per recording, so the file stays small no
    matter how long the recording runs."""
    return max(2.0, minutes * 60.0 / MAX_FRAMES)


def find_ffmpeg() -> str | None:
    """ffmpeg on the PATH, if the PC has it (needed for MP4)."""
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def capture_jpeg_frame(path: str | Path, width: int = FRAME_WIDTH,
                       quality: int = JPEG_QUALITY) -> Path:
    """Capture the whole virtual screen as one JPEG file (Windows
    only). BitBlt into a bitmap, then GDI+ saves it scaled to at most
    `width` px wide at the given JPEG quality — all native code, so a
    frame takes well under a second. Raises RuntimeError otherwise."""
    if sys.platform != "win32":
        raise RuntimeError("screen recording works on Windows only")
    import ctypes

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    gdiplus = ctypes.windll.gdiplus
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    SRCCOPY = 0x00CC0020
    x0 = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y0 = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        raise RuntimeError("could not read screen size")
    hdc_screen = user32.GetDC(0)
    if not hdc_screen:
        raise RuntimeError("could not open the screen device")
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbm = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    token = ctypes.c_ulong(0)
    bitmap = ctypes.c_void_p(0)
    scaled = ctypes.c_void_p(0)
    try:
        if not hdc_mem or not hbm:
            raise RuntimeError("could not allocate the capture bitmap")
        gdi32.SelectObject(hdc_mem, hbm)
        if not gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x0, y0, SRCCOPY):
            raise RuntimeError("screen copy failed")

        class GdiplusStartupInput(ctypes.Structure):
            _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                        ("DebugEventCallback", ctypes.c_void_p),
                        ("SuppressBackgroundThread", ctypes.c_int),
                        ("SuppressExternalCodecs", ctypes.c_int)]

        inp = GdiplusStartupInput(1, None, 0, 0)
        if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) != 0:
            raise RuntimeError("GDI+ could not start")
        try:
            if gdiplus.GdipCreateBitmapFromHBITMAP(hbm, None, ctypes.byref(bitmap)) != 0:
                raise RuntimeError("GDI+ could not read the capture")
            save_bitmap = bitmap
            if w > width:
                tw = width
                th = max(1, round(h * width / w))
                if gdiplus.GdipCreateBitmapFromScan0(
                        tw, th, 0, 0x26200A, None, ctypes.byref(scaled)) != 0:
                    raise RuntimeError("GDI+ could not make the scaled bitmap")
                graphics = ctypes.c_void_p(0)
                if gdiplus.GdipGetImageGraphicsContext(
                        scaled, ctypes.byref(graphics)) != 0:
                    raise RuntimeError("GDI+ could not draw the scaled frame")
                try:
                    if gdiplus.GdipDrawImageRectI(
                            graphics, bitmap, 0, 0, tw, th) != 0:
                        raise RuntimeError("GDI+ could not scale the frame")
                finally:
                    gdiplus.GdipDeleteGraphics(graphics)
                save_bitmap = scaled
            clsid = _jpeg_encoder_clsid(gdiplus)
            if clsid is None:
                raise RuntimeError("no JPEG encoder on this PC")
            params = _quality_params(quality)
            status = gdiplus.GdipSaveImageToFile(
                save_bitmap, str(out), clsid,
                ctypes.byref(params) if params is not None else None)
            if status != 0:
                raise RuntimeError(f"GDI+ could not save the JPEG (status {status})")
        finally:
            gdiplus.GdiplusShutdown(token)
    finally:
        if bitmap:
            gdiplus.GdipDisposeImage(bitmap)
        if scaled:
            gdiplus.GdipDisposeImage(scaled)
        if hbm:
            gdi32.DeleteObject(hbm)
        if hdc_mem:
            gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)
    return out


def _jpeg_encoder_clsid(gdiplus):
    """The CLSID of the image/jpeg encoder, or None."""
    import ctypes

    num = ctypes.c_uint(0)
    size = ctypes.c_uint(0)
    gdiplus.GdipGetImageEncodersSize(ctypes.byref(num), ctypes.byref(size))
    if not size.value:
        return None
    buf = ctypes.create_string_buffer(size.value)

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

    gdiplus.GdipGetImageEncoders(num, size, buf)
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
    import ctypes

    class EncoderParameter(ctypes.Structure):
        _fields_ = [("Guid", ctypes.c_ubyte * 16),
                    ("NumberOfValues", ctypes.c_ulong),
                    ("Type", ctypes.c_ulong),
                    ("Value", ctypes.c_void_p)]

    class EncoderParameters(ctypes.Structure):
        _fields_ = [("Count", ctypes.c_uint),
                    ("Parameter", EncoderParameter * 1)]

    guid_bytes = bytes.fromhex("B5E45B1D4AFA2D459CDD5DB35105E7EB")
    value = ctypes.c_ulong(max(1, min(100, quality)))
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
        """Current state: running, minutes, elapsed, frames."""
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            elapsed = (time.time() - self._started_at) if self._started_at else 0
            return {"running": running, "minutes": self._minutes,
                    "elapsed_seconds": int(elapsed), "frames": self._frames}

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
            interval = max(2.0, self._duration_seconds / MAX_FRAMES)
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
                except Exception as exc:
                    if self.log is not None:
                        self.log.warning("Screen record frame failed: %s", exc)
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
            result = self._encode(frames, run_dir,
                                  "screen-record", fps)
            if result is None:
                self._finish(None, None, len(frames))
                return
            video, kind = result
            size_mb = video.stat().st_size / (1024 * 1024)
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
