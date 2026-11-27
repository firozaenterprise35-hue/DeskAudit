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
    assert frame_interval_seconds(1) == 2.0
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
        time.sleep(3.0)  # frame 0 at ~0 s, frame 1 at ~2 s
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
        # Wait until frame 0 exists (next frame is due at 2 s), then
        # stop at once — only one frame, so no video can be made.
        deadline = time.time() + 3
        while rec.status()["frames"] < 1 and time.time() < deadline:
            time.sleep(0.05)
        ok, msg = rec.stop()
        assert ok and "থামানো হলো" in msg
        assert done.wait(6)
        assert got["video"] is None and got["frames"] == 1
    print("recorder too short sends nothing passed")
