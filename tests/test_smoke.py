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


def test_ai_summary_cp1252_console(tmp_path=None):
    """Regression: --ai-summary prints a Bengali narrative. On a Windows
    console (cp1252/cp437) that print crashed with UnicodeEncodeError
    because main() never set the console encoding. main() must reconfigure
    stdout to UTF-8 first, so the same call works on a cp1252 console."""
    import io
    import os

    import main as main_mod

    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    (tmp / "config.yaml").write_text(
        "db_path: data/test.db\nlog_path: data/test.log\n", encoding="utf-8")
    db = Database(tmp / "data" / "test.db")
    now = datetime.now().astimezone()
    sid = db.start_app_session("chrome.exe", r"C:\x\chrome.exe", "Example", now)
    db.close_app_session(sid, now + timedelta(minutes=10), 600, 540, 60)
    db.close()
    date = now.strftime("%Y-%m-%d")
    old_cwd, old_stdout = os.getcwd(), sys.stdout
    buf = io.BytesIO()
    cp1252_out = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    try:
        os.chdir(tmp)
        sys.stdout = cp1252_out
        rc = main_mod.main(["--ai-summary", "--date", date,
                            "--config", str(tmp / "config.yaml")])
        cp1252_out.flush()  # TextIOWrapper buffers; flush before reading
    finally:
        sys.stdout = old_stdout
        os.chdir(old_cwd)
    assert rc == 0
    assert (tmp / "reports" / f"{date}-summary.md").exists()
    printed = buf.getvalue().decode("utf-8")
    assert "আপনি" in printed  # Bengali narrative reached the console
    print("cp1252 console summary test passed")


if __name__ == "__main__":
    test_format_duration()
    test_db_and_report()
    test_ai_summary_cp1252_console()
