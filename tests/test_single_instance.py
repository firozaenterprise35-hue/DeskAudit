"""Single-instance guard tests (v12.14).

Two DeskAudit.exe processes used to run side by side (Windows startup
+ Start Menu click, or installed + portable copy) — both polled Telegram
with the same stored offset and both sent every alert, so every command
reply and every "new program" message arrived twice. The agent now
stops itself at once when another agent already holds the lock.
"""

import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.alerts import notify_windows_text  # noqa: E402
from deskaudit.backup import (  # noqa: E402
    acquire_agent_lock,
    agent_is_running,
    release_agent_lock,
    try_acquire_agent_lock,
)
from deskaudit.config import Config  # noqa: E402


def test_try_acquire_agent_lock_held():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fh1, reason1 = try_acquire_agent_lock(root)
        assert fh1 is not None and reason1 == "ok"
        assert agent_is_running(root) is True
        fh2, reason2 = try_acquire_agent_lock(root)
        assert fh2 is None and reason2 == "held"
        # The old wrapper still reports "already held" as None.
        assert acquire_agent_lock(root) is None
        release_agent_lock(fh1)
        assert agent_is_running(root) is False
        fh3, reason3 = try_acquire_agent_lock(root)
        assert fh3 is not None and reason3 == "ok"
        release_agent_lock(fh3)
    print("try_acquire_agent_lock held/ok passed")


def test_run_agent_stops_when_lock_held():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "config.yaml").write_text("", encoding="utf-8")
        holder, _reason = try_acquire_agent_lock(root)
        assert holder is not None
        try:
            from main import run_agent

            config = Config.load(root / "config.yaml")
            log = logging.getLogger("deskaudit-test-single-instance")
            # Must return 2 at once — before starting the watcher,
            # the dashboard, Telegram polling or any tracking.
            assert run_agent(config, log) == 2
        finally:
            release_agent_lock(holder)
    print("run_agent stops when lock held passed")


def test_notify_windows_text_no_crash_on_linux():
    # On Linux there is no toast — it must return False, never raise.
    assert notify_windows_text("শিরোনাম", "বার্তা") is False
    print("notify_windows_text linux no-crash passed")
