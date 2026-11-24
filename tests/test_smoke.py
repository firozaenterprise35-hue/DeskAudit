"""Smoke test — runs on any OS, no Windows APIs, no real tracking.

Run:  python -m pytest tests/   or   python tests/test_smoke.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.config import Config
from deskaudit.database import Database
from deskaudit.report import generate_report
from deskaudit.utils import format_duration


def test_format_duration():
    assert format_duration(45) == "45s"
    assert format_duration(125) == "2m"
    assert format_duration(7320) == "2h 2m"


def test_db_and_report(tmp_path=None):
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone()
    sid = db.start_app_session("chrome.exe", r"C:\x\chrome.exe", "Example", now)
    db.close_app_session(sid, now + timedelta(minutes=10), 600, 540, 60)
    db.log_file_event("created", r"C:\Users\Firoza\Desktop\notes.txt")
    date = now.strftime("%Y-%m-%d")
    text, md_path, csv_path = generate_report(db, date, tmp / "reports")
    assert "chrome.exe" in text
    assert md_path.exists() and csv_path.exists()
    db.close()
    print("smoke test passed")


if __name__ == "__main__":
    test_format_duration()
    test_db_and_report()
