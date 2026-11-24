"""Browser helpers: domains, categories, search queries, URL redaction,
Chrome / Firefox timestamp conversion, browser identification.

No network, no passwords, no form data. Only metadata from a URL.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Browser identification
# ---------------------------------------------------------------------------

# exe name (lowercase) -> (display name, engine)
BROWSERS: dict[str, tuple[str, str]] = {
    "chrome.exe": ("Chrome", "chromium"),
    "msedge.exe": ("Edge", "chromium"),
    "brave.exe": ("Brave", "chromium"),
    "opera.exe": ("Opera", "chromium"),
    "vivaldi.exe": ("Vivaldi", "chromium"),
    "chromium.exe": ("Chromium", "chromium"),
    "firefox.exe": ("Firefox", "firefox"),
    # Linux / macOS process names, so the same code can be smoke-tested.
    "chrome": ("Chrome", "chromium"),
    "chromium": ("Chromium", "chromium"),
    "firefox": ("Firefox", "firefox"),
}

# Suffixes Windows puts at the end of a browser window title.
TITLE_SUFFIXES = [
    " - Google Chrome",
    " - Microsoft Edge",
    " - Brave",
    " - Mozilla Firefox",
    " - Opera",
    " - Vivaldi",
    " - Chromium",
]

CHROME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def browser_for_exe(exe_name: str) -> tuple[str, str] | None:
    return BROWSERS.get((exe_name or "").lower())


def is_browser_exe(exe_name: str) -> bool:
    return (exe_name or "").lower() in BROWSERS


def clean_window_title(title: str) -> str:
    """'Stack Overflow - Google Chrome' -> 'Stack Overflow'."""
    title = (title or "").strip()
    for suffix in TITLE_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    # "Page Title - Site Name" is common; keep the whole title, the
    # report shows it as-is. Only the browser suffix is removed.
    return title


def domain_of(url: str) -> str:
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def base_domain(domain: str) -> str:
    """mail.google.com -> google.com (for category lookup)."""
    parts = (domain or "").split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain or ""


# ---------------------------------------------------------------------------
# Categories — broad level: what kind of site is this, productive or not?
# ---------------------------------------------------------------------------

# domain substring -> (category, is_productive)
CATEGORY_MAP: dict[str, tuple[str, bool]] = {
    # work / coding / docs
    "github.com": ("coding", True),
    "gitlab.com": ("coding", True),
    "stackoverflow.com": ("coding", True),
    "stackexchange.com": ("coding", True),
    "docs.google.com": ("work", True),
    "drive.google.com": ("work", True),
    "sheets.google.com": ("work", True),
    "notion.so": ("work", True),
    "notion.site": ("work", True),
    "trello.com": ("work", True),
    "asana.com": ("work", True),
    "slack.com": ("work", True),
    "teams.microsoft.com": ("work", True),
    "zoom.us": ("work", True),
    "meet.google.com": ("work", True),
    "outlook.": ("email", True),
    "mail.google.com": ("email", True),
    "gmail.com": ("email", True),
    "calendar.google.com": ("work", True),
    "figma.com": ("work", True),
    "canva.com": ("work", True),
    "chatgpt.com": ("ai", True),
    "claude.ai": ("ai", True),
    "openai.com": ("ai", True),
    # learning
    "coursera.org": ("learning", True),
    "udemy.com": ("learning", True),
    "khanacademy.org": ("learning", True),
    "w3schools.com": ("learning", True),
    "geeksforgeeks.org": ("learning", True),
    "wikipedia.org": ("learning", True),
    "medium.com": ("learning", True),
    # search
    "google.com": ("search", True),
    "bing.com": ("search", True),
    "duckduckgo.com": ("search", True),
    "search.yahoo.com": ("search", True),
    "search.brave.com": ("search", True),
    # shopping
    "amazon.": ("shopping", False),
    "daraz.com": ("shopping", False),
    "aliexpress.com": ("shopping", False),
    "ebay.com": ("shopping", False),
    "shopify.com": ("shopping", False),
    # social / video / entertainment — distracting by default
    "facebook.com": ("social", False),
    "instagram.com": ("social", False),
    "x.com": ("social", False),
    "twitter.com": ("social", False),
    "linkedin.com": ("social", True),
    "reddit.com": ("social", False),
    "tiktok.com": ("social", False),
    "youtube.com": ("video", False),
    "youtu.be": ("video", False),
    "netflix.com": ("video", False),
    "twitch.tv": ("video", False),
    "spotify.com": ("music", False),
    # news
    "bbc.com": ("news", False),
    "prothomalo.com": ("news", False),
    "bdnews24.com": ("news", False),
    "cnn.com": ("news", False),
}


def _domain_matches(key: str, domain: str) -> bool:
    """Label-boundary domain match. A key ending in "." is a family
    prefix ("outlook." matches outlook.live.com and mail.outlook.com);
    any other key is a full domain ("box.com" must not match
    "mybox.com", "github.com" must match "gist.github.com")."""
    key = (key or "").lower()
    d = (domain or "").lower()
    if not key or not d:
        return False
    if key.endswith("."):
        return d.startswith(key) or ("." + key) in d
    return d == key or d.endswith("." + key)


def classify_domain(domain: str, productive_domains=None, distracting_domains=None) -> tuple[str, bool]:
    """Return (category, is_productive). User config overrides built-ins."""
    d = (domain or "").lower()
    if productive_domains:
        for item in productive_domains:
            if _domain_matches(item, d):
                return ("work", True)
    if distracting_domains:
        for item in distracting_domains:
            if _domain_matches(item, d):
                return ("distraction", False)
    for key, value in CATEGORY_MAP.items():
        if _domain_matches(key, d):
            return value
    if d.endswith(".gov") or d.endswith(".edu") or d.endswith(".ac.bd"):
        return ("learning", True)
    if not d:
        return ("unknown", False)
    return ("other", False)


# ---------------------------------------------------------------------------
# Search queries
# ---------------------------------------------------------------------------

SEARCH_PARAMS: dict[str, str] = {
    "google.": "q",
    "bing.com": "q",
    "duckduckgo.com": "q",
    "search.yahoo.com": "p",
    "yahoo.com": "p",
    "search.brave.com": "q",
    "brave.com": "q",
    "ecosia.org": "q",
    "youtube.com": "search_query",
    "baidu.com": "wd",
    "yandex.": "text",
    "ask.com": "q",
}


def extract_search_query(url: str) -> str | None:
    """'https://www.google.com/search?q=python+sqlite' -> 'python sqlite'."""
    try:
        parsed = urlparse(url or "")
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    param = None
    for key, value in SEARCH_PARAMS.items():
        if key in host:
            param = value
            break
    if not param:
        return None
    qs = parse_qs(parsed.query)
    values = qs.get(param)
    if not values:
        return None
    query = values[0].strip()
    return query[:200] if query else None


# ---------------------------------------------------------------------------
# URL redaction — never store tokens / passwords from a URL
# ---------------------------------------------------------------------------

SENSITIVE_PARAMS = {
    "password", "passwd", "pwd", "token", "access_token", "refresh_token",
    "auth", "api_key", "apikey", "secret", "code", "session", "sessionid",
    "credit", "card", "cvv", "otp",
}


def redact_url(url: str) -> str:
    """Drop sensitive query params and the fragment; cap length at 500."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        kept = {k: v for k, v in qs.items() if k.lower() not in SENSITIVE_PARAMS}
        from urllib.parse import urlencode

        query = urlencode(kept, doseq=True)
        clean = parsed._replace(query=query, fragment="").geturl()
    except Exception:
        clean = url.split("#")[0]
    return clean[:500]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def chrome_time_to_dt(microseconds: int | None) -> datetime | None:
    """Chrome/Edge/Brave: microseconds since 1601-01-01 UTC."""
    if not microseconds:
        return None
    try:
        return CHROME_EPOCH + timedelta(microseconds=int(microseconds))
    except Exception:
        return None


def dt_to_chrome_time(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - CHROME_EPOCH
    return int(delta.total_seconds() * 1_000_000)


def firefox_time_to_dt(microseconds: int | None) -> datetime | None:
    """Firefox: microseconds since Unix epoch UTC."""
    if not microseconds:
        return None
    try:
        return datetime.fromtimestamp(int(microseconds) / 1_000_000, tz=timezone.utc)
    except Exception:
        return None


def dt_to_firefox_time(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000)


def is_probably_url(text: str) -> bool:
    return bool(re.match(r"^https?://", (text or "").strip(), re.IGNORECASE))
