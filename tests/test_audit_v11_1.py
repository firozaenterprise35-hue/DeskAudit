"""Regression tests for the v11.1 audit fixes (independent audit, 2026-11-24).

Each test names the finding it covers (C1..C6 critical, M4..M29 major,
plus the minor ones). The production path is exercised wherever possible
— e.g. the moved-event size goes through the real file-watcher handler,
not a hand-seeded DB row.

Run:  python3 tests/test_audit_v11_1.py
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.app_tracker import AppTracker
from deskaudit.browser_utils import classify_domain
from deskaudit.config import Config
from deskaudit.database import Database
from deskaudit.file_watcher import _Handler
from deskaudit.llm_summary import _stats_payload
from deskaudit.projects import Project, Tagger
from deskaudit.report import render_markdown
from deskaudit.summary import generate_weekly_summary
from deskaudit.transfers import (DEFAULT_TRANSFERS, Transfer, TransferMonitor,
                                 classify_transfer_domain, diff_snapshots,
                                 snapshot_drive)


def _tmpdb():
    tmp = Path(tempfile.mkdtemp())
    return tmp, Database(tmp / "test.db")


# --- C1 + M19: folder rules -------------------------------------------------
def test_folder_rules_windows_paths():
    # The shipped examples use forward slashes; real Windows paths use
    # backslashes. Both directions must match after normalisation.
    # (On Windows, from_dict expands %USERPROFILE% to C:\Users\... —
    # on Linux %VAR% is not expanded by expandvars, so the test uses
    # already-expanded paths, as the agent sees them on Windows.)
    office = Project(name="Office", folders=["C:/Users/Firoza/Documents/Office"])
    assert office.matches(path=r"C:\Users\Firoza\Documents\Office\report.xlsx")
    back = Project(name="B", folders=[r"C:\Users\Firoza\Documents\Office"])
    assert back.matches(path="C:/Users/Firoza/Documents/Office/report.xlsx")
    # The rule folder itself is the path.
    assert office.matches(path=r"C:\Users\Firoza\Documents\Office")
    # A from_dict rule in the shipped forward-slash style matches too.
    via_dict = Project.from_dict({"name": "Office",
                                  "folders": ["C:/Users/Firoza/Documents/Office"]})
    assert via_dict.matches(path=r"C:\Users\Firoza\Documents\Office\report.xlsx")
    # M19: no substring false positives — "Office" must not match MyOffice.
    sub = Project(name="S", folders=["Office"])
    assert not sub.matches(path=r"C:\Users\Firoza\Documents\MyOffice\file.txt")
    assert not sub.matches(path=r"C:\Users\Firoza\Documents\backoffice\file.txt")
    print("C1/M19 folder rules passed")


# --- C3: moved-event size through the real watcher --------------------------
def test_moved_event_size():
    tmp, db = _tmpdb()
    src_dir = tmp / "src"
    dst_dir = tmp / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    cfg = Config.load(tmp / "config.yaml") if (tmp / "config.yaml").exists() else Config()
    handler = _Handler(db, cfg, tagger=None)
    # The file is moved: src no longer exists, dest does.
    dest = dst_dir / "big.zip"
    dest.write_bytes(b"x" * 1024)
    handler._log("moved", str(src_dir / "big.zip"), dest=str(dest))
    rows = db.conn.execute(
        "SELECT size_bytes FROM file_events WHERE event_type='moved'").fetchall()
    assert len(rows) == 1 and rows[0]["size_bytes"] == 1024, rows
    db.close()
    print("C3 moved-event size passed")


# --- C2: plain copies (created events) --------------------------------------
def test_created_copy_detection():
    tmp, db = _tmpdb()
    sensitive = tmp / "CompanyData"
    sensitive.mkdir()
    original = sensitive / "secret.xlsx"
    original.write_bytes(b"y" * 500)
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    # A copy OUT of the sensitive folder: created event elsewhere,
    # same name + same size as the original still inside it.
    db.log_file_event("created", str(tmp / "Desktop" / "secret.xlsx"),
                      size_bytes=500, occurred_at=base + timedelta(minutes=5))
    # A genuinely new file with no original inside -> no row.
    db.log_file_event("created", str(tmp / "Desktop" / "new.xlsx"),
                      size_bytes=123, occurred_at=base + timedelta(minutes=6))
    # A big file copied (created, >= 100 MB) -> large_transfer.
    db.log_file_event("created", str(tmp / "Downloads" / "huge.zip"),
                      size_bytes=150 * 1024 * 1024,
                      occurred_at=base + timedelta(minutes=7))
    monitor = TransferMonitor(db, {**DEFAULT_TRANSFERS,
                                   "sensitive_folders": [str(sensitive)]})
    found = monitor.scan_file_events(date)
    kinds = {t.transfer_type for t in found}
    assert "sensitive_copy" in kinds, kinds
    assert "large_transfer" in kinds, kinds
    assert db.transfer_counts(date)["sensitive_copy"]["count"] == 1
    db.close()
    print("C2 created-copy detection passed")


# --- C4: sensitive folders are watched --------------------------------------
def test_effective_watch_folders():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text(
        "watch_folders:\n  - '%s'\n" % str(tmp / "Docs"), encoding="utf-8")
    cfg = Config.load(tmp / "config.yaml")
    cfg.transfers = {**DEFAULT_TRANSFERS,
                     "sensitive_folders": [str(tmp / "Accounts")]}
    folders = [str(p) for p in cfg.effective_watch_folders()]
    assert any("Accounts" in f for f in folders), folders
    assert any("Docs" in f for f in folders), folders
    print("C4 effective watch folders passed")


# --- C5: open session is flushed every poll ---------------------------------
def test_session_flush():
    tmp, db = _tmpdb()
    tracker = AppTracker(db, 300)
    tracker.tick(5.0)  # opens a session (unknown app on Linux) and flushes
    tracker.tick(5.0)
    rows = db.conn.execute(
        "SELECT active_seconds, duration_seconds FROM app_sessions").fetchall()
    assert len(rows) == 1, rows
    assert float(rows[0]["active_seconds"] or 0) >= 10.0, rows
    assert float(rows[0]["duration_seconds"] or 0) > 0, rows
    tracker.stop()
    db.close()
    print("C5 session flush passed")


# --- C6: weekly LLM payload carries real numbers ---------------------------
def test_weekly_llm_payload():
    tmp, db = _tmpdb()
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", base, project="Office")
    db.close_app_session(sid, base + timedelta(minutes=45), 2700, 2700, 0)
    db.upsert_project("Office", daily_goal_minutes=240)
    result = generate_weekly_summary(db, end=date, days=1)
    payload = _stats_payload(result)
    assert payload["active_minutes"] == 45, payload
    assert payload["projects"] and payload["projects"][0]["project"] == "Office", payload
    assert payload["visits"] == 0 and payload["file_events"] == 0
    db.close()
    print("C6 weekly LLM payload passed")


# --- M14: alerts see the whole day, not just 100 rows ----------------------
def test_alerts_over_100_rows():
    tmp, db = _tmpdb()
    from deskaudit.alerts import check_alerts

    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    # 120 cloud rows, each 60 s of use: 120 minutes total — the alert
    # threshold is 15 min, so cloud_upload must fire.
    for i in range(120):
        db.log_transfer(date, "cloud", "drive.google.com",
                        occurred_at=(base + timedelta(minutes=i)).isoformat(timespec="seconds"),
                        details=json.dumps({"seconds": 60}))
    alerts = check_alerts(db, date)
    assert any(a.alert_type == "cloud_upload" for a in alerts), [a.alert_type for a in alerts]
    db.close()
    print("M14 alerts over 100 rows passed")


# --- M17: distinct events are not deduped away ------------------------------
def test_print_jobs_same_doc_twice():
    tmp, db = _tmpdb()
    monitor = TransferMonitor(db, {})
    now = datetime.now().astimezone()
    import deskaudit.transfers as tr

    jobs = [
        {"job_id": 5, "document": "Invoice.docx", "printer": "HP",
         "user": "u", "pages": 1, "size_bytes": 100},
        {"job_id": 6, "document": "Invoice.docx", "printer": "HP",
         "user": "u", "pages": 1, "size_bytes": 100},
    ]
    # Two jobs, same document, same printer, one poll: both must log.
    # (The old code stamped both with the same poll time, so the
    # transfers UNIQUE key silently dropped the second one.)
    orig = tr.list_print_jobs
    tr.list_print_jobs = lambda: jobs
    try:
        found = monitor.poll_printers(now)
    finally:
        tr.list_print_jobs = orig
    assert len(found) == 2, found
    assert db.transfer_counts(now.strftime("%Y-%m-%d"))["print"]["count"] == 2
    db.close()
    print("M17 print jobs same doc twice passed")


def test_usb_copy_same_name_two_folders():
    tmp, db = _tmpdb()
    monitor = TransferMonitor(db, {})
    date = datetime.now().strftime("%Y-%m-%d")
    assert monitor._log(Transfer(
        "usb_copy", channel="E:", file_name="a.xlsx", file_path=r"E:\x\a.xlsx",
        size_bytes=10, destination="E:", date=date)) is True
    assert monitor._log(Transfer(
        "usb_copy", channel="E:", file_name="a.xlsx", file_path=r"E:\y\a.xlsx",
        size_bytes=10, destination="E:", date=date)) is True
    assert db.transfer_counts(date)["usb_copy"]["count"] == 2
    db.close()
    print("M17 usb copy same name two folders passed")


# --- M6: --project-add with a quote -----------------------------------------
def test_project_add_with_quotes():
    tmp = Path(tempfile.mkdtemp())
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text("db_path: 'data/test.db'\n", encoding="utf-8")
    (tmp / "projects.yaml").write_text('default_project: "Untagged"\n\nprojects:\n', encoding="utf-8")
    r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"),
                        "--config", str(cfg_path), "--project-add", 'Client "A"',
                        "--folder", r"%USERPROFILE%\Documents\ClientA"],
                       capture_output=True, text=True, cwd=str(tmp))
    assert r.returncode == 0, r.stderr
    # The file must still parse, and the project must load.
    tagger = Tagger.load(tmp / "projects.yaml")
    assert any(p.name == 'Client "A"' for p in tagger.projects), tagger.projects
    print("M6 project-add with quotes passed")


# --- M4: bad scalar config values fall back ---------------------------------
def test_config_bad_scalar():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text(
        "poll_interval_seconds: 'fast'\nbrowser_poll_seconds: 'slow'\n",
        encoding="utf-8")
    cfg = Config.load(tmp / "config.yaml")
    assert cfg.poll_interval_seconds == 5, cfg.poll_interval_seconds
    assert cfg.browser_poll_seconds == 60, cfg.browser_poll_seconds
    print("M4 config bad scalar passed")


# --- M20: exclusions are scoped to the app folder ---------------------------
def test_is_excluded_scoped():
    cfg = Config(project_root=Path("/app/DeskAudit"),
                 exclude_folders={"data", "reports", ".venv", "__pycache__"},
                 exclude_extensions={".tmp", ".log", ".lock"})
    assert cfg.is_excluded(Path("/app/DeskAudit/data/deskaudit.db")) is True
    assert cfg.is_excluded(Path("/app/DeskAudit/reports/2026-01-01.md")) is True
    assert cfg.is_excluded(Path(r"C:\Users\Firoza\Documents\data\invoice.xlsx")) is False
    assert cfg.is_excluded(Path(r"C:\Users\Firoza\Documents\reports\Q4.xlsx")) is False
    assert cfg.is_excluded(Path(r"C:\Users\Firoza\Documents\notes.tmp")) is True
    print("M20 is_excluded scoped passed")


# --- M5: broken projects.yaml warns, does not crash -------------------------
def test_projects_load_broken_yaml():
    tmp = Path(tempfile.mkdtemp())
    bad = tmp / "projects.yaml"
    bad.write_text("projects: [\n  - name: \"Unclosed\n", encoding="utf-8")
    tagger = Tagger.load(bad)  # must not raise
    assert tagger.projects == []
    # Bad ints in a project dict fall back to defaults.
    p = Project.from_dict({"name": "X", "daily_goal_minutes": "oops", "priority": "high"})
    assert p.daily_goal_minutes == 0 and p.priority == 50
    print("M5 projects load broken yaml passed")


# --- Domain boundary matching ------------------------------------------------
def test_domain_boundary():
    assert classify_transfer_domain("profile.io") is None
    assert classify_transfer_domain("mybox.com") is None
    assert classify_transfer_domain("www.dropbox.com") == "cloud"
    assert classify_transfer_domain("mail.google.com") == "email"
    assert classify_domain("notoutlook.com")[0] != "email"
    assert classify_domain("outlook.live.com")[0] == "email"
    assert classify_domain("gist.github.com")[0] == "coding"
    print("domain boundary tests passed")


# --- USB snapshot: only new files are copies ---------------------------------
def test_diff_new_only():
    old = {"a.txt": (10, 100), "b.txt": (20, 200)}
    new = {"a.txt": (99, 300), "b.txt": (20, 200), "c.txt": (30, 300)}
    added = diff_snapshots(old, new)
    assert added == [("c.txt", 30)], added  # a.txt changed in place: not a copy
    # Dotfiles are included in snapshots.
    tmp = Path(tempfile.mkdtemp())
    (tmp / ".env").write_text("secret=1", encoding="utf-8")
    snap = snapshot_drive(str(tmp))
    assert ".env" in snap, snap
    print("diff new-only + dotfiles passed")


# --- Parent rollup ------------------------------------------------------------
def test_parent_rollup():
    tmp, db = _tmpdb()
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    db.upsert_project("Office / Client A", parent="Office", daily_goal_minutes=60)
    db.upsert_project("Office", daily_goal_minutes=240)
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", base, project="Office / Client A")
    db.close_app_session(sid, base + timedelta(minutes=30), 1800, 1800, 0)
    totals = {r["project"]: r for r in db.project_totals(date)}
    assert totals["Office / Client A"]["active"] == 1800
    assert totals["Office"]["active"] == 1800, totals  # rolled up
    db.close()
    print("parent rollup passed")


# --- Markdown pipe escaping -----------------------------------------------------
def test_markdown_pipe_escape():
    summary = {
        "date": "2026-11-24", "active_seconds": 60, "idle_seconds": 0,
        "apps_used": 1, "file_counts": {},
        "top_apps": [{"app": "WINWORD.EXE", "title": "a | b", "active_seconds": 60,
                      "total_seconds": 60, "sessions": 1}],
        "projects": [], "untagged": {}, "transfers": {"total": 0, "counts": {}, "rows": []},
        "browser": {"active_seconds": 0, "productive_seconds": 0, "distracting_seconds": 0,
                    "domains_used": 0, "visits": 0, "top_domains": [], "searches": [],
                    "downloads": []},
    }
    md = render_markdown(summary)
    row = [l for l in md.splitlines() if "WINWORD" in l][0]
    import re

    # Splitting on UNESCAPED pipes must still give 4 cells (+2 empty ends).
    assert len(re.split(r"(?<!\\)\|", row)) == 6, row
    assert "a \\| b" in row
    print("markdown pipe escape passed")


# --- Weekly transfer labels are Bengali ---------------------------------------
def test_weekly_transfer_labels():
    tmp, db = _tmpdb()
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    db.log_transfer(date, "usb_copy", "E:", "a.xlsx", r"E:\a.xlsx", 10, "E:")
    result = generate_weekly_summary(db, end=date, days=1)
    section = [s for s in result["sections"] if s["title"] == "ডেটা ট্রান্সফার"][0]
    assert "usb_copy" not in section["text"], section["text"]
    assert "USB-তে কপি" in section["text"], section["text"]
    db.close()
    print("weekly transfer labels passed")


# --- M10: --ai-summary --llm keeps the local sections -------------------------
def test_ai_summary_llm_keeps_local():
    tmp = Path(tempfile.mkdtemp())
    root = Path(__file__).resolve().parents[1]
    (tmp / "config.yaml").write_text("db_path: 'data/test.db'\n", encoding="utf-8")
    (tmp / "projects.yaml").write_text('default_project: "Untagged"\n\nprojects:\n', encoding="utf-8")
    db = Database(tmp / "data" / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", base, project="Office")
    db.close_app_session(sid, base + timedelta(minutes=30), 1800, 1800, 0)
    db.close()
    env = dict(os.environ)
    env.pop("DESKAUDIT_LLM_API_KEY", None)
    r = subprocess.run([sys.executable, str(root / "main.py"), "--config", str(tmp / "config.yaml"),
                        "--ai-summary", "--llm"],
                       capture_output=True, text=True, cwd=str(tmp), env=env)
    assert r.returncode == 0, r.stderr
    date = datetime.now().strftime("%Y-%m-%d")
    text = (tmp / "reports" / f"{date}-summary.md").read_text(encoding="utf-8")
    # Local sections survive (the old code overwrote the file with only
    # the LLM paragraph). No LLM key here, so no LLM section is added.
    assert "মূল কাজ" in text and "ডেটা ট্রান্সফার" in text, text
    assert "## LLM সামারি" not in text
    print("M10 ai-summary llm keeps local passed")


if __name__ == "__main__":
    test_folder_rules_windows_paths()
    test_moved_event_size()
    test_created_copy_detection()
    test_effective_watch_folders()
    test_session_flush()
    test_weekly_llm_payload()
    test_alerts_over_100_rows()
    test_print_jobs_same_doc_twice()
    test_usb_copy_same_name_two_folders()
    test_project_add_with_quotes()
    test_config_bad_scalar()
    test_is_excluded_scoped()
    test_projects_load_broken_yaml()
    test_domain_boundary()
    test_diff_new_only()
    test_parent_rollup()
    test_markdown_pipe_escape()
    test_weekly_transfer_labels()
    test_ai_summary_llm_keeps_local()
    print("ALL v11.1 AUDIT REGRESSION TESTS PASSED")
