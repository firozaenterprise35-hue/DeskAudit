"""Live screen view (v12.15) — one Telegram message whose photo is
refreshed in place, plus the dashboard's live card (which polls the
same capture through /api/live-shot).

A live view is NOT a video: it is a still photo, re-taken every
REFRESH_SECONDS and swapped into the same message with
editMessageMedia, so the owner's chat is never flooded with a new
photo per refresh. It runs on her command only (/live, or the
🔴 লাইভ দেখা menu button) and stops after MAX_SECONDS, on the
⏹️ button, on /live stop, or after three failed captures in a row
(locked screen is the usual cause) — whichever comes first. The
frame file is deleted when the view ends; like a screenshot,
nothing is kept afterwards.

The thread mirrors ScreenRecorder (screen_record.py): an RLock
guards the state, _finish marks the viewer stopped BEFORE calling
on_done, and every injectable (capture/send/edit) is faked in
tests/test_live_view.py so the timing runs on Linux too.
"""

import threading
import time
from datetime import datetime
from pathlib import Path

REFRESH_SECONDS = 10.0
MAX_SECONDS = 600  # a live view runs at most 10 minutes, then stops
MAX_CONSECUTIVE_FAILURES = 3


class LiveViewer:
    """One live-screen session.

    `send_fn(path, caption, reply_markup)` sends the first photo and
    returns its message id (None when the send failed).
    `edit_fn(message_id, path, caption, reply_markup)` swaps a fresh
    photo into that message and returns True on success.
    `on_done(reason, refreshes, last_error)` fires once, when the
    view ends — reason is "stopped", "time_up", "failed" or
    "send_failed". All three callables are injected in tests.
    """

    def __init__(self, run_dir, log=None, capture_fn=None, send_fn=None,
                 edit_fn=None, on_done=None, reply_markup=None,
                 refresh_seconds=REFRESH_SECONDS,
                 max_seconds=MAX_SECONDS):
        self._run_dir = Path(run_dir)
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._log = log
        self._capture = capture_fn
        self._send = send_fn
        self._edit = edit_fn
        self._on_done = on_done
        # The inline keyboard under the live photo (the ⏹️ stop
        # button) — sent with the first photo and with every refresh.
        self._reply_markup = reply_markup
        self._refresh_seconds = refresh_seconds
        self._max_seconds = max_seconds
        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()
        self._started_at = None
        self._message_id = None
        self._refreshes = 0
        self._last_error = None
        self._consecutive_failures = 0

    @property
    def _frame_path(self) -> Path:
        # One file, overwritten every refresh — a live view keeps no
        # history, so there is never more than one frame on disk.
        return self._run_dir / "live.jpg"

    def start(self) -> tuple[bool, str]:
        """Start the view. Returns (False, reason) when one is
        already running."""
        with self._lock:
            if self._thread is not None:
                return False, "already running"
            if self._capture is None:
                from .screen_record import capture_jpeg_frame

                self._capture = capture_jpeg_frame
            self._stop.clear()
            self._started_at = time.time()
            self._message_id = None
            self._refreshes = 0
            self._last_error = None
            self._consecutive_failures = 0
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "started"

    def stop(self) -> tuple[bool, str]:
        """Stop the view early. The thread sends the closing message
        through on_done; returns (False, reason) when nothing runs."""
        with self._lock:
            if self._thread is None:
                return False, "not running"
        self._stop.set()
        return True, "stopping"

    def status(self) -> dict:
        """Current state — safe to call from any thread."""
        with self._lock:
            running = self._thread is not None
            elapsed = 0
            if self._started_at is not None:
                elapsed = int(time.time() - self._started_at)
            return {
                "running": running,
                "refreshes": self._refreshes,
                "elapsed_seconds": elapsed,
                "seconds_left": max(0, self._max_seconds - elapsed),
                "last_error": self._last_error,
            }

    # -- internals ---------------------------------------------------
    def _caption(self) -> str:
        # The clock in the caption proves the photo is fresh — a
        # stale frame would show an old time.
        return ("🔴 লাইভ স্ক্রিন — "
                f"{datetime.now().strftime('%H:%M:%S')}")

    def _run(self):
        # Cleanup runs BEFORE _finish: by the time on_done tells the
        # owner the view ended, the frame file is already gone.
        reason = self._run_loop()
        try:
            self._frame_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            self._run_dir.rmdir()
        except Exception:
            pass
        self._finish(reason)

    def _run_loop(self) -> str:
        end = (self._started_at or time.time()) + self._max_seconds
        # Frame 0: capture, then send it as the live message.
        try:
            frame = self._capture(self._frame_path)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return "failed"
        try:
            message_id = (self._send(frame, self._caption(),
                                     self._reply_markup)
                          if self._send is not None else None)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            return "send_failed"
        if not message_id:
            return "send_failed"
        with self._lock:
            self._message_id = message_id
        index = 1
        while not self._stop.is_set():
            now = time.time()
            if now >= end:
                return "time_up"
            due = (self._started_at or now) + index * self._refresh_seconds
            remaining = due - time.time()
            if remaining > 0:
                self._stop.wait(remaining)
            if self._stop.is_set():
                break
            if time.time() >= end:
                return "time_up"
            try:
                frame = self._capture(self._frame_path)
                ok = (self._edit(self._message_id, frame,
                                 self._caption(), self._reply_markup)
                      if self._edit is not None else False)
                if not ok:
                    raise RuntimeError("live photo could not be updated")
                with self._lock:
                    self._refreshes += 1
                    self._last_error = None
                    self._consecutive_failures = 0
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)
                    self._consecutive_failures += 1
                    failed = self._consecutive_failures
                if self._log is not None:
                    self._log.warning("Live view refresh failed: %s",
                                      exc, exc_info=True)
                if failed >= MAX_CONSECUTIVE_FAILURES:
                    # The screen cannot be captured at all (locked
                    # screen is the usual cause) — stop now and
                    # report the reason instead of retrying until
                    # the 10 minutes run out.
                    return "failed"
            index += 1
        return "stopped"

    def _finish(self, reason: str):
        # Mark the view finished BEFORE notifying: status() must
        # already say "not running" when on_done fires, and a new
        # /live may start at once.
        with self._lock:
            self._thread = None
            refreshes = self._refreshes
            last_error = self._last_error
        if self._on_done is not None:
            try:
                self._on_done(reason, refreshes, last_error)
            except Exception as exc:
                if self._log is not None:
                    self._log.warning("Live view done failed: %s", exc)
