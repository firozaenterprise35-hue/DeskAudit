"""Backup & restore tests — synthetic DB only, no real data."""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.backup import (
    latest_backup, list_backups, make_backup, prune_backups, restore_backup,
)
from deskaudit.database import Database


def seed_db(tmp: Path) -> Path:
    db_path = tmp / "data" / "deskaudit.db"
    db = Database(db_path)
    now = datetime.now().astimezone()
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", now, project="Office")
    db.close_app_session(sid, now, 600, 600, 0)
    db.close()
    return db_path


def test_backup_restore():
    tmp = Path(tempfile.mkdtemp())
    db_path = seed_db(tmp)
    dest = make_backup(db_path, tmp)
    assert dest.exists() and dest.name.startswith("deskaudit-")
    assert len(list_backups(tmp)) == 1
    assert latest_backup(tmp) == dest
    # Add a second app session, then restore -> it must disappear.
    db = Database(db_path)
    now = datetime.now().astimezone()
    sid = db.start_app_session("chrome.exe", "", "Web", now, project="Personal")
    db.close_app_session(sid, now, 600, 600, 0)
    assert db.totals(now.strftime("%Y-%m-%d"))["apps"] == 2
    db.close()
    safety = restore_backup(db_path, dest, tmp)
    assert safety is not None and Path(safety).exists()
    db = Database(db_path)
    assert db.totals(now.strftime("%Y-%m-%d"))["apps"] == 1
    db.close()
    # Restoring the safety copy brings the second session back.
    restore_backup(db_path, safety, tmp)
    db = Database(db_path)
    assert db.totals(now.strftime("%Y-%m-%d"))["apps"] == 2
    db.close()
    print("backup/restore tests passed")


def test_prune():
    tmp = Path(tempfile.mkdtemp())
    db_path = seed_db(tmp)
    for _ in range(3):
        make_backup(db_path, tmp, keep_count=30)
        time.sleep(1.1)
    assert len(list_backups(tmp)) == 3
    deleted = prune_backups(tmp / "backups", keep_days=30, keep_count=2)
    assert len(deleted) == 1
    assert len(list_backups(tmp)) == 2
    print("prune tests passed")


if __name__ == "__main__":
    test_backup_restore()
    test_prune()
