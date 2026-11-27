"""Tests for the Telegram remote-control commands (v12.3):
/screenshot, /browser (+close/block/unblock), /screen, /shutdown,
/restart, /abort, and the new menu buttons.

Network is fully mocked. Windows-only commands are tested for
their safe failure message on Linux; the real paths run under Wine
in the frozen-exe smoke test.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

import deskaudit.telegram as tg  # noqa: E402
from deskaudit.database import Database  # noqa: E402
from deskaudit.projects import Tagger  # noqa: E402
from deskaudit.telegram import TelegramBot  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


def _seed_db(path):
    db = Database(path)
    now = datetime.now()
    bsid = db.start_browser_session("chrome", "https://www.youtube.com/watch?v=x",
                                    "youtube.com", "গান - YouTube", "entertainment",
                                    False, now - timedelta(minutes=20))
    db.update_browser_session(bsid, now - timedelta(minutes=5), 900, 700, 200)
    db.log_browser_visit("chrome", "https://www.facebook.com/feed", "facebook.com",
                         "Facebook", now - timedelta(minutes=10))
    db.log_browser_visit("chrome", "https://docs.google.com/doc", "docs.google.com",
                         "রিপোর্ট - Google Docs", now - timedelta(minutes=30))
    return db, today_str()


def _make_bot(tmp, db):
    root = Path(tmp)
    (root / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
            encoding="utf-8"), encoding="utf-8")
    projects_path = root / "projects.yaml"
    with projects_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"projects": [], "default_project": "Untagged"}, f)
    return TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                       project_root=root, config_path=root / "config.yaml",
                       projects_path=projects_path,
                       tagger=Tagger.load(projects_path)), root


class _Net:
    def __init__(self):
        self.sent = []
        self.posts = []

    def install(self):
        self._old_send = tg.send_message
        self._old_post = tg._post

        def fake_post(token, method, payload, timeout=15):
            self.posts.append((method, payload))
            if method == "sendMessage":
                self.sent.append((payload.get("chat_id"), payload.get("text", "")))
            return {"ok": True}

        tg.send_message = lambda token, chat_id, text: self.sent.append(
            (chat_id, text)) or True
        tg._post = fake_post
        return self

    def uninstall(self):
        tg.send_message = self._old_send
        tg._post = self._old_post

    def last_text(self):
        return self.sent[-1][1] if self.sent else ""


class _FakeProc:
    def __init__(self, pid, name):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self.terminated = False

    def terminate(self):
        self.terminated = True


class _FakePsutil:
    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, attrs=None):
        return list(self._procs)


def test_browser_command():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/browser")
            text = net.last_text()
            assert "youtube.com" in text, text
            assert "facebook.com" in text and "docs.google.com" in text
            assert "গান - YouTube" in text
            bot.handle_text("/browser frobnicate")
            assert "ব্যবহার" in net.last_text()
            bot.handle_text("/browser block")
            assert "কোন ডোমেইন" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("browser command passed")


def test_browser_close_confirm():
    procs = [_FakeProc(201, "chrome.exe"), _FakeProc(202, "msedge.exe"),
             _FakeProc(203, "notepad.exe")]
    old_psutil = sys.modules.get("psutil")
    sys.modules["psutil"] = _FakePsutil(procs)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot, _ = _make_bot(tmp, db)
            net = _Net().install()
            try:
                bot.handle_text("/browser close")
                assert bot._get_pending()["action"] == "close_browsers"
                assert "ব্রাউজার বন্ধ করব" in net.last_text()
                bot.handle_text("হ্যাঁ")
                assert procs[0].terminated and procs[1].terminated
                assert not procs[2].terminated
                assert "বন্ধ করা হয়েছে" in net.last_text()
            finally:
                net.uninstall()
                db.close()
    finally:
        if old_psutil is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = old_psutil
    print("browser close confirm passed")


def test_windows_only_commands():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/screenshot")
            assert "Windows" in net.last_text()
            bot.handle_text("/screen white")
            assert "Windows" in net.last_text()
            bot.handle_text("/screen off")
            assert "Windows" in net.last_text()
            bot.handle_text("/screen")
            assert "খোলা আছে" in net.last_text()
            bot.handle_text("/screen purple")
            assert "ব্যবহার" in net.last_text()
            bot.handle_text("/browser block facebook.com")
            assert bot._get_pending()["action"] == "block"
            bot.handle_text("হ্যাঁ")
            assert "Windows" in net.last_text()
            bot.handle_text("/abort")
            assert "Windows" in net.last_text()
            bot.handle_text("/shutdown")
            assert bot._get_pending()["action"] == "shutdown"
            bot.handle_text("হ্যাঁ")
            assert "Windows" in net.last_text()
            bot.handle_text("/restart")
            assert bot._get_pending()["action"] == "restart"
            bot.handle_text("না")
            assert "বাতিল" in net.last_text()
            assert bot._get_pending() is None
        finally:
            net.uninstall()
            db.close()
    print("windows-only commands passed")


def test_help_and_menu_new_buttons():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/help")
            text = net.last_text()
            for cmd in ("/screenshot", "/browser", "/screen", "/shutdown",
                        "/restart", "/abort"):
                assert cmd in text, cmd
            bot.handle_text("/menu")
            method, payload = net.posts[-1]
            kb = payload["reply_markup"]["inline_keyboard"]
            flat = [b["callback_data"] for row in kb for b in row]
            assert "m:shot" in flat and "m:browser" in flat
            # The full menu (v12.10) lists shutdown/restart too — but a
            # tap only opens the confirmation, it never shuts down.
            assert "m:shutdown" in flat and "m:restart" in flat
            bot.handle_callback("m:shutdown", "cb-sh")
            pending = bot._get_pending()
            assert pending and pending["action"] == "shutdown"
            bot._clear_pending()
            bot.handle_callback("m:shot", "cb-1")
            assert "Windows" in net.last_text()
            bot.handle_callback("m:browser", "cb-2")
            assert "youtube.com" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("help and menu new buttons passed")


def test_send_photo_multipart():
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp) / "x.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 10)
        calls = []
        old = tg._post_multipart
        tg._post_multipart = lambda token, method, fields, ff, fn, data, timeout=60: (
            calls.append((method, fields, ff, fn, len(data))) or {"ok": True})
        try:
            assert tg.send_photo("tok", "999", img, caption="hi") is True
            method, fields, ff, fn, size = calls[0]
            assert method == "sendPhoto" and ff == "photo"
            assert fields["caption"] == "hi" and size == 18
        finally:
            tg._post_multipart = old
    print("send_photo multipart passed")


def test_db_browser_getters():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        latest = db.latest_browser_session(day)
        assert latest is not None and latest["domain"] == "youtube.com"
        visits = db.recent_visits(day, limit=10)
        assert len(visits) == 2 and visits[0]["domain"] == "facebook.com"
        assert db.latest_browser_session("2000-01-01") is None
        assert db.recent_visits("2000-01-01") == []
        db.close()
    print("db browser getters passed")


if __name__ == "__main__":
    test_browser_command()
    test_browser_close_confirm()
    test_windows_only_commands()
    test_help_and_menu_new_buttons()
    test_send_photo_multipart()
    test_db_browser_getters()
    print("ALL TELEGRAM CONTROL TESTS PASSED")
