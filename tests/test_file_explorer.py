"""Tests for the Telegram file explorer (v12.9): drive/folder
browsing, copy / move / send / hide, the /hidden list, and the
dashboard settings roundtrip.

Network is fully mocked (tg._post replaced with a fake); all folders
live in temp dirs, so nothing touches a real drive or a real
Telegram chat.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deskaudit.telegram as tg  # noqa: E402
from deskaudit.database import Database  # noqa: E402
from deskaudit.file_explorer import (  # noqa: E402
    FileExplorer, copy_file, is_system_path, list_dir, move_file)
from deskaudit.telegram import TelegramBot  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _bot(db, explorer, root):
    return TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                       project_root=root, file_explorer=explorer)


def _last_markup(calls):
    for method, params in reversed(calls):
        if method in ("sendMessage", "editMessageText") \
                and params.get("reply_markup"):
            return params["reply_markup"]
    return {}


def test_list_dir_sorting_paging():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "zebra").mkdir()
    (root / "apple").mkdir()
    (root / "z.txt").write_text("z", encoding="utf-8")
    (root / "m.txt").write_text("m", encoding="utf-8")
    page = list_dir(root, 0, 2)
    assert page["total"] == 4 and page["pages"] == 2
    # Folders first (alpha), then files (alpha).
    assert [e["name"] for e in page["entries"]] == ["apple", "zebra"]
    page2 = list_dir(root, 1, 2)
    assert [e["name"] for e in page2["entries"]] == ["m.txt", "z.txt"]
    assert "পাওয়া যায়নি" in list_dir(root / "nope", 0, 20)["error"]
    print("list dir sorting paging passed")


def test_system_path_guard():
    assert is_system_path(r"C:\Windows\System32\x.dll")
    assert is_system_path("D:/Program Files/app/x.exe")
    assert is_system_path(r"C:\ProgramData\pkg\f.exe")
    assert not is_system_path("C:/")
    assert not is_system_path(r"D:\Accounts\file.xlsx")
    assert not is_system_path(r"C:\Users\Firoza\file.txt")
    print("system path guard passed")


def test_copy_move():
    tmp = tempfile.mkdtemp()
    src_dir = Path(tmp) / "src"
    dst_dir = Path(tmp) / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    f = src_dir / "report.xlsx"
    f.write_text("data", encoding="utf-8")
    ok, _msg, target = copy_file(f, dst_dir)
    assert ok and Path(target).read_text(encoding="utf-8") == "data"
    assert f.exists()  # copy keeps the original
    ok, _msg, target2 = copy_file(f, dst_dir)
    assert ok and target2.endswith("report (2).xlsx")  # collision suffix
    ok, msg, _t = copy_file(f, src_dir)
    assert not ok and "এই ফোল্ডারেই" in msg
    ok, msg, _t = copy_file(f, r"C:\Windows")
    assert not ok and "সিস্টেম ফোল্ডারে" in msg
    ok, msg, target3 = move_file(f, dst_dir)
    assert ok and Path(target3).exists() and not f.exists()
    print("copy move passed")


def test_hide_unhide():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    work = Path(tmp) / "Docs"
    work.mkdir()
    explorer = FileExplorer(db, {}, _NullLog(), project_root=root)
    victim = work / "secret.docx"
    victim.write_text("top secret", encoding="utf-8")
    ok, _msg, item = explorer.hide(victim)
    assert ok and item and item["id"]
    assert not victim.exists()
    assert Path(item["hidden_path"]).read_text(encoding="utf-8") == "top secret"
    row = db.get_hidden(item["id"])
    assert row["status"] == "hidden"
    assert row["original_path"] == str(victim)
    # Unhide restores it to the exact original place.
    assert "ফিরিয়ে দেওয়া হয়েছে" in explorer.unhide(item["id"])
    assert victim.read_text(encoding="utf-8") == "top secret"
    assert db.get_hidden(item["id"])["status"] == "restored"
    assert "আগেই" in explorer.unhide(item["id"])
    # Hide a missing file / a system file is refused.
    assert not explorer.hide(work / "gone.txt")[0]
    assert not explorer.hide(r"C:\Windows\x.dll")[0]
    # A name clash at the original place gets a suffix, not an overwrite.
    victim.write_text("v2", encoding="utf-8")
    ok, _msg, item2 = explorer.hide(victim)
    assert ok
    victim.write_text("new file in the way", encoding="utf-8")
    assert "ফিরিয়ে দেওয়া হয়েছে" in explorer.unhide(item2["id"])
    assert victim.read_text(encoding="utf-8") == "new file in the way"
    clash = list(work.glob("secret (ফেরত *).docx"))
    assert len(clash) == 1 and clash[0].read_text(encoding="utf-8") == "v2"
    print("hide unhide passed")


def test_can_send_and_roots():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    explorer = FileExplorer(db, {"max_send_mb": 50}, _NullLog(),
                            project_root=Path(tmp))
    assert explorer.can_send(50 * 1024 * 1024)
    assert not explorer.can_send(50 * 1024 * 1024 + 1)
    assert not explorer.can_send(0)
    assert not explorer.can_send(None)
    limited = FileExplorer(db, {"allowed_roots": [tmp]}, _NullLog(),
                           project_root=Path(tmp))
    assert limited.roots() == [tmp]
    print("can send and roots passed")


def test_telegram_explorer_flow():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    drive = Path(tmp) / "drive"
    docs = drive / "Docs"
    docs.mkdir(parents=True)
    (docs / "a.txt").write_text("aaa", encoding="utf-8")
    (docs / "b.txt").write_text("bbb", encoding="utf-8")
    explorer = FileExplorer(db, {"allowed_roots": [str(drive)]}, _NullLog(),
                            project_root=root)
    bot = _bot(db, explorer, root)
    calls = []

    def fake_post(token, method, params=None, **_kw):
        calls.append((method, params or {}))
        return {"ok": True, "result": {"message_id": 7}}

    orig_post = tg._post
    orig_send_doc = tg.send_document
    sent_docs = []
    tg._post = fake_post
    tg.send_document = lambda *a, **k: sent_docs.append(a) or True
    try:
        # /explore shows the (single) allowed root as a button.
        bot.handle_text("/explore")
        markup = _last_markup(calls)
        assert markup["inline_keyboard"][0][0]["callback_data"] == "x:drv:0"
        # Open the drive -> the Docs folder is listed, one edit (msg 7).
        bot.handle_callback("x:drv:0", "cb1")
        assert calls[-1][0] == "editMessageText"
        markup = _last_markup(calls)
        assert markup["inline_keyboard"][0][0]["callback_data"] == "x:o:0"
        # Open Docs -> two file buttons.
        bot.handle_callback("x:o:0", "cb2")
        markup = _last_markup(calls)
        assert markup["inline_keyboard"][0][0]["callback_data"] == "x:f:0"
        assert markup["inline_keyboard"][1][0]["callback_data"] == "x:f:1"
        # Select a.txt -> action buttons appear.
        bot.handle_callback("x:f:0", "cb3")
        markup = _last_markup(calls)
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row]
        assert "x:act:send" in flat and "x:act:copy" in flat
        assert "x:act:move" in flat and "x:act:hide" in flat
        # Send it: the document goes out.
        bot.handle_callback("x:act:send", "cb4")
        assert len(sent_docs) == 1 and str(sent_docs[0][2]).endswith("a.txt")
        # Copy it: pick the destination (up to the drive root), confirm.
        bot.handle_callback("x:act:copy", "cb5")
        bot.handle_callback("x:up", "cb6")
        markup = _last_markup(calls)
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row]
        assert "x:dst" in flat  # "এখানেই কপি করো" while choosing a dest
        bot.handle_callback("x:dst", "cb7")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        assert yes.startswith("confirm:yes:")
        token = yes.split(":")[2]
        assert "কার্যকর নয়" in bot._run_pending("deadbeef", from_button=True)
        assert "কপি হয়েছে" in bot._run_pending(token, from_button=True)
        assert (drive / "a.txt").read_text(encoding="utf-8") == "aaa"
        # Hide b.txt, then restore it from /hidden.
        bot.handle_callback("x:o:0", "cb8")
        bot.handle_callback("x:f:1", "cb9")
        bot.handle_callback("x:act:hide", "cb10")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        token = yes.split(":")[2]
        assert "হাইড হয়েছে" in bot._run_pending(token, from_button=True)
        assert not (docs / "b.txt").exists()
        assert len(db.hidden_held()) == 1
        hid = db.hidden_held()[0]["id"]
        assert "b.txt" in tg.build_hidden_text(db)
        bot.send_hidden_list()
        markup = _last_markup(calls)
        assert markup["inline_keyboard"][0][0]["callback_data"] == f"x:uh:{hid}"
        bot.handle_callback(f"x:uh:{hid}", "cb11")
        markup = _last_markup(calls)
        token = markup["inline_keyboard"][0][0]["callback_data"].split(":")[2]
        assert "ফিরিয়ে দেওয়া হয়েছে" in bot._run_pending(token, from_button=True)
        assert (docs / "b.txt").read_text(encoding="utf-8") == "bbb"
        print("telegram explorer flow passed")
    finally:
        tg._post = orig_post
        tg.send_document = orig_send_doc


def test_send_too_big_refused():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    drive = Path(tmp) / "drive"
    drive.mkdir()
    big = drive / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    explorer = FileExplorer(db, {"allowed_roots": [str(drive)],
                                 "max_send_mb": 1}, _NullLog(),
                            project_root=root)
    bot = _bot(db, explorer, root)
    calls = []

    def fake_post(token, method, params=None, **_kw):
        calls.append((method, params or {}))
        return {"ok": True, "result": {"message_id": 7}}

    orig_post = tg._post
    tg._post = fake_post
    try:
        bot.handle_text("/explore")
        bot.handle_callback("x:drv:0", "cb1")
        bot.handle_callback("x:f:0", "cb2")
        bot.handle_callback("x:act:send", "cb3")
        assert any("1 MB-এর বেশি" in (p.get("text") or "")
                   for _m, p in calls)
        print("send too big refused passed")
    finally:
        tg._post = orig_post


def test_settings_roundtrip():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "config.yaml").write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    from deskaudit.dashboard import get_settings, save_settings

    before = get_settings(root / "config.yaml")
    assert before["explorer"]["enabled"] is True
    assert before["explorer"]["page_size"] == 20
    assert before["explorer"]["max_send_mb"] == 50
    save_settings(root / "config.yaml", {"explorer": {
        "enabled": True, "hidden_path": "E:/DeskAudit-Hidden",
        "page_size": 10, "max_send_mb": 25,
        "allowed_roots": "D:/, E:/Accounts"}})
    after = get_settings(root / "config.yaml")
    ex = after["explorer"]
    assert ex["hidden_path"] == "E:/DeskAudit-Hidden"
    assert ex["page_size"] == 10
    assert ex["max_send_mb"] == 25
    assert ex["allowed_roots"] == ["D:/", "E:/Accounts"]
    print("settings roundtrip passed")


if __name__ == "__main__":
    test_list_dir_sorting_paging()
    test_system_path_guard()
    test_copy_move()
    test_hide_unhide()
    test_can_send_and_roots()
    test_telegram_explorer_flow()
    test_send_too_big_refused()
    test_settings_roundtrip()
    print("ALL FILE EXPLORER TESTS PASSED")
