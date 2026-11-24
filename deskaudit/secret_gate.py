"""Secret gate — open the dashboard with a hidden gesture.

The admin clicks N times on the desktop (any empty desktop spot), then
types a short code on the keyboard (default: 4 clicks, then ``1122``).
The dashboard (http://127.0.0.1:8000/) opens in the default browser.

Only the DeskAudit agent process listens for this; nothing is typed
anywhere else, no keystrokes are logged, and the gate only reacts to
the exact click count followed by the exact code. It is configured in
config.yaml under ``secret_gate:`` and can be changed from the
dashboard Settings page.

Windows only: it uses low-level mouse/keyboard hooks via ctypes +
user32 directly (pywin32 does not wrap SetWindowsHookEx). On any
other platform, or if the hooks cannot be installed (e.g. under Wine
with no desktop), start_secret_gate() logs a warning and returns None
— the agent keeps running without the gate.
"""

from __future__ import annotations

import logging
import threading
import time
import webbrowser

DEFAULT_SECRET_GATE = {
    "enabled": True,
    "clicks": 4,
    "code": "1122",
    "click_window_seconds": 2,
    "arm_seconds": 10,
}

# Window classes that mean "this click landed on the desktop itself"
# (wallpaper / desktop icons), not inside an app window.
DESKTOP_CLASSES = {"Progman", "WorkerW", "SHELLDLL_DefView", "SysListView32"}


class SecretGate:
    """Pure click/code state machine — no Windows calls, fully testable."""

    def __init__(self, clicks: int = 4, code: str = "1122",
                 click_window_seconds: float = 2.0, arm_seconds: float = 10.0,
                 on_trigger=None, clock=None):
        self.clicks_needed = max(1, int(clicks))
        self.code = str(code or "")
        self.click_window = float(click_window_seconds)
        self.arm_seconds = float(arm_seconds)
        self.on_trigger = on_trigger
        self._clock = clock or time.monotonic
        self._click_times: list[float] = []
        self._armed_until: float = 0.0
        self._typed: str = ""

    def _now(self) -> float:
        return self._clock()

    def is_armed(self) -> bool:
        return self._now() < self._armed_until

    def click(self, on_desktop: bool) -> bool:
        """Record one left click. Returns True when the gate just armed."""
        now = self._now()
        if self.is_armed():
            return False  # already armed — clicks do not re-arm
        if not on_desktop:
            self._click_times.clear()
            return False
        self._click_times.append(now)
        # Keep only clicks inside the window.
        self._click_times = [t for t in self._click_times if now - t <= self.click_window]
        if len(self._click_times) >= self.clicks_needed:
            self._click_times.clear()
            self._armed_until = now + self.arm_seconds
            self._typed = ""
            return True
        return False

    def key(self, char: str) -> bool:
        """Record one typed character. Returns True when the code matched."""
        now = self._now()
        if not self.is_armed():
            self._typed = ""
            return False
        self._typed += char
        if self.code.startswith(self._typed):
            if self._typed == self.code:
                self._armed_until = 0.0
                self._typed = ""
                if self.on_trigger is not None:
                    self.on_trigger()
                return True
            return False
        # Wrong character — the attempt is over.
        self._armed_until = 0.0
        self._typed = ""
        return False

    def disarm(self) -> None:
        self._armed_until = 0.0
        self._typed = ""
        self._click_times.clear()


class SecretGateListener:
    """Windows low-level hooks feeding a SecretGate (daemon thread).

    Uses ctypes + user32 directly (stdlib only): pywin32's win32gui does
    NOT wrap SetWindowsHookEx/CallNextHookEx, so the hook lives here.
    The callback objects are kept alive on self — if they were freed,
    Windows would call into dead memory and crash the process.
    """

    def __init__(self, gate: SecretGate, url: str, log: logging.Logger | None = None):
        self.gate = gate
        self.url = url
        self.log = log or logging.getLogger("deskaudit")
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._mouse_hook = None
        self._key_hook = None
        self._mouse_proc = None
        self._key_proc = None
        self._stopped = threading.Event()

    @property
    def clicks_needed(self) -> int:
        return self.gate.clicks_needed

    @property
    def code(self) -> str:
        return self.gate.code

    def start(self) -> bool:
        """Install the hooks. Returns False (no crash) when unavailable."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception as exc:  # not Windows, or user32 missing
            self.log.warning("Secret gate unavailable (user32 missing): %s", exc)
            return False

        WH_MOUSE_LL = 14
        WH_KEYBOARD_LL = 13
        WM_LBUTTONDOWN = 0x0201
        WM_KEYDOWN = 0x0100
        WM_QUIT = 0x0012

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM)

        def _class_at(x: int, y: int) -> str:
            try:
                pt = wintypes.POINT(x, y)
                hwnd = user32.WindowFromPoint(pt)
                if not hwnd:
                    return ""
                buf = ctypes.create_unicode_buffer(256)
                if user32.GetClassNameW(hwnd, buf, 256):
                    return buf.value
            except Exception:
                pass
            return ""

        def _on_mouse(ncode, wp, lp):
            try:
                if ncode >= 0 and wp == WM_LBUTTONDOWN:
                    info = ctypes.cast(lp, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if self.gate.click(_class_at(info.pt.x, info.pt.y) in DESKTOP_CLASSES):
                        self.log.info("Secret gate armed — type the code to open the dashboard.")
            except Exception:
                pass
            return user32.CallNextHookEx(self._mouse_hook, ncode, wp, lp)

        def _on_key(ncode, wp, lp):
            try:
                if ncode >= 0 and wp == WM_KEYDOWN:
                    vk = ctypes.cast(lp, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.vkCode
                    char = ""
                    if 0x30 <= vk <= 0x39:  # top-row digits
                        char = chr(ord("0") + (vk - 0x30))
                    elif 0x60 <= vk <= 0x69:  # numpad digits
                        char = chr(ord("0") + (vk - 0x60))
                    if char and self.gate.key(char):
                        self.log.info("Secret gate matched — opening the dashboard.")
                        try:
                            webbrowser.open(self.url)
                        except Exception:
                            pass
            except Exception:
                pass
            return user32.CallNextHookEx(self._key_hook, ncode, wp, lp)

        # Keep the callbacks alive for the life of the process.
        self._mouse_proc = HOOKPROC(_on_mouse)
        self._key_proc = HOOKPROC(_on_key)

        def _run():
            try:
                self._thread_id = kernel32.GetCurrentThreadId()
                self._mouse_hook = user32.SetWindowsHookExW(
                    WH_MOUSE_LL, self._mouse_proc, None, 0)
                self._key_hook = user32.SetWindowsHookExW(
                    WH_KEYBOARD_LL, self._key_proc, None, 0)
                if not self._mouse_hook or not self._key_hook:
                    raise RuntimeError("SetWindowsHookEx returned no hook handle")
                msg = wintypes.MSG()
                while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as exc:
                self.log.warning("Secret gate hooks failed (gate off): %s", exc)
            finally:
                for hook in (self._mouse_hook, self._key_hook):
                    if hook:
                        try:
                            user32.UnhookWindowsHookEx(hook)
                        except Exception:
                            pass
                self._stopped.set()

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name="deskaudit-secret-gate")
        self._thread.start()
        # Give the thread a moment to report a hook failure.
        self._stopped.wait(timeout=1.0)
        if self._stopped.is_set():
            self.log.warning("Secret gate could not start (no desktop hooks).")
            return False
        return True

    def stop(self) -> None:
        if self._thread_id is None:
            return
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        except Exception:
            pass


def start_secret_gate(settings: dict | None, url: str,
                      log: logging.Logger | None = None) -> SecretGateListener | None:
    """Build a gate from config and start it. None when disabled/unavailable."""
    merged = {**DEFAULT_SECRET_GATE, **(settings or {})}
    if not merged.get("enabled", True):
        return None
    gate = SecretGate(
        clicks=int(merged.get("clicks", 4)),
        code=str(merged.get("code", "1122")),
        click_window_seconds=float(merged.get("click_window_seconds", 2)),
        arm_seconds=float(merged.get("arm_seconds", 10)),
    )
    listener = SecretGateListener(gate, url, log=log)
    return listener if listener.start() else None
