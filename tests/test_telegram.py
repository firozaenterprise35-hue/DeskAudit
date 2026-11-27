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
from deskaudit.telegram import (TelegramBot, build_digest_text,
                                build_status_text, build_today_text,
                                send_long, send_message)  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


def _seed_db(path):
    db = Database(path)
    now = datetime.now()
    day = today_str()
    db.upsert_project("Office", daily_goal_minutes=240)
    sid = db.start_app_session("WINWORD.EXE", r"C:\Program Files\WINWORD.EXE",
                               "রিপোর্ট.docx - Word", now - timedelta(minutes=40),
                               project="Office")
    db.update_app_session(sid, now, 2400, 1800, 600)
    db.start_app_session("chrome.exe", r"C:\Chrome\chrome.exe",
                         "Gmail - Inbox", now - timedelta(minutes=5))
    db.log_file_event("modified", r"C:\Users\Firoza\Documents\Office\রিপোর্ট.docx",
                      occurred_at=now - timedelta(minutes=10), project="Office")
    db.log_transfer(day, "usb_copy", channel="E:", file_name="রিপোর্ট.docx",
                    file_path=r"E:\রিপোর্ট.docx", size_bytes=2500000,
                    destination="E: (USB)", occurred_at=now.isoformat(timespec="seconds"))
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
        # v12.1 full data: projects with goals, productive split,
        # searches, downloads, transfer destination + size, file time.
        assert "Office" in text and "লক্ষ্য" in text
        assert "কাজের" in text and "অন্যান্য" in text
        assert "মোবাইল দাম তালিকা" in text
        assert "price.pdf" in text and "1.1 MB" in text
        assert "E: (USB)" in text and "2.4 MB" in text
        assert "প্রথম কাজ" in text
        assert "বড় ডাউনলোড" in text
        # v12.14.2: the version, so a /status reply proves which build
        # is actually running on that PC.
        assert "DeskAudit v" in text
        # Metadata only: no typed content is ever in the text (there is
        # none to leak — the DB holds titles/names only).
        assert "পাসওয়ার্ড" not in text
        db.close()
    print("status text passed")


def test_today_text():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        text = build_today_text(db, day)
        assert "আজকের সারাংশ" in text
        assert "Office" in text and "মোবাইল দাম তালিকা" in text
        assert "এখন খোলা আছে" not in text  # no live section in /today
        db.close()
    print("today text passed")


def test_send_long_splits():
    import deskaudit.telegram as tg

    sent = []
    old_send = tg.send_message
    tg.send_message = lambda token, chat_id, text: sent.append(text) or True
    try:
        long_text = "\n".join(f"লাইন {i} — কিছু তথ্য" for i in range(300))
        assert len(long_text) > 3900
        assert tg.send_long("tok", "999", long_text) is True
        assert len(sent) > 1
        assert all(len(c) <= 3900 for c in sent)
        assert "".join(sent).count("লাইন") == 300
        assert tg.send_long("tok", "999", "ছোট") is True
        assert sent[-1] == "ছোট"
    finally:
        tg.send_message = old_send
    print("send_long splits passed")


def test_empty_day_hint():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(os.path.join(tmp, "empty.db"))
        text = build_status_text(db, today_str())
        assert "কোনো কাজ রেকর্ড হয়নি" in text
        db.close()
    print("empty day hint passed")


def test_digest_text():
    with tempfile.TemporaryDirectory() as tmp:
        db, day = _seed_db(os.path.join(tmp, "t.db"))
        text = build_digest_text(db, day, 60)
        assert "WINWORD.EXE" in text
        assert "রিপোর্ট.docx" in text
        assert "Office" in text  # project split in the digest
        assert "মোবাইল দাম তালিকা" in text
        assert "price.pdf" in text
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
        assert "Office" in text  # v12.1: projects in the email too
        assert "মোবাইল দাম তালিকা" in text
        assert "price.pdf" in text
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


def test_send_photo_id_returns_message_id():
    """v12.15: the live view needs the sent photo's message id so it
    can edit that one message — send_photo_id returns it."""
    import urllib.parse

    from deskaudit.telegram import send_photo_id

    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append((req.full_url, req.data))
        return _FakeResp({"ok": True, "result": {"message_id": 42}})

    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake-jpeg")
            path = f.name
        try:
            assert send_photo_id("tok123", "999", path, caption="hi") == 42
            assert send_photo_id("", "999", path) is None
            assert send_photo_id("tok123", "", path) is None
        finally:
            os.unlink(path)
    finally:
        urllib.request.urlopen = old
    assert bodies and "sendPhoto" in bodies[0][0], bodies
    assert b"fake-jpeg" in bodies[0][1], bodies
    print("send photo id returns message id passed")


def test_edit_photo_posts_media():
    """v12.15: editMessageMedia must carry the media JSON
    (attach://photo) plus the file — the live view's refresh."""
    import urllib.parse

    from deskaudit.telegram import edit_photo

    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append((req.full_url, req.data))
        return _FakeResp({"ok": True, "result": True})

    old = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake-jpeg")
            path = f.name
        try:
            assert edit_photo("tok123", "999", 42, path, caption="live") is True
            assert edit_photo("tok123", "999", 0, path) is False
            assert edit_photo("", "999", 42, path) is False
        finally:
            os.unlink(path)
    finally:
        urllib.request.urlopen = old
    assert bodies, "no request was made"
    url, body = bodies[0]
    assert "editMessageMedia" in url, url
    assert b'"type": "photo"' in body and b"attach://photo" in body, body
    assert b"fake-jpeg" in body, body
    print("edit photo posts media passed")


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


def test_manager_view_command_and_control_refusal():
    """v12.16: the manager's chat may run view commands (the reply
    goes to the manager) but control commands are refused — and a
    manager's হ্যাঁ can never fire the owner's pending confirmation."""
    import deskaudit.telegram as tg

    sent = []
    old_send = tg.send_message
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999",
                                   "manager_chat_id": "555"})
            # View command from the manager: answered in the manager's chat.
            bot._reply_chat_id = "555"
            bot._from_manager = True
            try:
                bot.handle_text("/status")
            finally:
                bot._reply_chat_id = None
                bot._from_manager = False
            assert sent[-1][0] == "555"
            assert "বর্তমান অবস্থা" in sent[-1][1]
            # Control commands: refused, in the manager's chat.
            bot._reply_chat_id = "555"
            bot._from_manager = True
            try:
                bot.handle_text("/shutdown")
                assert "শুধু দেখা যায়" in sent[-1][1]
                assert sent[-1][0] == "555"
                bot.handle_text("/record 1")
                assert "শুধু দেখা যায়" in sent[-1][1]
                bot.handle_text("/browser block example.com")
                assert "শুধু দেখা যায়" in sent[-1][1]
                # A stray হ্যাঁ from the manager must not fire the owner's pending.
                bot._set_pending("clear", {"date": day})
                bot.handle_text("হ্যাঁ")
                assert "শুধু দেখা যায়" in sent[-1][1]
                assert bot._get_pending() is not None
                bot._clear_pending()
            finally:
                bot._reply_chat_id = None
                bot._from_manager = False
            db.close()
    finally:
        tg.send_message = old_send
    print("manager view/refusal passed")


def test_manager_callbacks_refused():
    """v12.16: a manager's tap on a control button is refused; a
    view button works. The explorer's write taps are refused too."""
    import deskaudit.telegram as tg

    sent = []
    posts = []
    old_send = tg.send_message
    old_answer = tg.answer_callback
    old_post = tg._post
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    tg.answer_callback = lambda token, qid: True
    tg._post = lambda token, method, payload, timeout=15: posts.append(method) or {
        "ok": True, "result": {"message_id": 42}}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))

            class _FakeExplorer:
                def roots(self):
                    return []

            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999",
                                   "manager_chat_id": "555"},
                              file_explorer=_FakeExplorer())
            bot._reply_chat_id = "555"
            bot._from_manager = True
            try:
                bot.handle_callback("m:shutdown", "q1")
                assert "শুধু দেখা যায়" in sent[-1][1]
                bot.handle_callback("m:status", "q2")
                assert "বর্তমান অবস্থা" in sent[-1][1]
                bot.handle_callback("dq:yes:1:tok", "q3")
                assert "শুধু দেখা যায়" in sent[-1][1]
                bot.handle_callback("x:act:send", "q4")
                assert "শুধু দেখা যায়" in sent[-1][1]
                before = len(sent)
                bot.handle_callback("x:up", "q5")
                # "up" is browse-only — no refusal was sent, and the
                # explorer refreshed instead (via _post, not send).
                assert len(sent) == before
                assert posts and posts[-1] in ("sendMessage", "editMessageText")
            finally:
                bot._reply_chat_id = None
                bot._from_manager = False
            db.close()
    finally:
        tg.send_message = old_send
        tg.answer_callback = old_answer
        tg._post = old_post
    print("manager callbacks refused passed")


def test_poll_routes_manager_and_records_unknown():
    """v12.16: poll answers the manager's chat, and remembers a
    stranger's chat (for /manager) instead of answering it."""
    import deskaudit.telegram as tg

    sent = []

    def fake_get_updates(token, offset):
        return [
            {"update_id": 20,
             "message": {"text": "/status", "chat": {"id": 555},
                         "from": {"id": 555, "first_name": "Rahim"}}},
            {"update_id": 21,
             "message": {"text": "/status", "chat": {"id": 777},
                         "from": {"id": 777, "first_name": "Stranger"}}},
        ]

    old_updates = tg.get_updates
    old_send = tg.send_message
    tg.get_updates = fake_get_updates
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999",
                                   "manager_chat_id": "555"})
            assert bot.poll_commands() == 1  # manager answered; stranger ignored
            assert len(sent) == 1
            assert sent[0][0] == "555"
            assert "বর্তমান অবস্থা" in sent[0][1]
            unknown = bot._unknown_chats()
            assert any(str(c.get("chat_id")) == "777" for c in unknown)
            assert not any(str(c.get("chat_id")) == "555" for c in unknown)
            db.close()
    finally:
        tg.get_updates = old_updates
        tg.send_message = old_send
    print("poll routes manager passed")


def test_broadcast_goes_to_both_chats():
    """v12.16: alerts reach the owner AND the manager."""
    import deskaudit.telegram as tg

    sent = []
    old_send = tg.send_message
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999",
                                   "manager_chat_id": "555"})

            class _Alert:
                severity = "warning"
                title = "বড় ডাউনলোড"
                message = "1.2 MB ডাউনলোড হয়েছে"

            assert bot.notify_alert(_Alert()) is True
            assert [c for c, _t in sent] == ["999", "555"]
            assert "বড় ডাউনলোড" in sent[0][1]
            db.close()
    finally:
        tg.send_message = old_send
    print("broadcast to both passed")


def test_manager_add_and_remove():
    """v12.16: the owner approves a stranger from /manager; the
    welcome goes to the new manager; /manager remove revokes it."""
    import deskaudit.telegram as tg

    sent = []
    posts = []
    old_send = tg.send_message
    old_post = tg._post
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True
    tg._post = lambda token, method, payload, timeout=15: posts.append(payload) or {
        "ok": True, "result": {"message_id": 42}}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                              config_path=os.path.join(tmp, "config.yaml"))
            bot._record_unknown_chat({"id": 777, "first_name": "Rahim"})
            bot.handle_text("/manager")
            # show_manager sends with a keyboard (via _post), not send_message.
            assert posts and "Rahim" in posts[-1].get("text", "")
            bot.manager_add("777")
            assert bot.manager_chat_id == "777"
            assert sent[-1][0] == "777"  # the welcome went to the manager
            assert "ম্যানেজার" in sent[-1][1]
            bot.manager_remove()
            assert bot.manager_chat_id == ""
            assert any(c == "777" and "বন্ধ করা হয়েছে" in t for c, t in sent)
            assert sent[-1][0] == "999"  # the owner's confirmation
            assert "সরিয়ে দেওয়া হয়েছে" in sent[-1][1]
            db.close()
    finally:
        tg.send_message = old_send
        tg._post = old_post
    print("manager add/remove passed")


def test_drive_status_and_code():
    """v12.16: /drive explains the setup when unconfigured, and
    /drive code stores the refresh token after a mocked exchange."""
    import deskaudit.telegram as tg

    sent = []
    old_send = tg.send_message
    tg.send_message = lambda token, chat_id, text: sent.append((chat_id, text)) or True

    class _FakeResp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        return _FakeResp({"access_token": "acc1", "refresh_token": "ref9",
                          "expires_in": 3600})

    old_urlopen = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, day = _seed_db(os.path.join(tmp, "t.db"))
            bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999"})
            bot.handle_text("/drive")
            assert "সেট করা হয়নি" in sent[-1][1]
            bot2 = TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                               drive_settings={"client_id": "cid",
                                               "client_secret": "csec"})
            bot2.handle_text("/drive code abc123")
            assert "যুক্ত হয়েছে" in sent[-1][1]
            assert bot2.drive_settings.get("refresh_token") == "ref9"
            assert bot2.drive_uploader.configured is True
            db.close()
    finally:
        tg.send_message = old_send
        urllib.request.urlopen = old_urlopen
    print("drive status/code passed")


if __name__ == "__main__":
    test_send_message_mocked()
    test_send_message_failure()
    test_status_text()
    test_today_text()
    test_send_long_splits()
    test_empty_day_hint()
    test_digest_text()
    test_poll_commands()
    test_bot_not_configured()
    test_build_hourly_email()
    test_send_email_mocked()
    test_send_email_not_configured()
    test_send_email_failure()
    test_send_photo_id_returns_message_id()
    test_edit_photo_posts_media()
    test_manager_view_command_and_control_refusal()
    test_manager_callbacks_refused()
    test_poll_routes_manager_and_records_unknown()
    test_broadcast_goes_to_both_chats()
    test_manager_add_and_remove()
    test_drive_status_and_code()
    print("ALL TELEGRAM + EMAIL TESTS PASSED")
