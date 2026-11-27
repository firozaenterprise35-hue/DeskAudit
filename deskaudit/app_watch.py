"""Real-time new-program alerts (Telegram, v12.5).

Watches the process list: the agent snapshots every running process
once at startup (the baseline), then on every tick reports processes
that started AFTER the agent — i.e. software somebody opened while
DeskAudit runs. Each new program is reported to the owner's Telegram
immediately: which program, its exe path, its open window title, when
it opened, and which project it belongs to.

Metadata only, like everything else in DeskAudit: the alert says
WHICH program and WHICH window is open — never what anyone types or
writes inside it, no message/email text, no passwords, no clipboard.

System and background processes never alert (blocklist below), the
same exe is not re-reported within `cooldown_minutes`, and at most
`max_per_tick` messages go out per tick so a login-time burst cannot
spam the owner's Telegram.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

DEFAULT_APP_WATCH = {
    "enabled": True,
    "notify_new_app": True,
    "cooldown_minutes": 10,
    "max_per_tick": 5,
}

# Windows system / background processes that are never "new software".
_SYSTEM_PROCS = {
    "system", "idle", "registry", "smss.exe", "csrss.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "taskhostw.exe", "sihost.exe", "ctfmon.exe", "searchindexer.exe",
    "runtimebroker.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "backgroundtaskhost.exe",
    "dllhost.exe", "conhost.exe", "spoolsv.exe", "searchhost.exe",
    "widgets.exe", "widgetservice.exe", "audiodg.exe", "wudfhost.exe",
    "dashost.exe", "lsalso.exe", "memory compression", "secure system",
    "deskaudit.exe", "desk audit.exe",
}


def _iter_processes():
    """Current processes as small namespaces (pid, name, exe, create_time).

    psutil is the primary source. If it yields nothing (psutil's
    Windows backend does not work under Wine — NtQuerySystemInformation
    fails there — and psutil may be missing), fall back to enumerating
    visible windows with win32gui, which works everywhere on Windows.
    """
    procs = _iter_processes_psutil()
    if procs:
        return procs
    return _iter_windows_processes()


def _iter_processes_psutil():
    try:
        import psutil
    except Exception:
        return []
    out = []
    try:
        for p in psutil.process_iter(["pid", "name", "exe", "create_time"]):
            try:
                info = p.info
                out.append(SimpleNamespace(
                    pid=int(info.get("pid") or 0),
                    name=str(info.get("name") or ""),
                    exe=str(info.get("exe") or ""),
                    create_time=float(info.get("create_time") or 0.0),
                    title="",
                ))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _iter_windows_processes():
    """One entry per process that owns a visible top-level window.

    Windows-only (win32gui); returns [] anywhere else or on failure.
    The entry carries the window title too, so the alert can show it
    without a second lookup.
    """
    try:
        import os
        import sys
        if sys.platform != "win32":
            return []
        import win32api
        import win32con
        import win32gui
        import win32process

        found: dict[int, SimpleNamespace] = {}

        def _enum(hwnd, _arg):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if pid in found:
                return
            try:
                title = win32gui.GetWindowText(hwnd) or ""
            except Exception:
                title = ""
            name, exe = "", ""
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                    False, pid)
                try:
                    exe = win32process.GetModuleFileNameEx(handle, 0) or ""
                finally:
                    win32api.CloseHandle(handle)
                name = os.path.basename(exe) if exe else ""
            except Exception:
                pass
            if not name and not title:
                return
            found[pid] = SimpleNamespace(pid=int(pid), name=name, exe=exe,
                                        create_time=0.0, title=title)

        win32gui.EnumWindows(_enum, None)
        return list(found.values())
    except Exception:
        return []


def window_title_for_pid(pid: int) -> str:
    """Title of the pid's first visible top-level window ("" if none).

    Windows-only (win32gui); best effort, never raises.
    """
    try:
        import sys
        if sys.platform != "win32":
            return ""
        import win32con
        import win32gui
        import win32process

        found = []

        def _enum(hwnd, _arg):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if wpid == pid:
                try:
                    title = win32gui.GetWindowText(hwnd) or ""
                except Exception:
                    title = ""
                if title:
                    found.append(title)

        win32gui.EnumWindows(_enum, None)
        return found[0] if found else ""
    except Exception:
        return ""


class AppWatch:
    """Baseline + new-process detection. Pure logic; psutil is only
    touched by the default process reader, so tests inject fakes."""

    def __init__(self, settings: dict | None = None, log=None) -> None:
        self.settings = {**DEFAULT_APP_WATCH, **(settings or {})}
        self.log = log
        self._seen: set[int] = set()
        self._last_alert: dict[str, float] = {}
        self._baselined = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True)
                    and self.settings.get("notify_new_app", True))

    def baseline(self, processes=None) -> int:
        """Snapshot the processes running NOW. Returns how many were seen."""
        if processes is None:
            processes = _iter_processes()
        self._seen = {int(p.pid) for p in processes if int(p.pid) > 0}
        self._baselined = True
        return len(self._seen)

    def check(self, processes=None) -> list[dict]:
        """Processes that started since the baseline (or since the last
        check). Returns alert dicts, at most max_per_tick. Never raises."""
        try:
            if processes is None:
                processes = _iter_processes()
            if not self._baselined:
                self.baseline(processes)
                return []
            now = time.time()
            cooldown = float(self.settings.get("cooldown_minutes", 10) or 10) * 60
            max_per_tick = int(self.settings.get("max_per_tick", 5) or 5)
            alerts: list[dict] = []
            current = {int(p.pid) for p in processes if int(p.pid) > 0}
            for p in processes:
                pid = int(p.pid)
                if pid <= 0 or pid in self._seen:
                    continue
                name = (p.name or "").strip()
                key = name.lower()
                if key in _SYSTEM_PROCS or not name:
                    continue  # background noise: record it, never alert
                last = self._last_alert.get(key, 0.0)
                if now - last < cooldown:
                    continue  # same program restarted recently
                if len(alerts) >= max_per_tick:
                    continue  # cap the burst; the rest are still marked seen
                self._last_alert[key] = now
                alerts.append({
                    "pid": pid,
                    "name": name,
                    "exe": (p.exe or "").strip(),
                    "title": (getattr(p, "title", "") or "").strip()
                             or window_title_for_pid(pid),
                    "started_at": time.strftime("%H:%M:%S", time.localtime(
                        p.create_time or now)),
                })
            self._seen = current
            return alerts
        except Exception as exc:
            if self.log is not None:
                try:
                    self.log.warning("App watch check failed: %s", exc)
                except Exception:
                    pass
            return []
