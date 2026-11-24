"""Dashboard tests — summary data + live HTTP server, synthetic data only.

Run:  python -m pytest tests/test_dashboard.py -v
      python tests/test_dashboard.py
"""

import json
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.dashboard import DashboardServer, dashboard_summary, hourly_activity, range_summary
from deskaudit.database import Database
from deskaudit.projects import Project, Tagger


def seed_db(tmp: Path) -> tuple[Database, str]:
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone().replace(hour=10, minute=5, second=0, microsecond=0)
    date = now.strftime("%Y-%m-%d")
    # Office app session, 30 min active
    sid = db.start_app_session("WINWORD.EXE", r"C:\x\WINWORD.EXE", "Q4 Meeting notes", now,
                               project="Office", tags=["work"])
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1500, 300)
    # Study browser session, 20 min active, productive
    bsid = db.start_browser_session("Chrome", "https://coursera.org/learn/python",
                                    "coursera.org", "Python Course", "learning", True,
                                    now + timedelta(minutes=40), project="Study", tags=["learning"])
    db.close_browser_session(bsid, now + timedelta(minutes=60), 1200, 1200, 0)
    # Browser visit + search + download
    db.log_browser_visit("Chrome", "https://coursera.org/learn/python", "coursera.org",
                         "Python Course", now, project="Study")
    db.log_browser_search("Chrome", "python sqlite", "https://www.google.com/search?q=python+sqlite",
                          "google.com", now, project="Study")
    db.log_browser_download("Chrome", "https://example.com/report.pdf", "example.com",
                            r"C:\Users\F\Downloads\report.pdf", "report.pdf",
                            1234, 1234, "complete", now, project="Office")
    # File event in Office folder
    db.log_file_event("modified", "/home/user/Documents/Office/Q4 report.xlsx",
                      occurred_at=now, project="Office")
    return db, date


def test_dashboard_summary():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed_db(tmp)
    tagger = Tagger(projects=[
        Project(name="Office", priority=100, daily_goal_minutes=240),
        Project(name="Study", priority=90, daily_goal_minutes=120),
    ])
    s = dashboard_summary(db, date, tagger=tagger)
    # Active is app time (1500s); browser time (1200s) is a subset of it,
    # so it is NOT added on top.
    assert s["kpis"]["active_seconds"] == 1500, s["kpis"]
    assert s["kpis"]["idle_seconds"] == 300
    assert s["kpis"]["apps_used"] == 1
    assert s["kpis"]["browser_seconds"] == 1200
    assert s["kpis"]["productive_seconds"] == 1200
    assert s["kpis"]["visits"] == 1
    assert s["kpis"]["downloads"] == 1
    assert s["kpis"]["file_events"] == 1
    assert s["kpis"]["projects_worked"] == 2
    by_name = {p["project"]: p for p in s["projects"]}
    assert by_name["Office"]["active_seconds"] == 1500
    assert by_name["Office"]["goal_percent"] == 10
    assert by_name["Study"]["active_seconds"] == 1200
    # Hourly: both sessions started at hour 10; app 25m, browser 20m (subset)
    hours = hourly_activity(db, date)
    assert len(hours) == 24
    assert hours[10]["active_minutes"] == 25.0, hours[10]
    assert hours[10]["browser_minutes"] == 20.0, hours[10]
    assert hours[9]["active_minutes"] == 0
    # Categories, app details, recent files
    assert any(c["category"] == "learning" for c in s["categories"])
    assert s["app_details"][0]["app"] == "WINWORD.EXE"
    assert s["app_details"][0]["project"] == "Office"
    assert s["recent_files"][0]["project"] == "Office"
    # Range: 3 days, today is the last row, previous days are 0
    rows = range_summary(db, 3, end=date, tagger=tagger)
    assert len(rows) == 3
    assert rows[-1]["date"] == date
    assert rows[-1]["active_seconds"] == 1500
    assert rows[-1]["browser_seconds"] == 1200
    assert rows[0]["active_seconds"] == 0
    db.close()
    print("dashboard summary tests passed")


def test_dashboard_http():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed_db(tmp)
    db.close()
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240),
                              Project(name="Study", daily_goal_minutes=120)])
    server = DashboardServer(tmp / "test.db", host="127.0.0.1", port=0,
                             tagger=tagger, project_root=tmp)
    url = server.start()
    try:
        with urllib.request.urlopen(url + "api/health", timeout=5) as r:
            assert json.loads(r.read())["ok"] is True
        with urllib.request.urlopen(url + f"api/summary?date={date}", timeout=5) as r:
            data = json.loads(r.read())
            assert data["kpis"]["active_seconds"] == 1500
            assert len(data["hourly"]) == 24
            assert data["projects"]
        with urllib.request.urlopen(url + f"api/range?days=3&end={date}", timeout=5) as r:
            data = json.loads(r.read())
            assert len(data["rows"]) == 3
        with urllib.request.urlopen(url, timeout=5) as r:
            html = r.read().decode("utf-8")
            assert "DeskAudit" in html and "Hourly activity" in html
            assert "Projects" in html and "Top apps" in html
    finally:
        server.stop()
    print("dashboard HTTP tests passed")


if __name__ == "__main__":
    test_dashboard_summary()
    test_dashboard_http()
