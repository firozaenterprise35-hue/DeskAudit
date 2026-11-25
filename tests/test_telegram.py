"""Tests for the Telegram bot + hourly email digest (v12).

Network is fully mocked: urllib.request.urlopen and smtplib.SMTP are
replaced with fakes, so no real Telegram or SMTP traffic happens.
"""

import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit.database import Database  # noqa: E402
from deskaudit.emailer import build_hourly_email, configured as email_configured, send_email  # noqa: E402
from deskaudit.telegram import TelegramBot, build_digest_text, build_status_text, send_message  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


def _seed_db(path):
    db = Database(path)
    now = datetime.now()
    day = today_str()
    sid = db.start_app_session("WINWORD.EXE", r"C:\Program Files\WINWORD.EXE",
                               "রিপোর্ট.docx - Word", now - timedelta(minutes=40))
    db.update_app_session(sid, now, 2400, 1800, 600)
    db.start_app_session("chrome.exe", r"C:\Chrome\chrome.exe",
                         "Gmail - Inbox", now - timedelta(minutes=5))
    db.log_file_event("modified", r"C:\Users\Firoza\Documents\Office\রিপোর্ট.docx",
                      occurred_at=now - timedelta(minutes=10))
    db.log_transfer(day, "usb_copy", channel="E:", file_name="রিপোর্ট.docx",
                    file_path=r"E:\রিপোর্ট.docx", occurred_at=now.isoformat(timespec="seconds"))
    return db, day


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_message_mocked():
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode("utf-8")))
        return _FakeResp({"ok": True, "result": {"message_id": 1}})

    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        assert send_message("tok123", "999", "হ্যালো") is True
        assert calls[0]["chat_id"] == "999"
        assert calls[0]["text"] == "হ্যালো"
        assert send_message("", "999", "x") is False
        assert send_message("tok123", "", "x") is False
    finally:
        urllib.request.urlopen = old
    print("send_message mocked passed")


def test_send_message_failure():
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("no network")

    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        assert send_message("tok123", "999", "হ্যালো") is False
    finally:
        urllib.request.urlopen = old
    print("send_message failure passed")


def test_status_text():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        text = build_status_text(db, day)
        assert "WINWORD.EXE" in text
        assert "রিপোর্ট.docx" in text
        assert "usb_copy" in text
        assert "chrome.exe" in text or "Gmail" in text
        # Metadata only: no typed content is ever in the text (there is
        # none to leak — the DB holds titles/names only).
        assert "পাসওয়ার্ড" not in text
        db.close()
    print("status text passed")


def test_digest_text():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        text = build_digest_text(db, day, 60)
        assert "WINWORD.EXE" in text
        assert "রিপোর্ট.docx" in text
        db.close()
    print("digest text passed")


def test_poll_commands():
    import deskaudit.telegram as tg

    sent = []

    def fake_get_updates(token, offset):
        return [
            {"update_id": 10,
             "message": {"text": "/status", "chat": {"id": 999}}},
            {"update_id": 11,
             "message": {"text": "/status", "chat": {"id": 123}}},  # wrong chat: ignored
            {"update_id": 12,
             "message": {"text": "/help", "chat": {"id": 999}}},
        ]

    old_updates = tg.get_updates
    old_send = tg.send_message
    tg.get_updates = fake_get_updates
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999"})
            assert bot.poll_commands() == 2  # /status + /help; wrong chat ignored
            assert len(sent) == 2
            assert "বর্তমান অবস্থা" in sent[0][1]
            assert "/status" in sent[1][1]
            assert db.get_state("telegram_update_offset") == "13"
            # Second poll: nothing new.
            tg.get_updates = lambda token, offset: []
            assert bot.poll_commands() == 0
            db.close()
    finally:
        tg.get_updates = old_updates
        tg.send_message = old_send
    print("poll commands passed")


def test_bot_not_configured():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        bot = TelegramBot(db, {})
        assert bot.configured is False
        assert bot.send("hi") is False
        assert bot.send_status() is False
        db.close()
    print("bot not configured passed")


def test_build_hourly_email():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        subject, text, html = build_hourly_email(db, day, 60)
        assert "আপডেট" in subject
        assert "WINWORD.EXE" in text
        assert "রিপোর্ট.docx" in text
        assert "<table" in html and "WINWORD.EXE" in html
        assert "টাইপ করা লেখা" in text  # the metadata-only footer
        db.close()
    print("build hourly email passed")


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg

    def quit(self):
        pass


def test_send_email_mocked():
    import smtplib

    import deskaudit.emailer as em

    old = smtplib.SMTP
    smtplib.SMTP = _FakeSMTP
    _FakeSMTP.instances.clear()
    try:
        settings = {"smtp_host": "smtp.gmail.com", "smtp_port": 587,
                    "smtp_user": "me@gmail.com", "smtp_password": "app-pass-16",
                    "to_addr": "me@gmail.com"}
        assert email_configured(settings) is True
        ok, err = send_email(settings, "বিষয়", "মূল লেখা", "<p>html</p>")
        assert ok is True and err == ""
        inst = _FakeSMTP.instances[-1]
        assert inst.logged_in == ("me@gmail.com", "app-pass-16")
        assert inst.sent["Subject"] == "বিষয়"
        assert inst.sent["To"] == "me@gmail.com"
    finally:
        smtplib.SMTP = old
    print("send_email mocked passed")


def test_send_email_not_configured():
    ok, err = send_email({}, "বিষয়", "লেখা")
    assert ok is False and "not configured" in err
    print("send_email not configured passed")


def test_send_email_failure():
    import smtplib

    class _BadSMTP(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    old = smtplib.SMTP
    smtplib.SMTP = _BadSMTP
    try:
        settings = {"smtp_user": "me@gmail.com", "smtp_password": "wrong",
                    "to_addr": "me@gmail.com"}
        ok, err = send_email(settings, "বিষয়", "লেখা")
        assert ok is False and "535" in err
    finally:
        smtplib.SMTP = old
    print("send_email failure passed")


if __name__ == "__main__":
    test_send_message_mocked()
    test_send_message_failure()
    test_status_text()
    test_digest_text()
    test_poll_commands()
    test_bot_not_configured()
    test_build_hourly_email()
    test_send_email_mocked()
    test_send_email_not_configured()
    test_send_email_failure()
    print("ALL TELEGRAM + EMAIL TESTS PASSED")
