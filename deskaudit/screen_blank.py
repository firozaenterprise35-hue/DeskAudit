"""Fullscreen white/black screen overlay (Telegram /screen).

Shows a solid white or black window over the whole virtual screen
(all monitors), always on top, with no border, taskbar button or
close box — so the PC cannot be used until the owner lifts it with
/screen off from Telegram. The window lives in a daemon thread of
the agent process: if the agent stops, the overlay dies with it.

Windows only (win32gui, already bundled in the frozen exe). On any
other platform start_blank() returns (False, reason) instead of
raising, so the bot can answer in Bengali.
"""

from __future__ import annotations

import sys
import threading

_COLORS = {"white": 0xFFFFFF, "black": 0x000000}


class ScreenBlank:
    """One overlay window, owned by a daemon thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._ready = threading.Event()
        self._color: str | None = None
        self._lock = threading.Lock()

    @property
    def active_color(self) -> str | None:
        return self._color

    def start(self, color: str) -> tuple[bool, str]:
        """Show the overlay in `color` ("white" or "black").
        Returns (ok, message). Starting a new colour replaces the
        current overlay."""
        if sys.platform != "win32":
            return False, "windows_only"
        if color not in _COLORS:
            return False, "bad_color"
        with self._lock:
            if self._color == color and self._thread is not None \
                    and self._thread.is_alive():
                return True, "already"
            self._stop_locked()
            self._ready.clear()
            self._thread = threading.Thread(target=self._run,
                                            args=(color,), daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                return False, "timeout"
            self._color = color
            return True, "ok"

    def stop(self) -> tuple[bool, str]:
        """Hide the overlay. Returns (was_active, message)."""
        if sys.platform != "win32":
            return False, "windows_only"
        with self._lock:
            if self._color is None:
                return False, "not_active"
            self._stop_locked()
            return True, "ok"

    def _stop_locked(self) -> None:
        if self._hwnd:
            try:
                import win32gui

                win32gui.PostMessage(self._hwnd, 0x0010, 0, 0)  # WM_CLOSE
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._hwnd = None
        self._color = None

    def _run(self, color: str) -> None:
        try:
            import win32con
            import win32gui

            hinst = win32gui.GetModuleHandle(None)
            cls_name = "DeskAuditScreenBlank"

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == win32con.WM_DESTROY:
                    win32gui.PostQuitMessage(0)
                    return 0
                return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

            try:
                wc = win32gui.WNDCLASS()
                wc.hInstance = hinst
                wc.lpszClassName = cls_name
                wc.lpfnWndProc = wndproc
                wc.hbrBackground = _COLORS[color]
                wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
                win32gui.RegisterClass(wc)
            except Exception:
                pass  # class may already be registered from an earlier run
            user32 = __import__("ctypes").windll.user32
            x0 = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            y0 = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            w = user32.GetSystemMetrics(78)    # SM_CXVIRTUALSCREEN
            h = user32.GetSystemMetrics(79)    # SM_CYVIRTUALSCREEN
            hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW,
                cls_name, "DeskAudit",
                win32con.WS_POPUP | win32con.WS_VISIBLE,
                x0, y0, w, h, 0, 0, hinst, None)
            self._hwnd = hwnd
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x0, y0, w, h,
                                  win32con.SWP_SHOWWINDOW)
            self._ready.set()
            win32gui.PumpMessages()
        except Exception:
            self._ready.set()
        finally:
            self._hwnd = None


# One overlay per agent process.
_blank = ScreenBlank()


def start_blank(color: str) -> tuple[bool, str]:
    """Show the fullscreen overlay. Returns (ok, message)."""
    return _blank.start(color)


def stop_blank() -> tuple[bool, str]:
    """Hide the overlay. Returns (was_active, message)."""
    return _blank.stop()


def blank_status() -> str | None:
    """Current overlay colour, or None when the screen is free."""
    return _blank.active_color
