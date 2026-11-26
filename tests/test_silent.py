"""Tests for silent child-process launches (v12.5).

Audit finding: the alert toast launched PowerShell with plain
subprocess.run, so a console window flashed on screen every time an
alert fired. run_hidden() must pass CREATE_NO_WINDOW + a hidden
STARTUPINFO on Windows, and behave like a plain run elsewhere.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit import utils


def test_run_hidden_windows_flags():
    """On Windows the helper must hide the window (faked platform)."""
    calls = {}

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    def _recorder(argv, **kw):
        calls["kw"] = kw
        return SimpleNamespace(returncode=0)

    fake_sub = SimpleNamespace(
        CREATE_NO_WINDOW=0x08000000,
        STARTF_USESHOWWINDOW=0x00000001,
        SW_HIDE=0,
        STARTUPINFO=FakeStartupInfo,
        run=_recorder,
    )
    fake_sys = SimpleNamespace(platform="win32")
    old_sub, old_sys = utils.subprocess, utils.sys
    utils.subprocess, utils.sys = fake_sub, fake_sys
    try:
        utils.run_hidden(["powershell", "-x"], capture_output=True)
    finally:
        utils.subprocess, utils.sys = old_sub, old_sys
    kw = calls["kw"]
    assert kw.get("creationflags") == 0x08000000
    info = kw.get("startupinfo")
    assert info is not None
    assert info.dwFlags & 0x00000001
    assert info.wShowWindow == 0
    assert kw.get("capture_output") is True
    print("run_hidden windows flags passed")


def test_run_hidden_linux_passthrough():
    """Off Windows it is a plain run with no extra flags."""
    out = utils.run_hidden(["echo", "hi"], capture_output=True, text=True)
    assert out.returncode == 0
    assert out.stdout.strip() == "hi"
    print("run_hidden linux passthrough passed")


def test_alerts_powershell_uses_run_hidden():
    """notify_windows must call run_hidden with -WindowStyle Hidden."""
    import deskaudit.alerts as alerts
    from deskaudit.alerts import Alert

    calls = {}

    def _recorder(argv, **kw):
        calls["argv"] = argv
        return SimpleNamespace(returncode=0)

    old_run_hidden = utils.run_hidden
    utils.run_hidden = _recorder
    old_sys = alerts.sys
    alerts.sys = SimpleNamespace(platform="win32")
    try:
        assert alerts.notify_windows([Alert("t", "m", "warning", "2026-11-26")]) is True
    finally:
        utils.run_hidden = old_run_hidden
        alerts.sys = old_sys
    argv = calls["argv"]
    assert argv[0] == "powershell"
    assert "-WindowStyle" in argv and "Hidden" in argv
    assert "-NoProfile" in argv
    print("alerts powershell uses run_hidden passed")


def test_flush_dns_uses_run_hidden():
    """_flush_dns must go through run_hidden (no console flash)."""
    import deskaudit.telegram as tg

    calls = {}

    def _recorder(argv, **kw):
        calls["argv"] = argv

    old_run_hidden = utils.run_hidden
    utils.run_hidden = _recorder
    old_sys = tg.sys
    tg.sys = SimpleNamespace(platform="win32")
    try:
        tg._flush_dns()
    finally:
        utils.run_hidden = old_run_hidden
        tg.sys = old_sys
    assert calls["argv"] == ["ipconfig", "/flushdns"]
    print("flush_dns uses run_hidden passed")


if __name__ == "__main__":
    test_run_hidden_windows_flags()
    test_run_hidden_linux_passthrough()
    test_alerts_powershell_uses_run_hidden()
    test_flush_dns_uses_run_hidden()
    print("ALL SILENT TESTS PASSED")
