"""SQLite storage for DeskAudit.

Tables
------
app_sessions: one row per foreground app + window title run.
    active_seconds counts only time when the user was NOT idle.
file_events: one row per created / modified / deleted / moved file.

All timestamps are stored as ISO strings in local time, so reports can
group by calendar date without timezone maths.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    exe_path TEXT,
    window_title TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL DEFAULT 0,
    active_seconds REAL DEFAULT 0,
    idle_seconds REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS file_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,          -- created | modified | deleted | moved
    path TEXT NOT NULL,
    dest_path TEXT,
    is_directory INTEGER DEFAULT 0,
    size_bytes INTEGER,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_started ON app_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_file_occurred ON file_events(occurred_at);
-- Browser tracking (see browser_tracker.py / browser_history.py).
-- browser_visits: one row per history visit, deduped on browser+url+time.
CREATE TABLE IF NOT EXISTS browser_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    browser TEXT NOT NULL,
    url TEXT NOT NULL,
    domain TEXT,
    title TEXT,
    category TEXT,
    is_productive INTEGER DEFAULT 0,
    search_query TEXT,
    visited_at TEXT NOT NULL,
    UNIQUE(browser, url, visited_at)
);
CREATE TABLE IF NOT EXISTS browser_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    browser TEXT NOT NULL,
    url TEXT,
    domain TEXT,
    title TEXT,
    category TEXT,
    is_productive INTEGER DEFAULT 0,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL DEFAULT 0,
    active_seconds REAL DEFAULT 0,
    idle_seconds REAL DEFAULT 0,
    tab_switches INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS browser_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    browser TEXT NOT NULL,
    url TEXT,
    domain TEXT,
    file_path TEXT,
    file_name TEXT,
    total_bytes INTEGER,
    received_bytes INTEGER,
    state TEXT,
    started_at TEXT,
    UNIQUE(browser, file_path, started_at)
);
CREATE TABLE IF NOT EXISTS browser_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    browser TEXT NOT NULL,
    query TEXT NOT NULL,
    url TEXT,
    domain TEXT,
    searched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_visited ON browser_visits(visited_at);
CREATE INDEX IF NOT EXISTS idx_browser_domain ON browser_visits(domain);
CREATE INDEX IF NOT EXISTS idx_browser_session_started ON browser_sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_browser_search ON browser_searches(searched_at);
-- Project tagging (see projects.py). project/tags columns are added to the
-- tables above by _migrate_project_columns() for existing databases.
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    description TEXT DEFAULT '',
    color TEXT DEFAULT '',
    parent TEXT DEFAULT '',
    daily_goal_minutes INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS project_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    project TEXT NOT NULL,
    tags TEXT DEFAULT '',
    UNIQUE(match_type, match_value)
);
-- Alerts (see alerts.py). One row per alert, deduped on date+type+title.
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    is_read INTEGER DEFAULT 0,
    UNIQUE(date, alert_type, title)
);
CREATE INDEX IF NOT EXISTS idx_alerts_date ON alerts(date);
-- Data transfers (see transfers.py). Metadata only: file name, size,
-- destination (drive letter / domain / printer), time and app. Never
-- the content of what was typed or sent.
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    transfer_type TEXT NOT NULL,   -- usb_insert | usb_remove | usb_copy |
                                   -- cloud | email | message | print |
                                   -- sensitive_copy | large_transfer
    channel TEXT DEFAULT '',       -- drive letter, domain, printer, folder
    file_name TEXT DEFAULT '',
    file_path TEXT DEFAULT '',
    size_bytes INTEGER,
    destination TEXT DEFAULT '',
    app TEXT DEFAULT '',
    occurred_at TEXT NOT NULL,
    details TEXT DEFAULT '',
    UNIQUE(date, transfer_type, channel, file_name, file_path, occurred_at)
);
CREATE INDEX IF NOT EXISTS idx_transfers_date ON transfers(date);
CREATE INDEX IF NOT EXISTS idx_transfers_type ON transfers(transfer_type);
-- Project indexes are created in _migrate_project_columns(), after the
-- project/tags columns exist (fresh DBs get them there too).
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        try:
            # WAL lets dashboard reads proceed while the agent writes
            # (otherwise a write lock can fail a dashboard request).
            # busy_timeout makes short locks wait instead of erroring.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=10000")
        except Exception:
            pass
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate_project_columns()
        self._migrate_transfers_dedup()

    def _migrate_transfers_dedup(self) -> None:
        """v11.1: the transfers UNIQUE key now includes file_path, so the
        same file name in two folders (or two print jobs of one document
        in one poll) are no longer silently dropped as duplicates.
        SQLite cannot ALTER a UNIQUE constraint, so an old table is
        rebuilt (copy rows, drop, rename). Fresh DBs already have the
        new key — the check below skips them."""
        try:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='transfers'"
            ).fetchone()
            if row is None or "file_path, occurred_at" in (row["sql"] or ""):
                return
            self.conn.execute(
                """CREATE TABLE transfers_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    transfer_type TEXT NOT NULL,
                    channel TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    size_bytes INTEGER,
                    destination TEXT DEFAULT '',
                    app TEXT DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    details TEXT DEFAULT '',
                    UNIQUE(date, transfer_type, channel, file_name, file_path, occurred_at)
                )""")
            self.conn.execute(
                """INSERT OR IGNORE INTO transfers_new
                   (id, date, transfer_type, channel, file_name, file_path,
                    size_bytes, destination, app, occurred_at, details)
                   SELECT id, date, transfer_type, channel, file_name, file_path,
                          size_bytes, destination, app, occurred_at, details
                   FROM transfers""")
            self.conn.execute("DROP TABLE transfers")
            self.conn.execute("ALTER TABLE transfers_new RENAME TO transfers")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_date ON transfers(date)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_transfers_type ON transfers(transfer_type)")
            self.conn.commit()
        except Exception:
            # Never break startup over a migration; the old key still works.
            self.conn.rollback()

    def _migrate_project_columns(self) -> None:
        """Add project/tags columns to tables created before tagging existed.
        Safe to run on every start: PRAGMA check first, ALTER only if missing."""
        columns = {
            "app_sessions": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
            "file_events": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
            "browser_visits": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
            "browser_sessions": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
            "browser_downloads": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
            "browser_searches": [("project", "TEXT DEFAULT 'Untagged'"), ("tags", "TEXT DEFAULT ''")],
        }
        for table, cols in columns.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, definition in cols:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_app_project ON app_sessions(project)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_browser_visit_project ON browser_visits(project)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_file_project ON file_events(project)")
        self.conn.commit()

    @staticmethod
    def _tags_str(tags) -> str:
        if not tags:
            return ""
        if isinstance(tags, str):
            return tags
        return ",".join(str(t) for t in tags)

    # -- app sessions -------------------------------------------------
    def start_app_session(self, app_name: str, exe_path: str, title: str,
                          started_at: datetime, project: str = "Untagged",
                          tags=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO app_sessions (app_name, exe_path, window_title, started_at, project, tags)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (app_name, exe_path, title, started_at.isoformat(timespec="seconds"),
             project, self._tags_str(tags)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_app_session(self, session_id: int, ended_at: datetime,
                          duration: float, active: float, idle: float) -> None:
        self.update_app_session(session_id, ended_at, duration, active, idle)

    def update_app_session(self, session_id: int, ended_at: datetime,
                           duration: float, active: float, idle: float) -> None:
        """Write the current durations into an OPEN session row without
        closing it. The agent flushes this every poll, so a force-stop,
        reboot or crash loses at most one poll interval — the old code
        only wrote durations on window change / clean shutdown, so the
        whole current session was lost on every taskkill."""
        self.conn.execute(
            """UPDATE app_sessions SET ended_at=?, duration_seconds=?,
               active_seconds=?, idle_seconds=? WHERE id=?""",
            (ended_at.isoformat(timespec="seconds"), duration, active, idle, session_id),
        )
        self.conn.commit()

    # -- file events --------------------------------------------------
    def log_file_event(self, event_type: str, path: str, dest_path: str | None = None,
                       is_directory: bool = False, size_bytes: int | None = None,
                       occurred_at: datetime | None = None,
                       project: str = "Untagged", tags=None) -> None:
        when = occurred_at or datetime.now().astimezone()
        self.conn.execute(
            """INSERT INTO file_events
               (event_type, path, dest_path, is_directory, size_bytes, occurred_at, project, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_type, path, dest_path, int(is_directory), size_bytes,
             when.isoformat(timespec="seconds"), project, self._tags_str(tags)),
        )
        self.conn.commit()

    # -- queries used by reports --------------------------------------
    def app_totals(self, date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT app_name, window_title,
                      SUM(active_seconds) AS active,
                      SUM(duration_seconds) AS total,
                      COUNT(*) AS sessions
               FROM app_sessions
               WHERE substr(started_at, 1, 10) = ?
               GROUP BY app_name, window_title
               ORDER BY active DESC""",
            (date,),
        ).fetchall()

    def totals(self, date: str) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(active_seconds), 0) AS active,
                      COALESCE(SUM(idle_seconds), 0) AS idle,
                      COALESCE(SUM(duration_seconds), 0) AS total,
                      COUNT(DISTINCT app_name) AS apps
               FROM app_sessions WHERE substr(started_at, 1, 10) = ?""",
            (date,),
        ).fetchone()
        return row

    def file_counts(self, date: str) -> dict[str, int]:
        rows = self.conn.execute(
            """SELECT event_type, COUNT(*) AS n FROM file_events
               WHERE substr(occurred_at, 1, 10) = ? GROUP BY event_type""",
            (date,),
        ).fetchall()
        return {r["event_type"]: r["n"] for r in rows}

    # -- browser: state (last-seen watermark per profile) -----------
    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # -- browser: visits / sessions / downloads / searches -----------
    def log_browser_visit(self, browser: str, url: str, domain: str, title: str,
                          visited_at: datetime, search_query: str | None = None,
                          project: str = "Untagged", tags=None) -> bool:
        from .browser_utils import classify_domain

        category, productive = classify_domain(domain or "")
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO browser_visits
                   (browser, url, domain, title, category, is_productive,
                    search_query, visited_at, project, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (browser, url, domain, title, category, int(productive),
                 search_query, visited_at.isoformat(timespec="seconds"),
                 project, self._tags_str(tags)),
            )
            self.conn.commit()
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception:
            return False

    def log_browser_search(self, browser: str, query: str, url: str,
                           domain: str, searched_at: datetime,
                           project: str = "Untagged", tags=None) -> None:
        self.conn.execute(
            """INSERT INTO browser_searches (browser, query, url, domain, searched_at, project, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (browser, query, url, domain, searched_at.isoformat(timespec="seconds"),
             project, self._tags_str(tags)),
        )
        self.conn.commit()

    def log_browser_download(self, browser: str, url: str, domain: str,
                             file_path: str, file_name: str,
                             total_bytes: int | None, received_bytes: int | None,
                             state: str, started_at: datetime | None,
                             project: str = "Untagged", tags=None) -> bool:
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO browser_downloads
                   (browser, url, domain, file_path, file_name, total_bytes,
                    received_bytes, state, started_at, project, tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (browser, url, domain, file_path, file_name, total_bytes,
                 received_bytes, state,
                 started_at.isoformat(timespec="seconds") if started_at else None,
                 project, self._tags_str(tags)),
            )
            self.conn.commit()
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception:
            return False

    def start_browser_session(self, browser: str, url: str, domain: str, title: str,
                              category: str, productive: bool,
                              started_at: datetime, project: str = "Untagged",
                              tags=None) -> int:
        cur = self.conn.execute(
            """INSERT INTO browser_sessions
               (browser, url, domain, title, category, is_productive, started_at, project, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (browser, url, domain, title, category, int(productive),
             started_at.isoformat(timespec="seconds"), project, self._tags_str(tags)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_browser_session(self, session_id: int, ended_at: datetime,
                              duration: float, active: float, idle: float,
                              tab_switches: int = 0) -> None:
        self.update_browser_session(session_id, ended_at, duration, active,
                                    idle, tab_switches)

    def update_browser_session(self, session_id: int, ended_at: datetime,
                               duration: float, active: float, idle: float,
                               tab_switches: int = 0) -> None:
        """Same as close_browser_session but without closing: the periodic
        flush for the open browser session (see update_app_session)."""
        self.conn.execute(
            """UPDATE browser_sessions SET ended_at=?, duration_seconds=?,
               active_seconds=?, idle_seconds=?, tab_switches=? WHERE id=?""",
            (ended_at.isoformat(timespec="seconds"), duration, active, idle,
             tab_switches, session_id),
        )
        self.conn.commit()

    def latest_url_for_title(self, browser: str, title: str) -> str:
        """Most recent visit URL whose title matches (for title-only sessions)."""
        if not title:
            return ""
        row = self.conn.execute(
            """SELECT url FROM browser_visits
               WHERE browser=? AND title LIKE ? ORDER BY visited_at DESC LIMIT 1""",
            (browser, f"%{title[:60]}%"),
        ).fetchone()
        return row["url"] if row else ""

    # -- browser: report queries --------------------------------------
    def browser_domain_totals(self, date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT domain, category, is_productive,
                      SUM(active_seconds) AS active,
                      SUM(duration_seconds) AS total,
                      COUNT(*) AS sessions
               FROM browser_sessions
               WHERE substr(started_at, 1, 10) = ? AND domain != ''
               GROUP BY domain, category ORDER BY active DESC""",
            (date,),
        ).fetchall()

    def browser_visit_domain_totals(self, date: str) -> list[sqlite3.Row]:
        """Fallback for reports when only history was imported (no live
        sessions yet): aggregate visits by domain."""
        return self.conn.execute(
            """SELECT domain, category, is_productive,
                      0 AS active, 0 AS total, COUNT(*) AS sessions
               FROM browser_visits
               WHERE substr(visited_at, 1, 10) = ? AND domain != ''
               GROUP BY domain, category ORDER BY sessions DESC""",
            (date,),
        ).fetchall()

    def browser_totals(self, date: str) -> sqlite3.Row:
        return self.conn.execute(
            """SELECT COALESCE(SUM(active_seconds), 0) AS active,
                      COALESCE(SUM(duration_seconds), 0) AS total,
                      COUNT(DISTINCT domain) AS domains,
                      COALESCE(SUM(CASE WHEN is_productive THEN active_seconds ELSE 0 END), 0) AS productive,
                      COALESCE(SUM(CASE WHEN NOT is_productive THEN active_seconds ELSE 0 END), 0) AS distracting
               FROM browser_sessions WHERE substr(started_at, 1, 10) = ?""",
            (date,),
        ).fetchone()

    def browser_visit_count(self, date: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM browser_visits WHERE substr(visited_at, 1, 10) = ?",
            (date,),
        ).fetchone()
        return int(row["n"] or 0)

    def top_searches(self, date: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT query, COUNT(*) AS n FROM browser_searches
               WHERE substr(searched_at, 1, 10) = ?
               GROUP BY query ORDER BY n DESC, query ASC LIMIT ?""",
            (date, limit),
        ).fetchall()

    def downloads_for_date(self, date: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT browser, file_name, total_bytes, state, started_at
               FROM browser_downloads
               WHERE substr(started_at, 1, 10) = ? ORDER BY started_at DESC""",
            (date,),
        ).fetchall()

    # -- projects -------------------------------------------------------
    def upsert_project(self, name: str, description: str = "", color: str = "",
                       parent: str = "", daily_goal_minutes: int = 0) -> None:
        self.conn.execute(
            """INSERT INTO projects (name, description, color, parent, daily_goal_minutes)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description, color=excluded.color,
                 parent=excluded.parent, daily_goal_minutes=excluded.daily_goal_minutes""",
            (name, description, color, parent, daily_goal_minutes),
        )
        self.conn.commit()

    def list_projects(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM projects ORDER BY name").fetchall()

    def set_project_override(self, match_type: str, match_value: str,
                             project: str, tags=None) -> None:
        self.conn.execute(
            """INSERT INTO project_overrides (match_type, match_value, project, tags)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(match_type, match_value) DO UPDATE SET
                 project=excluded.project, tags=excluded.tags""",
            (match_type, match_value, project, self._tags_str(tags)),
        )
        self.conn.commit()

    def list_project_overrides(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM project_overrides ORDER BY match_type, match_value").fetchall()

    def _browser_overlap_seconds(self, date: str, project: str) -> float:
        """Seconds of browser time already counted in app time for a project.

        A browser session runs INSIDE its browser's app session (Chrome is
        an app), so naively adding app + browser time double-counts it.
        Overlap is measured session-by-session from the time intervals:
        for each browser session, the part that falls inside an app session
        of the SAME project, capped by that browser session's active time.
        Browser time whose app session was tagged to a DIFFERENT project
        (or not recorded at all) is NOT overlap — it is real extra time.
        """
        app_rows = self.conn.execute(
            """SELECT started_at, ended_at, active_seconds FROM app_sessions
               WHERE substr(started_at, 1, 10) = ? AND project = ?
               AND ended_at IS NOT NULL""",
            (date, project)).fetchall()
        browser_rows = self.conn.execute(
            """SELECT started_at, ended_at, active_seconds FROM browser_sessions
               WHERE substr(started_at, 1, 10) = ? AND project = ?
               AND ended_at IS NOT NULL""",
            (date, project)).fetchall()
        if not app_rows or not browser_rows:
            return 0.0

        def parse(ts):
            try:
                return datetime.fromisoformat(str(ts))
            except ValueError:
                return None

        apps = [(parse(r["started_at"]), parse(r["ended_at"])) for r in app_rows]
        apps = [(s, e) for s, e in apps if s and e]
        total_overlap = 0.0
        for b in browser_rows:
            bs, be = parse(b["started_at"]), parse(b["ended_at"])
            if not bs or not be:
                continue
            overlap = 0.0
            for a_s, a_e in apps:
                start, end = max(bs, a_s), min(be, a_e)
                if end > start:
                    overlap += (end - start).total_seconds()
            # A browser session cannot overlap more than its own active time.
            total_overlap += min(overlap, float(b["active_seconds"] or 0))
        return total_overlap

    def project_totals(self, date: str) -> list[sqlite3.Row]:
        """Per-project time, plus file/visit counts.

        ``active`` = app time + browser time − overlap. Browser time is a
        SUBSET of app time when both are tagged to the same project, so
        adding them would double-count (e.g. 30 min Chrome + 30 min browser
        session = 30 min, not 60). ``app_active`` / ``browser_active`` are
        still returned separately for the split shown in reports.
        Uses a UNION so a project that only has browser time still appears."""
        rows = self.conn.execute(
            """SELECT project,
                      COALESCE(SUM(app_active), 0) AS app_active,
                      COALESCE(SUM(browser_active), 0) AS browser_active,
                      COALESCE(SUM(file_events), 0) AS file_events,
                      COALESCE(SUM(visits), 0) AS visits
               FROM (
                 SELECT project, SUM(active_seconds) AS app_active, 0 AS browser_active,
                        0 AS file_events, 0 AS visits
                 FROM app_sessions WHERE substr(started_at, 1, 10) = ? GROUP BY project
                 UNION ALL
                 SELECT project, 0, SUM(active_seconds), 0, 0
                 FROM browser_sessions WHERE substr(started_at, 1, 10) = ? GROUP BY project
                 UNION ALL
                 SELECT project, 0, 0, COUNT(*), 0
                 FROM file_events WHERE substr(occurred_at, 1, 10) = ? GROUP BY project
                 UNION ALL
                 SELECT project, 0, 0, 0, COUNT(*)
                 FROM browser_visits WHERE substr(visited_at, 1, 10) = ? GROUP BY project
               ) GROUP BY project""",
            (date, date, date, date),
        ).fetchall()
        # sqlite3.Row is read-only, so rebuild as dict-like rows with the
        # corrected ``active`` column, sorted by it.
        out = []
        for r in rows:
            app_active = float(r["app_active"] or 0)
            browser_active = float(r["browser_active"] or 0)
            overlap = self._browser_overlap_seconds(date, r["project"])
            active = max(0.0, app_active + browser_active - overlap)
            out.append({
                "project": r["project"],
                "app_active": app_active,
                "browser_active": browser_active,
                "active": active,
                "file_events": int(r["file_events"] or 0),
                "visits": int(r["visits"] or 0),
            })
        out.sort(key=lambda x: -x["active"])
        # Parent rollup: a child project's time also counts toward its
        # parent ("Office / Client A" rolls up into "Office"), so the
        # parent row shows the whole team's time while the child row
        # keeps its own. `parent` was stored but never used anywhere.
        try:
            parents = {r["name"]: (r["parent"] or "") for r in self.conn.execute(
                "SELECT name, parent FROM projects").fetchall()}
        except Exception:
            parents = {}
        if parents:
            by_name = {row["project"]: row for row in out}
            for row in list(out):
                parent = parents.get(row["project"]) or ""
                if parent and parent != row["project"]:
                    prow = by_name.get(parent)
                    if prow is None:
                        prow = {"project": parent, "app_active": 0.0,
                                "browser_active": 0.0, "active": 0.0,
                                "file_events": 0, "visits": 0}
                        out.append(prow)
                        by_name[parent] = prow
                    prow["app_active"] += row["app_active"]
                    prow["browser_active"] += row["browser_active"]
                    prow["active"] += row["active"]
                    prow["file_events"] += row["file_events"]
                    prow["visits"] += row["visits"]
            out.sort(key=lambda x: -x["active"])
        return out

    def untagged_top(self, date: str, limit: int = 10) -> dict:
        """Top untagged items, for --suggest-projects."""
        def rows(sql, params):
            return [{"value": r[0], "seconds": float(r[1] or 0), "count": int(r[2] or 0)}
                    for r in self.conn.execute(sql, params).fetchall()]

        # Folders are grouped in Python, not SQL: paths can use Windows
        # backslashes (C:\\Users\\...) or Unix slashes (/home/...), and the
        # old SQL substr/instr('\\') trick only worked for the first kind.
        folder_counts: dict[str, int] = {}
        for (path,) in self.conn.execute(
                """SELECT path FROM file_events
                   WHERE substr(occurred_at, 1, 10) = ? AND project = 'Untagged'""",
                (date,)).fetchall():
            # Normalise both separators, then take the parent directory.
            parent = str(path or "").replace("\\", "/").rsplit("/", 1)[0] or "/"
            folder_counts[parent] = folder_counts.get(parent, 0) + 1
        folders = [{"value": folder, "seconds": 0.0, "count": n}
                   for folder, n in sorted(folder_counts.items(),
                                           key=lambda kv: -kv[1])[:limit]]

        return {
            "apps": rows(
                """SELECT app_name, SUM(active_seconds), COUNT(*) FROM app_sessions
                   WHERE substr(started_at, 1, 10) = ? AND project = 'Untagged'
                   GROUP BY app_name ORDER BY 2 DESC LIMIT ?""", (date, limit)),
            "domains": rows(
                """SELECT domain, SUM(active_seconds), COUNT(*) FROM browser_sessions
                   WHERE substr(started_at, 1, 10) = ? AND project = 'Untagged' AND domain != ''
                   GROUP BY domain ORDER BY 2 DESC LIMIT ?""", (date, limit)),
            "folders": folders,
            "titles": rows(
                """SELECT window_title, SUM(active_seconds), COUNT(*) FROM app_sessions
                   WHERE substr(started_at, 1, 10) = ? AND project = 'Untagged' AND window_title != ''
                   GROUP BY window_title ORDER BY 2 DESC LIMIT ?""", (date, limit)),
        }

    def retag_all(self, tagger, date: str | None = None) -> dict:
        """Recompute project/tags for stored rows using the current rules.
        Returns counts per table. Used by --retag after editing projects.yaml."""
        from .projects import Tagger as _Tagger  # local import, avoids cycles

        counts = {"app_sessions": 0, "file_events": 0, "browser_visits": 0,
                  "browser_sessions": 0, "browser_downloads": 0, "browser_searches": 0}
        date_filter = " AND substr(started_at, 1, 10) = ?" if date else ""
        # app_sessions
        rows = self.conn.execute(
            f"SELECT id, app_name, exe_path, window_title FROM app_sessions WHERE 1=1{date_filter}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_app(r["app_name"], r["exe_path"] or "", r["window_title"] or "")
            cur = self.conn.execute(
                "UPDATE app_sessions SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["app_sessions"] += cur.rowcount
        # file_events
        date_filter_f = " AND substr(occurred_at, 1, 10) = ?" if date else ""
        rows = self.conn.execute(
            f"SELECT id, path, dest_path FROM file_events WHERE 1=1{date_filter_f}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_file(r["path"], r["dest_path"] or "")
            cur = self.conn.execute(
                "UPDATE file_events SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["file_events"] += cur.rowcount
        # browser_visits
        date_filter_v = " AND substr(visited_at, 1, 10) = ?" if date else ""
        rows = self.conn.execute(
            f"SELECT id, url, domain, title, search_query FROM browser_visits WHERE 1=1{date_filter_v}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_browser(r["url"] or "", r["domain"] or "",
                                               r["title"] or "", r["search_query"] or "")
            cur = self.conn.execute(
                "UPDATE browser_visits SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["browser_visits"] += cur.rowcount
        # browser_sessions
        rows = self.conn.execute(
            f"SELECT id, url, domain, title FROM browser_sessions WHERE 1=1{date_filter}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_browser(r["url"] or "", r["domain"] or "", r["title"] or "")
            cur = self.conn.execute(
                "UPDATE browser_sessions SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["browser_sessions"] += cur.rowcount
        # browser_downloads
        date_filter_d = " AND substr(started_at, 1, 10) = ?" if date else ""
        rows = self.conn.execute(
            f"SELECT id, url, domain, file_path FROM browser_downloads WHERE 1=1{date_filter_d}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_download(r["url"] or "", r["domain"] or "", r["file_path"] or "")
            cur = self.conn.execute(
                "UPDATE browser_downloads SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["browser_downloads"] += cur.rowcount
        # browser_searches
        date_filter_s = " AND substr(searched_at, 1, 10) = ?" if date else ""
        rows = self.conn.execute(
            f"SELECT id, query, url, domain FROM browser_searches WHERE 1=1{date_filter_s}",
            (date,) if date else ()).fetchall()
        for r in rows:
            project, tags = tagger.tag_search(r["query"] or "", r["url"] or "", r["domain"] or "")
            cur = self.conn.execute(
                "UPDATE browser_searches SET project=?, tags=? WHERE id=?",
                (project, self._tags_str(tags), r["id"]))
            counts["browser_searches"] += cur.rowcount
        self.conn.commit()
        return counts

    # -- alerts ---------------------------------------------------------
    def save_alert(self, date: str, alert_type: str, severity: str,
                   title: str, message: str, details: str = "") -> bool:
        """Insert an alert; returns True only when it is NEW (deduped)."""
        from datetime import datetime as _dt

        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO alerts
                   (date, alert_type, severity, title, message, details, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (date, alert_type, severity, title, message, details,
                 _dt.now().astimezone().isoformat(timespec="seconds")),
            )
            self.conn.commit()
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception:
            return False

    def list_alerts(self, date: str | None = None, limit: int = 50,
                    unread_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM alerts"
        params: list = []
        where = []
        if date:
            where.append("date = ?")
            params.append(date)
        if unread_only:
            where.append("is_read = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def mark_alert_read(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET is_read = 1 WHERE id = ?", (alert_id,))
        self.conn.commit()

    # -- data transfers ---------------------------------------------------
    def log_transfer(self, date: str, transfer_type: str, channel: str = "",
                     file_name: str = "", file_path: str = "",
                     size_bytes: int | None = None, destination: str = "",
                     app: str = "", occurred_at: str | None = None,
                     details: str = "") -> bool:
        """Insert a transfer row; False when it already exists (deduped)."""
        from datetime import datetime as _dt

        when = occurred_at or _dt.now().astimezone().isoformat(timespec="seconds")
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO transfers
                   (date, transfer_type, channel, file_name, file_path,
                    size_bytes, destination, app, occurred_at, details)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, transfer_type, channel, file_name, file_path,
                 size_bytes, destination, app, when, details),
            )
            self.conn.commit()
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception:
            return False

    def has_transfer(self, date: str, transfer_type: str, file_path: str) -> bool:
        """True when this exact file (full path) already has a row — the
        same file NAME in another folder is a different file."""
        row = self.conn.execute(
            """SELECT 1 FROM transfers WHERE date = ? AND transfer_type = ?
               AND file_path = ? LIMIT 1""",
            (date, transfer_type, file_path)).fetchone()
        return row is not None

    def transfers_for_date(self, date: str, limit: int | None = 100) -> list[sqlite3.Row]:
        """Rows for one day, newest first. limit=None returns all rows —
        exports and alert checks must not silently drop rows on busy days."""
        if limit is None:
            return self.conn.execute(
                """SELECT * FROM transfers WHERE date = ?
                   ORDER BY occurred_at DESC""",
                (date,)).fetchall()
        return self.conn.execute(
            """SELECT * FROM transfers WHERE date = ?
               ORDER BY occurred_at DESC LIMIT ?""",
            (date, limit)).fetchall()

    def transfer_counts(self, date: str) -> dict[str, dict]:
        """{type: {"count": n, "bytes": total_size}} for one day."""
        rows = self.conn.execute(
            """SELECT transfer_type, COUNT(*) AS n,
                      COALESCE(SUM(size_bytes), 0) AS b
               FROM transfers WHERE date = ? GROUP BY transfer_type""",
            (date,)).fetchall()
        return {r["transfer_type"]: {"count": int(r["n"] or 0),
                                     "bytes": int(r["b"] or 0)} for r in rows}

    def transfer_count_between(self, start: str, end: str) -> dict[str, dict]:
        rows = self.conn.execute(
            """SELECT transfer_type, COUNT(*) AS n,
                      COALESCE(SUM(size_bytes), 0) AS b
               FROM transfers WHERE date BETWEEN ? AND ? GROUP BY transfer_type""",
            (start, end)).fetchall()
        return {r["transfer_type"]: {"count": int(r["n"] or 0),
                                     "bytes": int(r["b"] or 0)} for r in rows}

    def delete_day(self, date: str) -> dict[str, int]:
        """Delete one day's recorded activity (used by Telegram /clear).
        Projects, overrides and kv_state are kept — only history goes.
        Returns {table: rows deleted}."""
        specs = [
            ("app_sessions", "substr(started_at, 1, 10) = ?"),
            ("browser_sessions", "substr(started_at, 1, 10) = ?"),
            ("browser_visits", "substr(visited_at, 1, 10) = ?"),
            ("browser_searches", "substr(searched_at, 1, 10) = ?"),
            ("browser_downloads", "substr(started_at, 1, 10) = ?"),
            ("file_events", "substr(occurred_at, 1, 10) = ?"),
            ("transfers", "date = ?"),
            ("alerts", "date = ?"),
        ]
        out: dict[str, int] = {}
        try:
            for table, where in specs:
                cur = self.conn.execute(f"DELETE FROM {table} WHERE {where}", (date,))
                out[table] = int(cur.rowcount or 0)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return out

    def reset_data(self) -> dict[str, int]:
        """Delete ALL recorded activity (used by Telegram /reset, after a
        backup). Projects, overrides and kv_state are kept: kv_state
        holds the Telegram update offset and browser watermarks, and
        wiping it would make the bot re-answer old commands and the
        browser tracker re-import history. Returns {table: rows}."""
        tables = ["app_sessions", "browser_sessions", "browser_visits",
                  "browser_searches", "browser_downloads", "file_events",
                  "transfers", "alerts"]
        out: dict[str, int] = {}
        try:
            for table in tables:
                cur = self.conn.execute(f"DELETE FROM {table}")
                out[table] = int(cur.rowcount or 0)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return out

    def search_today(self, date: str, query: str, limit: int = 10) -> dict:
        """Case-insensitive search of one day: file names, window titles
        and browser search queries containing `query`."""
        like = f"%{query}%"
        try:
            files = self.conn.execute(
                """SELECT event_type, path, occurred_at FROM file_events
                   WHERE substr(occurred_at, 1, 10) = ? AND path LIKE ?
                   ORDER BY occurred_at DESC LIMIT ?""",
                (date, like, limit),
            ).fetchall()
        except Exception:
            files = []
        try:
            titles = self.conn.execute(
                """SELECT app_name, window_title, started_at FROM app_sessions
                   WHERE substr(started_at, 1, 10) = ? AND window_title LIKE ?
                   ORDER BY started_at DESC LIMIT ?""",
                (date, like, limit),
            ).fetchall()
        except Exception:
            titles = []
        try:
            searches = self.conn.execute(
                """SELECT query, searched_at FROM browser_searches
                   WHERE substr(searched_at, 1, 10) = ? AND query LIKE ?
                   ORDER BY searched_at DESC LIMIT ?""",
                (date, like, limit),
            ).fetchall()
        except Exception:
            searches = []
        return {"files": files, "titles": titles, "searches": searches}

    def latest_browser_session(self, date: str) -> sqlite3.Row | None:
        """The most recently started browser session of one day
        (used by Telegram /browser for the live tab)."""
        try:
            return self.conn.execute(
                """SELECT browser, url, domain, title, started_at
                   FROM browser_sessions
                   WHERE substr(started_at, 1, 10) = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (date,),
            ).fetchone()
        except Exception:
            return None

    def recent_visits(self, date: str, limit: int = 10) -> list[sqlite3.Row]:
        """Latest history visits of one day, newest first
        (used by Telegram /browser)."""
        try:
            return self.conn.execute(
                """SELECT browser, url, domain, title, visited_at
                   FROM browser_visits
                   WHERE substr(visited_at, 1, 10) = ?
                   ORDER BY visited_at DESC LIMIT ?""",
                (date, limit),
            ).fetchall()
        except Exception:
            return []

    def close(self) -> None:
        self.conn.close()
