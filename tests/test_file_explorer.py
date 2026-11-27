"""Tests for the Telegram file explorer (v12.9, 360\u00b0 in v12.11):
drive/folder browsing, copy / move / send / hide for files AND
folders, rename, new folder, search, delete into quarantine, the
/hidden list with paging, and the dashboard settings roundtrip.

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
    FileExplorer, copy_dir, copy_file, dir_size, drive_free,
    is_system_path, list_dir, move_dir, move_file, norm_root,
    valid_name)
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



def _flat_markup(calls):
    markup = _last_markup(calls)
    return [b["callback_data"] for row in markup["inline_keyboard"]
            for b in row]


def _last_text(calls):
    for method, params in reversed(calls):
        if method == "sendMessage" and params.get("text"):
            return params["text"]
    return ""


def test_norm_root_dir_size_valid_name():
    assert norm_root("C:") == "C:\\"
    assert norm_root("D:") == "D:\\"
    assert norm_root("/tmp/x") == "/tmp/x"
    assert norm_root("C:\\Users") == "C:\\Users"
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "sub").mkdir()
    (root / "a.txt").write_text("12345", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("12", encoding="utf-8")
    size, done = dir_size(root)
    assert size == 7 and done
    size, done = dir_size(root, cap_bytes=6)
    assert size >= 6 and not done
    assert drive_free(root) == "" or drive_free(root).endswith(
        ("B", "KB", "MB", "GB"))
    assert valid_name("নতুন ফোল্ডার")[0]
    assert not valid_name("")[0]
    assert not valid_name("..")[0]
    assert not valid_name("a/b")[0]
    assert not valid_name("a:b")[0]
    assert not valid_name("a?b")[0]
    print("norm root dir size valid name passed")


def test_copy_move_dir():
    import deskaudit.file_explorer as fe

    tmp = tempfile.mkdtemp()
    src_dir = Path(tmp) / "src"
    dst_dir = Path(tmp) / "dst"
    src = src_dir / "Photos"
    (src / "inner").mkdir(parents=True)
    (src / "a.jpg").write_text("a", encoding="utf-8")
    (src / "inner" / "b.jpg").write_text("b", encoding="utf-8")
    dst_dir.mkdir()
    ok, _msg, target = copy_dir(src, dst_dir)
    assert ok and Path(target, "inner", "b.jpg").read_text() == "b"
    assert src.exists()  # copy keeps the original
    ok, _msg, target2 = copy_dir(src, dst_dir)
    assert ok and target2.endswith("Photos (2)")
    ok, msg, _t = copy_dir(src, src)
    assert not ok and "নিজের ভেতরে" in msg
    ok, msg, _t = copy_dir(src, r"C:\Windows")
    assert not ok and "সিস্টেম ফোল্ডারে" in msg
    old_cap = fe.FOLDER_COPY_MAX_BYTES
    fe.FOLDER_COPY_MAX_BYTES = 1
    try:
        ok, msg, _t = copy_dir(src, dst_dir)
        assert not ok and "অনেক বড়" in msg
    finally:
        fe.FOLDER_COPY_MAX_BYTES = old_cap
    ok, _msg, target3 = move_dir(src, dst_dir)
    assert ok and Path(target3, "a.jpg").exists() and not src.exists()
    print("copy move dir passed")


def test_hide_unhide_folder_and_dead_row():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    work = Path(tmp) / "Docs"
    work.mkdir()
    explorer = FileExplorer(db, {}, _NullLog(), project_root=root)
    folder = work / "Secret"
    (folder / "inner").mkdir(parents=True)
    (folder / "s.txt").write_text("s", encoding="utf-8")
    ok, _msg, item = explorer.hide(folder)
    assert ok and item and item["id"]
    assert not folder.exists()
    assert Path(item["hidden_path"], "s.txt").read_text() == "s"
    assert "ফিরিয়ে দেওয়া হয়েছে" in explorer.unhide(item["id"])
    assert (folder / "s.txt").read_text() == "s"
    # Hiding something already inside the hidden folder is refused
    # (relative hidden_path included — the v12.9 startswith check
    # missed that case).
    rel = FileExplorer(db, {"hidden_path": "hidden"}, _NullLog(),
                       project_root=root)
    inside = root / "hidden" / "already.txt"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("x", encoding="utf-8")
    ok, msg, _i = rel.hide(inside)
    assert not ok and "আগেই লুকানো" in msg
    # A hidden copy that vanished is marked purged, not listed forever.
    victim = work / "gone.docx"
    victim.write_text("v", encoding="utf-8")
    ok, _msg, item2 = explorer.hide(victim)
    assert ok
    Path(item2["hidden_path"]).unlink()
    assert "আর নেই" in explorer.unhide(item2["id"])
    assert db.get_hidden(item2["id"])["status"] == "purged"
    assert all(r["id"] != item2["id"] for r in db.hidden_held())
    print("hide unhide folder and dead row passed")


def test_quarantine_user():
    from deskaudit.delete_protect import DeleteProtector

    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    work = Path(tmp) / "Docs"
    work.mkdir()
    notified = []
    protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                notify=lambda item: notified.append(item) or True)
    f = work / "old.txt"
    f.write_text("old", encoding="utf-8")
    ok, msg = protector.quarantine_user(f)
    assert ok and "সুরক্ষায় রাখা আছে" in msg
    assert not f.exists()
    assert len(notified) == 1 and notified[0]["source"] == "telegram"
    assert len(db.quarantined_held()) == 1
    qid = db.quarantined_held()[0]["id"]
    assert "ফিরে এসেছে" in protector.restore(qid)
    assert f.read_text() == "old"
    folder = work / "OldFolder"
    (folder / "x").mkdir(parents=True)
    (folder / "x" / "y.txt").write_text("y", encoding="utf-8")
    ok, _msg = protector.quarantine_user(folder)
    assert ok and not folder.exists()
    assert not protector.quarantine_user(work / "nope.txt")[0]
    assert not protector.quarantine_user(r"C:\Windows\system32")[0]
    print("quarantine user passed")


def test_explorer_folder_actions():
    from deskaudit.delete_protect import DeleteProtector

    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    drive = Path(tmp) / "drive"
    photos = drive / "Photos"
    (photos / "inner").mkdir(parents=True)
    (photos / "a.jpg").write_text("a", encoding="utf-8")
    (photos / "inner" / "b.jpg").write_text("b", encoding="utf-8")
    (drive / "top.txt").write_text("top", encoding="utf-8")
    explorer = FileExplorer(db, {"allowed_roots": [str(drive)]}, _NullLog(),
                            project_root=root)
    protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                notify=lambda item: True)
    bot = TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                      project_root=root, file_explorer=explorer,
                      delete_protector=protector)
    calls = []
    sent_docs = []

    def fake_post(token, method, params=None, **_kw):
        calls.append((method, params or {}))
        return {"ok": True, "result": {"message_id": 7}}

    orig_post = tg._post
    orig_send_doc = tg.send_document
    tg._post = fake_post
    tg.send_document = lambda *a, **k: sent_docs.append(a) or True
    try:
        bot.handle_text("/explore")
        bot.handle_callback("x:drv:0", "cb1")
        flat = _flat_markup(calls)
        assert "x:o:0" in flat and "x:fsel:0" in flat  # folder + ⚙️
        assert "x:mkdir" in flat and "x:search" in flat
        # Select the folder with ⚙️ -> details + folder actions.
        bot.handle_callback("x:fsel:0", "cb2")
        flat = _flat_markup(calls)
        assert "x:act:send" in flat and "x:act:copy" in flat
        assert "x:act:rename" in flat and "x:act:delete" in flat
        assert any("ফোল্ডার —" in (p.get("text") or "")
                   for _m, p in calls)
        # Rename it by typing the new name.
        bot.handle_callback("x:act:rename", "cb3")
        assert "নতুন নাম লিখুন" in _last_text(calls)
        bot.handle_text("ছবি")
        assert (drive / "ছবি" / "a.jpg").exists()
        assert not photos.exists()
        # New folder by typing its name.
        bot.handle_callback("x:back", "cb4")
        bot.handle_callback("x:mkdir", "cb5")
        bot.handle_text("নতুন")
        assert (drive / "নতুন").is_dir()
        # Search the drive for "top".
        bot.handle_callback("x:search", "cb6")
        bot.handle_text("top")
        assert any("top.txt" in (p.get("text") or "") for _m, p in calls)
        # Copy the folder INTO নতুন: select ছবি, copy, open নতুন, dest.
        bot.handle_callback("x:fsel:0", "cb7")  # ⚙️ on ছবি
        bot.handle_callback("x:act:copy", "cb8")
        bot.handle_callback("x:o:1", "cb9")  # open নতুন (2nd folder)
        flat = _flat_markup(calls)
        assert "x:dst" in flat
        bot.handle_callback("x:dst", "cb10")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        token = yes.split(":")[-1]
        assert "কপি হয়েছে" in bot._run_pending(token, from_button=True)
        assert (drive / "নতুন" / "ছবি" / "a.jpg").exists()
        # The browsing message was refreshed — no stale "কপি করব" line.
        edits = [p for m, p in calls if m == "editMessageText"]
        assert edits and "কপি করব" not in edits[-1].get("text", "")
        # Send the copied folder as a zip.
        bot.handle_callback("x:fsel:0", "cb11")  # ⚙️ on ছবি (in নতুন)
        bot.handle_callback("x:act:send", "cb12")
        assert len(sent_docs) == 1 and str(sent_docs[0][2]).endswith(".zip")
        # Hide the copied folder, then restore it from /hidden.
        bot.handle_callback("x:act:hide", "cb13")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        token = yes.split(":")[-1]
        assert "হাইড হয়েছে" in bot._run_pending(token, from_button=True)
        assert not (drive / "নতুন" / "ছবি").exists()
        assert len(db.hidden_held()) == 1
        hid = db.hidden_held()[0]["id"]
        bot.handle_callback(f"x:uh:{hid}", "cb14")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        token = yes.split(":")[-1]
        assert "ফিরিয়ে দেওয়া হয়েছে" in bot._run_pending(token, from_button=True)
        assert (drive / "নতুন" / "ছবি" / "a.jpg").exists()
        # Delete top.txt from the explorer -> quarantine + /deleted.
        bot.handle_callback("x:up", "cb15")  # back to the drive root
        bot.handle_callback("x:f:2", "cb16")  # top.txt (3rd entry)
        bot.handle_callback("x:act:delete", "cb17")
        markup = _last_markup(calls)
        yes = markup["inline_keyboard"][0][0]["callback_data"]
        token = yes.split(":")[-1]
        assert "সুরক্ষায় রাখা আছে" in bot._run_pending(token, from_button=True)
        assert not (drive / "top.txt").exists()
        assert len(db.quarantined_held()) == 1
        assert "top.txt" in tg.build_deleted_text(db)
        # Stale pending: a hide confirm dies when the owner navigates.
        bot.handle_callback("x:o:0", "cb18")  # open ছবি
        bot.handle_callback("x:f:1", "cb19")  # select a.jpg
        bot.handle_callback("x:act:hide", "cb20")
        assert bot._get_pending() is not None
        bot.handle_callback("x:up", "cb21")
        assert bot._get_pending() is None
        # A file tap while choosing a destination only hints.
        bot.handle_callback("x:o:0", "cb22")
        bot.handle_callback("x:f:1", "cb23")
        bot.handle_callback("x:act:copy", "cb24")
        bot.handle_callback("x:up", "cb25")
        bot.handle_callback("x:o:0", "cb26")  # open ছবি again (mode on)
        bot.handle_callback("x:f:1", "cb27")  # tap a.jpg while choosing
        assert "গন্তব্য ফোল্ডার" in _last_text(calls)
        state = bot._explorer_state()
        assert state["mode"] == "copy" and not state["sel"]
        print("explorer folder actions passed")
    finally:
        tg._post = orig_post
        tg.send_document = orig_send_doc


def test_hidden_pagination_and_open_file():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    drive = Path(tmp) / "drive"
    drive.mkdir()
    explorer = FileExplorer(db, {"allowed_roots": [str(drive)]}, _NullLog(),
                            project_root=root)
    bot = _bot(db, explorer, root)
    calls = []

    def fake_post(token, method, params=None, **_kw):
        calls.append((method, params or {}))
        return {"ok": True, "result": {"message_id": 7}}

    orig_post = tg._post
    tg._post = fake_post
    try:
        # Hide 12 files -> /hidden pages 10 + 2.
        for i in range(12):
            f = drive / f"f{i:02}.txt"
            f.write_text("x", encoding="utf-8")
            ok, _m, _it = explorer.hide(f)
            assert ok
        bot.send_hidden_list()
        markup = _last_markup(calls)
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row]
        assert sum(1 for c in flat if c.startswith("x:uh:")) == 10
        assert "x:hp:1" in flat
        bot.handle_callback("x:hp:1", "cb1")
        assert calls[-1][0] == "editMessageText"  # pager edits in place
        markup = _last_markup(calls)
        flat = [b["callback_data"] for row in markup["inline_keyboard"]
                for b in row]
        assert sum(1 for c in flat if c.startswith("x:uh:")) == 2
        assert "x:hp:0" in flat
        # /explore <file path> opens the parent and selects the file.
        victim = drive / "keep.txt"
        victim.write_text("keep", encoding="utf-8")
        bot.handle_text(f"/explore {victim}")
        state = bot._explorer_state()
        assert state["cwd"] == str(drive) and state["sel"] == str(victim)
        print("hidden pagination and open file passed")
    finally:
        tg._post = orig_post



if __name__ == "__main__":
    test_list_dir_sorting_paging()
    test_system_path_guard()
    test_copy_move()
    test_hide_unhide()
    test_can_send_and_roots()
    test_telegram_explorer_flow()
    test_send_too_big_refused()
    test_settings_roundtrip()
    test_norm_root_dir_size_valid_name()
    test_copy_move_dir()
    test_hide_unhide_folder_and_dead_row()
    test_quarantine_user()
    test_explorer_folder_actions()
    test_hidden_pagination_and_open_file()
    print("ALL FILE EXPLORER TESTS PASSED")
