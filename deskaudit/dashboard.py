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
  * Data transfers (organisation): USB copies, cloud / email / message
    channels, prints, sensitive-folder copies, large transfers

API (all JSON, all local):
  GET /api/health
  GET /api/summary?date=YYYY-MM-DD
  GET /api/range?days=7&end=YYYY-MM-DD
  GET /api/alerts?date=YYYY-MM-DD
  GET /api/transfers?date=YYYY-MM-DD
  GET /api/narrative?date=YYYY-MM-DD
  GET /api/weekly?end=YYYY-MM-DD&days=7
  GET /api/export?date=YYYY-MM-DD&format=md|csv|browser|projects|transfers
  GET /api/settings — current settings (the API key value is never returned)
  POST /api/settings — save settings; writes config.local.yaml only,
    never config.yaml. The agent/dashboard must restart to apply them.

Security: binds to 127.0.0.1 by default. Use --host 0.0.0.0 only on a
trusted local network — the dashboard shows window titles and URLs.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
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
    # Untagged time is the APP time that has no project. Browser time is
    # a subset of app time (Chrome is an app), so adding domains on top
    # would double-count it. (Matches the narrative + report, which use
    # apps only.)
    untagged_seconds = sum(
        float(a.get("seconds", 0)) for a in summary["untagged"].get("apps", [])
    )
    tsum = transfer_summary(db, day)
    return {
        **summary,
        "hourly": hourly_activity(db, day),
        "app_details": app_details(db, day),
        "categories": category_totals(db, day),
        "file_types": file_type_totals(db, day),
        "recent_files": recent_file_events(db, day),
        "transfers": tsum,
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
            "transfers_total": tsum["total"],
            "usb_copies": tsum["counts"].get("usb_copy", {}).get("count", 0),
        },
    }


def transfer_summary(db: Database, day: str) -> dict:
    """Data transfers for one day: counts by type, latest rows, USB drives."""
    try:
        counts = db.transfer_counts(day)
        rows = db.transfers_for_date(day, limit=50)
    except Exception:
        counts, rows = {}, []
    total = sum(c.get("count", 0) for c in counts.values())
    return {
        "date": day,
        "total": total,
        "counts": counts,
        "rows": [{
            "type": r["transfer_type"], "channel": r["channel"] or "",
            "file": r["file_name"] or "", "path": r["file_path"] or "",
            "size_bytes": r["size_bytes"],
            "destination": r["destination"] or "", "app": r["app"] or "",
            "at": r["occurred_at"] or "",
        } for r in rows],
        "usb_drives": sorted({r["channel"] for r in rows
                              if r["transfer_type"] in ("usb_insert", "usb_copy", "usb_remove")
                              and r["channel"]}),
        "cloud_domains": sorted({r["channel"] for r in rows
                                 if r["transfer_type"] == "cloud" and r["channel"]}),
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
# Settings — read/write config.local.yaml (never config.yaml, never git)
# ---------------------------------------------------------------------------

# Editable settings: (section, key) -> (kind, min, max). The API key is
# write-only: GET reports only whether one is set, never its value.
_INT_SETTINGS: dict[tuple[str, str], tuple[int, int]] = {
    ("agent", "poll_interval_seconds"): (1, 60),
    ("agent", "idle_threshold_seconds"): (60, 3600),
    ("agent", "browser_poll_seconds"): (10, 600),
    ("agent", "browser_receiver_port"): (1, 65535),
    ("alerts", "distracting_minutes"): (1, 1440),
    ("alerts", "untagged_minutes"): (1, 1440),
    ("alerts", "untagged_percent"): (1, 100),
    ("alerts", "idle_minutes"): (1, 1440),
    ("alerts", "large_download_mb"): (1, 100000),
    ("alerts", "new_app_min_minutes"): (0, 1440),
    ("backup", "backup_keep_days"): (1, 365),
    ("backup", "backup_keep_count"): (1, 1000),
    ("secret_gate", "clicks"): (2, 8),
    ("transfers", "poll_seconds"): (10, 600),
    ("transfers", "cloud_min_seconds"): (0, 3600),
    ("transfers", "communication_min_seconds"): (0, 3600),
    ("transfers", "cloud_alert_minutes"): (1, 1440),
    ("transfers", "communication_alert_minutes"): (1, 1440),
    ("transfers", "large_transfer_mb"): (1, 100000),
}
_BOOL_SETTINGS: set[tuple[str, str]] = {
    ("agent", "browser_enabled"),
    ("agent", "track_downloads"),
    ("agent", "track_searches"),
    ("agent", "browser_receiver_enabled"),
    ("agent", "dashboard_on_start"),
    ("alerts", "enabled"),
    ("alerts", "notify"),
    ("backup", "backup_on_start"),
    ("secret_gate", "enabled"),
    ("transfers", "enabled"),
    ("transfers", "usb_alert"),
    ("transfers", "print_alert"),
    ("transfers", "sensitive_copy_alert"),
}
# List settings: the UI sends one comma-separated string, stored as a list.
_LIST_SETTINGS: set[tuple[str, str]] = {
    ("transfers", "sensitive_folders"),
}
_LLM_PROVIDERS = ("gemini", "openai", "anthropic")


def _local_path(config_path: Path) -> Path:
    return Path(config_path).with_name("config.local.yaml")


def _read_local_raw(config_path: Path) -> dict:
    """config.local.yaml as a plain dict ({} when missing/broken)."""
    import yaml  # type: ignore

    path = _local_path(config_path)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def get_settings(config_path: str | Path) -> dict:
    """Current effective settings for the dashboard Settings page.

    Values come from the merged config (config.yaml + local overlay).
    The LLM API key is never returned — only whether one is set and
    where it comes from (local file, env var, or nowhere).
    """
    from .alerts import DEFAULT_ALERTS

    cfg_path = Path(config_path)
    cfg = Config.load(cfg_path)
    local = _read_local_raw(cfg_path)
    local_llm = local.get("llm") if isinstance(local.get("llm"), dict) else {}
    if local_llm.get("api_key"):
        key_source = "local"
    elif os.environ.get("DESKAUDIT_LLM_API_KEY"):
        key_source = "env"
    else:
        key_source = "none"
    alerts = {**DEFAULT_ALERTS, **(cfg.alerts or {})}
    from .secret_gate import DEFAULT_SECRET_GATE
    from .transfers import DEFAULT_TRANSFERS

    gate = {**DEFAULT_SECRET_GATE, **(cfg.secret_gate or {})}
    transfers = {**DEFAULT_TRANSFERS, **(cfg.transfers or {})}
    return {
        "llm": {
            "provider": str(cfg.llm.get("provider") or "gemini"),
            "model": str(cfg.llm.get("model") or ""),
            "api_key_set": key_source != "none",
            "api_key_source": key_source,
        },
        "agent": {
            "poll_interval_seconds": int(cfg.poll_interval_seconds),
            "idle_threshold_seconds": int(cfg.idle_threshold_seconds),
            "browser_enabled": bool(cfg.browser_enabled),
            "browser_poll_seconds": int(cfg.browser_poll_seconds),
            "track_downloads": bool(cfg.track_downloads),
            "track_searches": bool(cfg.track_searches),
            "browser_receiver_enabled": bool(cfg.browser_receiver_enabled),
            "browser_receiver_port": int(cfg.browser_receiver_port),
            "dashboard_on_start": bool(cfg.dashboard_on_start),
        },
        "secret_gate": {
            "enabled": bool(gate.get("enabled", True)),
            "clicks": int(gate.get("clicks", 4)),
            "code": str(gate.get("code", "1122")),
            "click_window_seconds": int(gate.get("click_window_seconds", 2)),
            "arm_seconds": int(gate.get("arm_seconds", 10)),
        },
        "alerts": {
            "enabled": bool(alerts.get("enabled", True)),
            "notify": bool(alerts.get("notify", True)),
            "distracting_minutes": int(alerts.get("distracting_minutes", 60)),
            "untagged_minutes": int(alerts.get("untagged_minutes", 60)),
            "untagged_percent": int(alerts.get("untagged_percent", 50)),
            "idle_minutes": int(alerts.get("idle_minutes", 120)),
            "large_download_mb": int(alerts.get("large_download_mb", 500)),
            "new_app_min_minutes": int(alerts.get("new_app_min_minutes", 5)),
        },
        "backup": {
            "backup_keep_days": int(cfg.backup_keep_days),
            "backup_keep_count": int(cfg.backup_keep_count),
            "backup_on_start": bool(cfg.backup_on_start),
        },
        "transfers": {
            "enabled": bool(transfers.get("enabled", True)),
            "poll_seconds": int(transfers.get("poll_seconds", 60)),
            "sensitive_folders": [str(f) for f in (transfers.get("sensitive_folders") or [])],
            "cloud_min_seconds": int(transfers.get("cloud_min_seconds", 60)),
            "communication_min_seconds": int(transfers.get("communication_min_seconds", 60)),
            "cloud_alert_minutes": int(transfers.get("cloud_alert_minutes", 15)),
            "communication_alert_minutes": int(transfers.get("communication_alert_minutes", 30)),
            "large_transfer_mb": int(transfers.get("large_transfer_mb", 100)),
            "usb_alert": bool(transfers.get("usb_alert", True)),
            "print_alert": bool(transfers.get("print_alert", True)),
            "sensitive_copy_alert": bool(transfers.get("sensitive_copy_alert", True)),
        },
        "config_path": cfg_path.name,
        "local_path": _local_path(cfg_path).name,
        "local_exists": _local_path(cfg_path).exists(),
        "note": "Saved to config.local.yaml (config.yaml is never changed). "
                "Restart the agent and the dashboard for changes to take effect.",
    }


def save_settings(config_path: str | Path, updates: dict) -> dict:
    """Validate `updates` and merge them into config.local.yaml.

    `updates` uses the same sections as get_settings(). Unknown keys are
    ignored. An empty llm.api_key clears the stored key. Raises
    ValueError on any invalid value (nothing is written then).
    Returns the fresh get_settings() view.
    """
    import yaml  # type: ignore

    if not isinstance(updates, dict):
        raise ValueError("settings must be a JSON object")
    cfg_path = Path(config_path)
    new_local: dict[str, dict] = {}
    errors: list[str] = []
    for section, values in updates.items():
        if section not in ("llm", "agent", "alerts", "backup", "secret_gate", "transfers"):
            continue
        if not isinstance(values, dict):
            errors.append(f"{section} must be an object")
            continue
        merged: dict = {}
        for key, value in values.items():
            if section == "llm" and key == "provider":
                if value not in _LLM_PROVIDERS:
                    errors.append(f"llm.provider must be one of {', '.join(_LLM_PROVIDERS)}")
                else:
                    merged[key] = value
            elif section == "llm" and key == "model":
                if not isinstance(value, str) or not (1 <= len(value) <= 100):
                    errors.append("llm.model must be a non-empty name")
                else:
                    merged[key] = value
            elif section == "llm" and key == "api_key":
                if not isinstance(value, str):
                    errors.append("llm.api_key must be a string")
                elif value == "":
                    merged[key] = None  # clear the stored key
                else:
                    merged[key] = value
            elif section == "secret_gate" and key == "code":
                if not isinstance(value, str) or not value.isdigit() or not (1 <= len(value) <= 8):
                    errors.append("secret_gate.code must be 1-8 digits")
                else:
                    merged[key] = value
            elif (section, key) in _LIST_SETTINGS:
                # Comma-separated string from the UI -> list of folders.
                if not isinstance(value, str):
                    errors.append(f"{section}.{key} must be a comma-separated string")
                else:
                    items = [part.strip() for part in value.split(",")]
                    items = [item for item in items if item]
                    if any(len(item) > 260 for item in items):
                        errors.append(f"{section}.{key}: a folder path is too long")
                    else:
                        merged[key] = items
            elif (section, key) in _INT_SETTINGS:
                lo, hi = _INT_SETTINGS[(section, key)]
                if isinstance(value, bool) or not isinstance(value, int) or not (lo <= value <= hi):
                    errors.append(f"{section}.{key} must be an integer {lo}-{hi}")
                else:
                    merged[key] = value
            elif (section, key) in _BOOL_SETTINGS:
                if not isinstance(value, bool):
                    errors.append(f"{section}.{key} must be true or false")
                else:
                    merged[key] = value
            # unknown keys are ignored
        if merged:
            new_local[section] = merged
    if errors:
        raise ValueError("; ".join(errors))
    if not new_local:
        return get_settings(cfg_path)
    # File layout differs from the API grouping: llm/alerts/secret_gate/
    # transfers are nested dicts in the yaml, but agent/backup keys live
    # at the top level (poll_interval_seconds, backup_keep_days, ...).
    local = _read_local_raw(cfg_path)
    for section, values in new_local.items():
        if section in ("llm", "alerts", "secret_gate", "transfers"):
            current = local.get(section)
            if not isinstance(current, dict):
                current = {}
            for key, value in values.items():
                if value is None:
                    current.pop(key, None)  # clear
                else:
                    current[key] = value
            local[section] = current
        else:  # agent, backup: top-level keys
            for key, value in values.items():
                if value is None:
                    local.pop(key, None)
                else:
                    local[key] = value
    path = _local_path(cfg_path)
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(local, f, sort_keys=False, allow_unicode=True)
    return get_settings(cfg_path)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).resolve().parent / "dashboard_static"


def make_handler(db_path: Path, tagger, project_root: Path, alerts_settings: dict | None = None,
                 config_path: Path | None = None,
                 transfers_settings: dict | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep console quiet
            pass

        def _send_json(self, payload, code: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self._responded = True
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._responded = True
            self.wfile.write(data)

        def _open_db(self) -> Database:
            return Database(db_path)

        def do_GET(self):
            # Any unexpected error (e.g. a locked database) must answer
            # 500 JSON, never drop the connection silently — the page
            # would otherwise sit on "Loading…" forever.
            try:
                self._do_GET()
            except Exception as exc:
                if not getattr(self, "_responded", False):
                    try:
                        self._send_json({"ok": False, "error": str(exc)}, 500)
                    except Exception:
                        pass

        def _do_GET(self):
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
                    found = check_alerts(db, day, settings=alerts_settings or {}, tagger=tagger,
                                         transfer_settings=transfers_settings or {})
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

            if parsed.path == "/api/transfers":
                day = _parse_date(get("date"))
                db = self._open_db()
                try:
                    self._send_json(transfer_summary(db, day))
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
                           "projects": (f"{day}-projects.csv", "text/csv"),
                           "transfers": (f"{day}-transfers.csv", "text/csv")}
                name, ctype = mapping.get(fmt, mapping["md"])
                path = project_root / "reports" / name
                if path.exists():
                    self._send_file(path, ctype)
                else:
                    self._send_json({"ok": False, "error": f"export {name} not found"}, 404)
                return

            if parsed.path == "/api/settings":
                if config_path is None:
                    self._send_json({"ok": False, "error": "settings unavailable"}, 500)
                else:
                    self._send_json(get_settings(config_path))
                return

            self._send_json({"ok": False, "error": "unknown route"}, 404)

        def do_POST(self):
            try:
                self._do_POST()
            except Exception as exc:
                if not getattr(self, "_responded", False):
                    try:
                        self._send_json({"ok": False, "error": str(exc)}, 500)
                    except Exception:
                        pass

        def _do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/settings":
                self._send_json({"ok": False, "error": "unknown route"}, 404)
                return
            if config_path is None:
                self._send_json({"ok": False, "error": "settings unavailable"}, 500)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            try:
                updates = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, OSError):
                self._send_json({"ok": False, "error": "invalid JSON body"}, 400)
                return
            try:
                saved = save_settings(config_path, updates)
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return
            except OSError as e:
                self._send_json({"ok": False, "error": f"could not save settings: {e}"}, 500)
                return
            self._send_json({"ok": True, "saved": saved})

    return Handler


class DashboardServer:
    def __init__(self, db_path, host: str = "127.0.0.1", port: int = 8000,
                 tagger=None, project_root: Path | None = None,
                 alerts_settings: dict | None = None,
                 config_path: Path | None = None,
                 transfers_settings: dict | None = None):
        self.db_path = Path(db_path)
        self.host = host
        self.port = port
        self.tagger = tagger
        self.alerts_settings = alerts_settings or {}
        self.transfers_settings = transfers_settings or {}
        self.project_root = Path(project_root) if project_root else Path(".")
        self.config_path = Path(config_path) if config_path else None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> str:
        handler = make_handler(self.db_path, self.tagger, self.project_root,
                               alerts_settings=self.alerts_settings,
                               config_path=self.config_path,
                               transfers_settings=self.transfers_settings)
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
