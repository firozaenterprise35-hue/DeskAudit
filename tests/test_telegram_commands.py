"""Tests for the Telegram remote-control commands (v12.2).

Covers the read-only commands (/apps /projects /transfers /alerts
/files /week /search /report /menu), control (/pause /resume /mute
/unmute /digest /lock), app control (/close /freeze with two-step
confirmation), data management (/clear /reset /backup) and /goal.

Network is fully mocked: send_message / send_document / _post /
get_updates are replaced with fakes, and psutil is replaced with a
fake module, so no real Telegram, SMTP or process traffic happens.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml  # noqa: E402

import deskaudit.telegram as tg  # noqa: E402
from deskaudit.database import Database  # noqa: E402
from deskaudit.projects import Tagger, update_project_goal  # noqa: E402
from deskaudit.telegram import TelegramBot  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


def _seed_db(path, day=None):
    db = Database(path)
    now = datetime.now()
    day = day or today_str()
    db.upsert_project("Office", daily_goal_minutes=240)
    sid = db.start_app_session("WINWORD.EXE", r"C:\Program Files\WINWORD.EXE",
                               "রিপোর্ট.docx - Word", now - timedelta(minutes=40),
                               project="Office")
    db.update_app_session(sid, now, 2400, 1800, 600)
    db.log_file_event("modified", r"C:\Users\Firoza\Documents\Office\রিপোর্ট.docx",
                      occurred_at=now - timedelta(minutes=10), project="Office")
    db.log_transfer(day, "usb_copy", channel="E:", file_name="রিপোর্ট.docx",
                    file_path=r"E:\রিপোর্ট.docx", size_bytes=2500000,
                    destination="E: (USB)",
                    occurred_at=now.isoformat(timespec="seconds"))
    bsid = db.start_browser_session("chrome", "https://docs.google.com/doc",
                                    "docs.google.com", "রিপোর্ট - Google Docs",
                                    "productivity", True,
                                    now - timedelta(minutes=30), project="Office")
    db.update_browser_session(bsid, now - timedelta(minutes=10), 1200, 900, 300)
    db.log_browser_search("chrome", "মোবাইল দাম তালিকা",
                          "https://www.google.com/search?q=x", "google.com",
                          now - timedelta(minutes=20), project="Office")
    db.log_browser_download("chrome", "https://example.com/price.pdf",
                            "example.com", r"C:\Downloads\price.pdf",
                            "price.pdf", 1200000, 1200000, "complete",
                            now - timedelta(minutes=15), project="Office")
    db.save_alert(day, "large_download", "warning",
                  "বড় ডাউনলোড: price.pdf", "1.2 MB ডাউনলোড হয়েছে")
    return db, day


def _make_bot(tmp, db):
    """Bot with full context in a temp project root."""
    root = Path(tmp)
    (root / "config.yaml").write_text(
        (Path(__file__).resolve().parent.parent / "config.yaml").read_text(
            encoding="utf-8"), encoding="utf-8")
    projects_path = root / "projects.yaml"
    with projects_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump({"projects": [{"name": "Office",
                                      "daily_goal_minutes": 240}],
                        "default_project": "Untagged"}, f,
                       sort_keys=False, allow_unicode=True)
    tagger = Tagger.load(projects_path)
    return TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                       project_root=root, config_path=root / "config.yaml",
                       projects_path=projects_path, tagger=tagger), root


class _Net:
    """Captures everything the bot would send to Telegram."""

    def __init__(self):
        self.sent = []          # (chat_id, text)
        self.markup = []        # (chat_id, text, reply_markup)
        self.docs = []          # (chat_id, path, caption)
        self.posts = []         # raw _post payloads

    def install(self):
        self._old_send = tg.send_message
        self._old_doc = tg.send_document
        self._old_post = tg._post

        def fake_post(token, method, payload, timeout=15):
            self.posts.append((method, payload))
            if method == "sendMessage":
                self.sent.append((payload.get("chat_id"), payload.get("text", "")))
            return {"ok": True}

        tg.send_message = lambda token, chat_id, text: self.sent.append(
            (chat_id, text)) or True
        tg.send_document = lambda token, chat_id, path, caption="": self.docs.append(
            (chat_id, str(path), caption)) or True
        tg._post = fake_post
        return self

    def uninstall(self):
        tg.send_message = self._old_send
        tg.send_document = self._old_doc
        tg._post = self._old_post

    def texts(self):
        out = [t for _, t in self.sent]
        out += [t for _, t, _ in self.markup]
        return out

    def last_text(self):
        return self.texts()[-1] if self.texts() else ""


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


def test_query_commands():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/apps")
            assert "WINWORD.EXE" in net.last_text()
            bot.handle_text("/projects")
            assert "Office" in net.last_text() and "লক্ষ্য" in net.last_text()
            bot.handle_text("/transfers")
            assert "রিপোর্ট.docx" in net.last_text() and "E: (USB)" in net.last_text()
            bot.handle_text("/alerts")
            assert "বড় ডাউনলোড" in net.last_text()
            bot.handle_text("/files")
            assert "রিপোর্ট.docx" in net.last_text()
            bot.handle_text("/week")
            assert day in net.last_text()
            bot.handle_text("/search রিপোর্ট")
            assert "রিপোর্ট.docx" in net.last_text()
            bot.handle_text("/search zzzzz")
            assert "পাওয়া যায়নি" in net.last_text()
            bot.handle_text("/search")
            assert "/search-এর পরে" in net.last_text()
            bot.handle_text("/help")
            assert "/close" in net.last_text() and "/reset" in net.last_text()
            bot.handle_text("/কী-করতে-পারো")
            assert "/help" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("query commands passed")


def test_menu_and_callbacks():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            # /menu sends a message with an inline keyboard via _post.
            bot.handle_text("/menu")
            assert net.posts, "menu must post a message with markup"
            method, payload = net.posts[-1]
            assert method == "sendMessage"
            kb = payload["reply_markup"]["inline_keyboard"]
            assert any(b["callback_data"] == "m:status"
                       for row in kb for b in row)
            assert not any(b["callback_data"].startswith("m:reset")
                           for row in kb for b in row)  # nothing destructive
            # Tap the Status button.
            bot.handle_callback("m:status", "cb-1")
            assert any("বর্তমান অবস্থা" in t for _, t in net.sent)
            assert any(m == "answerCallbackQuery" for m, _ in net.posts)
            # Tap an unknown button.
            bot.handle_callback("m:nope", "cb-2")
            assert "চিনলাম না" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("menu and callbacks passed")


def test_pause_resume():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, root = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/pause")
            assert (root / "PAUSE").exists()
            assert "বন্ধ রাখা হয়েছে" in net.last_text()
            bot.handle_text("/resume")
            assert not (root / "PAUSE").exists()
            assert "আবার চালু" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("pause/resume passed")


def test_mute_unmute():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/mute 60")
            assert bot.muted() is True
            assert bot.send_digest() is False
            assert bot.notify_alert(type("A", (), {
                "severity": "critical", "title": "t", "message": "m"})()) is False
            bot.handle_text("/unmute")
            assert bot.muted() is False
            assert bot.send_digest() is True  # now it sends again
            bot.handle_text("/mute xyz")
            assert "মিনিট দিন" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("mute/unmute passed")


def test_digest_command():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, root = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/digest 30")
            assert bot.settings["digest_minutes"] == 30
            local = yaml.safe_load((root / "config.local.yaml").read_text(
                encoding="utf-8"))
            assert local["telegram"]["digest_minutes"] == 30
            assert "30 মিনিটে" in net.last_text()
            bot.handle_text("/digest off")
            assert bot.settings["digest_minutes"] == 0
            assert "বন্ধ" in net.last_text()
            bot.handle_text("/digest")
            assert "বন্ধ" in net.last_text()
            bot.handle_text("/digest 99999")
            assert "0–1440" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("digest command passed")


def test_lock_windows_only():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/lock")
            # This VM is Linux: the command must fail safe, not crash.
            assert "Windows" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("lock windows-only passed")


def test_close_confirm_flow():
    procs = [_FakeProc(111, "chrome.exe"), _FakeProc(112, "chrome.exe"),
             _FakeProc(113, "notepad.exe")]
    old_psutil = sys.modules.get("psutil")
    sys.modules["psutil"] = _FakePsutil(procs)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot, _ = _make_bot(tmp, db)
            net = _Net().install()
            try:
                # Protected app: refused outright, no pending stored.
                bot.handle_text("/close explorer")
                assert "⛔" in net.last_text()
                assert bot._get_pending() is None
                # Unknown app: nothing to close.
                bot.handle_text("/close zzzzz")
                assert "পাওয়া যায়নি" in net.last_text()
                # Real app: asks for confirmation, stores pending.
                bot.handle_text("/close chrome")
                assert bot._get_pending() is not None
                assert bot._get_pending()["action"] == "close"
                assert "নিশ্চিত" in net.last_text()
                assert not procs[0].terminated
                # Cancel with না.
                bot.handle_text("না")
                assert "বাতিল" in net.last_text()
                assert bot._get_pending() is None
                assert not procs[0].terminated
                # Ask again, confirm with হ্যাঁ.
                bot.handle_text("/close chrome")
                bot.handle_text("হ্যাঁ")
                assert procs[0].terminated and procs[1].terminated
                assert not procs[2].terminated  # notepad untouched
                assert "বন্ধ করা হয়েছে" in net.last_text()
                # A bare হ্যাঁ with nothing pending is harmless.
                bot.handle_text("হ্যাঁ")
                assert "অপেক্ষমাণ কাজ নেই" in net.last_text()
            finally:
                net.uninstall()
                db.close()
    finally:
        if old_psutil is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = old_psutil
    print("close confirm flow passed")


def test_freeze_windows_only():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/freeze chrome")
            assert "Windows" in net.last_text()
            assert bot._get_pending() is None
        finally:
            net.uninstall()
            db.close()
    print("freeze windows-only passed")


def test_clear_and_reset():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        # Seed yesterday's rows into the SAME db via a second connection.
        db2 = Database(os.path.join(tmp, "t.db"))
        sid = db2.start_app_session("notepad.exe", r"C:\Windows\notepad.exe",
                                    "নোট.txt", datetime.now() - timedelta(days=1))
        db2.update_app_session(sid, datetime.now() - timedelta(days=1),
                               600, 500, 100)
        bot, root = _make_bot(tmp, db)
        net = _Net().install()
        try:
            # /clear with a bad date asks again.
            bot.handle_text("/clear 24-11-2026")
            assert "YYYY-MM-DD" in net.last_text()
            # Clear today: confirm, only today's rows go.
            bot.handle_text(f"/clear {day}")
            assert bot._get_pending()["action"] == "clear"
            bot.handle_text("হ্যাঁ")
            assert "মুছে ফেলা হয়েছে" in net.last_text()
            assert db.totals(day)["active"] == 0
            assert float(db2.totals(yesterday)["active"] or 0) > 0
            # /reset: confirm, backup first, everything goes.
            bot.handle_text("/reset")
            bot.handle_text("হ্যাঁ")
            assert "সব ডেটা মুছে ফেলা হয়েছে" in net.last_text()
            assert "ব্যাকআপ নেওয়া হয়েছে" in net.last_text()
            assert float(db.totals(yesterday)["active"] or 0) == 0
            backups = list((root / "backups").glob("deskaudit-*.db"))
            assert backups, "reset must back up first"
            # Projects survive a reset.
            assert db.list_projects(), "projects must survive /reset"
        finally:
            net.uninstall()
            db.close()
            db2.close()
    print("clear and reset passed")


def test_backup_and_report():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, root = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/backup")
            assert "ব্যাকআপ নেওয়া হয়েছে" in net.last_text()
            assert list((root / "backups").glob("deskaudit-*.db"))
            bot.handle_text("/report")
            assert net.docs, "report must send a document"
            assert net.docs[0][1].endswith("-summary.md")
            assert Path(net.docs[0][1]).exists()
            assert "রিপোর্ট পাঠিয়ে" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("backup and report passed")


def test_goal_command():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, root = _make_bot(tmp, db)
        net = _Net().install()
        try:
            bot.handle_text("/goal Office 120")
            assert "120" in net.last_text() or "2ঘ" in net.last_text()
            raw = yaml.safe_load((root / "projects.yaml").read_text(
                encoding="utf-8"))
            assert raw["projects"][0]["daily_goal_minutes"] == 120
            assert bot.tagger.by_name["Office"].daily_goal_minutes == 120
            assert db.list_projects()[0]["daily_goal_minutes"] == 120
            # A brand-new project is created.
            bot.handle_text("/goal NewProj 45")
            assert "নতুন প্রজেক্ট" in net.last_text()
            bot.handle_text("/goal Office")
            assert "ব্যবহার" in net.last_text()
            bot.handle_text("/goal Office abc")
            assert "মিনিট সংখ্যায়" in net.last_text()
        finally:
            net.uninstall()
            db.close()
    print("goal command passed")


def test_update_project_goal_unit():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "projects.yaml"
        with p.open("w", encoding="utf-8") as f:
            yaml.safe_dump({"projects": [{"name": "Office",
                                         "daily_goal_minutes": 240}]}, f)
        assert update_project_goal(p, "Office", 90) == "updated"
        assert update_project_goal(p, "Study", 60) == "created"
        try:
            update_project_goal(p, "Office", -5)
        except ValueError:
            pass
        else:
            raise AssertionError("negative goal must raise ValueError")
        try:
            update_project_goal(Path(tmp) / "nope.yaml", "Office", 10)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing file must raise FileNotFoundError")
    print("update_project_goal unit passed")


def test_delete_day_and_reset_data_unit():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        counts = db.delete_day(day)
        assert sum(counts.values()) > 0
        assert counts["app_sessions"] >= 1
        assert db.totals(day)["active"] == 0
        db2, _ = _seed_db(os.path.join(tmp, "t.db"))
        counts = db2.reset_data()
        assert sum(counts.values()) > 0
        assert db2.totals(day)["active"] == 0
        assert db2.get_state("telegram_update_offset") is None  # untouched either way
        db.close()
        db2.close()
    print("delete_day and reset_data unit passed")


def test_poll_callbacks_and_wrong_chat():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot, _ = _make_bot(tmp, db)
        net = _Net().install()
        old_updates = tg.get_updates
        tg.get_updates = lambda token, offset: [
            {"update_id": 20, "callback_query": {
                "id": "cb-9", "from": {"id": 999}, "data": "m:week"}},
            {"update_id": 21, "callback_query": {
                "id": "cb-10", "from": {"id": 123}, "data": "m:status"}},
            {"update_id": 22, "message": {
                "text": "/apps", "chat": {"id": 999}}},
        ]
        try:
            assert bot.poll_commands() == 2  # callback + message; wrong chat ignored
            assert db.get_state("telegram_update_offset") == "23"
            assert any("গত ৭ দিন" in t for _, t in net.sent)
            assert any("WINWORD.EXE" in t for _, t in net.sent)
        finally:
            tg.get_updates = old_updates
            net.uninstall()
            db.close()
    print("poll callbacks and wrong chat passed")


if __name__ == "__main__":
    test_query_commands()
    test_menu_and_callbacks()
    test_pause_resume()
    test_mute_unmute()
    test_digest_command()
    test_lock_windows_only()
    test_close_confirm_flow()
    test_freeze_windows_only()
    test_clear_and_reset()
    test_backup_and_report()
    test_goal_command()
    test_update_project_goal_unit()
    test_delete_day_and_reset_data_unit()
    test_poll_callbacks_and_wrong_chat()
    print("ALL TELEGRAM COMMAND TESTS PASSED")
