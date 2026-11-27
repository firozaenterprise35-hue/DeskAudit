"""Daily + weekly summary tests — Bengali narrative, synthetic data only."""

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.database import Database
from deskaudit.projects import Project, Tagger
from deskaudit.summary import (
    generate_daily_summary, generate_weekly_summary,
    render_daily_markdown, render_weekly_markdown,
    save_daily_summary, save_weekly_summary,
)


def seed(tmp: Path):
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    # Office 2h active (goal 240m -> 50%, not met), Study browser 30m
    sid = db.start_app_session("WINWORD.EXE", "", "Q4 Meeting notes", base, project="Office")
    db.close_app_session(sid, base + timedelta(hours=2), 7200, 7200, 0)
    bsid = db.start_browser_session("Chrome", "https://coursera.org/learn/python",
                                    "coursera.org", "Python Course", "learning", True,
                                    base + timedelta(hours=3), project="Study")
    db.close_browser_session(bsid, base + timedelta(hours=3, minutes=30), 1800, 1800, 0)
    db.log_browser_search("Chrome", "python sqlite", "https://www.google.com/search?q=python+sqlite",
                          "google.com", base, project="Study")
    db.log_file_event("created", "/home/user/Documents/Office/new.xlsx", occurred_at=base, project="Office")
    db.log_file_event("deleted", "/tmp/old.txt", occurred_at=base, project="Untagged")
    return db, date


def test_daily_summary():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed(tmp)
    tagger = Tagger(projects=[
        Project(name="Office", priority=100, daily_goal_minutes=240),
        Project(name="Study", priority=90, daily_goal_minutes=120),
    ])
    r = generate_daily_summary(db, date, tagger=tagger)
    assert r["date"] == date
    assert "WINWORD.EXE" in r["narrative"] or "Office" in r["narrative"]
    assert "Office" in r["narrative"]
    titles = [s["title"] for s in r["sections"]]
    for expected in ["মূল কাজ", "প্রোডাক্টিভিটি", "প্রজেক্ট ও গোল", "ফাইল",
                     "ব্রাউজার", "আনট্যাগড", "আগামীকালের জন্য"]:
        assert expected in titles, titles
    assert r["stats"]["active_seconds"] == 7200
    assert r["stats"]["productive_seconds"] == 1800
    md = render_daily_markdown(r)
    assert "# DeskAudit" in md and "Office" in md
    result, path = save_daily_summary(db, date, reports_dir=tmp / "reports", tagger=tagger)
    assert path.exists() and path.name == f"{date}-summary.md"
    db.close()
    print("daily summary tests passed")


def test_weekly_summary():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed(tmp)
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240)])
    r = generate_weekly_summary(db, end=date, days=7, tagger=tagger)
    assert r["days"] == 7 and len(r["rows"]) == 7
    assert r["stats"]["total_active_seconds"] == 7200
    assert r["stats"]["best_day"] == date
    assert r["stats"]["top_project"] == "Office"
    md = render_weekly_markdown(r)
    assert "সাপ্তাহিক" in md
    result, path = save_weekly_summary(db, end=date, reports_dir=tmp / "reports", tagger=tagger)
    assert path.exists() and path.name == f"{date}-weekly.md"
    db.close()
    print("weekly summary tests passed")


def test_untagged_total():
    # v12.13 (M4): only the top 5 untagged apps were summed as the
    # "total" — 6 untagged apps x 600 s reported 3000 s, not 3600 s.
    from deskaudit.report import build_summary

    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    for i in range(6):
        sid = db.start_app_session(f"APP{i}.EXE", "", f"App {i}", base,
                                   project="Untagged")
        db.close_app_session(sid, base + timedelta(minutes=10), 600, 600, 0)
    assert db.untagged_total(date) == 3600.0
    summary = build_summary(db, date)
    assert summary["untagged_seconds"] == 3600.0, summary["untagged_seconds"]
    assert len(summary["untagged"]["apps"]) == 5  # top-5 still only for display
    r = generate_daily_summary(db, date)
    untagged = [s for s in r["sections"] if s["title"] == "আনট্যাগড"]
    assert untagged and "1h 0m" in untagged[0]["text"], untagged
    db.close()
    print("untagged total passed")


if __name__ == "__main__":
    test_daily_summary()
    test_weekly_summary()
    test_untagged_total()
