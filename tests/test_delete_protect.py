"""Tests for delete protection (v12.8): Recycle Bin watch, the
protected-folder mirror, quarantine move/restore/purge, and the
Telegram হ্যাঁ/না choice (হ্যাঁ = purge, না = restore).

Network is fully mocked (tg._post replaced with a fake); the fake
Recycle Bin and protected folders live in temp dirs, so nothing
touches a real drive or a real Telegram chat.
"""

import json
import os
import struct
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import deskaudit.telegram as tg  # noqa: E402
from deskaudit.database import Database  # noqa: E402
from deskaudit.delete_protect import (  # noqa: E402
    DeleteProtector, is_temp_path, parse_recycle_info)
from deskaudit.telegram import TelegramBot  # noqa: E402
from deskaudit.utils import today_str  # noqa: E402


def _make_info_bin(tmp, original_path, content=b"hello"):
    """A fake $Recycle.Bin/<SID>/ pair: $I metadata + $R content."""
    sid = Path(tmp) / "$Recycle.Bin" / "S-1-5-21-1"
    sid.mkdir(parents=True, exist_ok=True)
    deleted_ft = 133600000000000000  # a plausible FILETIME
    data = (struct.pack("<q", 1) + struct.pack("<q", len(content))
            + struct.pack("<q", deleted_ft)
            + original_path.encode("utf-16-le") + b"\x00\x00")
    (sid / "$IABCDEF.docx").write_bytes(data)
    (sid / "$RABCDEF.docx").write_bytes(content)
    return sid


def _bot(db, protector):
    return TelegramBot(db, {"bot_token": "tok", "chat_id": "999"},
                       delete_protector=protector)


def test_parse_recycle_info():
    tmp = tempfile.mkdtemp()
    sid = _make_info_bin(tmp, r"C:\Users\Firoza\Documents\report.docx", b"data123")
    parsed = parse_recycle_info(sid / "$IABCDEF.docx")
    assert parsed is not None
    assert parsed["original_path"] == r"C:\Users\Firoza\Documents\report.docx"
    assert parsed["size_bytes"] == 7
    assert isinstance(parsed["deleted_at"], datetime)
    assert parse_recycle_info(sid / "$RABCDEF.docx") is None  # not an $I file
    print("parse recycle info passed")


def test_is_temp_path():
    assert is_temp_path(r"C:\Users\Firoza\AppData\Local\Temp\foo.tmp")
    assert is_temp_path(r"C:\Windows\Temp\setup.log")
    assert is_temp_path(r"D:\Temp\cache.dat")
    assert is_temp_path(r"C:\$Recycle.Bin\S-1-5-21\$RABC.txt")
    assert is_temp_path(r"E:\DeskAudit-Quarantine\held\abc\file.docx",
                        quarantine_root=r"E:\DeskAudit-Quarantine")
    assert not is_temp_path(r"C:\Users\Firoza\Documents\report.docx")
    assert not is_temp_path(r"D:\Accounts\invoice.xlsx")
    print("temp path exclusion passed")


def test_recycle_bin_scan_quarantines():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    sent = []
    protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                notify=lambda item: sent.append(item) or True)
    sid = _make_info_bin(tmp, r"D:\Office\report.docx", b"report-bytes")
    found = protector.scan_recycle_bins([Path(tmp) / "$Recycle.Bin"])
    assert len(found) == 1, found
    item = found[0]
    assert item["file_name"] == "report.docx"
    assert item["source"] == "recycle_bin"
    assert Path(item["quarantine_path"]).read_bytes() == b"report-bytes"
    assert not (sid / "$RABCDEF.docx").exists()  # moved out of the bin
    assert not (sid / "$IABCDEF.docx").exists()  # orphan $I dropped
    assert sent and sent[0]["id"] == item["id"]
    row = db.get_quarantined(item["id"])
    assert row is not None and row["status"] == "held"
    # Second scan: already seen, nothing again.
    assert protector.scan_recycle_bins([Path(tmp) / "$Recycle.Bin"]) == []
    print("recycle bin scan passed")


def test_recycle_bin_skips_temp_and_missing():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    protector = DeleteProtector(db, {}, _NullLog(), project_root=Path(tmp) / "p")
    sid = _make_info_bin(tmp, r"C:\Users\Firoza\AppData\Local\Temp\junk.tmp")
    found = protector.scan_recycle_bins([Path(tmp) / "$Recycle.Bin"])
    assert found == []  # TEMP files are never protected
    # $I with no $R sibling (already emptied) is skipped, not an error.
    (sid / "$RABCDEF.docx").unlink(missing_ok=True)
    sid2 = _make_info_bin(tmp, r"D:\Office\gone.docx")
    (sid2 / "$RABCDEF.docx").unlink()
    assert protector.scan_recycle_bins([Path(tmp) / "$Recycle.Bin"]) == []
    print("recycle bin skips passed")


def test_mirror_delete_restore_purge():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    protected = Path(tmp) / "Documents"
    protected.mkdir()
    target = protected / "notes.txt"
    target.write_text("important notes", encoding="utf-8")
    sent = []
    protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                protected_folders=[protected],
                                notify=lambda item: sent.append(item) or True)
    # First scan mirrors the file.
    assert protector.scan_protected() == []
    mirror_copies = list((root / "data" / "quarantine" / "mirror").glob("*"))
    assert len(mirror_copies) == 1
    assert mirror_copies[0].read_text(encoding="utf-8") == "important notes"
    # Shift+Delete: the file is gone, the mirror is quarantined.
    target.unlink()
    found = protector.scan_protected()
    assert len(found) == 1, found
    assert found[0]["source"] == "mirror"
    assert found[0]["file_name"] == "notes.txt"
    assert sent and sent[0]["id"] == found[0]["id"]
    # না (restore): the file comes back to its exact original place.
    assert "ফিরে এসেছে" in protector.restore(found[0]["id"])
    assert target.read_text(encoding="utf-8") == "important notes"
    row = db.get_quarantined(found[0]["id"])
    assert row["status"] == "restored"
    # Delete it again, then হ্যাঁ (purge): the copy is gone for good.
    target.unlink()
    found = protector.scan_protected()
    assert len(found) == 1
    assert "পারমেনেন্টলি" in protector.purge(found[0]["id"])
    assert db.get_quarantined(found[0]["id"])["status"] == "purged"
    print("mirror delete/restore/purge passed")


def test_mirror_rename_is_not_a_delete():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    protected = Path(tmp) / "Documents"
    protected.mkdir()
    old = protected / "old.txt"
    old.write_text("renamed", encoding="utf-8")
    protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                protected_folders=[protected])
    assert protector.scan_protected() == []
    new = protected / "new.txt"
    old.rename(new)
    db.log_file_event("moved", str(old), dest_path=str(new))
    assert protector.scan_protected() == []  # a rename is not a delete
    assert new.exists()
    print("mirror rename passed")


def test_telegram_delete_choice():
    tmp = tempfile.mkdtemp()
    db = Database(Path(tmp) / "db.sqlite")
    root = Path(tmp) / "project"
    protected = Path(tmp) / "Documents"
    protected.mkdir()
    calls = []

    def fake_post(token, method, params=None, **_kw):
        calls.append((method, params or {}))
        return {"ok": True, "result": {"message_id": 1}}

    orig_post = tg._post
    tg._post = fake_post
    try:
        protector = DeleteProtector(db, {}, _NullLog(), project_root=root,
                                    protected_folders=[protected])
        bot = _bot(db, protector)
        victim = protected / "secret.docx"
        victim.write_text("top secret", encoding="utf-8")
        protector.scan_protected()
        victim.unlink()
        found = protector.scan_protected()
        assert len(found) == 1
        # The protector has no notify here (bot wired after), so send it.
        assert bot.notify_deleted(found[0])
        assert calls, "no Telegram call recorded"
        markup = calls[-1][1].get("reply_markup", {})
        buttons = markup["inline_keyboard"][0]
        assert "confirm:yes:" in buttons[0]["callback_data"]
        assert "confirm:no:" in buttons[1]["callback_data"]
        token = buttons[0]["callback_data"].split(":")[2]
        # Wrong token must not decide anything.
        assert "কার্যকর নয়" in bot._run_pending("deadbeef", from_button=True)
        # হ্যাঁ on the right token purges the copy for good.
        assert "পারমেনেন্টলি" in bot._run_pending(token, from_button=True)
        assert db.get_quarantined(found[0]["id"])["status"] == "purged"

        # A second file: replying "না" restores it instead of cancelling.
        victim2 = protected / "keep.docx"
        victim2.write_text("keep me", encoding="utf-8")
        protector.scan_protected()
        victim2.unlink()
        found2 = protector.scan_protected()
        assert len(found2) == 1
        assert bot.notify_deleted(found2[0])
        bot.handle_text("না")
        assert victim2.read_text(encoding="utf-8") == "keep me"
        assert db.get_quarantined(found2[0]["id"])["status"] == "restored"
        # /deleted lists what is held.
        text = tg.build_deleted_text(db)
        assert "সুরক্ষায়" in text
        print("telegram delete choice passed")
    finally:
        tg._post = orig_post


def test_cli_delete_protect_test():
    """Regression: --delete-protect-test crashed with UnboundLocalError
    (the CLI branch used `db` before any branch had created it)."""
    import subprocess

    tmp = tempfile.mkdtemp()
    cfg = Path(tmp) / "config.yaml"
    cfg.write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    proc = subprocess.run(
        [sys.executable, str(main_py), "--config", str(cfg),
         "--delete-protect-test"],
        capture_output=True, text=True, timeout=60, cwd=tmp)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Quarantine:" in proc.stdout, proc.stdout
    assert "No fresh deletes found" in proc.stdout, proc.stdout
    print("cli delete-protect test passed")


def test_settings_roundtrip():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "config.yaml").write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    from deskaudit.dashboard import get_settings, save_settings

    before = get_settings(root / "config.yaml")
    assert before["delete_protect"]["enabled"] is True
    assert before["delete_protect"]["poll_seconds"] == 30
    save_settings(root / "config.yaml", {"delete_protect": {
        "enabled": True, "quarantine_path": "E:/DeskAudit-Quarantine",
        "poll_seconds": 45, "mirror_max_mb": 200,
        "protected_folders": "D:/Accounts, D:/HR"}})
    after = get_settings(root / "config.yaml")
    dp = after["delete_protect"]
    assert dp["quarantine_path"] == "E:/DeskAudit-Quarantine"
    assert dp["poll_seconds"] == 45
    assert dp["mirror_max_mb"] == 200
    assert dp["protected_folders"] == ["D:/Accounts", "D:/HR"]
    print("settings roundtrip passed")


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


if __name__ == "__main__":
    test_parse_recycle_info()
    test_is_temp_path()
    test_recycle_bin_scan_quarantines()
    test_recycle_bin_skips_temp_and_missing()
    test_mirror_delete_restore_purge()
    test_mirror_rename_is_not_a_delete()
    test_telegram_delete_choice()
    test_cli_delete_protect_test()
    test_settings_roundtrip()
    print("ALL DELETE PROTECT TESTS PASSED")
