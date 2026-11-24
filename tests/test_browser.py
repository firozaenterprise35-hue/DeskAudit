"""Browser tracking tests — synthetic Chrome + Firefox DBs, no real browser.

Run:  python -m pytest tests/test_browser.py -v
      python tests/test_browser.py
"""

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.browser_history import (
    BrowserProfile,
    read_chromium_downloads,
    read_chromium_history,
    read_firefox_history,
)
from deskaudit.browser_tracker import BrowserTracker
from deskaudit.browser_utils import (
    chrome_time_to_dt,
    classify_domain,
    clean_window_title,
    dt_to_chrome_time,
    dt_to_firefox_time,
    domain_of,
    extract_search_query,
    redact_url,
)
from deskaudit.config import Config
from deskaudit.database import Database
from deskaudit.report import build_summary


def make_chrome_db(path: Path, when: datetime):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, last_visit_time INTEGER);
        CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, transition INTEGER);
        CREATE TABLE downloads (id INTEGER PRIMARY KEY, current_path TEXT, target_path TEXT, start_time INTEGER, received_bytes INTEGER, total_bytes INTEGER, state INTEGER);
        CREATE TABLE download_url_chains (id INTEGER, chain_index INTEGER, url TEXT);
    """)
    ct = dt_to_chrome_time(when)
    conn.execute("INSERT INTO urls VALUES (1, 'https://www.google.com/search?q=python+sqlite', 'python sqlite - Google Search', 3, ?)", (ct,))
    conn.execute("INSERT INTO urls VALUES (2, 'https://github.com/python/cpython', 'python/cpython', 1, ?)", (ct + 60_000_000,))
    conn.execute("INSERT INTO visits VALUES (1, 1, ?, 0)", (ct,))
    conn.execute("INSERT INTO visits VALUES (2, 2, ?, 0)", (ct + 60_000_000,))
    conn.execute("INSERT INTO downloads VALUES (1, 'C:/Users/F/Downloads/report.pdf', 'C:/Users/F/Downloads/report.pdf', ?, 1234, 1234, 1)", (ct,))
    conn.execute("INSERT INTO download_url_chains VALUES (1, 0, 'https://example.com/report.pdf')")
    conn.commit()
    conn.close()


def make_firefox_db(path: Path, when: datetime):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER, last_visit_date INTEGER);
        CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, place_id INTEGER, visit_date INTEGER, visit_type INTEGER);
    """)
    ft = dt_to_firefox_time(when)
    conn.execute("INSERT INTO moz_places VALUES (1, 'https://www.youtube.com/watch?v=abc', 'Some Video - YouTube', 2, ?)", (ft,))
    conn.execute("INSERT INTO moz_historyvisits VALUES (1, 1, ?, 1)", (ft,))
    conn.commit()
    conn.close()


def test_utils():
    assert domain_of("https://mail.google.com/mail/u/0/") == "mail.google.com"
    assert clean_window_title("Stack Overflow - Google Chrome") == "Stack Overflow"
    assert extract_search_query("https://www.google.com/search?q=python+sqlite") == "python sqlite"
    assert extract_search_query("https://www.youtube.com/results?search_query=lofi") == "lofi"
    assert extract_search_query("https://github.com/python/cpython") is None
    assert "token=secret" not in redact_url("https://example.com/?q=x&token=secret")
    assert classify_domain("github.com") == ("coding", True)
    assert classify_domain("youtube.com") == ("video", False)
    assert classify_domain("facebook.com") == ("social", False)
    when = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    assert chrome_time_to_dt(dt_to_chrome_time(when)) == when


def test_chrome_and_firefox_history():
    tmp = Path(tempfile.mkdtemp())
    when = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
    chrome = tmp / "History"
    firefox = tmp / "places.sqlite"
    make_chrome_db(chrome, when)
    make_firefox_db(firefox, when)

    visits = read_chromium_history(chrome, "Chrome")
    assert len(visits) == 2
    assert visits[0].search_query == "python sqlite"
    assert visits[1].domain == "github.com"

    downloads = read_chromium_downloads(chrome, "Chrome")
    assert len(downloads) == 1
    assert downloads[0].file_name == "report.pdf"
    assert downloads[0].state == "complete"

    ff = read_firefox_history(firefox, "Firefox")
    assert len(ff) == 1
    assert ff[0].domain == "youtube.com"

    # `since` filter + dedup through the tracker
    db = Database(tmp / "test.db")
    config = Config()
    # Use a plain Config with test profiles injected.
    profiles = [
        BrowserProfile("Chrome", "chromium", "Default", chrome),
        BrowserProfile("Firefox", "firefox", "default", firefox),
    ]
    tracker = BrowserTracker(db, config, profiles=profiles)
    counts = tracker.audit_now()
    assert counts["visits"] == 3, counts
    assert counts["searches"] == 1, counts
    assert counts["downloads"] == 1, counts
    # Second audit must not duplicate visits/downloads.
    counts2 = tracker.audit_now()
    assert counts2["visits"] == 0, counts2

    date = "2026-11-23"
    summary = build_summary(db, date)
    assert summary["browser"]["visits"] == 3
    assert summary["browser"]["top_domains"]
    assert summary["browser"]["searches"][0]["query"] == "python sqlite"
    db.close()
    print("browser tests passed")


if __name__ == "__main__":
    test_utils()
    test_chrome_and_firefox_history()
