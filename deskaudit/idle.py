"""Idle detection.

Windows: call GetLastInputInfo via ctypes — no extra dependency.
Other OS / failure: return 0 so the agent never crashes for this.
"""

from __future__ import annotations

import ctypes
import sys
import time


def get_idle_seconds() -> float:
    if sys.platform != "win32":
        return 0.0
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # GetTickCount (32-bit) and dwTime share the same clock, so
        # their difference mod 2^32 stays correct even when the
        # counter wraps every ~49.7 days of uptime (v12.13 audit,
        # M8). The old code subtracted the 32-bit dwTime from the
        # 64-bit GetTickCount64 — after the first wrap that read
        # ~49 days of idle, forever, until reboot.
        tick = ctypes.windll.kernel32.GetTickCount()
        millis = (tick - info.dwTime) & 0xFFFFFFFF
        return max(0.0, millis / 1000.0)
    except Exception:
        return 0.0


def is_idle(threshold_seconds: int = 300) -> bool:
    return get_idle_seconds() >= threshold_seconds


if __name__ == "__main__":
    print(f"idle: {get_idle_seconds():.1f}s (now={time.time():.0f})")
