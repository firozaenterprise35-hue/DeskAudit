"""Alerts tests — every rule, dedup, and save/list, synthetic data only."""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.alerts import check_alerts, save_alerts
from deskaudit.database import Database
from deskaudit.projects import Project, Tagger


def seed(tmp: Path):
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    yesterday = base - timedelta(days=1)
    date = base.strftime("%Y-%m-%d")
    # Known app yesterday, so WINWORD is not "new" today.
    sid = db.start_app_session("WINWORD.EXE", "", "Old doc", yesterday, project="Office")
    db.close_app_session(sid, yesterday + timedelta(minutes=10), 600, 600, 0)
    # 6 deletions in 5 minutes -> burst + folder burst
    for i in range(6):
        db.log_file_event("deleted", f"/home/user/Documents/Office/file{i}.txt",
                          occurred_at=base + timedelta(minutes=i), project="Office")
    # Unknown app at 02:00 -> night alert + new app
    sid = db.start_app_session("mystery.exe", "", "Mystery", base.replace(hour=2), project="Untagged")
    db.close_app_session(sid, base.replace(hour=2, minute=30), 1800, 1800, 0)
    # Distracting browser 90 min (> 60 min limit)
    bsid = db.start_browser_session("Chrome", "https://youtube.com/watch", "youtube.com",
                                    "Video", "video", False, base + timedelta(hours=1),
                                    project="Personal")
    db.close_browser_session(bsid, base + timedelta(hours=2, minutes=30), 5400, 5400, 0)
    # Large download 600 MB
    db.log_browser_download("Chrome", "https://example.com/big.zip", "example.com",
                            "/tmp/big.zip", "big.zip", 600 * 1024 * 1024, 600 * 1024 * 1024,
                            "complete", base, project="Personal")
    # Idle 3h: add an app session with idle 10800s
    sid = db.start_app_session("chrome.exe", "", "Browsing", base + timedelta(hours=3), project="Personal")
    db.close_app_session(sid, base + timedelta(hours=4), 3600, 0, 10800)
    return db, date


def test_alerts():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed(tmp)
    tagger = Tagger(projects=[
        Project(name="Office", priority=100, daily_goal_minutes=240),
        Project(name="Personal", priority=10),
    ])
    alerts = check_alerts(db, date, tagger=tagger)
    types = {a.alert_type for a in alerts}
    assert "deletion_burst" in types, types
    assert "deletion_folder_burst" in types, types
    assert "unknown_app_night" in types, types
    assert "new_app" in types, types
    assert "distracting_over" in types, types
    assert "idle_over" in types, types
    assert "large_download" in types, types
    # Office goal: 0 active today (only deletions, no app time) and date is
    # today, so goal_missed must NOT fire (past days only).
    assert "project_goal_missed" not in types, types
    # Save + dedup: second save returns 0 new
    new1 = save_alerts(db, alerts)
    assert len(new1) == len(alerts)
    new2 = save_alerts(db, alerts)
    assert new2 == []
    rows = db.list_alerts(date=date)
    assert len(rows) == len(alerts)
    assert db.list_alerts(date=date, unread_only=True)
    db.mark_alert_read(int(rows[0]["id"]))
    assert len(db.list_alerts(date=date, unread_only=True)) == len(alerts) - 1
    db.close()
    print("alerts tests passed")


def test_goal_missed_only_past_days():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    past = datetime.now().astimezone() - timedelta(days=2)
    date = past.strftime("%Y-%m-%d")
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", past, project="Office")
    db.close_app_session(sid, past + timedelta(minutes=10), 600, 600, 0)
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240)])
    alerts = check_alerts(db, date, tagger=tagger)
    types = {a.alert_type for a in alerts}
    assert "project_goal_missed" in types, types
    db.close()
    print("goal-missed test passed")


def test_night_false_positive_fixed():
    """Audit fix 4: an app used in the daytime is NOT unknown at night.
    Day 1 10:00 chrome.exe, Day 2 02:00 chrome.exe -> no night alert."""
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    day1 = datetime.now().astimezone() - timedelta(days=1)
    day1 = day1.replace(hour=10, minute=0, second=0, microsecond=0)
    day2 = day1 + timedelta(days=1)
    night = day2.replace(hour=2, minute=0)
    sid = db.start_app_session("chrome.exe", "", "Daytime", day1, project="Personal")
    db.close_app_session(sid, day1 + timedelta(minutes=30), 1800, 1800, 0)
    sid = db.start_app_session("chrome.exe", "", "Night", night, project="Personal")
    db.close_app_session(sid, night + timedelta(minutes=30), 1800, 1800, 0)
    alerts = check_alerts(db, night.strftime("%Y-%m-%d"))
    assert "unknown_app_night" not in {a.alert_type for a in alerts}, [a.to_dict() for a in alerts]
    # Truly first-ever at night: same setup without the daytime session.
    tmp2 = Path(tempfile.mkdtemp())
    db2 = Database(tmp2 / "test.db")
    sid = db2.start_app_session("mystery.exe", "", "Night", night, project="Untagged")
    db2.close_app_session(sid, night + timedelta(minutes=30), 1800, 1800, 0)
    alerts2 = check_alerts(db2, night.strftime("%Y-%m-%d"))
    assert "unknown_app_night" in {a.alert_type for a in alerts2}, [a.to_dict() for a in alerts2]
    db.close(); db2.close()
    print("night false-positive test passed")


def test_new_app_quiet():
    """Audit fix 5: new_app only for non-safe apps used >= 5 minutes.
    A 1-minute launch and a new chrome.exe must NOT alert."""
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    # 1-minute mystery app
    sid = db.start_app_session("tiny.exe", "", "Tiny", base, project="Untagged")
    db.close_app_session(sid, base + timedelta(minutes=1), 60, 60, 0)
    # chrome.exe is a known safe app, even with 30 min use
    sid = db.start_app_session("chrome.exe", "", "Chrome", base + timedelta(hours=1), project="Personal")
    db.close_app_session(sid, base + timedelta(hours=1, minutes=30), 1800, 1800, 0)
    alerts = check_alerts(db, base.strftime("%Y-%m-%d"))
    assert "new_app" not in {a.alert_type for a in alerts}, [a.to_dict() for a in alerts]
    # A genuinely new, long-used, non-safe app still alerts.
    sid = db.start_app_session("mystery.exe", "", "Mystery", base + timedelta(hours=2), project="Untagged")
    db.close_app_session(sid, base + timedelta(hours=2, minutes=30), 1800, 1800, 0)
    alerts = check_alerts(db, base.strftime("%Y-%m-%d"))
    assert "new_app" in {a.alert_type for a in alerts}, [a.to_dict() for a in alerts]
    db.close()
    print("new_app quiet test passed")


def test_dashboard_no_duplicate_appbars():
    """Audit fix 2: Top Apps is rendered exactly once in the dashboard."""
    html = (Path(__file__).resolve().parents[1] / "deskaudit" / "dashboard_static" / "index.html").read_text(encoding="utf-8")
    assert html.count("$('appBars')") == 1, html.count("$('appBars')")
    assert "barRow(x.app" in html or "barRow(x.app+" in html
    print("dashboard duplicate test passed")


if __name__ == "__main__":
    test_alerts()
    test_goal_missed_only_past_days()
    test_night_false_positive_fixed()
    test_new_app_quiet()
    test_dashboard_no_duplicate_appbars()
