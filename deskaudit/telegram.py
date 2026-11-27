"""Telegram bot — outbound status, digests and critical-alert notices,
plus remote control of the agent from the owner's chat.

The bot sends Bengali summaries to ONE chat (the owner's). It never
receives or stores message content from anyone else, and DeskAudit
stays metadata-only: reports name WHICH app/window/file is active
(window titles and file names already logged locally) — never what
anyone typed, never message or email text, never passwords.

Commands (only the owner's chat id may use them; others are ignored):
  /status /today /week /apps /projects /transfers /alerts /files
  /search <word> /report /menu /help — read-only, always safe.
  /pause /resume — pause / resume tracking (PAUSE file).
  /mute [minutes] /unmute — silence alerts + digests.
  /digest <minutes|off> — change the hourly digest interval.
  /lock — lock the Windows PC.
  /close <app> /freeze <app> /unfreeze <app> — close or suspend a
    running app (two-step confirmation; DeskAudit itself and Windows
    system processes can never be closed).
  /clear [date] /reset — delete one day's data / all data (two-step
    confirmation; /reset backs the DB up first).
  /backup — back the DB up now. /goal <project> <minutes> — set a
  daily project goal.

Destructive commands ask "নিশ্চিত?" first and only run after the
owner replies হ্যাঁ/yes (or taps ✅). The pending confirmation is
stored in the DB (kv_state) and expires after 5 minutes.

Setup (owner does this once, in Telegram):
  1. Talk to @BotFather -> /newbot -> copy the bot token.
  2. Send /start to the new bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates in a browser and
     read chat.id (or ask @userinfobot for your own id).
  3. Put the token + chat id in Dashboard Settings -> Telegram
     (they are stored in config.local.yaml only, never in git), or
     pass them to the CLI test commands.
The agent polls getUpdates for messages and inline-button taps and
answers in the same chat. Everything here is stdlib-only (urllib).
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

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


def _post_multipart(token: str, method: str, fields: dict,
                    file_field: str, filename: str, file_bytes: bytes,
                    timeout: int = 60) -> dict | None:
    """POST one Bot API call with a file (multipart/form-data).
    Returns the decoded JSON or None on any failure. Never raises."""
    boundary = "----DeskAudit" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                 f"{value}\r\n").encode("utf-8")
    body += f"--{boundary}\r\n".encode()
    body += (f'Content-Disposition: form-data; name="{file_field}"; '
             f'filename="{filename}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        _API.format(token=token, method=method), data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _log.warning("Telegram %s failed: %s", method, exc)
        return None


def send_document(token: str, chat_id: str, path: str | Path,
                  caption: str = "") -> bool:
    """Send a local file as a Telegram document. Returns True only
    when Telegram answered ok. Never raises."""
    if not token or not chat_id:
        return False
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        _log.warning("Telegram sendDocument: cannot read %s: %s", path, exc)
        return False
    result = _post_multipart(token, "sendDocument",
                             {"chat_id": chat_id, "caption": caption[:1024]},
                             "document", Path(path).name, data)
    return bool(result and result.get("ok"))


def send_photo(token: str, chat_id: str, path: str | Path,
               caption: str = "") -> bool:
    """Send a local image as a Telegram photo (e.g. a screenshot).
    Returns True only when Telegram answered ok. Never raises."""
    if not token or not chat_id:
        return False
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        _log.warning("Telegram sendPhoto: cannot read %s: %s", path, exc)
        return False
    result = _post_multipart(token, "sendPhoto",
                             {"chat_id": chat_id, "caption": caption[:1024]},
                             "photo", Path(path).name, data)
    return bool(result and result.get("ok"))


def answer_callback(token: str, callback_query_id: str,
                    text: str = "") -> None:
    """Acknowledge an inline-button tap (stops the spinner). Never
    raises; a failure only leaves the spinner running."""
    if not token or not callback_query_id:
        return
    _post(token, "answerCallbackQuery",
          {"callback_query_id": callback_query_id, "text": text[:200]})


def _hosts_path(hosts_file: str | Path | None = None) -> Path:
    """The Windows hosts file (or `hosts_file` when given — used by
    tests). Raises RuntimeError off Windows with no override."""
    import sys

    if hosts_file is not None:
        return Path(hosts_file)
    if sys.platform != "win32":
        raise RuntimeError("hosts blocking works on Windows only")
    return Path(r"C:\Windows\System32\drivers\etc\hosts")


def _norm_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    return domain.split("/")[0].strip()


def block_domain(domain: str, hosts_file: str | Path | None = None) -> str:
    """Block a domain for the whole PC by pointing it at 127.0.0.1
    in the Windows hosts file (needs admin). Returns a short Bengali
    result. Raises RuntimeError with a plain message on failure."""
    domain = _norm_domain(domain)
    if not domain or "." not in domain:
        raise ValueError("সঠিক ডোমেইন দিন — যেমন facebook.com")
    path = _hosts_path(hosts_file)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = ""
    except PermissionError:
        raise RuntimeError("hosts ফাইল লেখা যায়নি — DeskAudit অ্যাডমিন হিসেবে চালাতে হবে")
    lines = text.splitlines()
    targets = {domain, "www." + domain}
    existing = {ln.split()[1].lower() for ln in lines
                if ln.strip() and not ln.strip().startswith("#")
                and len(ln.split()) >= 2 and ln.split()[0] == "127.0.0.1"}
    missing = sorted(t for t in targets if t not in existing)
    if not missing:
        return f"🌐 {domain} আগেই ব্লক করা আছে।"
    lines += [f"127.0.0.1 {t}  # DeskAudit" for t in missing]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except PermissionError:
        raise RuntimeError("hosts ফাইল লেখা যায়নি — DeskAudit অ্যাডমিন হিসেবে চালাতে হবে")
    _flush_dns()
    return f"🌐 {domain} ব্লক করা হয়েছে (এই PC-তে আর খুলবে না)।"


def unblock_domain(domain: str, hosts_file: str | Path | None = None) -> str:
    """Remove DeskAudit's block lines for a domain. Returns a short
    Bengali result. Raises RuntimeError with a plain message on failure."""
    domain = _norm_domain(domain)
    if not domain or "." not in domain:
        raise ValueError("সঠিক ডোমেইন দিন — যেমন facebook.com")
    path = _hosts_path(hosts_file)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"🌐 {domain} ব্লক করা নেই।"
    except PermissionError:
        raise RuntimeError("hosts ফাইল লেখা যায়নি — DeskAudit অ্যাডমিন হিসেবে চালাতে হবে")
    targets = {domain, "www." + domain}
    kept = []
    removed = 0
    for ln in text.splitlines():
        parts = ln.split()
        if (len(parts) >= 2 and parts[0] == "127.0.0.1"
                and parts[1].lower() in targets
                and "# DeskAudit" in ln):
            removed += 1
            continue
        kept.append(ln)
    if not removed:
        return f"🌐 {domain} ব্লক করা নেই।"
    try:
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except PermissionError:
        raise RuntimeError("hosts ফাইল লেখা যায়নি — DeskAudit অ্যাডমিন হিসেবে চালাতে হবে")
    _flush_dns()
    return f"🌐 {domain}-এর ব্লক তুলে দেওয়া হয়েছে।"


def _flush_dns() -> None:
    """Best-effort DNS cache flush (Windows). Never raises. Runs
    fully hidden — no console window (v12.5)."""
    from .utils import run_hidden

    if sys.platform != "win32":
        return
    try:
        run_hidden(["ipconfig", "/flushdns"], capture_output=True,
                   timeout=15)
    except Exception:
        pass


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


def build_apps_text(db, day: str) -> str:
    """Open apps right now + today's time per app (/apps)."""
    from .utils import today_str

    lines = [f"🖥️ অ্যাপ — এখন + আজ ({day} {_now_bangla()})", ""]
    windows = list_open_windows()
    open_row = _open_session(db, day)
    lines.append("এখন খোলা আছে:")
    if open_row:
        lines.append(f"  • সামনে: {open_row['app_name']} — "
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
    try:
        rows = db.app_totals(day)
    except Exception:
        rows = []
    by_app: dict[str, float] = {}
    sessions: dict[str, int] = {}
    for r in rows:
        by_app[r["app_name"]] = by_app.get(r["app_name"], 0.0) + float(r["active"] or 0)
        sessions[r["app_name"]] = sessions.get(r["app_name"], 0) + int(r["sessions"] or 0)
    if by_app:
        lines.append("")
        lines.append("আজকের সময়:")
        for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  • {app} — {_fmt_min(secs)} ({sessions[app]} সেশন)")
    if len(lines) <= 2:
        lines.append("(আজ এখনো কোনো অ্যাপ রেকর্ড হয়নি।)")
    return "\n".join(lines)


def build_projects_text(db, day: str) -> str:
    """Per-project time today with goal progress (/projects)."""
    s = _full_summary(db, day)
    lines = [f"🎯 প্রজেক্ট — আজ ({day})", ""]
    if s is None:
        return "\n".join(lines + ["(প্রজেক্টের তথ্য পড়া যায়নি।)"])
    projects = [p for p in s.get("projects", []) if p.get("project") != "Untagged"]
    if projects:
        for p in projects:
            goal = ""
            if p.get("goal_minutes"):
                goal = (f" / লক্ষ্য {_fmt_min(p['goal_minutes'] * 60)}"
                        f" ({p.get('goal_percent', 0)}%)"
                        f"{' ✓' if p.get('goal_met') else ''}")
            lines.append(f"  • {p['project']} — {_fmt_min(p['active_seconds'])}{goal}"
                         f" ({p['file_events']} ফাইল, {p['visits']} ভিজিট)")
    else:
        lines.append("  (আজ কোনো প্রজেক্টে কাজ রেকর্ড হয়নি।)")
    untagged_seconds = sum(a.get("seconds", 0)
                           for a in s.get("untagged", {}).get("apps", []))
    if untagged_seconds:
        lines.append(f"🏷️ প্রজেক্ট ছাড়া সময়: {_fmt_min(untagged_seconds)}")
    return "\n".join(lines)


def build_transfers_text(db, day: str) -> str:
    """Today's data transfers, row by row (/transfers)."""
    s = _full_summary(db, day)
    lines = [f"🔒 ডেটা ট্রান্সফার — আজ ({day})", ""]
    if s is None:
        return "\n".join(lines + ["(তথ্য পড়া যায়নি।)"])
    tr = s.get("transfers", {})
    if not tr.get("total"):
        return "\n".join(lines + ["  (আজ কোনো ডেটা ট্রান্সফার ধরা পড়েনি।)"])
    lines.append(f"মোট: {tr['total']}টি")
    for kind, c in sorted(tr.get("counts", {}).items()):
        lines.append(f"  • {kind}: {c.get('count', 0)}টি")
    lines.append("")
    for row in tr.get("rows", [])[:10]:
        size = f" ({_fmt_size(row.get('size'))})" if row.get("size") else ""
        dest = f" → {row['destination']}" if row.get("destination") else ""
        app = f" [{row['app']}]" if row.get("app") else ""
        lines.append(f"  • {_hhmm(row['at'])} {row['type']} — "
                     f"{row['file'] or row['channel']}{dest}{size}{app}")
    return "\n".join(lines)


def build_delete_text(item: dict) -> str:
    """One deleted file, quarantined: what it was, where it was, when
    it was deleted, and where it is hidden now (v12.8)."""
    when = _hhmm(item.get("deleted_at", "") or "")
    size = f" ({_fmt_size(item.get('size_bytes'))})" if item.get("size_bytes") else ""
    where = "Recycle Bin থেকে" if item.get("source") == "recycle_bin" \
        else "সুরক্ষিত ফোল্ডার থেকে (Shift+Delete হলেও)"
    folder = (item.get("original_path", "") or "").replace("\\", "/")
    folder = folder[:folder.rfind("/")] if "/" in folder else folder
    lines = [
        "🗑️ একটি ফাইল ডিলিট করা হয়েছে — কিন্তু মুছে যায়নি",
        f"📄 {item.get('file_name', '?')}{size}",
        f"📁 ছিল: {folder or '?'}",
        f"🕐 {when} — {where}",
        "👁️ এখন লুকিয়ে রাখা আছে (কোয়ারেন্টাইনে)। ইউজারের চোখে এটা ডিলিট হয়ে গেছে।",
        "",
        "পারমেনেন্টলি ডিলিট করব, নাকি আগের জায়গায় ফিরিয়ে দেব?",
    ]
    return "\n".join(lines)


def build_deleted_text(db, day: str | None = None) -> str:
    """Files currently held in quarantine (/deleted)."""
    from .utils import today_str

    day = day or today_str()
    try:
        held = db.quarantined_held(limit=20)
        today_rows = db.quarantined_for_date(day, limit=20)
    except Exception:
        held, today_rows = [], []
    lines = ["🗑️ মুছে যাওয়া ফাইল — সুরক্ষায় আছে", ""]
    if not held:
        lines.append("  (এখন কোনো ফাইল সুরক্ষায় নেই।)")
    else:
        lines.append(f"এখন সুরক্ষায়: {len(held)}টি")
        for row in held:
            size = f" ({_fmt_size(row['size_bytes'])})" if row["size_bytes"] else ""
            lines.append(f"  • #{row['id']} {_hhmm(row['deleted_at'])} "
                         f"{row['file_name']}{size}")
            lines.append(f"    ছিল: {(row['original_path'] or '').replace(chr(92), '/')}")
    decided = [r for r in today_rows if r["status"] != "held"]
    if decided:
        lines.append("")
        lines.append("আজ সিদ্ধান্ত হয়েছে:")
        for row in decided[:10]:
            verb = "পারমেনেন্টলি মোছা" if row["status"] == "purged" else "ফিরিয়ে দেওয়া"
            lines.append(f"  • #{row['id']} {row['file_name']} — {verb}")
    lines.append("")
    lines.append("হ্যাঁ/না চাপতে হয় ফাইলটি ডিলিট হওয়ার মেসেজের বোতামে।")
    return "\n".join(lines)


def build_alerts_text(db, day: str) -> str:
    """Today's alerts with severity + time (/alerts)."""
    try:
        alerts = db.list_alerts(date=day, limit=20)
    except Exception:
        alerts = []
    lines = [f"⚠️ অ্যালার্ট — আজ ({day})", ""]
    if not alerts:
        return "\n".join(lines + ["  (আজ কোনো অ্যালার্ট নেই।)"])
    for a in alerts:
        lines.append(f"  • [{a['severity']}] {_hhmm(a['created_at'])} {a['title']}")
    return "\n".join(lines)


def build_files_text(db, day: str) -> str:
    """Recent file work with times + today's counts (/files)."""
    files = _recent_files(db, day, limit=10)
    lines = [f"📁 ফাইল কাজ — আজ ({day})", ""]
    if files:
        for f in files:
            name = (f["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {_hhmm(f['occurred_at'])} {f['event_type']}: {name}")
    s = _full_summary(db, day)
    fc = (s or {}).get("file_counts", {}) if s else {}
    counts = ", ".join(f"{fc.get(k, 0)} {k}"
                       for k in ("created", "modified", "deleted", "moved")
                       if fc.get(k))
    if counts:
        lines.append(f"মোট: {counts}")
    if not files and not counts:
        lines.append("  (আজ কোনো ফাইল কাজ রেকর্ড হয়নি।)")
    return "\n".join(lines)


def build_week_text(db, day: str) -> str:
    """Last 7 days: active time + top project per day (/week)."""
    from .utils import today_str

    try:
        end = datetime.strptime(day, "%Y-%m-%d").date()
    except Exception:
        end = datetime.now().date()
    lines = ["📅 গত ৭ দিনের সারাংশ", ""]
    for back in range(6, -1, -1):
        d = (end - timedelta(days=back)).isoformat()
        try:
            totals = db.totals(d)
            active = float(totals["active"] or 0)
        except Exception:
            active = 0.0
        top = ""
        try:
            rows = db.project_totals(d)
            if rows:
                top = f" • {rows[0]['project']}"
        except Exception:
            pass
        mark = " (আজ)" if d == today_str() else ""
        lines.append(f"  • {d}: {_fmt_min(active)}{top}{mark}")
    return "\n".join(lines)


def build_browser_text(db, day: str) -> str:
    """Live browser tab + today's domains and recent visits (/browser)."""
    lines = [f"🌐 ব্রাউজার — আজ ({day})", ""]
    latest = None
    try:
        latest = db.latest_browser_session(day)
    except Exception:
        latest = None
    if latest is not None:
        lines.append("এখন / সর্বশেষ ট্যাব:")
        lines.append(f"  • {latest['browser']} — {latest['domain'] or '(?)'}")
        if latest['title']:
            lines.append(f"    {(latest['title'] or '')[:90]}")
        if latest['url']:
            lines.append(f"    {str(latest['url'])[:120]}")
        lines.append(f"    শুরু: {_hhmm(latest['started_at'])}")
    else:
        lines.append("  (আজ ব্রাউজার খোলা হয়নি।)")
    try:
        domains = db.browser_domain_totals(day)
    except Exception:
        domains = []
    if domains:
        lines.append("")
        lines.append("আজকের ডোমেইন:")
        for d in domains[:6]:
            mark = " ✓ কাজের" if d["is_productive"] else " ✗ অন্যান্য"
            lines.append(f"  • {d['domain']} — {_fmt_min(d['active'])}{mark}")
    try:
        visits = db.recent_visits(day, limit=8)
    except Exception:
        visits = []
    if visits:
        lines.append("")
        lines.append("সর্বশেষ ভিজিট:")
        for v in visits:
            lines.append(f"  • {_hhmm(v['visited_at'])} {v['domain']}"
                         f"{' — ' + (v['title'] or '')[:50] if v['title'] else ''}")
    return "\n".join(lines)


def build_new_app_text(info: dict, project: str = "Untagged") -> str:
    """One newly opened program, as a Telegram message (v12.5).

    Metadata only: which program, its exe path, its open window title,
    when it opened, and its project tag. Never what anyone types or
    writes inside it — the window title may show WHICH chat or document
    is open (e.g. "WhatsApp — Rina"), never the words typed there.
    """
    lines = ["🆕 নতুন প্রোগ্রাম খুলেছে",
             f"📌 {info.get('name') or '(?)'}"]
    if info.get("exe"):
        lines.append(f"📁 {info['exe']}")
    title = (info.get("title") or "").strip()
    lines.append(f"🪟 {title[:100]}" if title else "🪟 (এখনো কোনো উইন্ডো খোলা হয়নি)")
    lines.append(f"🕐 খুলেছে: {info.get('started_at') or 'এইমাত্র'}")
    lines.append(f"🎯 প্রজেক্ট: {project}")
    return "\n".join(lines)


def build_search_text(db, day: str, query: str) -> str:
    """Search one day's files, window titles and browser searches."""
    res = db.search_today(day, query)
    lines = [f"🔎 খোঁজা: “{query}” — আজ ({day})", ""]
    found = False
    if res["files"]:
        found = True
        lines.append("ফাইল:")
        for f in res["files"]:
            name = (f["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {_hhmm(f['occurred_at'])} {f['event_type']}: {name}")
    if res["titles"]:
        found = True
        lines.append("উইন্ডো:")
        for t in res["titles"]:
            lines.append(f"  • {_hhmm(t['started_at'])} {t['app_name']} — "
                         f"{(t['window_title'] or '')[:70]}")
    if res["searches"]:
        found = True
        lines.append("ব্রাউজার সার্চ:")
        for q in res["searches"]:
            lines.append(f"  • {_hhmm(q['searched_at'])} {q['query']}")
    if not found:
        lines.append("  (আজ এই শব্দে কিছু পাওয়া যায়নি।)")
    return "\n".join(lines)


HELP_TEXT = (
    "👋 DeskAudit বট — কমান্ড:\n"
    "📊 দেখা: /status /today /week /apps /projects /transfers /alerts /files\n"
    "🌐 /browser — কোন সাইটে কী চলছে | /browser close | /browser block <ডোমেইন>\n"
    "📸 /screenshot — এখনকার স্ক্রিনের ছবি পাঠানো\n"
    "🔎 /search <শব্দ> — আজ ফাইল/উইন্ডো/সার্চে খোঁজা\n"
    "📄 /report — আজকের রিপোর্ট ফাইল হিসেবে পাঠানো\n"
    "📋 /menu — বোতামসহ মেনু\n"
    "⏸ /pause — অডিট বন্ধ | ▶️ /resume — আবার চালু\n"
    "🔕 /mute [মিনিট] — অ্যালার্ট+ডাইজেস্ট চুপ | /unmute — আবার চালু\n"
    "🕐 /digest <মিনিট|off> — ডাইজেস্টের সময় বদলানো\n"
    "🔒 /lock — কম্পিউটার লক করা\n"
    "⬜ /screen white | /screen black | /screen off — স্ক্রিন ঢাকা/খোলা\n"
    "❌ /close <অ্যাপ> — চলমান অ্যাপ বন্ধ (নিশ্চিতকরণ লাগবে)\n"
    "🧊 /freeze <অ্যাপ> — অ্যাপ স্থগিত | /unfreeze — আবার চালু\n"
    "⏻ /shutdown | /restart — PC বন্ধ/রিস্টার্ট (নিশ্চিতকরণ লাগবে) | /abort — বাতিল\n"
    "🗑 /clear [তারিখ] — এক দিনের ডেটা মোছা (নিশ্চিতকরণ লাগবে)\n"
    "🗑 /deleted — ডিলিট হওয়া ফাইল, যা সুরক্ষায় লুকিয়ে আছে\n"
    "♻️ /reset — সব ডেটা মুছে নতুন শুরু, আগে ব্যাকআপ নেবে\n"
    "💾 /backup — এখনই ব্যাকআপ নেওয়া\n"
    "🎯 /goal <প্রজেক্ট> <মিনিট> — দৈনিক লক্ষ্য বদলানো\n"
    "শুধু আপনার চ্যাট থেকেই কমান্ড চলে।"
)


def menu_keyboard() -> dict:
    """Inline keyboard for /menu — read-only actions + pause/resume
    only. Nothing destructive is one tap away."""
    def btn(text, data):
        return {"text": text, "callback_data": data}

    return {"inline_keyboard": [
        [btn("📊 বর্তমান অবস্থা", "m:status"), btn("📅 আজকের সারাংশ", "m:today")],
        [btn("📸 স্ক্রিনশট", "m:shot"), btn("🌐 ব্রাউজার", "m:browser")],
        [btn("📅 সপ্তাহ", "m:week"), btn("🖥️ অ্যাপ", "m:apps")],
        [btn("🎯 প্রজেক্ট", "m:projects"), btn("🔒 ট্রান্সফার", "m:transfers")],
        [btn("📁 ফাইল", "m:files"), btn("⚠️ অ্যালার্ট", "m:alerts")],
        [btn("📄 রিপোর্ট", "m:report"), btn("⏸ বিরতি", "m:pause")],
        [btn("🗑️ মুছে যাওয়া ফাইল", "m:deleted"), btn("▶️ আবার চালু", "m:resume")],
        [btn("❓ সাহায্য", "m:help")],
    ]}


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
    """Outbound bot + remote control: scheduled digests, critical-alert
    notices, and answering commands in the owner's chat.

    `project_root` / `config_path` / `projects_path` / `tagger` /
    `reports_dir` are optional context for the control commands
    (pause file, settings persistence, goals, reports, backup). When
    they are missing, those commands reply that they are unavailable
    instead of crashing — the read-only commands always work."""

    # Processes that must never be closed/frozen from Telegram.
    _PROTECTED_APPS = {
        "deskaudit.exe", "explorer.exe", "dwm.exe", "csrss.exe",
        "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
        "smss.exe", "taskhostw.exe", "fontdrvhost.exe", "registry",
        "system", "idle",
    }
    # Audit note (v12.3 bug, fixed in v12.4): "y", "ok" and "করো" used
    # to confirm too — a casual "ok" reply could fire a pending
    # destructive action. Only an explicit yes counts now.
    _CONFIRM_WORDS = {"হ্যাঁ", "হ্যা", "yes", "confirm", "হ্যাঁ করো"}
    _CANCEL_WORDS = {"না", "no", "cancel", "বাতিল", "থাক"}
    _PENDING_TTL = 300  # a confirmation lives 5 minutes

    def __init__(self, db, settings: dict | None, log=None,
                 project_root=None, config_path=None, projects_path=None,
                 tagger=None, reports_dir=None, delete_protector=None):
        self.db = db
        self.settings = {**DEFAULT_TELEGRAM, **(settings or {})}
        self.log = log or _log
        self.project_root = Path(project_root) if project_root else None
        self.config_path = Path(config_path) if config_path else None
        self.projects_path = Path(projects_path) if projects_path else None
        self.tagger = tagger
        self.delete_protector = delete_protector
        self.reports_dir = (Path(reports_dir) if reports_dir else
                            (self.project_root / "reports"
                             if self.project_root else Path("reports")))

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

    def send_markup(self, text: str, reply_markup: dict) -> bool:
        """Send one message with an inline keyboard. Never raises."""
        if not self.configured:
            return False
        result = _post(self.token, "sendMessage", {
            "chat_id": self.chat_id, "text": text[:4000],
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        })
        return bool(result and result.get("ok"))

    # -- mute --------------------------------------------------------
    def muted(self) -> bool:
        """True while alerts + digests are silenced (/mute)."""
        raw = self.db.get_state("telegram_muted_until") if self.db else None
        if not raw:
            return False
        if raw == "forever":
            return True
        try:
            until = datetime.fromisoformat(raw)
            if until.tzinfo is not None:
                until = until.replace(tzinfo=None)
            return until > datetime.now()
        except Exception:
            return False

    def mute(self, minutes: int | None = None) -> str:
        if minutes is None:
            self.db.set_state("telegram_muted_until", "forever")
            return "🔕 অ্যালার্ট আর ডাইজেস্ট চুপ করে দেওয়া হয়েছে। /unmute লিখলে আবার চালু হবে।"
        until = datetime.now() + timedelta(minutes=minutes)
        self.db.set_state("telegram_muted_until", until.isoformat(timespec="seconds"))
        return (f"🔕 অ্যালার্ট আর ডাইজেস্ট {minutes} মিনিটের জন্য চুপ। "
                f"({until.strftime('%H:%M')} পর্যন্ত)")

    def unmute(self) -> str:
        self.db.set_state("telegram_muted_until", "")
        return "🔔 অ্যালার্ট আর ডাইজেস্ট আবার চালু হয়েছে।"

    # -- pause / resume ----------------------------------------------
    def pause_audit(self) -> str:
        if not self.project_root:
            return "⏸ বিরতি দেওয়া যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।"
        try:
            (self.project_root / "PAUSE").write_text("", encoding="utf-8")
            return ("⏸ অডিট বন্ধ রাখা হয়েছে — নতুন কাজ আর রেকর্ড হবে না।\n"
                    "আবার চালু করতে /resume লিখুন।")
        except Exception as exc:
            return f"⏸ বিরতি দেওয়া যায়নি: {exc}"

    def resume_audit(self) -> str:
        if not self.project_root:
            return "▶️ চালু করা যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।"
        try:
            (self.project_root / "PAUSE").unlink(missing_ok=True)
            return "▶️ অডিট আবার চালু হয়েছে — কাজ রেকর্ড হচ্ছে।"
        except Exception as exc:
            return f"▶️ চালু করা যায়নি: {exc}"

    # -- digest interval ---------------------------------------------
    def set_digest(self, minutes: int) -> str:
        """Change the digest interval (0 = off) and persist it to
        config.local.yaml so it survives a restart."""
        if not isinstance(minutes, int) or isinstance(minutes, bool) \
                or not (0 <= minutes <= 1440):
            return "🕐 /digest-এর সময় 0–1440 মিনিটের মধ্যে হতে হবে (0 = বন্ধ)।"
        self.settings["digest_minutes"] = minutes
        if self.config_path is not None:
            try:
                from .dashboard import save_settings

                save_settings(self.config_path,
                              {"telegram": {"digest_minutes": minutes}})
            except Exception as exc:
                return (f"🕐 ডাইজেস্ট {minutes} মিনিটে বদলানো হয়েছে এই "
                        f"চালু অবস্থায়, কিন্তু সেভ হয়নি ({exc}) — "
                        f"রিস্টার্টে আগেরটায় ফিরবে।")
        if minutes == 0:
            return "🕐 ঘণ্টার ডাইজেস্ট বন্ধ করে দেওয়া হয়েছে। /digest 60 লিখলে আবার চালু হবে।"
        return f"🕐 ডাইজেস্ট এখন প্রতি {minutes} মিনিটে আসবে।"

    # -- PC control ----------------------------------------------------
    def lock_pc(self) -> str:
        import sys

        if sys.platform != "win32":
            return "🔒 লক শুধু Windows PC-তে চলে।"
        try:
            import ctypes

            if not ctypes.windll.user32.LockWorkStation():
                return "🔒 লক করা যায়নি।"
            return "🔒 কম্পিউটার লক করে দেওয়া হয়েছে।"
        except Exception as exc:
            return f"🔒 লক করা যায়নি: {exc}"

    @staticmethod
    def _norm_app(name: str) -> str:
        name = (name or "").strip().lower()
        return name if name.endswith(".exe") else (name + ".exe" if name else "")

    def find_processes(self, app: str) -> tuple[list, str]:
        """Live processes whose exe name matches `app` (substring,
        case-insensitive). Returns (procs, error)."""
        try:
            import psutil
        except Exception:
            return [], "psutil পাওয়া যায়নি।"
        want = self._norm_app(app)
        if not want:
            return [], "অ্যাপের নাম দিন — যেমন /close chrome"
        if want in self._PROTECTED_APPS:
            return [], f"⛔ {want} বন্ধ করা যাবে না — সিস্টেমের জরুরি প্রসেস।"
        found = []
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    if want in (proc.info.get("name") or "").lower():
                        found.append(proc)
                except Exception:
                    continue
        except Exception as exc:
            return [], f"প্রসেস খোঁজা যায়নি: {exc}"
        return found, ""

    def _ask_confirm(self, label: str, action: str, args: dict) -> None:
        """Store a pending action and ask the owner to confirm it.

        The ✅/❌ buttons carry the pending action's token, so an old
        button can never fire a different, newer action (v12.3 bug:
        every button said plain "confirm:yes" and ran whatever was
        pending at tap time)."""
        token = self._set_pending(action, args)
        markup = {"inline_keyboard": [[
            {"text": "✅ হ্যাঁ, করো", "callback_data": f"confirm:yes:{token}"},
            {"text": "❌ না, বাতিল", "callback_data": f"confirm:no:{token}"},
        ]]}
        self.send_markup(f"{label}\n\nনিশ্চিত? “হ্যাঁ” লিখুন অথবা ✅ চাপুন। (৫ মিনিটের মধ্যে)",
                         markup)

    def request_close(self, app: str) -> None:
        procs, err = self.find_processes(app)
        if err:
            self.send(err)
            return
        if not procs:
            self.send(f"❌ {self._norm_app(app)} নামে কোনো চলমান অ্যাপ পাওয়া যায়নি।")
            return
        names = sorted({(p.info.get("name") or "?") for p in procs})
        self._ask_confirm(
            f"❌ {', '.join(names)} — {len(procs)}টি প্রসেস বন্ধ করব?\n"
            "সেভ না করা কাজ থাকলে হারিয়ে যেতে পারে।",
            "close", {"app": app})

    def request_freeze(self, app: str, freeze: bool) -> None:
        import sys

        if sys.platform != "win32":
            self.send("🧊 স্থগিত রাখা শুধু Windows PC-তে চলে।")
            return
        procs, err = self.find_processes(app)
        if err:
            self.send(err)
            return
        if not procs:
            self.send(f"❌ {self._norm_app(app)} নামে কোনো চলমান অ্যাপ পাওয়া যায়নি।")
            return
        verb = "স্থগিত রাখব" if freeze else "আবার চালু করব"
        names = sorted({(p.info.get("name") or "?") for p in procs})
        self._ask_confirm(
            f"🧊 {', '.join(names)} — {len(procs)}টি প্রসেস {verb}?",
            "freeze" if freeze else "unfreeze", {"app": app})

    @staticmethod
    def _nt_suspend(pid: int, suspend: bool) -> bool:
        """NtSuspendProcess / NtResumeProcess via ctypes (Windows)."""
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            ntdll = ctypes.windll.ntdll
            handle = kernel32.OpenProcess(0x0800, False, pid)  # SUSPEND_RESUME
            if not handle:
                return False
            try:
                fn = ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess
                return fn(handle) == 0
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    def _do_close(self, app: str) -> str:
        procs, err = self.find_processes(app)
        if err:
            return err
        if not procs:
            return f"❌ {self._norm_app(app)} এখন আর চলছে না।"
        closed = 0
        for proc in procs:
            try:
                proc.terminate()
                closed += 1
            except Exception:
                continue
        return f"❌ {self._norm_app(app)} বন্ধ করা হয়েছে ({closed}/{len(procs)}টি প্রসেস)।"

    def _do_freeze(self, app: str, freeze: bool) -> str:
        procs, err = self.find_processes(app)
        if err:
            return err
        if not procs:
            return f"❌ {self._norm_app(app)} এখন আর চলছে না।"
        done = sum(1 for p in procs
                   if self._nt_suspend(int(p.info.get("pid") or p.pid), freeze))
        verb = "স্থগিত রাখা হয়েছে" if freeze else "আবার চালু করা হয়েছে"
        return f"🧊 {self._norm_app(app)} {verb} ({done}/{len(procs)}টি প্রসেস)।"

    # -- data management -----------------------------------------------
    def request_clear(self, date: str) -> None:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            self.send("🗑 তারিখটা YYYY-MM-DD আকারে দিন — যেমন /clear 2026-11-24")
            return
        self._ask_confirm(
            f"🗑 {date} তারিখের সব রেকর্ড (অ্যাপ, ব্রাউজার, ফাইল, ট্রান্সফার, অ্যালার্ট) "
            "মুছে ফেলব? রিপোর্ট ফাইলগুলোও মুছে যাবে।",
            "clear", {"date": date})

    def _do_clear(self, date: str) -> str:
        try:
            counts = self.db.delete_day(date)
        except Exception as exc:
            return f"🗑 মোছা যায়নি: {exc}"
        total = sum(counts.values())
        try:
            for path in self.reports_dir.glob(f"{date}*"):
                path.unlink()
        except Exception:
            pass
        return f"🗑 {date} তারিখের {total}টি রেকর্ড মুছে ফেলা হয়েছে।"

    def request_reset(self) -> None:
        self._ask_confirm(
            "♻️ সব দিনের সব ডেটা মুছে ফেলব? মোছার আগে এখনই একটা ব্যাকআপ "
            "নেওয়া হবে। প্রজেক্টের নাম আর সেটিংস থাকবে, শুধু রেকর্ড করা "
            "কাজের হিসাব মুছে যাবে।",
            "reset", {})

    def _do_reset(self) -> str:
        if self.project_root is None:
            return "♻️ রিসেট করা যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।"
        try:
            from .backup import make_backup

            dest = make_backup(self.db.db_path, self.project_root)
            backup_note = f"ব্যাকআপ নেওয়া হয়েছে: {dest.name}"
        except Exception as exc:
            return f"♻️ ব্যাকআপ নেওয়া যায়নি, তাই রিসেট করা হয়নি: {exc}"
        try:
            counts = self.db.reset_data()
        except Exception as exc:
            return f"♻️ রিসেট করা যায়নি: {exc} ({backup_note})"
        try:
            for path in self.reports_dir.glob("*"):
                if path.is_file():
                    path.unlink()
        except Exception:
            pass
        return (f"♻️ সব ডেটা মুছে ফেলা হয়েছে ({sum(counts.values())}টি রেকর্ড)।\n"
                f"{backup_note}")

    def backup_now(self) -> str:
        if self.project_root is None:
            return "💾 ব্যাকআপ নেওয়া যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।"
        try:
            from .backup import make_backup

            dest = make_backup(self.db.db_path, self.project_root)
            return f"💾 ব্যাকআপ নেওয়া হয়েছে: {dest.name}"
        except Exception as exc:
            return f"💾 ব্যাকআপ নেওয়া যায়নি: {exc}"

    def set_goal(self, project: str, minutes: int) -> str:
        """Set a project's daily goal (projects.yaml + tagger + DB)."""
        if not isinstance(minutes, int) or isinstance(minutes, bool) \
                or not (0 <= minutes <= 1440):
            return "🎯 লক্ষ্য 0–1440 মিনিটের মধ্যে হতে হবে।"
        if self.projects_path is None:
            return "🎯 লক্ষ্য বদলানো যাচ্ছে না — projects.yaml-এর অবস্থান জানা নেই।"
        try:
            from .projects import update_project_goal

            result = update_project_goal(self.projects_path, project, minutes)
        except FileNotFoundError:
            return "🎯 projects.yaml পাওয়া যায়নি।"
        except Exception as exc:
            return f"🎯 লক্ষ্য বদলানো যায়নি: {exc}"
        # Keep the live tagger + DB in step with the file.
        try:
            if self.tagger is not None:
                for p in self.tagger.projects:
                    if p.name.lower() == project.lower():
                        p.daily_goal_minutes = minutes
                        project = p.name
                        break
                else:
                    from .projects import Project

                    self.tagger.projects.append(
                        Project(name=project, daily_goal_minutes=minutes))
                    self.tagger.by_name[project] = self.tagger.projects[-1]
            self.db.upsert_project(project, daily_goal_minutes=minutes)
        except Exception as exc:
            self.log.warning("Telegram /goal DB sync failed: %s", exc)
        verb = "বদলানো হয়েছে" if result == "updated" else "নতুন প্রজেক্ট হিসেবে যোগ করা হয়েছে"
        return (f"🎯 {project}-এর দৈনিক লক্ষ্য {verb}: "
                f"{_fmt_min(minutes * 60) if minutes else 'কোনো লক্ষ্য নেই'}।")

    def send_report(self, day: str | None = None) -> None:
        from .utils import today_str

        day = day or today_str()
        try:
            from .summary import save_daily_summary

            _, path = save_daily_summary(self.db, day, self.reports_dir,
                                         tagger=self.tagger)
        except Exception as exc:
            self.send(f"📄 রিপোর্ট বানানো যায়নি: {exc}")
            return
        if send_document(self.token, self.chat_id, path,
                         caption=f"DeskAudit — {day} দিনের রিপোর্ট"):
            self.send(f"📄 {day} দিনের রিপোর্ট পাঠিয়ে দেওয়া হয়েছে।")
        else:
            self.send("📄 রিপোর্ট পাঠানো যায়নি — টোকেন/চ্যাট দেখে নিন।")

    # -- screenshots -----------------------------------------------------
    def send_screenshot(self) -> None:
        """Capture the PC's screen and send it as a Telegram photo.
        On demand only, and only to the owner's chat — a screenshot
        shows screen content, so it is never taken automatically."""
        import sys
        from datetime import datetime as _dt

        if sys.platform != "win32":
            self.send("📸 স্ক্রিনশট শুধু Windows PC-তে চলে।")
            return
        if self.project_root is None:
            self.send("📸 স্ক্রিনশট নেওয়া যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।")
            return
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        path = self.project_root / "data" / f"shot-{stamp}.png"
        try:
            from .screenshot import capture_screen

            capture_screen(path)
        except Exception as exc:
            self.send(f"📸 স্ক্রিনশট নেওয়া যায়নি: {exc}")
            return
        try:
            if send_photo(self.token, self.chat_id, path,
                          caption=f"DeskAudit — স্ক্রিনশট {stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"):
                self.send("📸 স্ক্রিনশট পাঠিয়ে দেওয়া হয়েছে।")
            else:
                self.send("📸 স্ক্রিনশট পাঠানো যায়নি — টোকেন/চ্যাট দেখে নিন।")
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    # -- browser control -------------------------------------------------
    _BROWSER_EXES = ["chrome.exe", "msedge.exe", "brave.exe",
                     "firefox.exe", "opera.exe", "vivaldi.exe"]

    def request_close_browsers(self) -> None:
        found = []
        for exe in self._BROWSER_EXES:
            procs, _ = self.find_processes(exe)
            found += procs
        if not found:
            self.send("🌐 কোনো ব্রাউজার চলছে না।")
            return
        names = sorted({(p.info.get("name") or "?") for p in found})
        self._ask_confirm(
            f"🌐 সব ব্রাউজার বন্ধ করব? ({', '.join(names)} — {len(found)}টি প্রসেস)",
            "close_browsers", {})

    def _do_close_browsers(self) -> str:
        closed = 0
        total = 0
        for exe in self._BROWSER_EXES:
            procs, _ = self.find_processes(exe)
            total += len(procs)
            for proc in procs:
                try:
                    proc.terminate()
                    closed += 1
                except Exception:
                    continue
        if total == 0:
            return "🌐 কোনো ব্রাউজার চলছে না।"
        return f"🌐 সব ব্রাউজার বন্ধ করা হয়েছে ({closed}/{total}টি প্রসেস)।"

    def request_block(self, domain: str) -> None:
        self._ask_confirm(f"🌐 {domain} এই PC-তে ব্লক করব?",
                          "block", {"domain": domain})

    def request_unblock(self, domain: str) -> None:
        self._ask_confirm(f"🌐 {domain}-এর ব্লক তুলে দেব?",
                          "unblock", {"domain": domain})

    def _do_block(self, domain: str) -> str:
        try:
            return block_domain(domain)
        except Exception as exc:
            return f"🌐 ব্লক করা যায়নি: {exc}"

    def _do_unblock(self, domain: str) -> str:
        try:
            return unblock_domain(domain)
        except Exception as exc:
            return f"🌐 ব্লক তোলা যায়নি: {exc}"

    # -- screen blank ------------------------------------------------------
    def set_screen(self, color: str) -> str:
        """Show a fullscreen white/black overlay, or lift it."""
        from .screen_blank import blank_status, start_blank, stop_blank

        if color == "off":
            was_active, msg = stop_blank()
            if msg == "windows_only":
                return "⬜ স্ক্রিন সাদা/কালো করা শুধু Windows PC-তে চলে।"
            if not was_active:
                return "⬜ স্ক্রিন এখন এমনিতেই খোলা আছে।"
            return "⬜ স্ক্রিন আবার খুলে দেওয়া হয়েছে।"
        ok, msg = start_blank(color)
        if msg == "windows_only":
            return "⬜ স্ক্রিন সাদা/কালো করা শুধু Windows PC-তে চলে।"
        if not ok:
            return f"⬜ স্ক্রিন {color} করা যায়নি ({msg})।"
        name = "সাদা" if color == "white" else "কালো"
        return (f"⬜ পুরো স্ক্রিন {name} করে দেওয়া হয়েছে।\n"
                f"খুলতে /screen off লিখুন।")

    # -- shutdown / restart --------------------------------------------------
    def request_shutdown(self, restart: bool) -> None:
        verb = "রিস্টার্ট" if restart else "বন্ধ"
        self._ask_confirm(
            f"⏻ কম্পিউটার {verb} করব? ৬০ সেকেন্ড পরে {verb} হবে — "
            "এর মধ্যে /abort লিখলে বাতিল হবে।",
            "restart" if restart else "shutdown", {})

    def _do_shutdown(self, restart: bool) -> str:
        from .utils import run_hidden

        if sys.platform != "win32":
            return "⏻ PC বন্ধ/রিস্টার্ট শুধু Windows PC-তে চলে।"
        verb = "রিস্টার্ট" if restart else "বন্ধ"
        try:
            run_hidden(["shutdown", "/r" if restart else "/s", "/t", "60",
                        "/c", f"DeskAudit: {verb} হচ্ছে"],
                       capture_output=True, timeout=20)
        except Exception as exc:
            return f"⏻ {verb} করা যায়নি: {exc}"
        return (f"⏻ কম্পিউটার ৬০ সেকেন্ড পরে {verb} হবে।\n"
                f"বাতিল করতে /abort লিখুন।")

    def abort_shutdown(self) -> str:
        from .utils import run_hidden

        if sys.platform != "win32":
            return "⏻ PC বন্ধ/রিস্টার্ট শুধু Windows PC-তে চলে।"
        try:
            run_hidden(["shutdown", "/a"], capture_output=True, timeout=20)
        except Exception as exc:
            return f"⏻ বাতিল করা যায়নি: {exc}"
        return "⏻ বন্ধ/রিস্টার্ট বাতিল করা হয়েছে।"

    # -- pending confirmations ------------------------------------------
    def _get_pending(self) -> dict | None:
        raw = self.db.get_state("telegram_pending") if self.db else None
        if not raw:
            return None
        try:
            pending = json.loads(raw)
        except Exception:
            return None
        if time.time() > float(pending.get("expires", 0)):
            self._clear_pending()
            return None
        return pending

    def _set_pending(self, action: str, args: dict) -> str:
        """Store a pending action with a one-time token. Returns the
        token — the ✅/❌ buttons carry it, so only the button shown
        for THIS action can confirm it."""
        import secrets

        token = secrets.token_hex(4)
        payload = {"action": action, "args": args, "token": token,
                   "expires": time.time() + self._PENDING_TTL}
        self.db.set_state("telegram_pending", json.dumps(payload))
        return token

    def _clear_pending(self) -> None:
        try:
            self.db.set_state("telegram_pending", "")
        except Exception:
            pass

    def _run_pending(self, token: str | None = None,
                     *, from_button: bool = False) -> str:
        pending = self._get_pending()
        if not pending:
            return "⌛ কোনো অপেক্ষমাণ কাজ নেই (অথবা সময় শেষ হয়ে গেছে)। আবার কমান্ড দিন।"
        if from_button and pending.get("token") != token:
            # A button from an older confirmation (or a bare old-format
            # "confirm:yes" with no token at all): it must never run
            # the action that is pending NOW.
            return "⌛ এই নিশ্চিতকরণটি আর কার্যকর নয় — আবার কমান্ড দিন।"
        self._clear_pending()
        action = pending.get("action", "")
        args = pending.get("args", {}) or {}
        if action == "close":
            return self._do_close(str(args.get("app", "")))
        if action == "freeze":
            return self._do_freeze(str(args.get("app", "")), True)
        if action == "unfreeze":
            return self._do_freeze(str(args.get("app", "")), False)
        if action == "close_browsers":
            return self._do_close_browsers()
        if action == "block":
            return self._do_block(str(args.get("domain", "")))
        if action == "unblock":
            return self._do_unblock(str(args.get("domain", "")))
        if action == "clear":
            return self._do_clear(str(args.get("date", "")))
        if action == "reset":
            return self._do_reset()
        if action == "shutdown":
            return self._do_shutdown(False)
        if action == "restart":
            return self._do_shutdown(True)
        if action == "delete_protect":
            # হ্যাঁ = the quarantined copy is deleted for good.
            return self._do_delete_purge(int(args.get("qid", 0)))
        return "⌛ অচেনা অপেক্ষমাণ কাজ — বাতিল করা হয়েছে।"

    def _run_pending_no(self, token: str | None = None,
                        *, from_button: bool = False) -> str:
        """The 'no' branch of a pending confirmation. For most actions
        no means cancel — but for delete protection it means RESTORE
        the file to its original place (v12.8)."""
        pending = self._get_pending()
        if not pending:
            return "⌛ কোনো অপেক্ষমাণ কাজ নেই (অথবা সময় শেষ হয়ে গেছে)। আবার কমান্ড দিন।"
        if from_button and pending.get("token") != token:
            return "⌛ এই নিশ্চিতকরণটি আর কার্যকর নয় — আবার কমান্ড দিন।"
        self._clear_pending()
        action = pending.get("action", "")
        args = pending.get("args", {}) or {}
        if action == "delete_protect":
            # না = do NOT delete — unhide and restore the file.
            return self._do_delete_restore(int(args.get("qid", 0)))
        return "❌ বাতিল করা হয়েছে।"

    # -- outbound --------------------------------------------------------
    def send_digest(self, day: str | None = None) -> bool:
        from .utils import today_str

        if self.muted():
            return False
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
        if self.muted():
            return False
        text = (f"🚨 DeskAudit অ্যালার্ট [{alert.severity}]\n"
                f"{alert.title}\n{alert.message}").strip()
        return self.send(text)

    def notify_new_app(self, infos: list[dict]) -> int:
        """Immediate Telegram notice for each newly opened program.

        Metadata only: which program, its exe path, its open window
        title, when it opened, and its project tag. Never what anyone
        types or writes inside it. Returns how many were sent.
        Respects /mute, like the other immediate notices.
        """
        if not infos or self.muted():
            return 0
        sent = 0
        for info in infos:
            project = "Untagged"
            try:
                if self.tagger is not None:
                    project, _tags = self.tagger.tag_app(
                        str(info.get("name", "")),
                        str(info.get("exe", "")),
                        str(info.get("title", "")))
            except Exception:
                project = "Untagged"
            text = build_new_app_text(info, project)
            if self.send(text):
                sent += 1
        return sent

    # -- delete protection (v12.8) --------------------------------------
    def notify_deleted(self, item: dict) -> bool:
        """Immediate notice for one deleted file that was quarantined,
        with the owner's choice: হ্যাঁ = purge for good, না = restore
        to its original place. The buttons carry a one-time token, so
        an old button can never decide a different file."""
        if not item:
            return False
        qid = int(item.get("id") or 0)
        if not qid:
            return False
        token = self._set_pending("delete_protect", {"qid": qid})
        markup = {"inline_keyboard": [[
            {"text": "✅ হ্যাঁ, ডিলিট করো", "callback_data": f"confirm:yes:{token}"},
            {"text": "↩️ না, ফিরিয়ে দাও", "callback_data": f"confirm:no:{token}"},
        ]]}
        return self.send_markup(
            build_delete_text(item) + "\n\n(৫ মিনিটের মধ্যে সিদ্ধান্ত নিন — "
            "না জানালে ফাইলটা সুরক্ষায়ই থাকবে।)",
            markup)

    def _do_delete_purge(self, qid: int) -> str:
        if self.delete_protector is None:
            return "🗑 ডিলিট সুরক্ষা এই PC-তে চালু নেই।"
        try:
            return self.delete_protector.purge(qid)
        except Exception as exc:
            return f"🗑 মোছা যায়নি: {exc}"

    def _do_delete_restore(self, qid: int) -> str:
        if self.delete_protector is None:
            return "🗑 ডিলিট সুরক্ষা এই PC-তে চালু নেই।"
        try:
            return self.delete_protector.restore(qid)
        except Exception as exc:
            return f"🗑 ফেরানো যায়নি: {exc}"

    # -- command routing ---------------------------------------------------
    def handle_text(self, text: str) -> None:
        """Answer one message from the owner. Unknown text gets the
        help hint; a bare হ্যাঁ/না answers a pending confirmation."""
        from .utils import today_str

        text = (text or "").strip()
        if not text:
            return
        low = text.lower()
        if low in self._CONFIRM_WORDS:
            self.send(self._run_pending())
            return
        if low in self._CANCEL_WORDS:
            pending = self._get_pending()
            if pending is not None and pending.get("action") == "delete_protect":
                # না on a deleted file means: do NOT delete — restore it.
                self.send(self._run_pending_no())
            elif pending:
                self._clear_pending()
                self.send("❌ বাতিল করা হয়েছে।")
            else:
                self.send("❌ বাতিল করার মতো কিছু নেই।")
            return
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]
        # A new command means the owner has moved on: drop any pending
        # confirmation from an earlier command (v12.3 bug: a stale
        # pending could later fire from an unrelated "হ্যাঁ"). The
        # command that SETS a pending runs after this point, so it is
        # never cleared by its own arrival.
        if cmd.startswith("/"):
            self._clear_pending()
        day = today_str()
        if cmd in ("/start", "/help"):
            self.send(HELP_TEXT)
        elif cmd == "/menu":
            self.send_markup("📋 DeskAudit মেনু — চাপলেই কাজ হবে:",
                             menu_keyboard())
        elif cmd == "/status":
            self.send_status(day)
        elif cmd == "/today":
            self.send_today(day)
        elif cmd == "/week":
            self.send_long(build_week_text(self.db, day))
        elif cmd == "/apps":
            self.send_long(build_apps_text(self.db, day))
        elif cmd == "/projects":
            self.send_long(build_projects_text(self.db, day))
        elif cmd == "/transfers":
            self.send_long(build_transfers_text(self.db, day))
        elif cmd == "/deleted":
            self.send_long(build_deleted_text(self.db, day))
        elif cmd == "/alerts":
            self.send_long(build_alerts_text(self.db, day))
        elif cmd == "/files":
            self.send_long(build_files_text(self.db, day))
        elif cmd == "/search":
            if not args:
                self.send("🔎 /search-এর পরে শব্দ দিন — যেমন /search রিপোর্ট")
            else:
                self.send_long(build_search_text(self.db, day, " ".join(args)))
        elif cmd == "/report":
            self.send_report(day)
        elif cmd in ("/screenshot", "/shot"):
            self.send_screenshot()
        elif cmd == "/browser":
            if not args:
                self.send_long(build_browser_text(self.db, day))
            elif args[0].lower() == "close":
                self.request_close_browsers()
            elif args[0].lower() == "block":
                if len(args) < 2:
                    self.send("🌐 কোন ডোমেইন ব্লক করব? যেমন /browser block facebook.com")
                else:
                    self.request_block(args[1])
            elif args[0].lower() == "unblock":
                if len(args) < 2:
                    self.send("🌐 কোন ডোমেইনের ব্লক তুলব? যেমন /browser unblock facebook.com")
                else:
                    self.request_unblock(args[1])
            else:
                self.send("🌐 ব্যবহার: /browser, /browser close, /browser block <ডোমেইন>, /browser unblock <ডোমেইন>")
        elif cmd == "/screen":
            if not args:
                from .screen_blank import blank_status

                current = blank_status()
                state = f"এখন {current} আছে" if current else "এখন খোলা আছে"
                self.send(f"⬜ স্ক্রিন {state}। বদলাতে: /screen white, /screen black অথবা /screen off")
            elif args[0].lower() in ("white", "সাদা"):
                self.send(self.set_screen("white"))
            elif args[0].lower() in ("black", "কালো"):
                self.send(self.set_screen("black"))
            elif args[0].lower() in ("off", "খোলা"):
                self.send(self.set_screen("off"))
            else:
                self.send("⬜ ব্যবহার: /screen white, /screen black অথবা /screen off")
        elif cmd == "/shutdown":
            self.request_shutdown(restart=False)
        elif cmd == "/restart":
            self.request_shutdown(restart=True)
        elif cmd == "/abort":
            self.send(self.abort_shutdown())
        elif cmd == "/pause":
            self.send(self.pause_audit())
        elif cmd == "/resume":
            self.send(self.resume_audit())
        elif cmd == "/mute":
            minutes = None
            if args:
                try:
                    minutes = int(args[0])
                    if not (1 <= minutes <= 1440):
                        raise ValueError
                except ValueError:
                    self.send("🔕 /mute-এর পরে মিনিট দিন (1–1440) — যেমন /mute 60, অথবা শুধু /mute")
                    return
            self.send(self.mute(minutes))
        elif cmd == "/unmute":
            self.send(self.unmute())
        elif cmd == "/digest":
            if not args:
                current = self.settings.get("digest_minutes", 60)
                state = "বন্ধ" if not current else f"প্রতি {current} মিনিটে"
                self.send(f"🕐 ডাইজেস্ট এখন {state} আসে। বদলাতে: /digest 30 অথবা /digest off")
            elif args[0].lower() in ("off", "বন্ধ"):
                self.send(self.set_digest(0))
            else:
                try:
                    self.send(self.set_digest(int(args[0])))
                except ValueError:
                    self.send("🕐 /digest-এর পরে মিনিট দিন — যেমন /digest 30 অথবা /digest off")
        elif cmd == "/lock":
            self.send(self.lock_pc())
        elif cmd == "/close":
            if not args:
                self.send("❌ কোন অ্যাপ বন্ধ করব? যেমন /close chrome")
            else:
                self.request_close(" ".join(args))
        elif cmd == "/freeze":
            if not args:
                self.send("🧊 কোন অ্যাপ স্থগিত রাখব? যেমন /freeze chrome")
            else:
                self.request_freeze(" ".join(args), True)
        elif cmd == "/unfreeze":
            if not args:
                self.send("🧊 কোন অ্যাপ আবার চালু করব? যেমন /unfreeze chrome")
            else:
                self.request_freeze(" ".join(args), False)
        elif cmd == "/clear":
            date = args[0] if args else day
            self.request_clear(date)
        elif cmd == "/reset":
            self.request_reset()
        elif cmd == "/backup":
            self.send(self.backup_now())
        elif cmd == "/goal":
            if len(args) < 2:
                self.send("🎯 ব্যবহার: /goal <প্রজেক্ট> <মিনিট> — যেমন /goal Office 240")
            else:
                try:
                    minutes = int(args[-1])
                except ValueError:
                    self.send("🎯 মিনিট সংখ্যায় দিন — যেমন /goal Office 240")
                    return
                self.send(self.set_goal(" ".join(args[:-1]), minutes))
        else:
            self.send("❓ এই কমান্ডটা চিনলাম না। সব কমান্ড দেখতে /help লিখুন।")

    def handle_callback(self, data: str, callback_query_id: str) -> None:
        """Answer one inline-button tap from /menu or a confirmation."""
        from .utils import today_str

        answer_callback(self.token, callback_query_id)
        day = today_str()
        if data.startswith("confirm:"):
            # confirm:yes:<token> / confirm:no:<token> — the token must
            # match the pending action, or this is a stale button.
            bits = data.split(":")
            verdict = bits[1] if len(bits) > 1 else ""
            token = bits[2] if len(bits) > 2 else None
            if verdict == "yes":
                self.send(self._run_pending(token, from_button=True))
            elif verdict == "no":
                pending = self._get_pending()
                if pending is not None and token is not None \
                        and pending.get("token") == token:
                    if pending.get("action") == "delete_protect":
                        # না on a deleted file = restore it, not cancel.
                        self.send(self._run_pending_no(token, from_button=True))
                    else:
                        self._clear_pending()
                        self.send("❌ বাতিল করা হয়েছে।")
                else:
                    self.send("⌛ এই নিশ্চিতকরণটি আর কার্যকর নয় — আবার কমান্ড দিন।")
            else:
                self.send("❓ অচেনা বোতাম — /help লিখুন।")
            return
        if data.startswith("m:"):
            # A menu tap is a new action: drop any stale pending first.
            self._clear_pending()
            cmd = data[2:]
            if cmd == "status":
                self.send_status(day)
            elif cmd == "today":
                self.send_today(day)
            elif cmd == "week":
                self.send_long(build_week_text(self.db, day))
            elif cmd == "apps":
                self.send_long(build_apps_text(self.db, day))
            elif cmd == "projects":
                self.send_long(build_projects_text(self.db, day))
            elif cmd == "transfers":
                self.send_long(build_transfers_text(self.db, day))
            elif cmd == "deleted":
                self.send_long(build_deleted_text(self.db, day))
            elif cmd == "alerts":
                self.send_long(build_alerts_text(self.db, day))
            elif cmd == "files":
                self.send_long(build_files_text(self.db, day))
            elif cmd == "report":
                self.send_report(day)
            elif cmd == "shot":
                self.send_screenshot()
            elif cmd == "browser":
                self.send_long(build_browser_text(self.db, day))
            elif cmd == "pause":
                self.send(self.pause_audit())
            elif cmd == "resume":
                self.send(self.resume_audit())
            elif cmd == "help":
                self.send(HELP_TEXT)
            else:
                self.send("❓ এই বোতামটা চিনলাম না। /help লিখুন।")
        else:
            self.send("❓ অচেনা বোতাম — /help লিখুন।")

    def poll_commands(self) -> int:
        """Fetch new updates and answer commands. Returns how many
        updates were handled. Offset is kept in kv_state so a restart
        does not re-answer old commands. Handles both plain messages
        and inline-button taps (callback_query)."""
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
                cb = upd.get("callback_query") or {}
                if cb:
                    chat = str(((cb.get("from") or {}).get("id", "")))
                    if chat != self.chat_id:
                        continue  # only the owner's chat may command the bot
                    self.handle_callback(str(cb.get("data", "")),
                                         str(cb.get("id", "")))
                    handled += 1
                    continue
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = str((msg.get("chat") or {}).get("id", ""))
                if chat != self.chat_id:
                    continue  # only the owner's chat may command the bot
                if not text:
                    continue
                self.handle_text(text)
                handled += 1
            except Exception as exc:
                self.log.warning("Telegram command failed: %s", exc)
        if max_id is not None:
            try:
                self.db.set_state("telegram_update_offset", str(int(max_id) + 1))
            except Exception:
                pass
        return handled
