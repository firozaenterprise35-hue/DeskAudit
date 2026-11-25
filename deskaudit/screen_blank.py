"""Fullscreen white/black screen overlay (Telegram /screen).

Shows a solid white or black window over the whole virtual screen
(all monitors), always on top, with no border, taskbar button or
close box — so the PC cannot be used until the owner lifts it with
/screen off from Telegram. The window lives in a daemon thread of
the agent process: if the agent stops, the overlay dies with it.

Windows only (win32gui, already bundled in the frozen exe). On any
other platform start_blank() returns (False, reason) instead of
raising, so the bot can answer in Bengali.

Audit note (v12.3 bug, fixed here): the first version set
WNDCLASS.hbrBackground to a raw colour number (0xFFFFFF / 0x000000).
That field must hold a BRUSH handle — a colour number is an invalid
handle (and 0x000000 is NULL, "do not erase"), so Windows never
painted the window and the overlay stayed invisible. This version
creates a real solid brush with CreateSolidBrush, paints with it on
WM_ERASEBKGND and WM_PAINT, and uses one window class per colour
(so switching colours can never reuse a stale class).
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
        self._brush = None
        self._ready = threading.Event()
        self._failed = False
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
            self._failed = False
            self._thread = threading.Thread(target=self._run,
                                            args=(color,), daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                return False, "timeout"
            if self._failed or not self._hwnd:
                self._stop_locked()
                return False, "create_failed"
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
        if self._brush:
            try:
                import win32gui

                win32gui.DeleteObject(self._brush)
            except Exception:
                pass
            self._brush = None

    def _run(self, color: str) -> None:
        try:
            import win32con
            import win32gui

            hinst = win32gui.GetModuleHandle(None)
            # One class per colour: a registered class keeps its brush,
            # so reusing one class name could never switch colours.
            cls_name = f"DeskAuditScreenBlank_{color}"
            # A real brush handle — NOT the colour number (v12.3 bug).
            brush = win32gui.CreateSolidBrush(_COLORS[color])
            self._brush = brush

            def wndproc(hwnd, msg, wparam, lparam):
                if msg == win32con.WM_ERASEBKGND:
                    try:
                        hdc = wparam
                        rect = win32gui.GetClientRect(hwnd)
                        win32gui.FillRect(hdc, rect, brush)
                    except Exception:
                        pass
                    return 1
                if msg == win32con.WM_PAINT:
                    try:
                        hdc, ps = win32gui.BeginPaint(hwnd)
                        try:
                            rect = win32gui.GetClientRect(hwnd)
                            win32gui.FillRect(hdc, rect, brush)
                        finally:
                            win32gui.EndPaint(hwnd, ps)
                    except Exception:
                        pass
                    return 0
                if msg == win32con.WM_DESTROY:
                    win32gui.PostQuitMessage(0)
                    return 0
                return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

            try:
                wc = win32gui.WNDCLASS()
                wc.hInstance = hinst
                wc.lpszClassName = cls_name
                wc.lpfnWndProc = wndproc
                wc.hbrBackground = brush
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
            if not hwnd:
                self._failed = True
                self._ready.set()
                return
            self._hwnd = hwnd
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, x0, y0, w, h,
                                  win32con.SWP_SHOWWINDOW)
            win32gui.InvalidateRect(hwnd, None, True)
            win32gui.UpdateWindow(hwnd)
            self._ready.set()
            win32gui.PumpMessages()
        except Exception:
            self._failed = True
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
