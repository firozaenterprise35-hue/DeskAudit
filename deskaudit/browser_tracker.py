"""Active browser tracking — the live layer on top of history polling.

Three sources, in order of accuracy, all optional and all local:
1. Extension receiver (exact URL + title in real time) — see
   extension/chrome/ and deskaudit/browser_receiver.py. Best accuracy.
2. Chrome DevTools Protocol (CDP) — if the browser was started with
   --remote-debugging-port=9222, http://127.0.0.1:9222/json/list gives
   the exact active tab. No extension needed.
3. History polling + window title — always works, no setup. The tracker
   polls History / places.sqlite every browser_poll_seconds, matches the
   foreground window title to the most recent visit with the same title,
   and falls back to "title only" when no URL can be matched yet.

What is tracked per browser session (one row per browser + domain + title):
    started / ended, duration, active vs idle seconds, tab switches.
What is tracked per visit (from history): browser, URL, domain, title,
    visited_at, search query (if any), category, productive or not.
What is tracked per download: browser, URL, file name, size, state.
What is NOT tracked: incognito history, passwords, cookies, form data,
    clipboard, keystrokes, or the body of any page.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path

from .browser_history import (
    discover_profiles,
    read_profile_bookmarks,
    read_profile_downloads,
    read_profile_history,
)
from .browser_utils import (
    _domain_matches,
    browser_for_exe,
    classify_domain,
    clean_window_title,
    domain_of,
    extract_search_query,
    is_browser_exe,
)
from .database import Database

# Same tab re-posted by the extension within this window counts as
# one visit (v12.13 audit, M3). The extension posts every 60 s.
_EXT_VISIT_DEDUP_SECONDS = 300


class BrowserTracker:
    def __init__(self, db: Database, config, profiles=None, tagger=None):
        self.db = db
        self.config = config
        self.tagger = tagger
        # profiles=None -> auto-discover on Windows; tests inject their own.
        self._profiles = profiles
        self._last_history_poll: datetime | None = None
        self._poll_count = 0
        # Active browser session state.
        self._session_id: int | None = None
        self._key: tuple | None = None
        self._started_at: datetime | None = None
        self._active = 0.0
        self._idle = 0.0
        self._tab_switches = 0
        # Last tab reported by the extension (browser, url, title, at).
        # tick() uses it for the exact URL — the receiver itself must NOT
        # open a session, or every extension report creates a spurious
        # ~0 s session row even when Chrome is not the foreground app.
        self._extension_tab: dict | None = None
        # (browser, url) -> last time the extension re-post was logged
        # as a visit, so the 60 s re-posts do not flood browser_visits
        # (v12.13 audit, M3).
        self._last_ext_visit: dict[tuple, datetime] = {}

    def _track_searches(self) -> bool:
        """Whether search queries are recorded (config flag, v12.13
        audit M2: the Dashboard toggle did nothing)."""
        return bool(getattr(self.config, "track_searches", True))

    # -- history ------------------------------------------------------
    @property
    def profiles(self):
        if self._profiles is None:
            self._profiles = discover_profiles()
        return self._profiles

    def _last_seen(self, source: str) -> datetime | None:
        raw = self.db.get_state(f"browser_last_seen:{source}")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def poll_history(self, now: datetime | None = None) -> dict:
        """Read new history + downloads from every profile. Returns counts."""
        now = now or datetime.now().astimezone()
        counts = {"visits": 0, "downloads": 0, "searches": 0}
        if not getattr(self.config, "browser_enabled", True):
            return counts
        for profile in self.profiles:
            source = f"{profile.browser}:{profile.profile_name}"
            since = self._last_seen(source)
            try:
                visits = read_profile_history(profile, since)
            except Exception:
                continue
            newest: datetime | None = since
            for visit in visits:
                if self._is_excluded_domain(visit.domain):
                    continue
                project, tags = self._tag(visit.url, visit.domain, visit.title,
                                          visit.search_query or "")
                added = self.db.log_browser_visit(
                    visit.browser, visit.url, visit.domain, visit.title,
                    visit.visited_at, visit.search_query,
                    project=project, tags=tags,
                )
                if added:
                    counts["visits"] += 1
                    if visit.search_query and self._track_searches():
                        counts["searches"] += 1
                        self.db.log_browser_search(
                            visit.browser, visit.search_query, visit.url,
                            visit.domain, visit.visited_at,
                            project=project, tags=tags,
                        )
                if newest is None or visit.visited_at > newest:
                    newest = visit.visited_at
            if getattr(self.config, "track_downloads", True):
                try:
                    downloads = read_profile_downloads(profile, since)
                except Exception:
                    downloads = []
                for dl in downloads:
                    project, tags = (self.tagger.tag_download(dl.url, dl.domain, dl.file_path)
                                     if self.tagger is not None else ("Untagged", []))
                    if self.db.log_browser_download(
                        dl.browser, dl.url, dl.domain, dl.file_path,
                        dl.file_name, dl.total_bytes, dl.received_bytes,
                        dl.state, dl.started_at,
                        project=project, tags=tags,
                    ):
                        counts["downloads"] += 1
            if newest is not None:
                self.db.set_state(f"browser_last_seen:{source}", newest.isoformat())
        self._last_history_poll = now
        self._poll_count += 1
        return counts

    def _is_excluded_domain(self, domain: str) -> bool:
        # v12.13 (C-m11): was a raw substring check — "mail" excluded
        # gmail.com. Use label-boundary matching instead.
        excluded = getattr(self.config, "exclude_domains", set()) or set()
        return any(_domain_matches(item, domain) for item in excluded)

    def _tag(self, url: str = "", domain: str = "", title: str = "",
             search_query: str = "") -> tuple[str, list[str]]:
        if self.tagger is None:
            return "Untagged", []
        return self.tagger.tag_browser(url=url, domain=domain, title=title,
                                       search_query=search_query)

    # -- exact URL, when available -------------------------------------
    def cdp_active_tab(self) -> dict | None:
        """Ask CDP (port from config) for the active tab. Returns None if
        the browser was not started with --remote-debugging-port.

        Known limit (v12.13 audit): /json/list does not say which tab is
        the active one, so the FIRST http(s) page tab is returned — the
        caller cross-checks it against the foreground window title,
        which is right except when the window title is empty. The
        extension (exact active tab) and history polling stay the
        accurate paths."""
        port = getattr(self.config, "browser_cdp_port", 0) or 0
        if not port:
            return None
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=0.5
            ) as resp:
                tabs = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception:
            return None
        for tab in tabs:
            if tab.get("type") != "page":
                continue
            url = tab.get("url", "")
            if url.startswith(("http://", "https://")):
                return {"url": url, "title": tab.get("title", ""), "domain": domain_of(url)}
        return None

    def record_extension_tab(self, browser: str, url: str, title: str,
                             at: datetime | None = None) -> None:
        """Called by browser_receiver.py when the extension reports a tab.
        Treated as a visit (deduped); the exact URL is kept for tick()
        to use when this browser IS the foreground app.

        The extension re-posts the active tab every 60 s, so the same
        (browser, url) re-posted within 5 minutes is the SAME visit
        (v12.13 audit, M3): the old code logged a new visit row per
        post, so an 8-hour open Google tab made ~480 duplicate visit
        + search rows."""
        at = at or datetime.now().astimezone()
        from .browser_utils import redact_url

        url = redact_url(url)
        domain = domain_of(url)
        if self._is_excluded_domain(domain):
            return
        query = extract_search_query(url)
        project, tags = self._tag(url, domain, title, query or "")
        key = (browser, url)
        last = self._last_ext_visit.get(key)
        # Prune stale entries so the cache stays small.
        if self._last_ext_visit:
            self._last_ext_visit = {
                k: v for k, v in self._last_ext_visit.items()
                if (at - v).total_seconds() < _EXT_VISIT_DEDUP_SECONDS}
            last = self._last_ext_visit.get(key)
        if last is None or (at - last).total_seconds() >= _EXT_VISIT_DEDUP_SECONDS:
            self._last_ext_visit[key] = at
            if self.db.log_browser_visit(browser, url, domain, title[:200], at, query,
                                         project=project, tags=tags):
                if query and self._track_searches():
                    self.db.log_browser_search(browser, query, url, domain, at,
                                               project=project, tags=tags)
        self._extension_tab = {"browser": browser, "url": url,
                               "title": title, "at": at}

    # -- active session (foreground window) ----------------------------
    def _session_from_url(self, browser: str, url: str, title: str, now: datetime) -> None:
        domain = domain_of(url)
        category, productive = classify_domain(
            domain,
            getattr(self.config, "productive_domains", set()),
            getattr(self.config, "distracting_domains", set()),
        )
        key = (browser, domain, clean_window_title(title))
        if key != self._key:
            if self._session_id is not None:
                self._tab_switches += 1  # leaving this tab for another one
            self._close_session(now)
            project, tags = self._tag(url, domain, title)
            self._session_id = self.db.start_browser_session(
                browser, url, domain, clean_window_title(title),
                category, productive, now,
                project=project, tags=tags,
            )
            self._key = key
            self._started_at = now

    def tick(self, window, idle: bool, poll_interval: float = 5.0) -> dict | None:
        """One poll. `window` is an ActiveWindow from app_tracker.
        Returns the current browser info, or None when not in a browser."""
        now = datetime.now().astimezone()
        exe = getattr(window, "app_name", "") or ""
        if not is_browser_exe(exe):
            self._close_session(now)
            return None
        browser, _engine = browser_for_exe(exe) or ("Browser", "chromium")
        title = clean_window_title(getattr(window, "title", "") or "")

        # 0) Exact URL from the extension, when it is for THIS browser
        # and fresh (reported within the last 2 minutes).
        url = ""
        ext = self._extension_tab
        if ext and ext.get("browser") == browser:
            try:
                fresh = (now - ext["at"]).total_seconds() < 120
            except Exception:
                fresh = False
            if fresh:
                url = ext.get("url") or ""
        # 1) Exact URL if CDP is available.
        if not url:
            cdp = self.cdp_active_tab()
            if cdp and (not title or cdp.get("title", "")[:60] in title or title[:60] in cdp.get("title", "")):
                url = cdp["url"]
        # 2) Otherwise match the title to the most recent visit.
        if not url:
            url = self.db.latest_url_for_title(browser, title)
        if url:
            self._session_from_url(browser, url, title, now)
        else:
            # Title only: domain unknown yet, history poll will fill it in.
            key = (browser, "", title)
            if key != self._key:
                if self._session_id is not None:
                    self._tab_switches += 1
                self._close_session(now)
                category, productive = classify_domain("")
                project, tags = self._tag("", "", title)
                self._session_id = self.db.start_browser_session(
                    browser, "", "", title, category, productive, now,
                    project=project, tags=tags,
                )
                self._key = key
                self._started_at = now

        if idle:
            self._idle += poll_interval
        else:
            self._active += poll_interval
        self.flush(now)
        return {"browser": browser, "title": title, "url": url,
                "domain": domain_of(url) if url else ""}

    def flush(self, now: datetime | None = None) -> None:
        """Write the open browser session's durations into the DB without
        closing it (same reason as AppTracker.flush: a force-stop must
        not lose the whole current session)."""
        if self._session_id is None or self._started_at is None:
            return
        now = now or datetime.now().astimezone()
        duration = (now - self._started_at).total_seconds()
        try:
            self.db.update_browser_session(
                self._session_id, now, duration, self._active, self._idle,
                self._tab_switches)
        except Exception:
            pass

    def _close_session(self, now: datetime) -> None:
        if self._session_id is None or self._started_at is None:
            return
        duration = (now - self._started_at).total_seconds()
        self.db.close_browser_session(
            self._session_id, now, duration, self._active, self._idle,
            self._tab_switches,
        )
        self._session_id = None
        self._key = None
        self._started_at = None
        self._active = 0.0
        self._idle = 0.0
        self._tab_switches = 0

    def stop(self) -> None:
        self._close_session(datetime.now().astimezone())

    # -- one-off audit --------------------------------------------------
    def audit_now(self) -> dict:
        """Import all history (no `since` filter) — for --browser-audit."""
        counts = {"visits": 0, "downloads": 0, "searches": 0,
                  "profiles": 0, "bookmarks": 0}
        for profile in self.profiles:
            counts["profiles"] += 1
            try:
                visits = read_profile_history(profile, None)
            except Exception:
                visits = []
            for visit in visits:
                if self._is_excluded_domain(visit.domain):
                    continue
                project, tags = self._tag(visit.url, visit.domain, visit.title,
                                          visit.search_query or "")
                if self.db.log_browser_visit(
                    visit.browser, visit.url, visit.domain, visit.title,
                    visit.visited_at, visit.search_query,
                    project=project, tags=tags,
                ):
                    counts["visits"] += 1
                    if visit.search_query and self._track_searches():
                        counts["searches"] += 1
                        self.db.log_browser_search(
                            visit.browser, visit.search_query, visit.url,
                            visit.domain, visit.visited_at,
                            project=project, tags=tags,
                        )
                self.db.set_state(
                    f"browser_last_seen:{profile.browser}:{profile.profile_name}",
                    visit.visited_at.isoformat(),
                )
            if getattr(self.config, "track_downloads", True):
                try:
                    downloads = read_profile_downloads(profile, None)
                except Exception:
                    downloads = []
                for dl in downloads:
                    project, tags = (self.tagger.tag_download(dl.url, dl.domain, dl.file_path)
                                     if self.tagger is not None else ("Untagged", []))
                    if self.db.log_browser_download(
                        dl.browser, dl.url, dl.domain, dl.file_path, dl.file_name,
                        dl.total_bytes, dl.received_bytes, dl.state, dl.started_at,
                        project=project, tags=tags,
                    ):
                        counts["downloads"] += 1
            try:
                counts["bookmarks"] += len(read_profile_bookmarks(profile))
            except Exception:
                pass
        return counts
