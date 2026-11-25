"""Telegram bot — outbound status, digests and critical-alert notices.

The bot sends Bengali summaries to ONE chat (the owner's). It never
receives or stores message content from anyone else, and DeskAudit
stays metadata-only: the status reports WHICH app/window/file is
active (window titles and file names already logged locally) — never
what anyone typed, never message or email text, never passwords.

Setup (owner does this once, in Telegram):
  1. Talk to @BotFather -> /newbot -> copy the bot token.
  2. Send /start to the new bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and
     read chat.id (or ask @userinfobot for your own id).
  3. Put the token + chat id in Dashboard Settings -> Telegram
     (they are stored in config.local.yaml only, never in git), or
     pass them to the CLI test commands.
The agent polls getUpdates for /start, /status, /today and /help and
answers in the same chat. Everything here is stdlib-only (urllib).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

_log = logging.getLogger("deskaudit.telegram")

DEFAULT_TELEGRAM = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "poll_seconds": 30,
    "digest_minutes": 60,
    "notify_critical": True,
}

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_TEXT = 4000  # Telegram caps a message at 4096 chars


def _post(token: str, method: str, payload: dict, timeout: int = 15) -> dict | None:
    """POST one Bot API call; returns the decoded JSON or None on any
    failure (network, HTTP error, bad JSON). Never raises."""
    url = _API.format(token=token, method=method)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log.warning("Telegram %s failed: %s", method, exc)
        return None


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send one plain-text message. Returns True only when Telegram
    answered ok. Long texts are truncated to the API limit."""
    if not token or not chat_id or not text:
        return False
    result = _post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text[:_MAX_TEXT],
        "disable_web_page_preview": True,
    })
    return bool(result and result.get("ok"))


def get_updates(token: str, offset: int | None) -> list[dict]:
    """Long-poll-free getUpdates batch (timeout 10). Returns the
    result list, or [] on failure."""
    payload: dict = {"timeout": 10}
    if offset is not None:
        payload["offset"] = offset
    result = _post(token, "getUpdates", payload, timeout=15)
    if result and result.get("ok"):
        return result.get("result") or []
    return []


def list_open_windows() -> list[dict]:
    """Every visible top-level window right now: [{app, title}].
    Windows-only (win32gui + psutil); returns [] anywhere else or on
    any error. Used for 'which software is open' — DeskAudit's own
    tracker only knows the FOREGROUND app, so this fills the gap.
    Titles are window titles only, never typed content."""
    if __import__("sys").platform != "win32":
        return []
    try:
        import win32gui
        import win32process

        import psutil

        found: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _cb(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = (win32gui.GetWindowText(hwnd) or "").strip()
                if not title:
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    app = psutil.Process(pid).name()
                except Exception:
                    app = "?"
                key = (app, title)
                if key not in seen:
                    seen.add(key)
                    found.append({"app": app, "title": title})
            except Exception:
                pass

        win32gui.EnumWindows(_cb, None)
        return found
    except Exception as exc:
        _log.warning("list_open_windows failed: %s", exc)
        return []


def _fmt_min(seconds) -> str:
    try:
        minutes = int(float(seconds or 0) // 60)
    except Exception:
        minutes = 0
    if minutes >= 60:
        return f"{minutes // 60}ঘ {minutes % 60}মি"
    return f"{minutes}মি"


def _now_bangla() -> str:
    return datetime.now().strftime("%H:%M")


def _fmt_size(nbytes) -> str:
    """Human size for downloads / transfers (B, KB, MB)."""
    try:
        n = float(nbytes or 0)
    except Exception:
        return ""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{int(n)} B" if n else ""


def _hhmm(ts) -> str:
    """HH:MM out of an ISO timestamp, '' when missing."""
    try:
        return str(ts or "")[11:16]
    except Exception:
        return ""


def _day_bounds(db, day: str) -> tuple[str, str]:
    """(first activity, last activity) timestamps for the day."""
    try:
        row = db.conn.execute(
            """SELECT MIN(started_at) AS first_at,
                      MAX(COALESCE(ended_at, started_at)) AS last_at
               FROM app_sessions WHERE substr(started_at, 1, 10) = ?""",
            (day,),
        ).fetchone()
        return (row["first_at"] or "", row["last_at"] or "")
    except Exception:
        return "", ""


def _full_summary(db, day: str) -> dict | None:
    """The same dict the dashboard/report uses (report.build_summary):
    totals, projects with goals, browser productive/distracting,
    searches, downloads, transfers, file counts. None on failure —
    callers fall back to a minimal text instead of crashing."""
    try:
        from .report import build_summary

        return build_summary(db, day)
    except Exception as exc:
        _log.warning("Telegram build_summary failed: %s", exc)
        return None


def _window_searches(db, since: datetime, limit: int = 5) -> list:
    try:
        return db.conn.execute(
            """SELECT query, COUNT(*) AS n FROM browser_searches
               WHERE searched_at >= ? GROUP BY query
               ORDER BY n DESC, query ASC LIMIT ?""",
            (since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def _window_downloads(db, since: datetime, limit: int = 5) -> list:
    try:
        return db.conn.execute(
            """SELECT file_name, total_bytes, state FROM browser_downloads
               WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?""",
            (since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def _window_transfers(db, since: datetime, limit: int = 6) -> list:
    try:
        return db.conn.execute(
            """SELECT transfer_type, channel, file_name, size_bytes,
                      destination, occurred_at FROM transfers
               WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT ?""",
            (since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def _open_session(db, day: str):
    """The currently-open app session (ended_at NULL), if any."""
    try:
        row = db.conn.execute(
            """SELECT app_name, window_title, started_at FROM app_sessions
               WHERE substr(started_at, 1, 10) = ? AND ended_at IS NULL
               ORDER BY started_at DESC LIMIT 1""",
            (day,),
        ).fetchone()
        return row
    except Exception:
        return None


def _recent_files(db, day: str, limit: int = 5) -> list:
    try:
        return db.conn.execute(
            """SELECT event_type, path, occurred_at FROM file_events
               WHERE substr(occurred_at, 1, 10) = ?
               ORDER BY occurred_at DESC LIMIT ?""",
            (day, limit),
        ).fetchall()
    except Exception:
        return []


def _recent_sessions(db, day: str, since: datetime, limit: int = 8) -> list:
    """App sessions started since `since` (for the hourly digest)."""
    try:
        return db.conn.execute(
            """SELECT app_name, window_title, project,
                      COALESCE(active_seconds, 0) AS active
               FROM app_sessions
               WHERE substr(started_at, 1, 10) = ? AND started_at >= ?
               ORDER BY started_at DESC LIMIT ?""",
            (day, since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def build_status_text(db, day: str, include_open: bool = True) -> str:
    """Full Bengali status — every section the dashboard shows, in text:
    what is open NOW, today's totals (active/idle, first/last work),
    projects with goal progress, top apps, browser (productive vs
    distracting, top domains, searches, downloads), recent files with
    times, transfer rows with file names + destinations, alerts.
    Metadata only — app names, window titles, file names, domains."""
    title = "📊 DeskAudit — বর্তমান অবস্থা" if include_open else "📊 DeskAudit — আজকের সারাংশ"
    lines = [f"{title} ({day} {_now_bangla()})", ""]
    # --- right now -------------------------------------------------
    if include_open:
        open_row = _open_session(db, day)
        windows = list_open_windows()
        lines.append("🖥️ এখন খোলা আছে:")
        if open_row:
            lines.append(f"  • এখন সামনে: {open_row['app_name']} — "
                         f"{open_row['window_title'] or '(শিরোনাম নেই)'}")
        if windows:
            by_app: dict[str, list[str]] = {}
            for w in windows:
                by_app.setdefault(w["app"], []).append(w["title"])
            for app, titles in sorted(by_app.items()):
                lines.append(f"  • {app} ({len(titles)}টি উইন্ডো)")
                for t in titles[:3]:
                    lines.append(f"      — {t[:80]}")
        elif not open_row:
            lines.append("  • (এই মুহূর্তে কোনো সক্রিয় অ্যাপ ধরা পড়েনি)")
        lines.append("")
    # --- today, from the same summary the dashboard uses ----------
    s = _full_summary(db, day)
    if s is None:
        # Fallback: minimal totals only, never a crash.
        try:
            totals = db.totals(day)
            lines.append(f"⏱️ আজ মোট সক্রিয় সময়: {_fmt_min(totals['active'])}")
        except Exception:
            lines.append("⏱️ আজকের তথ্য পড়া যায়নি।")
        return "\n".join(lines)
    has_data = bool(s["active_seconds"] or s["top_apps"] or s["transfers"]["total"])
    first_at, last_at = _day_bounds(db, day)
    lines.append(f"⏱️ আজ মোট সক্রিয় সময়: {_fmt_min(s['active_seconds'])}"
                 f" (অলস {_fmt_min(s['idle_seconds'])})")
    if first_at:
        lines.append(f"   প্রথম কাজ {_hhmm(first_at)} • শেষ {_hhmm(last_at)}"
                     f" • {s['apps_used']}টি অ্যাপ ব্যবহৃত")
    projects = [p for p in s.get("projects", []) if p.get("project") != "Untagged"]
    if projects:
        lines.append("🎯 প্রজেক্ট:")
        for p in projects:
            goal = ""
            if p.get("goal_minutes"):
                goal = (f" / লক্ষ্য {_fmt_min(p['goal_minutes'] * 60)}"
                        f" ({p.get('goal_percent', 0)}%)"
                        f"{' ✓' if p.get('goal_met') else ''}")
            lines.append(f"  • {p['project']} — {_fmt_min(p['active_seconds'])}{goal}")
    untagged_seconds = sum(a.get("seconds", 0)
                           for a in s.get("untagged", {}).get("apps", []))
    if untagged_seconds:
        lines.append(f"🏷️ প্রজেক্ট ছাড়া সময়: {_fmt_min(untagged_seconds)}")
    if s.get("top_apps"):
        lines.append("📌 আজকের প্রধান অ্যাপ:")
        for a in s["top_apps"][:6]:
            t = f" — {a['title'][:60]}" if a.get("title") else ""
            lines.append(f"  • {a['app']} — {_fmt_min(a['active_seconds'])}{t}")
    b = s.get("browser", {})
    if b.get("active_seconds") or b.get("visits"):
        lines.append(f"🌐 ব্রাউজার: {_fmt_min(b['active_seconds'])}"
                     f" (কাজের {_fmt_min(b['productive_seconds'])}"
                     f" • অন্যান্য {_fmt_min(b['distracting_seconds'])})"
                     f" • {b.get('visits', 0)} ভিজিট")
        for d in b.get("top_domains", [])[:5]:
            if d.get("active_seconds"):
                mark = "কাজের" if d.get("productive") else (d.get("category") or "")
                lines.append(f"  • {d['domain']} — {_fmt_min(d['active_seconds'])} [{mark}]")
        for q in b.get("searches", [])[:5]:
            lines.append(f"  🔎 {q['query']} (x{q['count']})")
        for dl in b.get("downloads", [])[:5]:
            size = f" ({_fmt_size(dl.get('size'))})" if dl.get("size") else ""
            lines.append(f"  ⬇️ {dl['file']}{size} [{dl.get('state', '')}]")
    files = _recent_files(db, day, limit=8)
    if files:
        lines.append("📁 সর্বশেষ ফাইল কাজ:")
        for f in files:
            name = (f["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {_hhmm(f['occurred_at'])} {f['event_type']}: {name}")
    fc = s.get("file_counts", {})
    counts = ", ".join(f"{fc.get(k, 0)} {k}"
                       for k in ("created", "modified", "deleted", "moved")
                       if fc.get(k))
    if counts:
        lines.append(f"   আজ মোট ফাইল কাজ: {counts}")
    tr = s.get("transfers", {})
    if tr.get("total"):
        lines.append(f"🔒 আজ ডেটা ট্রান্সফার: {tr['total']}টি")
        for row in tr.get("rows", [])[:6]:
            size = f" ({_fmt_size(row.get('size'))})" if row.get("size") else ""
            dest = f" → {row['destination']}" if row.get("destination") else ""
            lines.append(f"  • {_hhmm(row['at'])} {row['type']} — "
                         f"{row['file'] or row['channel']}{dest}{size}")
    try:
        alerts = db.list_alerts(date=day, limit=5)
    except Exception:
        alerts = []
    if alerts:
        lines.append("⚠️ আজকের অ্যালার্ট:")
        for a in alerts:
            lines.append(f"  • [{a['severity']}] {a['title']}")
    if not has_data:
        lines.append("")
        lines.append("(আজ এখনো কোনো কাজ রেকর্ড হয়নি — এজেন্ট চালু আছে কিনা "
                     "দেখুন: Start Menu → DeskAudit)")
    return "\n".join(lines)


def build_today_text(db, day: str) -> str:
    """Full-day summary for /today — the same as /status without the
    'what is open right now' section."""
    return build_status_text(db, day, include_open=False)


def build_digest_text(db, day: str, since_minutes: int = 60) -> str:
    """Bengali digest of the last `since_minutes` minutes: apps used in
    the window (with project split), latest window, files with times,
    searches, downloads and transfer rows — not just counts."""
    since = datetime.now() - timedelta(minutes=since_minutes)
    lines = [f"🕐 DeskAudit — গত {since_minutes} মিনিটের আপডেট "
             f"({day} {_now_bangla()})", ""]
    sessions = _recent_sessions(db, day, since)
    if sessions:
        by_app: dict[str, float] = {}
        by_project: dict[str, float] = {}
        for s in sessions:
            by_app[s["app_name"]] = by_app.get(s["app_name"], 0) + float(s["active"] or 0)
            proj = s["project"] or "Untagged"
            by_project[proj] = by_project.get(proj, 0) + float(s["active"] or 0)
        lines.append("📌 এই সময়ে ব্যবহৃত অ্যাপ:")
        for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  • {app} — {_fmt_min(secs)}")
        # Which document/window was in front most recently.
        latest = sessions[0]
        if latest["window_title"]:
            lines.append(f"  সর্বশেষ উইন্ডো: {latest['app_name']} — "
                         f"{latest['window_title'][:90]}")
        projects = [(p, v) for p, v in by_project.items() if p != "Untagged"]
        if projects:
            lines.append("🎯 প্রজেক্ট:")
            for p, secs in sorted(projects, key=lambda kv: -kv[1]):
                lines.append(f"  • {p} — {_fmt_min(secs)}")
    else:
        lines.append("📌 এই সময়ে কোনো নতুন অ্যাপ সেশন শুরু হয়নি।")
    try:
        rows = db.conn.execute(
            """SELECT event_type, path, occurred_at FROM file_events
               WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 8""",
            (since.isoformat(timespec="seconds"),),
        ).fetchall()
    except Exception:
        rows = []
    if rows:
        lines.append("📁 ফাইল কাজ:")
        for r in rows:
            name = (r["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {_hhmm(r['occurred_at'])} {r['event_type']}: {name}")
    for q in _window_searches(db, since):
        lines.append(f"  🔎 {q['query']} (x{q['n']})")
    for dl in _window_downloads(db, since):
        size = f" ({_fmt_size(dl['total_bytes'])})" if dl["total_bytes"] else ""
        lines.append(f"  ⬇️ {dl['file_name']}{size} [{dl['state']}]")
    for t in _window_transfers(db, since):
        size = f" ({_fmt_size(t['size_bytes'])})" if t["size_bytes"] else ""
        dest = f" → {t['destination']}" if t["destination"] else ""
        lines.append(f"  🔒 {_hhmm(t['occurred_at'])} {t['transfer_type']} — "
                     f"{t['file_name'] or t['channel']}{dest}{size}")
    try:
        counts = db.transfer_counts(day)
    except Exception:
        counts = {}
    total_transfers = sum(v.get("count", 0) for v in counts.values())
    if total_transfers:
        lines.append(f"🔒 আজ মোট ডেটা ট্রান্সফার: {total_transfers}টি")
    try:
        alerts = db.list_alerts(date=day, limit=5)
    except Exception:
        alerts = []
    if alerts:
        lines.append("⚠️ আজকের অ্যালার্ট:")
        for a in alerts:
            lines.append(f"  • [{a['severity']}] {a['title']}")
    if len(lines) <= 2:
        lines.append("(এই সময়ে রেকর্ড করার মতো নতুন কাজ নেই।)")
    return "\n".join(lines)


def send_long(token: str, chat_id: str, text: str,
              chunk_chars: int = 3900) -> bool:
    """Send a long text as several Telegram messages, split on line
    boundaries so no section is cut mid-line. Returns True only when
    every chunk was accepted."""
    if not token or not chat_id or not text:
        return False
    if len(text) <= chunk_chars:
        return send_message(token, chat_id, text)
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > chunk_chars:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur.rstrip("\n"))
    ok = True
    for chunk in chunks:
        ok = send_message(token, chat_id, chunk) and ok
    return ok


class TelegramBot:
    """Outbound bot: scheduled digests, critical-alert notices, and
    answering /status, /today, /start, /help in the owner's chat."""

    def __init__(self, db, settings: dict | None, log=None):
        self.db = db
        self.settings = {**DEFAULT_TELEGRAM, **(settings or {})}
        self.log = log or _log

    @property
    def configured(self) -> bool:
        return bool(self.settings.get("bot_token") and self.settings.get("chat_id"))

    @property
    def token(self) -> str:
        return str(self.settings.get("bot_token") or "")

    @property
    def chat_id(self) -> str:
        return str(self.settings.get("chat_id") or "")

    def send(self, text: str) -> bool:
        if not self.configured:
            return False
        ok = send_message(self.token, self.chat_id, text)
        if not ok:
            self.log.warning("Telegram send failed (check bot token / chat id).")
        return ok

    def send_long(self, text: str) -> bool:
        """Send a long report split into several messages if needed."""
        if not self.configured:
            return False
        ok = send_long(self.token, self.chat_id, text)
        if not ok:
            self.log.warning("Telegram send failed (check bot token / chat id).")
        return ok

    def send_digest(self, day: str | None = None) -> bool:
        from .utils import today_str

        day = day or today_str()
        minutes = int(self.settings.get("digest_minutes", 60) or 60)
        return self.send_long(build_digest_text(self.db, day, minutes))

    def send_status(self, day: str | None = None) -> bool:
        from .utils import today_str

        return self.send_long(build_status_text(self.db, day or today_str()))

    def send_today(self, day: str | None = None) -> bool:
        from .utils import today_str

        return self.send_long(build_today_text(self.db, day or today_str()))

    def notify_alert(self, alert) -> bool:
        """Immediate notice for one critical alert (title + severity
        only — no content beyond what the alert itself stores)."""
        if not self.settings.get("notify_critical", True):
            return False
        text = (f"🚨 DeskAudit অ্যালার্ট [{alert.severity}]\n"
                f"{alert.title}\n{alert.message}").strip()
        return self.send(text)

    def poll_commands(self) -> int:
        """Fetch new updates and answer commands. Returns how many
        updates were handled. Offset is kept in kv_state so a restart
        does not re-answer old commands."""
        if not self.configured:
            return 0
        try:
            offset = self.db.get_state("telegram_update_offset")
            updates = get_updates(self.token, int(offset) if offset else None)
        except Exception as exc:
            self.log.warning("Telegram poll failed: %s", exc)
            return 0
        handled = 0
        max_id = None
        for upd in updates:
            try:
                max_id = upd.get("update_id", max_id)
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = str((msg.get("chat") or {}).get("id", ""))
                if chat != self.chat_id:
                    continue  # only the owner's chat may command the bot
                cmd = text.split()[0].lower() if text else ""
                from .utils import today_str

                if cmd in ("/start", "/help"):
                    self.send("👋 DeskAudit বট — কমান্ড:\n"
                              "/status — এখন কী চলছে\n"
                              "/today — আজকের সারাংশ\n"
                              "/help — এই তালিকা")
                elif cmd == "/status":
                    self.send_long(build_status_text(self.db, today_str()))
                elif cmd == "/today":
                    self.send_long(build_today_text(self.db, today_str()))
                handled += 1
            except Exception as exc:
                self.log.warning("Telegram command failed: %s", exc)
        if max_id is not None:
            try:
                self.db.set_state("telegram_update_offset", str(int(max_id) + 1))
            except Exception:
                pass
        return handled
