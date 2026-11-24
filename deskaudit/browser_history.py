"""Read browser history + downloads directly from the browser's own
SQLite files — no extension, no password, no network needed.

Why copy first? Chrome / Edge / Firefox lock their History / places.sqlite
while the browser is running. Copying to a temp file and opening the copy
read-only is the standard, safe way to read them.

Supported
----------
Chromium (Chrome, Edge, Brave, Opera, Vivaldi):
    <profile>/History  — urls, visits, downloads, download_url_chains
Firefox:
    <profile>/places.sqlite — moz_places, moz_historyvisits
    (downloads in modern Firefox live in the same places DB / library)

What is NOT read: passwords, cookies, form data, saved logins, extensions,
incognito / private history (browsers never write it to disk).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .browser_utils import (
    chrome_time_to_dt,
    domain_of,
    extract_search_query,
    firefox_time_to_dt,
    redact_url,
)


@dataclass
class HistoryVisit:
    browser: str
    url: str
    domain: str
    title: str
    visited_at: datetime
    visit_count: int = 1
    transition: str = ""
    search_query: str | None = None


@dataclass
class DownloadRecord:
    browser: str
    url: str
    domain: str
    file_path: str
    file_name: str
    total_bytes: int | None
    received_bytes: int | None
    state: str
    started_at: datetime | None


@dataclass
class BrowserProfile:
    browser: str
    engine: str  # chromium | firefox
    profile_name: str
    history_path: Path


# ---------------------------------------------------------------------------
# Profile discovery (Windows paths; env vars make it testable)
# ---------------------------------------------------------------------------

def _localappdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))


def _appdata() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))


CHROMIUM_LAYOUT: list[tuple[str, str, str]] = [
    # (browser, engine, relative path under LOCALAPPDATA)
    ("Chrome", "chromium", r"Google\Chrome\User Data"),
    ("Edge", "chromium", r"Microsoft\Edge\User Data"),
    ("Brave", "chromium", r"BraveSoftware\Brave-Browser\User Data"),
    ("Opera", "chromium", r"Opera Software\Opera Stable"),
    ("Opera GX", "chromium", r"Opera Software\Opera GX Stable"),
    ("Vivaldi", "chromium", r"Vivaldi\User Data"),
]


def discover_profiles() -> list[BrowserProfile]:
    """Find every installed browser profile that has a History file."""
    profiles: list[BrowserProfile] = []
    local = _localappdata()
    roaming = _appdata()

    for browser, engine, rel in CHROMIUM_LAYOUT:
        # Split on "\" so the same code works on Windows and on Linux/macOS
        # (where "\" is not a path separator) for testing.
        base = local.joinpath(*rel.split("\\"))
        # Opera stores History directly under its folder, not under a profile.
        if browser.startswith("Opera"):
            hist = base / "History"
            if hist.exists():
                profiles.append(BrowserProfile(browser, engine, "Default", hist))
            continue
        if not base.exists():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if child.name not in ("Default",) and not child.name.startswith("Profile "):
                continue
            hist = child / "History"
            if hist.exists():
                profiles.append(BrowserProfile(browser, engine, child.name, hist))

    # Firefox: %APPDATA%\Mozilla\Firefox\Profiles\*.default*
    ff_base = roaming / "Mozilla" / "Firefox" / "Profiles"
    if ff_base.exists():
        for child in sorted(ff_base.iterdir()):
            if not child.is_dir():
                continue
            places = child / "places.sqlite"
            if places.exists():
                profiles.append(BrowserProfile("Firefox", "firefox", child.name, places))
    return profiles


# ---------------------------------------------------------------------------
# Locked-DB copy + read-only open
# ---------------------------------------------------------------------------

def copy_locked_db(src: Path) -> Path:
    """Copy a (possibly locked) DB to a temp file. Returns the temp path."""
    tmp = Path(tempfile.gettempdir()) / f"deskaudit-{src.stem}-{os.getpid()}.db"
    shutil.copy2(src, tmp)
    # Also copy -wal / -shm if present, so the copy is not stale.
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            try:
                shutil.copy2(side, Path(str(tmp) + suffix))
            except OSError:
                pass
    return tmp


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Chromium
# ---------------------------------------------------------------------------

def read_chromium_history(history_path: Path, browser: str,
                          since: datetime | None = None) -> list[HistoryVisit]:
    if not history_path.exists():
        return []
    tmp = copy_locked_db(history_path)
    visits: list[HistoryVisit] = []
    try:
        conn = _open_ro(tmp)
        try:
            if not _table_exists(conn, "urls") or not _table_exists(conn, "visits"):
                return []
            # visit_time is Chrome time (µs since 1601). Filter in Python
            # after converting, so a bad row never breaks the whole read.
            rows = conn.execute(
                """SELECT u.url AS url, u.title AS title, u.visit_count AS visit_count,
                          v.visit_time AS visit_time, v.transition AS transition
                   FROM visits v JOIN urls u ON v.url = u.id
                   ORDER BY v.visit_time ASC"""
            ).fetchall()
            for row in rows:
                visited = chrome_time_to_dt(row["visit_time"])
                if visited is None:
                    continue
                if since is not None and visited <= since:
                    continue
                url = redact_url(row["url"] or "")
                if not url:
                    continue
                visits.append(HistoryVisit(
                    browser=browser, url=url, domain=domain_of(url),
                    title=(row["title"] or "")[:200],
                    visited_at=visited.astimezone(),
                    visit_count=int(row["visit_count"] or 1),
                    transition=str(row["transition"] or ""),
                    search_query=extract_search_query(url),
                ))
        finally:
            conn.close()
    except Exception:
        return visits
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return visits


def read_chromium_downloads(history_path: Path, browser: str,
                            since: datetime | None = None) -> list[DownloadRecord]:
    if not history_path.exists():
        return []
    tmp = copy_locked_db(history_path)
    out: list[DownloadRecord] = []
    try:
        conn = _open_ro(tmp)
        try:
            if not _table_exists(conn, "downloads"):
                return []
            rows = conn.execute(
                """SELECT d.current_path AS path, d.target_path AS target,
                          d.start_time AS start_time, d.received_bytes AS received,
                          d.total_bytes AS total, d.state AS state,
                          c.url AS url
                   FROM downloads d
                   LEFT JOIN download_url_chains c
                     ON c.id = d.id AND c.chain_index = 0
                   ORDER BY d.start_time ASC"""
            ).fetchall()
            for row in rows:
                started = chrome_time_to_dt(row["start_time"])
                if started is not None and since is not None and started <= since:
                    continue
                path = row["target"] or row["path"] or ""
                url = redact_url(row["url"] or "")
                out.append(DownloadRecord(
                    browser=browser, url=url, domain=domain_of(url),
                    file_path=path, file_name=Path(path).name if path else "",
                    total_bytes=row["total"], received_bytes=row["received"],
                    state=_chromium_download_state(row["state"]),
                    started_at=started.astimezone() if started else None,
                ))
        finally:
            conn.close()
    except Exception:
        return out
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return out


def _chromium_download_state(state: int | None) -> str:
    return {0: "in_progress", 1: "complete", 2: "cancelled", 3: "interrupted"}.get(
        int(state) if state is not None else -1, "unknown")


# ---------------------------------------------------------------------------
# Firefox
# ---------------------------------------------------------------------------

def read_firefox_history(places_path: Path, browser: str = "Firefox",
                         since: datetime | None = None) -> list[HistoryVisit]:
    if not places_path.exists():
        return []
    tmp = copy_locked_db(places_path)
    visits: list[HistoryVisit] = []
    try:
        conn = _open_ro(tmp)
        try:
            if not _table_exists(conn, "moz_places") or not _table_exists(conn, "moz_historyvisits"):
                return []
            rows = conn.execute(
                """SELECT p.url AS url, p.title AS title, p.visit_count AS visit_count,
                          h.visit_date AS visit_date, h.visit_type AS visit_type
                   FROM moz_historyvisits h
                   JOIN moz_places p ON h.place_id = p.id
                   ORDER BY h.visit_date ASC"""
            ).fetchall()
            for row in rows:
                visited = firefox_time_to_dt(row["visit_date"])
                if visited is None:
                    continue
                if since is not None and visited <= since:
                    continue
                url = redact_url(row["url"] or "")
                if not url or url.startswith("about:") or url.startswith("moz-"):
                    continue
                visits.append(HistoryVisit(
                    browser=browser, url=url, domain=domain_of(url),
                    title=(row["title"] or "")[:200],
                    visited_at=visited.astimezone(),
                    visit_count=int(row["visit_count"] or 1),
                    transition=str(row["visit_type"] or ""),
                    search_query=extract_search_query(url),
                ))
        finally:
            conn.close()
    except Exception:
        return visits
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return visits


def read_firefox_bookmarks(places_path: Path) -> list[dict]:
    """Bookmarks only — title + URL, no history. Used by --browser-audit."""
    if not places_path.exists():
        return []
    tmp = copy_locked_db(places_path)
    try:
        conn = _open_ro(tmp)
        try:
            if not _table_exists(conn, "moz_bookmarks") or not _table_exists(conn, "moz_places"):
                return []
            rows = conn.execute(
                """SELECT p.url AS url, b.title AS title
                   FROM moz_bookmarks b JOIN moz_places p ON b.fk = p.id
                   WHERE p.url IS NOT NULL AND p.url NOT LIKE 'place:%'
                   ORDER BY b.title"""
            ).fetchall()
            return [{"title": r["title"] or "", "url": redact_url(r["url"] or "")}
                    for r in rows]
        finally:
            conn.close()
    except Exception:
        return []
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def read_chromium_bookmarks(user_data_dir: Path) -> list[dict]:
    """Chromium Bookmarks is JSON, not SQLite. Returns title + URL.

    Reads every profile folder (Default, Profile 1, ...) and a
    root-level Bookmarks file (Opera keeps it next to History)."""
    import json

    out: list[dict] = []

    def _read_file(path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "url" and node.get("url"):
                    out.append({"title": node.get("name", ""), "url": redact_url(node["url"])})
                for child in node.get("children", []):
                    walk(child)

        walk(data.get("roots", {}))

    # Opera-style: Bookmarks directly under the user data dir.
    _read_file(user_data_dir / "Bookmarks")
    if user_data_dir.is_dir():
        for child in sorted(user_data_dir.iterdir()):
            if child.is_dir() and (child.name == "Default"
                                   or child.name.startswith("Profile ")):
                _read_file(child / "Bookmarks")
    return out


def read_profile_bookmarks(profile: BrowserProfile) -> list[dict]:
    """Bookmarks for one discovered profile, whatever its engine."""
    if profile.engine == "firefox":
        return read_firefox_bookmarks(profile.history_path)
    # Chromium: History is <user data>/<profile>/History, except Opera
    # where it is <user data>/History directly.
    user_data = (profile.history_path.parent if profile.browser.startswith("Opera")
                 else profile.history_path.parent.parent)
    return read_chromium_bookmarks(user_data)


# ---------------------------------------------------------------------------
# One entry point used by the tracker
# ---------------------------------------------------------------------------

def read_profile_history(profile: BrowserProfile,
                         since: datetime | None = None) -> list[HistoryVisit]:
    if profile.engine == "firefox":
        return read_firefox_history(profile.history_path, profile.browser, since)
    return read_chromium_history(profile.history_path, profile.browser, since)


def read_profile_downloads(profile: BrowserProfile,
                           since: datetime | None = None) -> list[DownloadRecord]:
    if profile.engine == "firefox":
        return []  # Firefox downloads are not in places.sqlite in a stable form
    return read_chromium_downloads(profile.history_path, profile.browser, since)
