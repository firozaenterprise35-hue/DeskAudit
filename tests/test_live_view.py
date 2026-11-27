"""Tests for the live screen view (v12.15, deskaudit/live_view.py).

The viewer thread is timing-driven, so the tests run it with a
0.05 s refresh and a short max duration — fast on Linux, where the
real capture would refuse anyway (the capture is faked here).
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit.live_view import LiveViewer  # noqa: E402


class _FakeCapture:
    """Writes a tiny file and counts calls; optionally fails."""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times

    def __call__(self, path):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("screen copy failed")
        Path(path).write_bytes(b"fake-jpeg")
        return Path(path)


def _viewer(tmp, **kw):
    done = threading.Event()
    got = {}

    def on_done(reason, refreshes, last_error):
        got.update(reason=reason, refreshes=refreshes,
                   last_error=last_error)
        done.set()

    viewer = LiveViewer(Path(tmp) / "live", on_done=on_done, **kw)
    return viewer, done, got


def test_live_view_refreshes_one_message():
    """One photo is sent, then the SAME message is edited on every
    refresh — never a new photo per refresh."""
    with tempfile.TemporaryDirectory() as tmp:
        capture = _FakeCapture()
        sends, edits = [], []

        def send_fn(path, caption, markup):
            sends.append((str(path), caption, markup))
            return 42

        def edit_fn(message_id, path, caption, markup):
            edits.append((message_id, str(path), caption, markup))
            return True

        viewer, done, got = _viewer(
            tmp, capture_fn=capture, send_fn=send_fn, edit_fn=edit_fn,
            reply_markup={"inline_keyboard": []},
            refresh_seconds=0.05, max_seconds=60)
        ok, _msg = viewer.start()
        assert ok
        time.sleep(0.3)
        ok, _msg = viewer.stop()
        assert ok
        assert done.wait(5), "live view did not finish"
        assert got["reason"] == "stopped"
        assert len(sends) == 1, sends
        assert sends[0][1].startswith("🔴 লাইভ স্ক্রিন — "), sends
        assert sends[0][2] == {"inline_keyboard": []}
        assert len(edits) >= 2, edits
        assert all(e[0] == 42 for e in edits), edits
        assert all(e[2].startswith("🔴 লাইভ স্ক্রিন — ") for e in edits), edits
        assert got["refreshes"] == len(edits)
        assert viewer.status()["running"] is False
    print("live view refreshes one message passed")


def test_live_view_start_twice_and_stop_idle():
    with tempfile.TemporaryDirectory() as tmp:
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(),
            send_fn=lambda p, c, m: 7, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=60)
        ok, _msg = viewer.stop()
        assert not ok
        ok, _msg = viewer.start()
        assert ok
        ok, _msg = viewer.start()
        assert not ok
        ok, _msg = viewer.stop()
        assert ok
        assert done.wait(5)
        assert got["reason"] == "stopped"
    print("live view start twice and stop idle passed")


def test_live_view_time_up():
    """The view stops by itself when the max duration runs out."""
    with tempfile.TemporaryDirectory() as tmp:
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(),
            send_fn=lambda p, c, m: 7, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=0.25)
        ok, _msg = viewer.start()
        assert ok
        assert done.wait(5), "live view did not stop on its own"
        assert got["reason"] == "time_up"
        assert viewer.status()["running"] is False
    print("live view time up passed")


def test_live_view_fail_fast():
    """Three failed captures in a row stop the view with the reason
    (locked screen is the usual cause) — it must not retry until
    the 10 minutes run out."""
    with tempfile.TemporaryDirectory() as tmp:
        started = time.time()
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(fail_times=99),
            send_fn=lambda p, c, m: 7, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=60)
        ok, _msg = viewer.start()
        assert ok
        assert done.wait(5), "failing live view did not stop fast"
        assert time.time() - started < 5
        assert got["reason"] == "failed"
        assert got["last_error"] == "screen copy failed"
    print("live view fail fast passed")


def test_live_view_send_failure():
    """A first photo that cannot be sent ends the view at once
    (nothing to refresh)."""
    with tempfile.TemporaryDirectory() as tmp:
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(),
            send_fn=lambda p, c, m: None, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=60)
        ok, _msg = viewer.start()
        assert ok
        assert done.wait(5)
        assert got["reason"] == "send_failed"
    print("live view send failure passed")


def test_live_view_status():
    with tempfile.TemporaryDirectory() as tmp:
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(),
            send_fn=lambda p, c, m: 7, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=60)
        st = viewer.status()
        assert st["running"] is False and st["refreshes"] == 0
        assert st["seconds_left"] == 60
        ok, _msg = viewer.start()
        assert ok
        time.sleep(0.2)
        st = viewer.status()
        assert st["running"] is True and st["refreshes"] >= 1
        assert st["seconds_left"] <= 60
        viewer.stop()
        assert done.wait(5)
    print("live view status passed")


def test_live_view_frame_file_cleaned_up():
    """One frame file is reused and deleted when the view ends."""
    with tempfile.TemporaryDirectory() as tmp:
        viewer, done, got = _viewer(
            tmp, capture_fn=_FakeCapture(),
            send_fn=lambda p, c, m: 7, edit_fn=lambda *a: True,
            refresh_seconds=0.05, max_seconds=60)
        viewer.start()
        time.sleep(0.15)
        viewer.stop()
        assert done.wait(5)
        assert not (Path(tmp) / "live").exists()
    print("live view frame file cleaned up passed")


if __name__ == "__main__":
    test_live_view_refreshes_one_message()
    test_live_view_start_twice_and_stop_idle()
    test_live_view_time_up()
    test_live_view_fail_fast()
    test_live_view_send_failure()
    test_live_view_status()
    test_live_view_frame_file_cleaned_up()
    print("ALL LIVE VIEW TESTS PASSED")
