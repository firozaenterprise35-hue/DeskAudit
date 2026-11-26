"""Small helpers shared by the trackers and the report generator."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone


def format_duration(seconds: float) -> str:
    """3599 -> '59m', 7320 -> '2h 2m', 45 -> '45s'."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def run_hidden(argv, **kwargs):
    """Run a child process with NO console window on Windows.

    Audit note (v12.5): the old code launched PowerShell (alert toasts),
    ipconfig, shutdown, tasklist/taskkill and schtasks with plain
    subprocess.run — from the windowed exe that spawns a fresh console
    window which flashes on screen every time an alert fires (every
    30 min). CREATE_NO_WINDOW + a hidden STARTUPINFO keep every launch
    invisible. Off Windows it is a plain subprocess.run.
    """
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        if "startupinfo" not in kwargs:
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = info
    return subprocess.run(argv, **kwargs)


def today_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")
