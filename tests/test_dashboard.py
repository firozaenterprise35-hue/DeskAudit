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
    # Data transfers: USB copy + cloud session (for the transfers section)
    db.log_transfer(date, "usb_copy", "E:", "report.xlsx", r"E:\report.xlsx",
                    2 * 1024 * 1024, "E:",
                    occurred_at=now.isoformat(timespec="seconds"))
    db.log_transfer(date, "cloud", "drive.google.com",
                    occurred_at=(now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    details='{"seconds": 1200}')
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
    # Data transfers (seeded): USB copy + cloud use
    assert s["transfers"]["total"] == 2, s["transfers"]
    assert s["kpis"]["transfers_total"] == 2
    assert s["kpis"]["usb_copies"] == 1
    assert s["transfers"]["cloud_domains"] == ["drive.google.com"]
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
                             tagger=tagger, project_root=tmp, lock_enabled=False)
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


def test_dashboard_settings():
    import os

    from deskaudit.dashboard import get_settings, save_settings

    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text(
        "poll_interval_seconds: 5\nllm:\n  provider: gemini\n"
        "  model: gemini-3.5-flash-lite\n  api_key: ''\n",
        encoding="utf-8")
    old_env = os.environ.pop("DESKAUDIT_LLM_API_KEY", None)
    try:
        view = get_settings(tmp / "config.yaml")
        assert view["llm"]["provider"] == "gemini"
        assert view["llm"]["api_key_set"] is False
        assert "api_key" not in view["llm"]
        assert view["agent"]["poll_interval_seconds"] == 5
        assert view["local_exists"] is False
        # Save a key + a threshold: local file is created, config.yaml untouched.
        saved = save_settings(tmp / "config.yaml", {
            "llm": {"api_key": "test-key-123", "model": "gemini-3.5-flash-lite"},
            "alerts": {"distracting_minutes": 90},
        })
        assert saved["llm"]["api_key_set"] is True
        assert saved["alerts"]["distracting_minutes"] == 90
        assert "test-key-123" not in json.dumps(saved)  # key never returned
        local_text = (tmp / "config.local.yaml").read_text(encoding="utf-8")
        assert "test-key-123" in local_text
        assert "api_key: ''" in (tmp / "config.yaml").read_text(encoding="utf-8")
        # Existing local keys survive a later partial save (merge, not replace).
        save_settings(tmp / "config.yaml", {"agent": {"poll_interval_seconds": 10}})
        assert "test-key-123" in (tmp / "config.local.yaml").read_text(encoding="utf-8")
        assert get_settings(tmp / "config.yaml")["agent"]["poll_interval_seconds"] == 10
        # Empty key clears it; bad values raise and write nothing.
        save_settings(tmp / "config.yaml", {"llm": {"api_key": ""}})
        assert get_settings(tmp / "config.yaml")["llm"]["api_key_set"] is False
        # Secret gate + dashboard_on_start round-trip through the same API.
        saved = save_settings(tmp / "config.yaml", {
            "agent": {"dashboard_on_start": False},
            "secret_gate": {"clicks": 3, "code": "9999"},
        })
        assert saved["agent"]["dashboard_on_start"] is False
        assert saved["secret_gate"]["clicks"] == 3
        assert saved["secret_gate"]["code"] == "9999"
        assert saved["secret_gate"]["enabled"] is True
        try:
            save_settings(tmp / "config.yaml", {"secret_gate": {"code": "ab12"}})
        except ValueError:
            pass
        else:
            raise AssertionError("non-digit gate code must raise ValueError")
        try:
            save_settings(tmp / "config.yaml", {"secret_gate": {"clicks": 99}})
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range click count must raise ValueError")
        try:
            save_settings(tmp / "config.yaml", {"agent": {"poll_interval_seconds": 9999}})
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range value must raise ValueError")
        try:
            save_settings(tmp / "config.yaml", {"llm": {"provider": "nope"}})
        except ValueError:
            pass
        else:
            raise AssertionError("bad provider must raise ValueError")
    finally:
        if old_env is not None:
            os.environ["DESKAUDIT_LLM_API_KEY"] = old_env
    print("dashboard settings tests passed")


def test_dashboard_settings_http():
    tmp = Path(tempfile.mkdtemp())
    db, date = seed_db(tmp)
    db.close()
    (tmp / "config.yaml").write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240)])
    server = DashboardServer(tmp / "test.db", host="127.0.0.1", port=0,
                             tagger=tagger, project_root=tmp,
                             config_path=tmp / "config.yaml", lock_enabled=False)
    url = server.start()
    try:
        with urllib.request.urlopen(url + "api/settings", timeout=5) as r:
            data = json.loads(r.read())
            assert data["agent"]["poll_interval_seconds"] == 5
            assert data["llm"]["api_key_set"] is False
        body = json.dumps({"llm": {"api_key": "http-key-1"},
                           "agent": {"poll_interval_seconds": 7}}).encode("utf-8")
        req = urllib.request.Request(url + "api/settings", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            assert data["ok"] is True
            assert data["saved"]["llm"]["api_key_set"] is True
            assert data["saved"]["agent"]["poll_interval_seconds"] == 7
        assert "http-key-1" in (tmp / "config.local.yaml").read_text(encoding="utf-8")
        # Bad value -> 400, nothing saved.
        body = json.dumps({"agent": {"poll_interval_seconds": 9999}}).encode("utf-8")
        req = urllib.request.Request(url + "api/settings", data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert json.loads(e.read())["ok"] is False
    finally:
        server.stop()
    print("dashboard settings HTTP tests passed")


def test_dashboard_js_syntax():
    """The dashboard page's <script> must parse. A stray brace once
    killed the whole script, so the page opened but stayed on
    "Loading…" forever — and no test caught it. node --check when
    available, otherwise a brace-balance check (which catches the
    same class of mistake)."""
    import shutil
    import subprocess

    from deskaudit.dashboard import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<script>" in html and "</script>" in html
    js = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    assert "function load()" in js and "function render()" in js
    node = shutil.which("node")
    if node:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                        encoding="utf-8") as f:
            f.write(js)
            js_path = f.name
        try:
            proc = subprocess.run([node, "--check", js_path],
                                  capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0, proc.stderr
        finally:
            Path(js_path).unlink(missing_ok=True)
    else:
        # Fallback: braces must balance (the stray-`}` bug was 156 vs 157).
        assert js.count("{") == js.count("}"), (
            f"unbalanced braces: {js.count('{')} open vs {js.count('}')} close")
        assert js.count("(") == js.count(")")
    print("dashboard JS syntax test passed")


if __name__ == "__main__":
    test_dashboard_summary()
    test_dashboard_http()
    test_dashboard_settings()
    test_dashboard_settings_http()
    test_dashboard_js_syntax()
