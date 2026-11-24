"""DeskAudit web dashboard — stdlib only, no extra install.

Runs a local web server (default http://127.0.0.1:8000) that shows everything
DeskAudit knows, in one page:

    python main.py --dashboard
    python main.py --dashboard --port 8001
    python main.py --dashboard --host 127.0.0.1 --no-open

Sections (nothing is hidden behind another tab — one scrollable page):
  * Header: date picker, prev / next day, Today, range (1 / 7 / 14 / 30 days),
    live refresh, export buttons
  * KPI cards: Active, Idle, Apps used, Browser time, Productive,
    Distracting, Projects worked, File events, Visits, Searches,
    Downloads, Untagged time, Goals met
  * Charts: hourly activity (0-23h), top apps, top domains, projects
    (with goal progress), productive vs distracting donut,
    browser categories, file events by type, 7/14/30-day trend
  * Tables: app sessions, browser domains, top searches, downloads,
    recent file events, projects, untagged items
  * Filters: project dropdown, text search inside every table

API (all JSON, all local):
  GET /api/health
  GET /api/summary?date=YYYY-MM-DD
  GET /api/range?days=7&end=YYYY-MM-DD
  GET /api/alerts?date=YYYY-MM-DD
  GET /api/narrative?date=YYYY-MM-DD
  GET /api/weekly?end=YYYY-MM-DD&days=7
  GET /api/export?date=YYYY-MM-DD&format=md|csv|browser|projects

Security: binds to 127.0.0.1 by default. Use --host 0.0.0.0 only on a
trusted local network — the dashboard shows window titles and URLs.
"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .database import Database
from .report import build_summary
from .utils import today_str


# ---------------------------------------------------------------------------
# Data layer — pure functions, easy to test without starting a server
# ---------------------------------------------------------------------------

def _parse_date(raw: str | None) -> str:
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return today_str()


def hourly_activity(db: Database, day: str) -> list[dict]:
    """Active minutes per hour (0-23), from app + browser sessions.

    Approximation: a session counts toward the hour it started in.
    Sessions are usually minutes long, so this is accurate enough for a
    daily rhythm chart, and it is stated as such in the UI.
    """
    hours = [{"hour": h, "active_minutes": 0.0, "browser_minutes": 0.0} for h in range(24)]
    # App time is the total; browser time is a SUBSET of it (Chrome is an app),
    # so never add the two together.
    for table, key in (("app_sessions", "started_at"), ("browser_sessions", "started_at")):
        rows = db.conn.execute(
            f"SELECT substr({key}, 12, 2) AS h, SUM(active_seconds) AS s "
            f"FROM {table} WHERE substr({key}, 1, 10) = ? GROUP BY h",
            (day,),
        ).fetchall()
        for r in rows:
            try:
                h = int(r["h"])
            except (TypeError, ValueError):
                continue
            if 0 <= h < 24:
                minutes = round(float(r["s"] or 0) / 60, 1)
                if table == "browser_sessions":
                    hours[h]["browser_minutes"] = round(hours[h]["browser_minutes"] + minutes, 1)
                else:
                    hours[h]["active_minutes"] = round(hours[h]["active_minutes"] + minutes, 1)
    return hours


def app_details(db: Database, day: str) -> list[dict]:
    """Top apps by active time, with project + top window title."""
    rows = db.conn.execute(
        """SELECT app_name, project, SUM(active_seconds) AS active,
                  SUM(duration_seconds) AS total, COUNT(*) AS sessions,
                  MAX(window_title) AS title
           FROM app_sessions WHERE substr(started_at, 1, 10) = ?
           GROUP BY app_name, project ORDER BY active DESC LIMIT 20""",
        (day,),
    ).fetchall()
    return [{
        "app": r["app_name"], "project": r["project"] or "Untagged",
        "title": r["title"] or "",
        "active_seconds": float(r["active"] or 0),
        "total_seconds": float(r["total"] or 0),
        "sessions": int(r["sessions"] or 0),
    } for r in rows]


def category_totals(db: Database, day: str) -> list[dict]:
    rows = db.conn.execute(
        """SELECT category, SUM(active_seconds) AS active, COUNT(*) AS sessions
           FROM browser_sessions WHERE substr(started_at, 1, 10) = ?
           GROUP BY category ORDER BY active DESC""",
        (day,),
    ).fetchall()
    # Fall back to visits when there are no live sessions yet.
    if not rows:
        rows = db.conn.execute(
            """SELECT category, 0 AS active, COUNT(*) AS sessions
               FROM browser_visits WHERE substr(visited_at, 1, 10) = ?
               GROUP BY category ORDER BY sessions DESC""",
            (day,),
        ).fetchall()
    return [{
        "category": r["category"] or "other",
        "active_seconds": float(r["active"] or 0),
        "sessions": int(r["sessions"] or 0),
    } for r in rows]


def recent_file_events(db: Database, day: str, limit: int = 20) -> list[dict]:
    rows = db.conn.execute(
        """SELECT event_type, path, dest_path, project, occurred_at
           FROM file_events WHERE substr(occurred_at, 1, 10) = ?
           ORDER BY occurred_at DESC LIMIT ?""",
        (day, limit),
    ).fetchall()
    return [{
        "event": r["event_type"], "path": r["path"], "dest": r["dest_path"] or "",
        "project": r["project"] or "Untagged", "at": r["occurred_at"],
    } for r in rows]


def file_type_totals(db: Database, day: str) -> list[dict]:
    rows = db.conn.execute(
        """SELECT event_type, COUNT(*) AS n FROM file_events
           WHERE substr(occurred_at, 1, 10) = ? GROUP BY event_type""",
        (day,),
    ).fetchall()
    return [{"event": r["event_type"], "count": int(r["n"] or 0)} for r in rows]


def dashboard_summary(db: Database, day: str, tagger=None) -> dict:
    """Everything the dashboard page needs for one day, in one JSON object."""
    summary = build_summary(db, day, tagger=tagger)
    total = summary["active_seconds"] + summary["idle_seconds"]
    productive = summary["browser"]["productive_seconds"]
    distracting = summary["browser"]["distracting_seconds"]
    projects = summary["projects"]
    goals_met = sum(1 for p in projects if p.get("goal_met"))
    goals_total = sum(1 for p in projects if p.get("goal_minutes"))
    untagged_seconds = sum(
        float(a.get("seconds", 0)) for a in summary["untagged"].get("apps", [])
    ) + sum(
        float(d.get("seconds", 0)) for d in summary["untagged"].get("domains", [])
    )
    return {
        **summary,
        "hourly": hourly_activity(db, day),
        "app_details": app_details(db, day),
        "categories": category_totals(db, day),
        "file_types": file_type_totals(db, day),
        "recent_files": recent_file_events(db, day),
        "kpis": {
            "active_seconds": summary["active_seconds"],
            "idle_seconds": summary["idle_seconds"],
            "total_seconds": total,
            "active_percent": round(100 * summary["active_seconds"] / total) if total else 0,
            "apps_used": summary["apps_used"],
            "browser_seconds": summary["browser"]["active_seconds"],
            "productive_seconds": productive,
            "distracting_seconds": distracting,
            "productive_percent": round(100 * productive / (productive + distracting)) if (productive + distracting) else 0,
            "projects_worked": len(projects),
            "goals_met": goals_met,
            "goals_total": goals_total,
            "file_events": sum(summary["file_counts"].values()),
            "visits": summary["browser"]["visits"],
            "searches": len(summary["browser"]["searches"]),
            "downloads": len(summary["browser"]["downloads"]),
            "untagged_seconds": untagged_seconds,
        },
    }


def range_summary(db: Database, days: int = 7, end: str | None = None,
                  tagger=None) -> list[dict]:
    """One row per day for the trend chart: active, idle, browser,
    productive, distracting, visits, file events, top project."""
    end_date = datetime.strptime(_parse_date(end), "%Y-%m-%d").date()
    out = []
    for i in range(days - 1, -1, -1):
        day = (end_date - timedelta(days=i)).isoformat()
        s = build_summary(db, day, tagger=tagger)
        top_project = s["projects"][0]["project"] if s["projects"] else ""
        out.append({
            "date": day,
            "active_seconds": s["active_seconds"],
            "idle_seconds": s["idle_seconds"],
            "browser_seconds": s["browser"]["active_seconds"],
            "productive_seconds": s["browser"]["productive_seconds"],
            "distracting_seconds": s["browser"]["distracting_seconds"],
            "visits": s["browser"]["visits"],
            "file_events": sum(s["file_counts"].values()),
            "projects": len(s["projects"]),
            "top_project": top_project,
        })
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def make_handler(db_path: Path, tagger, project_root: Path, alerts_settings: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep console quiet
            pass

        def _send_json(self, payload, code: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _open_db(self) -> Database:
            return Database(db_path)

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            get = lambda k, default=None: (query.get(k) or [default])[0]

            if parsed.path in ("/", "/index.html"):
                index = STATIC_DIR / "index.html"
                if index.exists():
                    self._send_file(index, "text/html; charset=utf-8")
                else:
                    self._send_json({"ok": False, "error": "dashboard page missing"}, 500)
                return

            if parsed.path == "/api/health":
                self._send_json({"ok": True, "service": "deskaudit-dashboard"})
                return

            if parsed.path == "/api/summary":
                day = _parse_date(get("date"))
                db = self._open_db()
                try:
                    self._send_json(dashboard_summary(db, day, tagger=tagger))
                finally:
                    db.close()
                return

            if parsed.path == "/api/range":
                try:
                    days = max(1, min(90, int(get("days", 7) or 7)))
                except ValueError:
                    days = 7
                db = self._open_db()
                try:
                    self._send_json({
                        "end": _parse_date(get("end")),
                        "days": days,
                        "rows": range_summary(db, days, end=get("end"), tagger=tagger),
                    })
                finally:
                    db.close()
                return

            if parsed.path == "/api/alerts":
                from .alerts import check_alerts, save_alerts

                day = _parse_date(get("date"))
                db = self._open_db()
                try:
                    found = check_alerts(db, day, settings=alerts_settings or {}, tagger=tagger)
                    save_alerts(db, found)
                    rows = db.list_alerts(date=day, limit=50)
                    self._send_json({"date": day, "alerts": [{
                        "id": r["id"], "type": r["alert_type"], "severity": r["severity"],
                        "title": r["title"], "message": r["message"],
                        "is_read": bool(r["is_read"]),
                    } for r in rows]})
                finally:
                    db.close()
                return

            if parsed.path == "/api/narrative":
                from .summary import generate_daily_summary

                day = _parse_date(get("date"))
                db = self._open_db()
                try:
                    self._send_json(generate_daily_summary(db, day, tagger=tagger))
                finally:
                    db.close()
                return

            if parsed.path == "/api/weekly":
                from .summary import generate_weekly_summary

                try:
                    days = max(1, min(30, int(get("days", 7) or 7)))
                except ValueError:
                    days = 7
                db = self._open_db()
                try:
                    self._send_json(generate_weekly_summary(
                        db, end=_parse_date(get("end")), days=days, tagger=tagger))
                finally:
                    db.close()
                return

            if parsed.path == "/api/export":
                day = _parse_date(get("date"))
                fmt = (get("format") or "md").lower()
                from .report import generate_report

                db = self._open_db()
                try:
                    generate_report(db, day, project_root / "reports", tagger=tagger)
                finally:
                    db.close()
                mapping = {"md": (f"{day}.md", "text/markdown"),
                           "csv": (f"{day}.csv", "text/csv"),
                           "browser": (f"{day}-browser.csv", "text/csv"),
                           "projects": (f"{day}-projects.csv", "text/csv")}
                name, ctype = mapping.get(fmt, mapping["md"])
                path = project_root / "reports" / name
                if path.exists():
                    self._send_file(path, ctype)
                else:
                    self._send_json({"ok": False, "error": f"export {name} not found"}, 404)
                return

            self._send_json({"ok": False, "error": "unknown route"}, 404)

    return Handler


class DashboardServer:
    def __init__(self, db_path, host: str = "127.0.0.1", port: int = 8000,
                 tagger=None, project_root: Path | None = None,
                 alerts_settings: dict | None = None):
        self.db_path = Path(db_path)
        self.host = host
        self.port = port
        self.tagger = tagger
        self.alerts_settings = alerts_settings or {}
        self.project_root = Path(project_root) if project_root else Path(".")
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> str:
        handler = make_handler(self.db_path, self.tagger, self.project_root,
                               alerts_settings=self.alerts_settings)
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = self.server.server_address[1]  # 0 -> real port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def serve_forever(self) -> None:
        if self.server is not None:
            self.server.serve_forever()
