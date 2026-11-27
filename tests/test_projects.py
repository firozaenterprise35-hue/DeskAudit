"""Project tagging tests — rule engine, DB retag, report totals.

Run:  python -m pytest tests/test_projects.py -v
      python tests/test_projects.py
"""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.database import Database
from deskaudit.projects import Project, Tagger
from deskaudit.report import build_summary


def make_tagger() -> Tagger:
    return Tagger(projects=[
        Project(name="Office", priority=100, folders=["/home/user/Documents/Office"],
                apps=["WINWORD.EXE"], domains=["outlook.office.com"],
                window_keywords=["Meeting"], tags=["work"],
                daily_goal_minutes=240),
        Project(name="Study", priority=90, domains=["coursera.org", "github.com"],
                file_patterns=["*tutorial*"], tags=["learning"],
                daily_goal_minutes=120),
        Project(name="DeskAudit", priority=110, file_patterns=["*deskaudit*"],
                window_keywords=["DeskAudit"], tags=["coding"]),
    ], overrides=[], default_project="Untagged")


def test_rule_engine():
    t = make_tagger()
    # Folder beats app priority? DeskAudit has higher priority (110) but
    # Office folder matches first only if it matches; here Office folder wins
    # over Study because Study does not match this file.
    assert t.tag_file("/home/user/Documents/Office/Q4 report.xlsx")[0] == "Office"
    assert t.tag_app("WINWORD.EXE", title="Q4 Meeting notes")[0] == "Office"
    assert t.tag_browser(url="https://coursera.org/learn/python", domain="coursera.org")[0] == "Study"
    assert t.tag_browser(url="https://github.com/python/cpython", domain="github.com")[0] == "Study"
    # DeskAudit: window keyword + file pattern, priority 110 wins over Study.
    assert t.tag_app("Code.exe", title="DeskAudit - main.py")[0] == "DeskAudit"
    assert t.tag_file("/tmp/deskaudit-notes.txt")[0] == "DeskAudit"
    # Secondary tags: DeskAudit also matches Study? No — only DeskAudit matches.
    project, tags = t.tag_browser(url="https://github.com/x", domain="github.com", title="DeskAudit")
    assert project == "DeskAudit"
    assert "learning" in tags  # Study also matched (domain), so its tag is secondary
    # Nothing matches -> Untagged
    assert t.tag_app("notepad.exe", title="random")[0] == "Untagged"
    # Override wins over everything
    t.add_override("domain", "github.com", "Office")
    assert t.tag_browser(url="https://github.com/x", domain="github.com")[0] == "Office"


def test_db_retag_and_report():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    t = make_tagger()
    now = datetime.now().astimezone()
    # App session: Office (Word, Meeting title)
    sid = db.start_app_session("WINWORD.EXE", r"C:\x\WINWORD.EXE", "Q4 Meeting notes", now,
                               project="Untagged")
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1500, 300)
    # App session: untagged, then retag should fix it
    sid2 = db.start_app_session("chrome.exe", "", "Coursera - Python", now,
                                project="Untagged")
    db.close_app_session(sid2, now + timedelta(minutes=10), 600, 600, 0)
    # File event in Office folder
    db.log_file_event("modified", "/home/user/Documents/Office/Q4 report.xlsx",
                      occurred_at=now, project="Untagged")
    # Browser visit to coursera — tag at insert time
    project, tags = t.tag_browser(url="https://coursera.org/learn/python", domain="coursera.org")
    db.log_browser_visit("Chrome", "https://coursera.org/learn/python", "coursera.org",
                         "Python Course", now, project=project, tags=tags)
    # Retag: chrome session title "Coursera - Python" has no rule, so it stays
    # Untagged; the Word session should already be Office via retag too.
    # v12.13 (C-m9): counts are rows whose project/tags ACTUALLY changed —
    # the old SQLite rowcount counted matched rows even when nothing
    # changed, so the chrome session (Untagged -> Untagged) no longer
    # counts. Only the Word session changes here.
    counts = db.retag_all(t, date=now.strftime("%Y-%m-%d"))
    assert counts["app_sessions"] == 1, counts
    assert counts["file_events"] == 1, counts
    # A second retag changes nothing at all (idempotent).
    counts2 = db.retag_all(t, date=now.strftime("%Y-%m-%d"))
    assert all(v == 0 for v in counts2.values()), counts2

    date = now.strftime("%Y-%m-%d")
    summary = build_summary(db, date, tagger=t)
    by_name = {p["project"]: p for p in summary["projects"]}
    assert "Office" in by_name, summary["projects"]
    assert by_name["Office"]["active_seconds"] == 1500
    assert by_name["Office"]["goal_minutes"] == 240
    assert by_name["Office"]["goal_percent"] == 10  # 25m / 240m
    assert "Study" in by_name
    assert by_name["Study"]["visits"] == 1
    # Untagged top should list chrome.exe
    assert summary["untagged"]["apps"]
    db.close()
    print("project tests passed")


def test_project_no_double_count():
    """Audit fix 1: browser time is a subset of app time, not extra time.
    30 min Chrome app + 30 min browser session in the same project = 30 min."""
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = now.strftime("%Y-%m-%d")
    sid = db.start_app_session("chrome.exe", "", "Coursera", now, project="Study")
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1800, 0)
    bsid = db.start_browser_session("Chrome", "https://coursera.org/x", "coursera.org",
                                    "Course", "learning", True, now, project="Study")
    db.close_browser_session(bsid, now + timedelta(minutes=30), 1800, 1800, 0)
    rows = {r["project"]: r for r in db.project_totals(date)}
    assert rows["Study"]["active"] == 1800, rows["Study"]
    assert rows["Study"]["app_active"] == 1800
    assert rows["Study"]["browser_active"] == 1800
    # Mixed case: browser time whose app session was tagged elsewhere IS
    # extra time — Word 10 min (Study) + browser 30 min (Study, Chrome app
    # tagged Untagged) = 40 min, not 10 and not 60.
    now2 = now + timedelta(hours=1)
    sid = db.start_app_session("WINWORD.EXE", "", "Notes", now2, project="Study")
    db.close_app_session(sid, now2 + timedelta(minutes=10), 600, 600, 0)
    sid = db.start_app_session("chrome.exe", "", "Coursera", now2 + timedelta(minutes=10), project="Untagged")
    db.close_app_session(sid, now2 + timedelta(minutes=40), 1800, 1800, 0)
    bsid = db.start_browser_session("Chrome", "https://coursera.org/y", "coursera.org",
                                    "Course", "learning", True,
                                    now2 + timedelta(minutes=10), project="Study")
    db.close_browser_session(bsid, now2 + timedelta(minutes=40), 1800, 1800, 0)
    rows = {r["project"]: r for r in db.project_totals(date)}
    # Study: app 1800 + 600, browser 1800 + 1800, overlap only the first 1800
    assert rows["Study"]["active"] == 4200, rows["Study"]
    db.close()
    print("project double-count fix test passed")


def test_untagged_folders_both_separators():
    """Audit fix 3: untagged folders work for / and \\ paths."""
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone()
    db.log_file_event("modified", "/home/user/Documents/Office/a.xlsx", occurred_at=now, project="Untagged")
    db.log_file_event("modified", "/home/user/Documents/Office/b.xlsx", occurred_at=now, project="Untagged")
    db.log_file_event("modified", r"C:\Users\F\Documents\Office\c.xlsx", occurred_at=now, project="Untagged")
    folders = {f["value"]: f["count"] for f in db.untagged_top(now.strftime("%Y-%m-%d"))["folders"]}
    assert folders.get("/home/user/Documents/Office") == 2, folders
    assert folders.get("C:/Users/F/Documents/Office") == 1, folders
    db.close()
    print("untagged folders test passed")


if __name__ == "__main__":
    test_rule_engine()
    test_db_retag_and_report()
    test_project_no_double_count()
    test_untagged_folders_both_separators()
