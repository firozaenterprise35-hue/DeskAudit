"""Foreground app / window tracker (Windows, with a safe fallback).

Every poll (default 5s) it asks Windows which window is in front, resolves
its process name + exe path with psutil, and keeps one open DB session per
(app, window title). When the app/title changes, or the user goes idle and
comes back, the old session is closed and a new one starts.

Idle time inside a session is stored separately, so reports can show
active time vs. total time.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime

from .database import Database
from .idle import get_idle_seconds

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore

# pywin32 is Windows-only; import lazily so `python -m py_compile` and the
# smoke test also work on Linux / macOS.
win32gui = win32process = None
if sys.platform == "win32":
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        win32gui = win32process = None


@dataclass
class ActiveWindow:
    app_name: str
    exe_path: str
    title: str


def get_active_window() -> ActiveWindow:
    """Return the foreground app. Falls back to 'unknown' off-Windows."""
    if win32gui is None or win32process is None:
        return ActiveWindow("unknown", "", "")
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app_name, exe_path = "unknown", ""
        if psutil is not None:
            try:
                proc = psutil.Process(pid)
                app_name = proc.name()
                exe_path = proc.exe()
            except Exception:
                pass
        return ActiveWindow(app_name, exe_path, title[:200])
    except Exception:
        return ActiveWindow("unknown", "", "")


class AppTracker:
    def __init__(self, db: Database, idle_threshold_seconds: int = 300, tagger=None):
        self.db = db
        self.idle_threshold = idle_threshold_seconds
        self.tagger = tagger
        self._session_id: int | None = None
        self._current: ActiveWindow | None = None
        self._started_at: datetime | None = None
        self._active = 0.0
        self._idle = 0.0

    def _close_session(self, now: datetime) -> None:
        if self._session_id is None or self._started_at is None:
            return
        duration = (now - self._started_at).total_seconds()
        self.db.close_app_session(
            self._session_id, now, duration, self._active, self._idle
        )
        self._session_id = None
        self._current = None
        self._started_at = None
        self._active = 0.0
        self._idle = 0.0

    def flush(self, now: datetime | None = None) -> None:
        """Write the open session's accumulated durations into the DB
        without closing it. Called every poll: if the agent is force-
        stopped, the PC reboots, or the process crashes, the current
        session keeps everything up to the last poll instead of losing
        the whole stretch (durations used to live only in memory)."""
        if self._session_id is None or self._started_at is None:
            return
        now = now or datetime.now().astimezone()
        duration = (now - self._started_at).total_seconds()
        try:
            self.db.update_app_session(self._session_id, now, duration,
                                       self._active, self._idle)
        except Exception:
            pass

    def tick(self, poll_interval: float = 5.0) -> ActiveWindow:
        """One poll. Returns the current foreground window."""
        now = datetime.now().astimezone()
        window = get_active_window()
        idle = get_idle_seconds() >= self.idle_threshold

        key = (window.app_name, window.title)
        current_key = (
            (self._current.app_name, self._current.title) if self._current else None
        )

        if key != current_key:
            self._close_session(now)
            project, tags = ("Untagged", [])
            if self.tagger is not None:
                project, tags = self.tagger.tag_app(
                    window.app_name, window.exe_path, window.title)
            self._session_id = self.db.start_app_session(
                window.app_name, window.exe_path, window.title, now,
                project=project, tags=tags,
            )
            self._current = window
            self._started_at = now

        if idle:
            self._idle += poll_interval
        else:
            self._active += poll_interval
        self.flush(now)
        return window

    def stop(self) -> None:
        self._close_session(datetime.now().astimezone())
