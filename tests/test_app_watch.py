"""Tests for the real-time new-program watch (v12.5) + silent launches.

The watch is pure logic: processes are injected as small namespaces,
so no real processes are needed. The Telegram send path uses the same
recorded-network pattern as the other Telegram tests.
"""

import os
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit.app_watch import DEFAULT_APP_WATCH, AppWatch
from deskaudit.database import Database


def _proc(pid, name, exe="", create_time=1700000000.0):
    return SimpleNamespace(pid=pid, name=name, exe=exe, create_time=create_time)


def test_baseline_then_new():
    watch = AppWatch()
    assert watch.baseline([_proc(10, "explorer.exe"), _proc(20, "chrome.exe")]) == 2
    alerts = watch.check([_proc(10, "explorer.exe"), _proc(20, "chrome.exe"),
                          _proc(30, "notepad.exe", "C:\\Windows\\notepad.exe")])
    assert len(alerts) == 1
    a = alerts[0]
    assert a["name"] == "notepad.exe"
    assert a["exe"] == "C:\\Windows\\notepad.exe"
    assert a["pid"] == 30
    assert a["started_at"]  # HH:MM:SS
    assert a["title"] == ""  # off Windows there is no window to look up
    # Second tick: nothing new.
    assert watch.check([_proc(10, "explorer.exe"), _proc(20, "chrome.exe"),
                        _proc(30, "notepad.exe", "C:\\Windows\\notepad.exe")]) == []
    print("baseline then new passed")


def test_system_processes_skipped():
    watch = AppWatch()
    watch.baseline([_proc(10, "explorer.exe")])
    alerts = watch.check([_proc(10, "explorer.exe"), _proc(11, "svchost.exe"),
                          _proc(12, "dwm.exe"), _proc(13, "DeskAudit.exe"),
                          _proc(14, "conhost.exe")])
    assert alerts == []
    print("system processes skipped passed")


def test_cooldown():
    watch = AppWatch({"cooldown_minutes": 10})
    watch.baseline([_proc(10, "explorer.exe")])
    assert len(watch.check([_proc(10, "explorer.exe"),
                            _proc(30, "notepad.exe")])) == 1
    # Notepad closed and reopened (new pid) within the cooldown: silent.
    assert watch.check([_proc(10, "explorer.exe"),
                        _proc(31, "notepad.exe")]) == []
    # After the cooldown it alerts again.
    watch._last_alert["notepad.exe"] = time.time() - 11 * 60
    assert len(watch.check([_proc(10, "explorer.exe"),
                            _proc(32, "notepad.exe")])) == 1
    print("cooldown passed")


def test_max_per_tick():
    watch = AppWatch({"max_per_tick": 5})
    watch.baseline([_proc(10, "explorer.exe")])
    procs = [_proc(10, "explorer.exe")]
    procs += [_proc(100 + i, f"app{i}.exe") for i in range(7)]
    alerts = watch.check(procs)
    assert len(alerts) == 5
    # The capped ones are still marked seen — they never alert later.
    assert watch.check(procs) == []
    print("max per tick passed")


def test_check_never_raises():
    watch = AppWatch()
    watch.baseline([_proc(10, "explorer.exe")])
    assert watch.check([None]) == []
    assert watch.check("not-a-list") == []
    print("check never raises passed")


def test_build_new_app_text():
    from deskaudit.telegram import build_new_app_text

    text = build_new_app_text({"name": "notepad.exe",
                               "exe": "C:\\Windows\\notepad.exe",
                               "title": "রিপোর্ট.docx - Word",
                               "started_at": "15:02:11"}, "Office")
    assert "নতুন প্রোগ্রাম" in text
    assert "notepad.exe" in text
    assert "C:\\Windows\\notepad.exe" in text
    assert "রিপোর্ট.docx - Word" in text
    assert "15:02:11" in text
    assert "Office" in text
    text2 = build_new_app_text({"name": "calc.exe", "exe": "", "title": "",
                                "started_at": ""}, "Untagged")
    assert "কোনো উইন্ডো খোলা হয়নি" in text2
    print("build new app text passed")


def test_notify_new_app():
    from deskaudit.projects import Tagger
    from deskaudit.telegram import TelegramBot
    import deskaudit.telegram as tg

    sent = []
    old_send = tg.send_message
    tg.send_message = lambda token, chat_id, text: sent.append(text) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(os.path.join(tmp, "t.db"))
            import logging
            bot = TelegramBot(db, {"enabled": True, "bot_token": "T",
                                   "chat_id": "123", "poll_seconds": 30,
                                   "digest_minutes": 60, "notify_critical": True},
                              logging.getLogger("t"), project_root=tmp,
                              tagger=Tagger.empty())
            n = bot.notify_new_app([{"name": "notepad.exe",
                                     "exe": "C:\\Windows\\notepad.exe",
                                     "title": "নোট.txt",
                                     "started_at": "15:02:11"},
                                    {"name": "chrome.exe",
                                     "exe": "C:\\Program Files\\Google\\Chrome\\chrome.exe",
                                     "title": "YouTube",
                                     "started_at": "15:02:20"}])
            assert n == 2
            assert len(sent) == 2
            assert "notepad.exe" in sent[0] and "নোট.txt" in sent[0]
            assert "chrome.exe" in sent[1]
            # Muted: nothing goes out.
            from datetime import datetime, timedelta
            db.set_state("telegram_muted_until",
                         (datetime.now() + timedelta(hours=1)).isoformat())
            assert bot.notify_new_app([{"name": "calc.exe", "exe": "",
                                        "title": "", "started_at": ""}]) == 0
            assert len(sent) == 2
            db.close()
    finally:
        tg.send_message = old_send
    print("notify new app passed")


if __name__ == "__main__":
    test_baseline_then_new()
    test_system_processes_skipped()
    test_cooldown()
    test_max_per_tick()
    test_check_never_raises()
    test_build_new_app_text()
    test_notify_new_app()
    print("ALL APP WATCH TESTS PASSED")
