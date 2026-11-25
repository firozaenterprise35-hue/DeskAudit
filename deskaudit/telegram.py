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
            """SELECT app_name, window_title,
                      COALESCE(active_seconds, 0) AS active
               FROM app_sessions
               WHERE substr(started_at, 1, 10) = ? AND started_at >= ?
               ORDER BY started_at DESC LIMIT ?""",
            (day, since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def build_status_text(db, day: str) -> str:
    """Full Bengali status: what is open NOW, and what happened today.
    Metadata only — app names, window titles, file names, domains."""
    lines = [f"📊 DeskAudit — বর্তমান অবস্থা ({day} {_now_bangla()})", ""]
    # --- right now -------------------------------------------------
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
    # --- today -----------------------------------------------------
    try:
        totals = db.totals(day)
        active = _fmt_min(totals["active"])
    except Exception:
        active = "—"
    lines.append("")
    lines.append(f"⏱️ আজ মোট সক্রিয় সময়: {active}")
    try:
        apps = db.app_totals(day)[:5]
    except Exception:
        apps = []
    if apps:
        lines.append("📌 আজকের প্রধান অ্যাপ:")
        for a in apps:
            lines.append(f"  • {a['app_name']} — {_fmt_min(a['active'])}")
    try:
        domains = db.browser_domain_totals(day)[:5]
    except Exception:
        domains = []
    if domains:
        lines.append("🌐 আজকের প্রধান সাইট:")
        for d in domains:
            lines.append(f"  • {d['domain']} — {_fmt_min(d['active'])}")
    files = _recent_files(db, day)
    if files:
        lines.append("📁 সর্বশেষ ফাইল কাজ:")
        for f in files:
            name = (f["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {f['event_type']}: {name}")
    try:
        counts = db.transfer_counts(day)
    except Exception:
        counts = {}
    total_transfers = sum(v.get("count", 0) for v in counts.values())
    if total_transfers:
        lines.append(f"🔒 আজ ডেটা ট্রান্সফার: {total_transfers}টি")
        for kind, v in sorted(counts.items()):
            lines.append(f"  • {kind}: {v.get('count', 0)}টি")
    try:
        alerts = db.list_alerts(date=day, limit=5)
    except Exception:
        alerts = []
    if alerts:
        lines.append("⚠️ আজকের অ্যালার্ট:")
        for a in alerts:
            lines.append(f"  • [{a['severity']}] {a['title']}")
    return "\n".join(lines)


def build_digest_text(db, day: str, since_minutes: int = 60) -> str:
    """Bengali digest of the last `since_minutes` minutes."""
    since = datetime.now() - timedelta(minutes=since_minutes)
    lines = [f"🕐 DeskAudit — গত {since_minutes} মিনিটের আপডেট "
             f"({day} {_now_bangla()})", ""]
    sessions = _recent_sessions(db, day, since)
    if sessions:
        by_app: dict[str, float] = {}
        for s in sessions:
            by_app[s["app_name"]] = by_app.get(s["app_name"], 0) + float(s["active"] or 0)
        lines.append("📌 এই সময়ে ব্যবহৃত অ্যাপ:")
        for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  • {app} — {_fmt_min(secs)}")
        # Which document/window was in front most recently.
        latest = sessions[0]
        if latest["window_title"]:
            lines.append(f"  সর্বশেষ উইন্ডো: {latest['app_name']} — "
                         f"{latest['window_title'][:90]}")
    else:
        lines.append("📌 এই সময়ে কোনো নতুন অ্যাপ সেশন শুরু হয়নি।")
    try:
        rows = db.conn.execute(
            """SELECT event_type, path FROM file_events
               WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 5""",
            (since.isoformat(timespec="seconds"),),
        ).fetchall()
    except Exception:
        rows = []
    if rows:
        lines.append("📁 ফাইল কাজ:")
        for r in rows:
            name = (r["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {r['event_type']}: {name}")
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

    def send_digest(self, day: str | None = None) -> bool:
        from .utils import today_str

        day = day or today_str()
        minutes = int(self.settings.get("digest_minutes", 60) or 60)
        return self.send(build_digest_text(self.db, day, minutes))

    def send_status(self, day: str | None = None) -> bool:
        from .utils import today_str

        return self.send(build_status_text(self.db, day or today_str()))

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
                    self.send(build_status_text(self.db, today_str()))
                elif cmd == "/today":
                    self.send(build_digest_text(self.db, today_str(), 60 * 24))
                handled += 1
            except Exception as exc:
                self.log.warning("Telegram command failed: %s", exc)
        if max_id is not None:
            try:
                self.db.set_state("telegram_update_offset", str(int(max_id) + 1))
            except Exception:
                pass
        return handled
