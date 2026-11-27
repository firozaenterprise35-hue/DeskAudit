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
  /explore [folder] — browse drives/folders from Telegram: copy,
    move, send (files, or folders as a zip), hide, rename, make a
    new folder, search, or delete into the delete-protect
    quarantine. /hidden — list hidden files and restore them.

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


def send_photo_id(token: str, chat_id: str, path: str | Path,
                  caption: str = "", reply_markup: dict | None = None) -> int | None:
    """Send a photo and return its message id (the live view edits
    that one message instead of sending a new photo on every
    refresh). None on any failure. Never raises."""
    if not token or not chat_id:
        return None
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        _log.warning("Telegram sendPhoto: cannot read %s: %s", path, exc)
        return None
    fields: dict = {"chat_id": chat_id, "caption": caption[:1024]}
    if reply_markup is not None:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    result = _post_multipart(token, "sendPhoto", fields,
                             "photo", Path(path).name, data)
    if result and result.get("ok"):
        try:
            return int((result.get("result") or {}).get("message_id") or 0) or None
        except Exception:
            return None
    return None


def edit_photo(token: str, chat_id: str, message_id: int, path: str | Path,
               caption: str = "", reply_markup: dict | None = None) -> bool:
    """Replace the photo of a message the bot sent earlier
    (editMessageMedia) — the live view refreshes one message in
    place instead of sending a new photo every 10 seconds. The
    caption and the stop button travel with the new photo. The
    media field is a JSON string pointing at the attached file
    (attach://photo). Never raises."""
    if not token or not chat_id or not message_id:
        return False
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        _log.warning("Telegram editMessageMedia: cannot read %s: %s", path, exc)
        return False
    media = {"type": "photo", "media": "attach://photo",
             "caption": caption[:1024]}
    fields: dict = {"chat_id": chat_id, "message_id": str(message_id),
                    "media": json.dumps(media, ensure_ascii=False)}
    if reply_markup is not None:
        fields["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    result = _post_multipart(token, "editMessageMedia", fields,
                             "photo", Path(path).name, data)
    return bool(result and result.get("ok"))


def send_video(token: str, chat_id: str, path: str | Path,
               caption: str = "") -> bool:
    """Send a local video as a Telegram video (e.g. a screen
    recording). Returns True only when Telegram answered ok. Never
    raises. Telegram accepts at most 50 MB per file."""
    if not token or not chat_id:
        return False
    try:
        data = Path(path).read_bytes()
    except Exception as exc:
        _log.warning("Telegram sendVideo: cannot read %s: %s", path, exc)
        return False
    result = _post_multipart(token, "sendVideo",
                             {"chat_id": chat_id, "caption": caption[:1024]},
                             "video", Path(path).name, data,
                             timeout=300)
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
    # v12.14.2: the version, so a /status reply proves which build is
    # actually running (an old agent answers with its own version).
    from deskaudit import __version__

    lines.append("")
    lines.append(f"DeskAudit v{__version__}")
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
    lines.append("নিচের বোতামে সিদ্ধান্ত নিন — ✅ পারমেনেন্টলি মোছা, ↩️ ফিরিয়ে দেওয়া।")
    return "\n".join(lines)


def build_hidden_text(db, day: str | None = None) -> str:
    """Files the owner hid from Telegram (/hidden)."""
    from .utils import today_str

    day = day or today_str()
    try:
        held = db.hidden_held(limit=20)
        today_rows = db.hidden_for_date(day, limit=20)
    except Exception:
        held, today_rows = [], []
    lines = ["🫥 লুকানো ফাইল — টেলিগ্রাম থেকে হাইড করা", ""]
    if not held:
        lines.append("  (এখন কোনো ফাইল লুকানো নেই।)")
    else:
        lines.append(f"এখন লুকানো: {len(held)}টি")
        for row in held:
            size = f" ({_fmt_size(row['size_bytes'])})" if row["size_bytes"] else ""
            lines.append(f"  • #{row['id']} {_hhmm(row['hidden_at'])} "
                         f"{row['file_name']}{size}")
            lines.append(f"    ছিল: {(row['original_path'] or '').replace(chr(92), '/')}")
    restored = [r for r in today_rows if r["status"] == "restored"]
    if restored:
        lines.append("")
        lines.append("আজ ফিরিয়ে দেওয়া হয়েছে:")
        for row in restored[:10]:
            lines.append(f"  • #{row['id']} {row['file_name']}")
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
    "👋 DeskAudit বট — সব কমান্ড (বেশিরভাগের /menu-তে বোতাম আছে; "
    "/unfreeze আর /browser block|unblock লিখে দিতে হয়):\n"
    "📊 দেখা:\n"
    "  /status /today /week /apps /projects /transfers /alerts /files\n"
    "  🌐 /browser — কোন সাইটে কী চলছে | /browser close | /browser block <ডোমেইন> | /browser unblock <ডোমেইন>\n"
    "  📸 /screenshot — এখনকার স্ক্রিনের ছবি পাঠানো\n"
    "  🔴 /live — লাইভ স্ক্রিন দেখা (এক মেসেজে ১০ সেকেন্ডে তাজা ছবি, ১০ মিনিট পর্যন্ত) | /live stop — থামানো\n"
    "  🔎 /search <শব্দ> — আজ ফাইল/উইন্ডো/সার্চে খোঁজা\n"
    "  📄 /report — আজকের রিপোর্ট ফাইল হিসেবে পাঠানো\n"
    "  📂 /explore [ফোল্ডার] — ড্রাইভ/ফোল্ডার ঘুরে দেখা: ফাইল ও ফোল্ডার\n"
    "    কপি/মুভ/পাঠানো/হাইড, নাম বদলানো, নতুন ফোল্ডার, খোঁজা, মুছে ফেলা\n"
    "  🫥 /hidden — লুকানো ফাইল দেখা ও ফিরিয়ে দেওয়া\n"
    "  🗑 /deleted — ডিলিট হওয়া ফাইল, যা সুরক্ষায় লুকিয়ে আছে\n"
    "🎛️ নিয়ন্ত্রণ:\n"
    "  ⬜ /screen white | /screen black | /screen off — স্ক্রিন ঢাকা/খোলা\n"
    "  🎥 /record [মিনিট] — স্ক্রিন রেকর্ড (১/৩/৫/১০/১৫/২০/৩০ মিনিট, বা /record ৭) | /record stop — থামানো | /record status — অবস্থা\n"
    "  🔒 /lock — কম্পিউটার লক করা\n"
    "  ❌ /close <অ্যাপ> — চলমান অ্যাপ বন্ধ (নিশ্চিতকরণ লাগবে)\n"
    "  🧊 /freeze <অ্যাপ> — অ্যাপ স্থগিত | /unfreeze <অ্যাপ> — আবার চালু\n"
    "  ⏸ /pause — অডিট বন্ধ | ▶️ /resume — আবার চালু\n"
    "  🔕 /mute [মিনিট] — অ্যালার্ট+ডাইজেস্ট চুপ (সময় না দিলে চিরদিনের জন্য) | /unmute — আবার চালু\n"
    "  🕐 /digest <মিনিট|off> — ডাইজেস্টের সময় বদলানো\n"
    "  🎯 /goal <প্রজেক্ট> <মিনিট> — দৈনিক লক্ষ্য বদলানো\n"
    "💾 ডেটা ও PC:\n"
    "  💾 /backup — এখনই ব্যাকআপ নেওয়া\n"
    "  🗑 /clear [তারিখ] — এক দিনের ডেটা মোছা (নিশ্চিতকরণ লাগবে)\n"
    "  ⏻ /shutdown | /restart — PC বন্ধ/রিস্টার্ট (নিশ্চিতকরণ লাগবে) | /abort — বাতিল\n"
    "  ♻️ /reset — সব ডেটা মুছে নতুন শুরু, আগে ব্যাকআপ নেবে\n"
    "শুধু আপনার চ্যাট থেকেই কমান্ড চলে।"
)


def menu_keyboard() -> dict:
    """Inline keyboard for /menu — every feature, grouped so the
    owner can reach it with one tap.

    The safety rule (v12.2, kept in every version since): NO button
    executes a destructive action by itself. A button either shows
    something, asks what is needed (close/freeze/goal/clear/search
    need an argument), or opens the token-bound confirmation first
    (shutdown/restart/reset). Tapping a button can never delete,
    close or shut anything down on its own."""
    def btn(text, data):
        return {"text": text, "callback_data": data}

    return {"inline_keyboard": [
        # 📊 দেখা — read-only, one tap shows it.
        [btn("📊 বর্তমান অবস্থা", "m:status"), btn("📅 আজকের সারাংশ", "m:today")],
        [btn("📅 সপ্তাহ", "m:week"), btn("🖥️ অ্যাপ", "m:apps")],
        [btn("🎯 প্রজেক্ট", "m:projects"), btn("🔒 ট্রান্সফার", "m:transfers")],
        [btn("📁 ফাইল", "m:files"), btn("⚠️ অ্যালার্ট", "m:alerts")],
        [btn("🔎 খোঁজা", "m:search"), btn("📄 রিপোর্ট", "m:report")],
        [btn("🌐 ব্রাউজার", "m:browser"), btn("📸 স্ক্রিনশট", "m:shot")],
        [btn("🔴 লাইভ দেখা", "m:live")],
        [btn("📂 ফাইল ঘোরাঘুরি", "m:explore"), btn("🫥 লুকানো ফাইল", "m:hidden")],
        [btn("🗑️ মুছে যাওয়া ফাইল", "m:deleted"), btn("❓ সাহায্য", "m:help")],
        # 🎛️ নিয়ন্ত্রণ — one tap, or a prompt for what is needed.
        [btn("⬜ স্ক্রিন", "m:screen"), btn("🔒 কম্পিউটার লক", "m:lock")],
        [btn("🎥 স্ক্রিন রেকর্ড", "m:record")],
        [btn("❌ অ্যাপ বন্ধ", "m:close"), btn("🧊 অ্যাপ স্থগিত", "m:freeze")],
        [btn("⏸ বিরতি", "m:pause"), btn("▶️ আবার চালু", "m:resume")],
        [btn("🔕 চুপ করো", "m:mute"), btn("🔔 আবার চালু", "m:unmute")],
        [btn("🕐 ডাইজেস্ট", "m:digest"), btn("🎯 লক্ষ্য", "m:goal")],
        # 💾 ডেটা ও PC — backup runs at once; the rest ask first.
        [btn("💾 ব্যাকআপ", "m:backup"), btn("🗑 এক দিন মোছা", "m:clear")],
        [btn("⏻ PC বন্ধ", "m:shutdown"), btn("🔄 রিস্টার্ট", "m:restart")],
        [btn("🛑 বন্ধ বাতিল", "m:abort"), btn("♻️ সব রিসেট", "m:reset")],
    ]}


def screen_keyboard() -> dict:
    """Sub-menu for /screen: white / black / lift the overlay."""
    def btn(text, data):
        return {"text": text, "callback_data": data}

    return {"inline_keyboard": [
        [btn("⬜ সাদা করো", "m:screen_white"),
         btn("⬛ কালো করো", "m:screen_black")],
        [btn("🔓 খুলে দাও", "m:screen_off"),
         btn("🔙 মেনু", "m:menu")],
    ]}


def record_keyboard() -> dict:
    """Sub-menu for /record: pick a duration, or stop / check the
    recording that is already running (v12.14)."""
    def btn(text, data):
        return {"text": text, "callback_data": data}

    return {"inline_keyboard": [
        [btn("1 মিনিট", "m:record_1"), btn("3 মিনিট", "m:record_3")],
        [btn("5 মিনিট", "m:record_5"), btn("10 মিনিট", "m:record_10")],
        [btn("15 মিনিট", "m:record_15"), btn("20 মিনিট", "m:record_20")],
        [btn("30 মিনিট", "m:record_30"), btn("📊 অবস্থা", "m:record_status")],
        [btn("⏹️ থামাও", "m:record_stop"), btn("🔙 মেনু", "m:menu")],
    ]}


def live_keyboard() -> dict:
    """The one button under the live photo: stop the live view."""
    def btn(text, data):
        return {"text": text, "callback_data": data}

    return {"inline_keyboard": [
        [btn("⏹️ থামাও", "m:live_stop")],
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
                 tagger=None, reports_dir=None, delete_protector=None,
                 file_explorer=None):
        self.db = db
        self.settings = {**DEFAULT_TELEGRAM, **(settings or {})}
        self.log = log or _log
        self.project_root = Path(project_root) if project_root else None
        self.config_path = Path(config_path) if config_path else None
        self.projects_path = Path(projects_path) if projects_path else None
        self.tagger = tagger
        self.delete_protector = delete_protector
        self.file_explorer = file_explorer
        self._recorder = None  # created lazily: ScreenRecorder (v12.14)
        self._live_viewer = None  # created lazily: LiveViewer (v12.15)
        self.reports_dir = (Path(reports_dir) if reports_dir else
                            (self.project_root / "reports"
                             if self.project_root else Path("reports")))
        # v12.14: update_ids answered by THIS process. The stored offset
        # normally keeps an update from being answered twice, but if the
        # offset save ever fails (DB busy) the next poll re-fetches the
        # same updates — without this set the same command would be
        # answered a second time. Bounded: cleared once it grows past
        # 1000 entries (the offset is the real guard; this is the
        # safety net for a single process lifetime).
        self._handled_update_ids: set[int] = set()

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

    def send_video(self, path: str | Path, caption: str = "") -> bool:
        """Send a local video file to the owner's chat. Never raises."""
        if not self.configured:
            return False
        ok = send_video(self.token, self.chat_id, path, caption)
        if not ok:
            self.log.warning("Telegram sendVideo failed (check bot token / chat id).")
        return ok

    def send_file(self, path: str | Path, caption: str = "") -> bool:
        """Send a local file as a Telegram document. Never raises."""
        if not self.configured:
            return False
        ok = send_document(self.token, self.chat_id, path, caption)
        if not ok:
            self.log.warning("Telegram sendDocument failed (check bot token / chat id).")
        return ok

    def send_photo_id(self, path: str | Path, caption: str = "",
                      reply_markup: dict | None = None) -> int | None:
        """Send a photo and return its message id (the live view
        edits that one message). Never raises."""
        if not self.configured:
            return None
        message_id = send_photo_id(self.token, self.chat_id, path,
                                   caption, reply_markup)
        if message_id is None:
            self.log.warning("Telegram sendPhoto failed (check bot token / chat id).")
        return message_id

    def edit_photo(self, message_id: int, path: str | Path, caption: str = "",
                   reply_markup: dict | None = None) -> bool:
        """Swap a fresh photo into a message the bot sent earlier
        (the live view's refresh). Never raises."""
        if not self.configured:
            return False
        ok = edit_photo(self.token, self.chat_id, message_id, path,
                        caption, reply_markup)
        if not ok:
            self.log.warning("Telegram editMessageMedia failed (check bot token / chat id).")
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

    def send_markup_id(self, text: str, reply_markup: dict) -> int | None:
        """Like send_markup, but returns the sent message's id so the
        explorer can EDIT that one message instead of spamming a new
        message on every tap. Never raises."""
        if not self.configured:
            return None
        result = _post(self.token, "sendMessage", {
            "chat_id": self.chat_id, "text": text[:4000],
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        })
        if result and result.get("ok"):
            try:
                return int((result.get("result") or {}).get("message_id") or 0) or None
            except Exception:
                return None
        return None

    def edit_markup(self, message_id: int, text: str,
                    reply_markup: dict) -> bool:
        """Edit the text + keyboard of a message the bot sent earlier
        (the explorer's one browsing message). Never raises."""
        if not self.configured or not message_id:
            return False
        result = _post(self.token, "editMessageText", {
            "chat_id": self.chat_id, "message_id": message_id,
            "text": text[:4000], "disable_web_page_preview": True,
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
        """Live processes whose exe name matches `app` EXACTLY
        (case-insensitive, ".exe" optional). Returns (procs, error).

        Exact matching only (v12.13 audit): the old substring match
        let "/close vchost" match svchost.exe and "/close e" match
        chrome.exe, bypassing the system-process protection entirely
        — the confirmation step re-ran the same matcher, so it did
        not close the hole either."""
        try:
            import psutil
        except Exception:
            return [], "psutil পাওয়া যায়নি।"
        want = self._norm_app(app)
        if not want:
            return [], "অ্যাপের নাম দিন — যেমন /close chrome"
        stem = want[:-4] if want.endswith(".exe") else want
        if len(stem) < 3:
            return [], "অ্যাপের নাম খুব ছোট — পুরো নাম দিন, যেমন /close chrome"
        for prot in self._PROTECTED_APPS:
            pstem = prot[:-4] if prot.endswith(".exe") else prot
            if want == prot or stem == pstem or stem in pstem or pstem in stem:
                return [], f"⛔ {want} বন্ধ করা যাবে না — সিস্টেমের জরুরি প্রসেস।"
        found = []
        try:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    name = (proc.info.get("name") or "").lower()
                    if name == want or name == stem:
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
    # -- screen recording (v12.14) ----------------------------------
    @property
    def recorder(self):
        """The bot's ScreenRecorder, created on first use. None when
        the agent's folder is unknown (no place to keep frames)."""
        if self._recorder is None and self.project_root is not None:
            from .screen_record import ScreenRecorder

            self._recorder = ScreenRecorder(
                self.project_root, self.log,
                on_done=self._on_recording_done)
        return self._recorder

    def _on_recording_done(self, video, kind, minutes: int, frames: int) -> None:
        """Called (in the recorder thread) when a recording finishes.
        Sends the video to the owner's chat, then deletes it — like
        a screenshot, nothing is kept after sending."""
        if video is None:
            # v12.14.1: say WHY it failed. The first version only said
            # "too short", so a locked screen (every frame failing)
            # looked like a broken feature with no clue what to try.
            reason = ""
            rec = self._recorder
            err = rec.last_error if rec is not None else None
            if err:
                reason = f" কারণ: {err}।"
                if "screen copy failed" in err:
                    reason += (" স্ক্রিন লক করা থাকলে খুলে নিন, "
                               "তারপর আবার চেষ্টা করুন।")
            if frames < 2:
                self.send("🎥 রেকর্ড খুব ছোট হয়েছে — ভিডিও বানানো যায়নি।"
                          + reason + " আরেকবার চেষ্টা করুন।")
            else:
                self.send(f"🎥 রেকর্ড শেষ হয়েছে কিন্তু ভিডিও বানানো যায়নি — "
                          f"{frames}টি ফ্রেম ধরা পড়েছিল।" + reason)
            return
        caption = f"🎥 স্ক্রিন রেকর্ড — {minutes} মিনিট ({frames} ফ্রেম)"
        try:
            if kind == "mp4":
                ok = self.send_video(video, caption)
            else:
                ok = self.send_file(video, caption)
            if ok:
                self.send("🎥 রেকর্ড পাঠিয়ে দেওয়া হয়েছে।")
            else:
                self.send("🎥 ভিডিও পাঠানো যায়নি — টোকেন/চ্যাট দেখে নিন।")
        finally:
            try:
                parent = Path(video).parent
                Path(video).unlink(missing_ok=True)
                parent.rmdir()  # the run dir is empty now
            except Exception:
                pass

    def start_recording(self, minutes) -> None:
        """Start a screen recording for `minutes` minutes."""
        import sys

        from .screen_record import screen_locked_reason, validate_minutes

        try:
            minutes = validate_minutes(minutes)
        except ValueError as exc:
            self.send(str(exc))
            return
        # v12.14.2: check BEFORE starting. On a locked screen every
        # frame fails, and the old flow only learned that after the
        # whole recording ran. The check returns None off Windows, so
        # it runs before the platform check below.
        locked = screen_locked_reason()
        if locked:
            self.send(locked)
            return
        if sys.platform != "win32":
            self.send("🎥 স্ক্রিন রেকর্ড শুধু Windows PC-তে চলে।")
            return
        rec = self.recorder
        if rec is None:
            self.send("🎥 রেকর্ড করা যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।")
            return
        ok, msg = rec.start(minutes)
        self.send(msg)

    def stop_recording(self) -> None:
        """Stop the running recording early (the video so far is sent)."""
        rec = self.recorder
        if rec is None:
            self.send("🎥 এখন কোনো রেকর্ড চলছে না।")
            return
        _ok, msg = rec.stop()
        self.send(msg)

    def recording_status(self) -> None:
        """Report whether a recording is running and how long is left."""
        rec = self.recorder
        if rec is None:
            self.send("🎥 এখন কোনো রেকর্ড চলছে না।")
            return
        st = rec.status()
        if not st["running"]:
            self.send("🎥 এখন কোনো রেকর্ড চলছে না।")
            return
        elapsed = st["elapsed_seconds"]
        left = max(0, st["minutes"] * 60 - elapsed)
        err_note = ""
        if st.get("last_error"):
            err_note = (f" ⚠️ শেষ চেষ্টায় স্ক্রিন ধরা যায়নি "
                        f"({st['last_error']})।")
        self.send(f"🎥 রেকর্ড চলছে — {st['minutes']} মিনিটের মধ্যে "
                  f"{elapsed // 60} মিনিট {elapsed % 60:02d} সেকেন্ড হয়েছে, "
                  f"আরও {left // 60} মিনিট {left % 60:02d} সেকেন্ড বাকি "
                  f"({st['frames']} ফ্রেম ধরা পড়েছে)।{err_note} থামাতে /record stop লিখুন।")

    def show_record_menu(self) -> None:
        """Duration picker for /record (like the /screen sub-menu)."""
        self.send_markup(
            "🎥 কত মিনিট রেকর্ড করব?\n"
            "রেকর্ড চুপচাপ চলবে — PC-তে কোনো উইন্ডো, শব্দ বা চিহ্ন দেখা যাবে না। "
            "শেষ হলে ভিডিও এখানেই পাঠিয়ে দেওয়া হবে।\n"
            "অন্য সময় চাইলে /record <মিনিট> লিখুন (১–৬০)।",
            record_keyboard())

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

    # -- live screen view (v12.15) ----------------------------------
    @property
    def live_viewer(self):
        """The bot's LiveViewer, created on first use. None when the
        agent's folder is unknown (no place to keep the frame)."""
        if self._live_viewer is None and self.project_root is not None:
            from .live_view import LiveViewer

            self._live_viewer = LiveViewer(
                self.project_root / "data" / "live",
                log=self.log,
                send_fn=self._live_send,
                edit_fn=self._live_edit,
                on_done=self._on_live_done,
                reply_markup=live_keyboard())
        return self._live_viewer

    def _live_send(self, path, caption, reply_markup) -> int | None:
        return self.send_photo_id(path, caption, reply_markup)

    def _live_edit(self, message_id, path, caption, reply_markup) -> bool:
        return self.edit_photo(message_id, path, caption, reply_markup)

    def _on_live_done(self, reason: str, refreshes: int, last_error) -> None:
        """Called (in the viewer thread) when a live view ends."""
        if reason == "stopped":
            self.send(f"⏹️ লাইভ দেখা থামানো হয়েছে — ছবি {refreshes} বার তাজা হয়েছে।")
        elif reason == "time_up":
            self.send("🔴 লাইভ দেখা শেষ হয়েছে (১০ মিনিট) — আবার দেখতে /live লিখুন।")
        elif reason == "send_failed":
            self.send("🔴 লাইভ ছবি পাঠানো যায়নি — টোকেন/চ্যাট দেখে নিন।")
        else:  # failed — captures kept failing (locked screen is usual)
            err = f" ({last_error})" if last_error else ""
            self.send(f"🔴 লাইভ দেখা বন্ধ হয়ে গেছে{err} — আবার চেষ্টা করতে /live লিখুন।")

    def start_live(self) -> None:
        """Start a live screen view in one Telegram message.

        On demand only, and only to the owner's chat — like a
        screenshot, a live view shows screen content, so it never
        starts by itself."""
        import sys

        from .screen_record import screen_locked_reason

        # v12.14.2: check BEFORE starting. On a locked screen every
        # capture fails, and the view would only learn that after
        # three failed refreshes.
        locked = screen_locked_reason()
        if locked:
            self.send(locked)
            return
        if sys.platform != "win32":
            self.send("🔴 লাইভ স্ক্রিন শুধু Windows PC-তে চলে।")
            return
        live = self.live_viewer
        if live is None:
            self.send("🔴 লাইভ দেখা যাচ্ছে না — এজেন্টের ফোল্ডার জানা নেই।")
            return
        ok, _msg = live.start()
        if not ok:
            self.live_status()

    def stop_live(self) -> None:
        """Stop the running live view early. The viewer thread sends
        the closing message when it stops."""
        live = self.live_viewer
        if live is None:
            self.send("🔴 এখন লাইভ দেখা চলছে না।")
            return
        ok, _msg = live.stop()
        if not ok:
            self.send("🔴 এখন লাইভ দেখা চলছে না।")

    def live_status(self) -> None:
        """Report whether a live view is running and how long is left."""
        live = self.live_viewer
        if live is None:
            self.send("🔴 এখন লাইভ দেখা চলছে না।")
            return
        st = live.status()
        if not st["running"]:
            self.send("🔴 এখন লাইভ দেখা চলছে না — দেখতে /live লিখুন।")
            return
        left = st["seconds_left"]
        err_note = ""
        if st.get("last_error"):
            err_note = (f" ⚠️ শেষ চেষ্টায় ছবি আসেনি "
                        f"({st['last_error']})।")
        self.send(f"🔴 লাইভ দেখা চলছে — ছবি {st['refreshes']} বার তাজা হয়েছে, "
                  f"আরও {left // 60} মিনিট {left % 60:02d} সেকেন্ড বাকি।{err_note} "
                  f"থামাতে /live stop লিখুন।")

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
        # Validate BEFORE asking (v12.13, T6): the old code asked
        # "নিশ্চিত?" for any raw string and only failed in _do_block.
        try:
            domain = _norm_domain(domain)
            if not domain or "." not in domain:
                raise ValueError("সঠিক ডোমেইন দিন — যেমন facebook.com")
        except ValueError as exc:
            self.send(f"🌐 {exc}")
            return
        self._ask_confirm(f"🌐 {domain} এই PC-তে ব্লক করব?",
                          "block", {"domain": domain})

    def request_unblock(self, domain: str) -> None:
        try:
            domain = _norm_domain(domain)
            if not domain or "." not in domain:
                raise ValueError("সঠিক ডোমেইন দিন — যেমন facebook.com")
        except ValueError as exc:
            self.send(f"🌐 {exc}")
            return
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

    # -- per-file delete decisions (v12.13, M9) ----------------------
    def _set_delete_pending(self, qid: int) -> str:
        """One pending decision per quarantined file, keyed by qid.
        The old single `telegram_pending` slot meant a second delete
        overwrote the first file's decision — its ✅/↩️ buttons then
        answered 'এই নিশ্চিতকরণটি আর কার্যকর নয়' and the file sat in
        quarantine with no remote action at all. Each file's buttons
        now carry their own token, independent of other deletes."""
        import secrets

        token = secrets.token_hex(4)
        store = self._delete_pending_store()
        store[str(qid)] = {"token": token,
                           "expires": time.time() + self._PENDING_TTL}
        try:
            self.db.set_state("telegram_pending_delete", json.dumps(store))
        except Exception:
            pass
        return token

    def _delete_pending_store(self) -> dict:
        try:
            raw = self.db.get_state("telegram_pending_delete") if self.db else ""
            store = json.loads(raw) if raw else {}
        except Exception:
            store = {}
        if not isinstance(store, dict):
            return {}
        now = time.time()
        return {k: v for k, v in store.items()
                if isinstance(v, dict) and now < float(v.get("expires", 0))}

    def _get_delete_pending(self, qid: int) -> dict | None:
        return self._delete_pending_store().get(str(qid))

    def _clear_delete_pending(self, qid: int) -> None:
        store = self._delete_pending_store()
        if str(qid) in store:
            del store[str(qid)]
            try:
                self.db.set_state("telegram_pending_delete", json.dumps(store))
            except Exception:
                pass
        # A text "হ্যাঁ" after this must not re-fire the same file.
        pending = self._get_pending()
        if (pending is not None and pending.get("action") == "delete_protect"
                and int((pending.get("args") or {}).get("qid") or 0) == int(qid)):
            self._clear_pending()

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
        if action == "explorer_copy":
            return self._do_explorer_copy(str(args.get("src", "")),
                                          str(args.get("dest", "")))
        if action == "explorer_move":
            return self._do_explorer_move(str(args.get("src", "")),
                                          str(args.get("dest", "")))
        if action == "explorer_hide":
            return self._do_explorer_hide(str(args.get("path", "")))
        if action == "explorer_unhide":
            return self._do_explorer_unhide(int(args.get("hid", 0)))
        if action == "explorer_delete":
            return self._do_explorer_delete(str(args.get("path", "")))
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
        # Per-file pending for THESE buttons (independent of other
        # deletes), plus the single slot so a typed "হ্যাঁ"/"না" still
        # answers the latest delete (v12.13, M9).
        token = self._set_delete_pending(qid)
        self._set_pending("delete_protect", {"qid": qid})
        markup = {"inline_keyboard": [[
            {"text": "✅ হ্যাঁ, ডিলিট করো", "callback_data": f"dq:yes:{qid}:{token}"},
            {"text": "↩️ না, ফিরিয়ে দাও", "callback_data": f"dq:no:{qid}:{token}"},
        ]]}
        return self.send_markup(
            build_delete_text(item) + "\n\n(৫ মিনিটের মধ্যে সিদ্ধান্ত নিন — "
            "না জানালে ফাইলটা সুরক্ষায়ই থাকবে।)",
            markup)

    def send_deleted_list(self) -> None:
        """Send /deleted with a ✅/↩️ button pair per held file
        (v12.13, M9): before this, /deleted was plain text — a file
        whose 5-minute decision window had passed could not be
        decided from Telegram at all. Each row gets a fresh per-file
        pending, so the buttons work even long after the delete."""
        from .utils import today_str

        day = today_str()
        try:
            held = self.db.quarantined_held(limit=10)
        except Exception:
            held = []
        keyboard = []
        for row in held:
            qid = int(row["id"])
            token = self._set_delete_pending(qid)
            keyboard.append([
                {"text": f"✅ #{qid} {row['file_name'][:24]}",
                 "callback_data": f"dq:yes:{qid}:{token}"},
                {"text": "↩️ ফিরিয়ে দাও",
                 "callback_data": f"dq:no:{qid}:{token}"},
            ])
        text = build_deleted_text(self.db, day)
        if keyboard:
            self.send_markup(text, {"inline_keyboard": keyboard})
        else:
            self.send_long(text)

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

    # -- file explorer (v12.9) ----------------------------------------
    def _explorer_state(self) -> dict:
        """The owner's browsing state: current folder, page, selected
        file, and a copy/move destination-picking mode. Kept in
        kv_state so a restart does not lose the place. "await" is a
        question the explorer asked (rename / new folder / search) —
        the owner's next message is its answer."""
        raw = self.db.get_state("telegram_explorer") if self.db else ""
        try:
            state = json.loads(raw) if raw else {}
        except Exception:
            state = {}
        await_ = state.get("await") or {}
        if not isinstance(await_, dict):
            await_ = {}
        return {"cwd": str(state.get("cwd") or ""),
                "page": int(state.get("page") or 0),
                "sel": str(state.get("sel") or ""),
                "mode": str(state.get("mode") or ""),
                "src": str(state.get("src") or ""),
                "msg": int(state.get("msg") or 0),
                "last": str(state.get("last") or ""),
                "await": {"kind": str(await_.get("kind") or ""),
                          "path": str(await_.get("path") or "")},
                "hpage": int(state.get("hpage") or 0),
                "hmsg": int(state.get("hmsg") or 0)}

    def _save_explorer_state(self, state: dict) -> None:
        try:
            self.db.set_state("telegram_explorer", json.dumps(state))
        except Exception:
            pass

    def _clear_explorer_await(self) -> None:
        """Drop a pending explorer question (rename / new folder /
        search) — the owner moved on to something else."""
        state = self._explorer_state()
        if state.get("await", {}).get("kind"):
            state["await"] = {}
            self._save_explorer_state(state)

    def _explorer_cancel_await(self) -> bool:
        """Cancel a pending explorer question. True when one was live."""
        if self.file_explorer is None:
            return False
        state = self._explorer_state()
        if not state.get("await", {}).get("kind"):
            return False
        state["await"] = {}
        self._save_explorer_state(state)
        return True

    def _explorer_listing(self, state: dict) -> dict:
        """The page of entries the buttons on screen refer to."""
        from .file_explorer import list_dir

        ex = self.file_explorer
        page_size = ex.page_size if ex is not None else 20
        return list_dir(state["cwd"], state["page"], page_size)

    def _explorer_text_and_markup(self, state: dict) -> tuple[str, dict]:
        """Render the current browsing view: text + one inline
        keyboard. Buttons carry short codes only (callback_data is
        capped at 64 bytes — full paths never travel in a button)."""
        from .file_explorer import _fmt_size as _ex_size
        from .file_explorer import _fmt_time as _ex_time
        from .file_explorer import dir_size as _ex_dir_size
        from .file_explorer import drive_free as _ex_free

        ex = self.file_explorer
        rows: list[list[dict]] = []

        def btn(text, data):
            return {"text": text, "callback_data": data}

        if not state["cwd"]:
            roots = ex.roots() if ex is not None else []
            lines = ["📂 ফাইল এক্সপ্লোরার — ড্রাইভ বেছে নিন", ""]
            if not roots:
                lines.append("  (কোনো ড্রাইভ পাওয়া যায়নি।)")
            for i, root in enumerate(roots):
                free = _ex_free(root) if ex is not None else ""
                label = f"💾 {root}" + (f" ({free} খালি)" if free else "")
                rows.append([btn(label, f"x:drv:{i}")])
            rows.append([btn("❓ সাহায্য", "m:help")])
            return "\n".join(lines), {"inline_keyboard": rows}

        listing = self._explorer_listing(state)
        cwd = state["cwd"]
        lines = [f"📂 {cwd}", ""]
        if listing.get("error"):
            lines.append(f"  {listing['error']}")
        entries = listing.get("entries") or []
        total = listing.get("total", 0)
        page, pages = listing.get("page", 0), listing.get("pages", 1)
        if not entries and not listing.get("error"):
            lines.append("  (এই ফোল্ডার খালি।)")
        for i, e in enumerate(entries):
            if e["is_dir"]:
                lines.append(f"  📁 {e['name']}/")
                rows.append([btn(f"📁 {e['name'][:40]}", f"x:o:{i}"),
                             btn("⚙️", f"x:fsel:{i}")])
            else:
                size = f" ({_ex_size(e['size'])})" if e["size"] is not None else ""
                lines.append(f"  📄 {e['name']}{size}")
                rows.append([btn(f"📄 {e['name'][:40]}", f"x:f:{i}")])
        if total:
            lines.append("")
            lines.append(f"মোট {total}টি — পাতা {page + 1}/{pages}")
        if state["mode"] in ("copy", "move") and state["src"]:
            verb = "কপি" if state["mode"] == "copy" else "মুভ"
            name = state["src"].replace("\\", "/").split("/")[-1]
            lines.append("")
            lines.append(f"📍 {verb} করব: {name}")
            lines.append("গন্তব্য ফোল্ডারে গিয়ে নিচের 📍 বোতাম চাপুন।")
            rows.insert(0, [btn(f"📍 এখানেই {verb} করো", "x:dst")])
            rows.append([btn("✖️ বাতিল", "x:back")])
        elif state["sel"]:
            from pathlib import Path as _Path

            sel_p = _Path(state["sel"])
            name = state["sel"].replace("\\", "/").split("/")[-1]
            lines.append("")
            lines.append(f"📄 বেছে নেওয়া: {name}")
            try:
                if sel_p.is_dir():
                    size, done = _ex_dir_size(sel_p)
                    try:
                        count = len(list(sel_p.iterdir()))
                    except Exception:
                        count = 0
                    lines.append(f"   📁 ফোল্ডার — {_ex_size(size)}"
                                 f"{'±' if not done else ''}, {count}টি আইটেম")
                elif sel_p.is_file():
                    st = sel_p.stat()
                    lines.append(f"   📄 {_ex_size(st.st_size)} — "
                                 f"{_ex_time(st.st_mtime)}")
                else:
                    lines.append("   (এটা আর নেই — হয়তো মুছে গেছে।)")
            except Exception:
                lines.append("   (তথ্য পাওয়া যায়নি।)")
            lines.append(f"   {state['sel']}")
            if sel_p.is_dir():
                rows.append([btn("📥 জিপ করে পাঠাও", "x:act:send"),
                             btn("📋 কপি", "x:act:copy")])
            else:
                rows.append([btn("📥 টেলিগ্রামে পাঠাও", "x:act:send"),
                             btn("📋 কপি", "x:act:copy")])
            rows.append([btn("➡️ মুভ", "x:act:move"),
                         btn("👁️ হাইড", "x:act:hide")])
            rows.append([btn("✏️ নাম বদলাও", "x:act:rename"),
                         btn("🗑️ মুছে ফেলো", "x:act:delete")])
            rows.append([btn("🔙 ফিরে যাও", "x:back")])
        else:
            rows.append([btn("📁 নতুন ফোল্ডার", "x:mkdir"),
                         btn("🔎 খুঁজুন", "x:search")])
        nav: list[dict] = []
        if page > 0:
            nav.append(btn("⬅️ আগের", f"x:pg:{page - 1}"))
        if page + 1 < pages:
            nav.append(btn("পরের ➡️", f"x:pg:{page + 1}"))
        nav.append(btn("⬆️ উপরে", "x:up"))
        nav.append(btn("🏠 ড্রাইভ", "x:root"))
        rows.append(nav)
        return "\n".join(lines), {"inline_keyboard": rows}

    def show_explorer(self) -> None:
        """Show the explorer's one browsing message: EDIT it when it
        still exists (so browsing does not spam the chat), otherwise
        send a fresh one and remember its id."""
        if self.file_explorer is None:
            self.send("📂 ফাইল এক্সপ্লোরার এই PC-তে চালু নেই।")
            return
        state = self._explorer_state()
        text, markup = self._explorer_text_and_markup(state)
        if text == state.get("last") and state.get("msg"):
            return  # nothing changed — leave the message alone
        if state.get("msg") and self.edit_markup(state["msg"], text, markup):
            state["last"] = text
            self._save_explorer_state(state)
            return
        msg_id = self.send_markup_id(text, markup)
        if msg_id:
            state["msg"] = msg_id
            state["last"] = text
            self._save_explorer_state(state)

    def explore_open(self, path: str | None) -> None:
        """Jump to a folder (or the drives view for None/empty)."""
        if self.file_explorer is None:
            self.send("📂 ফাইল এক্সপ্লোরার এই PC-তে চালু নেই।")
            return
        state = self._explorer_state()
        if not path:
            state.update(cwd="", page=0, sel="", mode="", src="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        from pathlib import Path as _Path
        from .file_explorer import norm_root

        target = _Path(norm_root(path))
        if target.is_file():
            # A file path opens its folder and selects the file.
            state.update(cwd=str(target.parent), page=0, sel=str(target),
                         mode="", src="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if not target.is_dir():
            self.send("📂 এই ফোল্ডারটা পাওয়া যায়নি।")
            return
        roots = [str(r) for r in
                 (self.file_explorer.settings.get("allowed_roots") or [])]
        if roots:
            norm = str(target).lower().replace("\\", "/").rstrip("/")
            ok = any(norm == str(r).lower().replace("\\", "/").rstrip("/")
                     or norm.startswith(str(r).lower().replace("\\", "/").rstrip("/") + "/")
                     for r in roots)
            if not ok:
                self.send("📂 এই ফোল্ডার এক্সপ্লোরারের বাইরে — "
                          "শুধু নির্ধারিত ফোল্ডারগুলোই খোলা যায়।")
                return
        state.update(cwd=str(target), page=0, sel="", mode="", src="")
        self._save_explorer_state(state)
        self.show_explorer()

    def _handle_explorer_callback(self, data: str) -> None:
        """One tap on the explorer's browsing message."""
        if self.file_explorer is None:
            self.send("📂 ফাইল এক্সপ্লোরার এই PC-তে চালু নেই।")
            return
        # A tap is a new action: drop any stale pending confirmation
        # first (v12.11 audit — the same class as the v12.3 bug: an
        # old hide/copy confirm could later fire from an unrelated
        # "হ্যাঁ"). The confirm buttons are a different prefix, so
        # they are never affected.
        self._clear_pending()
        state = self._explorer_state()
        parts = data.split(":")
        kind = parts[0] if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if kind == "drv":
            try:
                roots = self.file_explorer.roots()
                root = roots[int(arg)]
            except Exception:
                self.send("📂 ড্রাইভটা পাওয়া যায়নি।")
                return
            state.update(cwd=root, page=0, sel="", mode="", src="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if kind in ("o", "f", "fsel"):
            listing = self._explorer_listing(state)
            try:
                entry = (listing.get("entries") or [])[int(arg)]
            except Exception:
                self.send("📂 এটা আর নেই — ফোল্ডারটা আবার খুলুন।")
                return
            if kind == "o" and entry["is_dir"]:
                state.update(cwd=entry["path"], page=0, sel="")
                self._save_explorer_state(state)
                self.show_explorer()
            else:
                if state.get("mode") in ("copy", "move"):
                    # Choosing a destination: folders open, nothing
                    # can be selected until the mode ends.
                    self.send("📍 গন্তব্য ফোল্ডার বেছে নিন — ফোল্ডার খুলতে "
                              "পারবেন, কিন্তু এখন কিছু বেছে নেওয়া যাবে না।")
                    return
                state["sel"] = entry["path"]
                self._save_explorer_state(state)
                self.show_explorer()
            return
        if kind == "mkdir":
            state["await"] = {"kind": "mkdir", "path": state["cwd"]}
            self._save_explorer_state(state)
            self.send("📁 নতুন ফোল্ডারের নাম লিখুন "
                      "(বাতিল করতে 'বাতিল' লিখুন):")
            return
        if kind == "search":
            state["await"] = {"kind": "search", "path": state["cwd"]}
            self._save_explorer_state(state)
            self.send("🔎 কী খুঁজব? শব্দ লিখুন "
                      "(বাতিল করতে 'বাতিল' লিখুন):")
            return
        if kind == "hp":
            try:
                page = max(0, int(arg))
            except ValueError:
                page = 0
            self._send_hidden_buttons(page, refresh=True)
            return
        if kind == "pg":
            try:
                state["page"] = max(0, int(arg))
            except ValueError:
                state["page"] = 0
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if kind == "up":
            from pathlib import Path as _Path

            parent = str(_Path(state["cwd"]).parent) if state["cwd"] else ""
            if not parent or parent == state["cwd"]:
                state.update(cwd="", page=0, sel="", mode="", src="")
            else:
                state.update(cwd=parent, page=0, sel="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if kind == "root":
            state.update(cwd="", page=0, sel="", mode="", src="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if kind == "back":
            state.update(sel="", mode="", src="")
            self._save_explorer_state(state)
            self.show_explorer()
            return
        if kind == "act":
            self._explorer_action(arg, state)
            return
        if kind == "dst":
            self._explorer_choose_dest(state)
            return
        if kind == "uh":
            try:
                hid = int(arg)
            except ValueError:
                self.send("🫥 ফাইলটা চিনলাম না। /hidden লিখে আবার দেখুন।")
                return
            self._ask_confirm("🫥 এই ফাইলটা আগের জায়গায় ফিরিয়ে দেব?",
                              "explorer_unhide", {"hid": hid})
            return
        self.send("❓ এই বোতামটা চিনলাম না। /help লিখুন।")

    def _explorer_action(self, action: str, state: dict) -> None:
        sel = state.get("sel") or ""
        if not sel:
            self.send("📂 আগে একটা ফাইল বেছে নিন।")
            return
        from pathlib import Path as _Path

        if action == "send":
            sel_p = _Path(sel)
            if sel_p.is_dir():
                from .file_explorer import zip_dir

                ok, msg, zip_path = zip_dir(
                    sel, self.file_explorer.max_send_bytes)
                if not ok:
                    self.send(f"📥 {msg}")
                    return
                name = sel_p.name + ".zip"
                sent = False
                try:
                    sent = send_document(self.token, self.chat_id, zip_path,
                                         f"📥 DeskAudit — {name}")
                finally:
                    import os as _os
                    import shutil as _shutil

                    _shutil.rmtree(_os.path.dirname(zip_path),
                                   ignore_errors=True)
                self.send(f"📥 পাঠানো হয়েছে: {name}" if sent
                          else "📥 পাঠানো যায়নি — নেটওয়ার্ক/টোকেন দেখুন।")
                return
            try:
                size = _Path(sel).stat().st_size
            except Exception:
                self.send("📄 ফাইলটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।")
                return
            if not self.file_explorer.can_send(size):
                limit = self.file_explorer.settings.get("max_send_mb", 50)
                self.send(f"📥 এই ফাইলটা {limit} MB-এর বেশি — টেলিগ্রামে পাঠানো "
                          "যাবে না। অন্য ড্রাইভে কপি/মুভ করুন.")
                return
            name = sel.replace("\\", "/").split("/")[-1]
            if send_document(self.token, self.chat_id, sel,
                             f"📥 DeskAudit — {name}"):
                self.send(f"📥 পাঠানো হয়েছে: {name}")
            else:
                self.send("📥 পাঠানো যায়নি — নেটওয়ার্ক/টোকেন দেখুন।")
            return
        if action in ("copy", "move"):
            verb = "কপি" if action == "copy" else "মুভ"
            if not _Path(sel).exists():
                self.send("📄 এটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।")
                return
            state["mode"] = action
            state["src"] = sel
            state["sel"] = ""
            self._save_explorer_state(state)
            self.show_explorer()
            self.send(f"📍 {verb} করার গন্তব্য বেছে নিন — ফোল্ডার ঘুরে "
                      f"📍 “এখানেই {verb} করো” চাপুন।")
            return
        if action == "hide":
            name = sel.replace("\\", "/").split("/")[-1]
            self._ask_confirm(f"👁️ {name} হাইড করব? এটা তার জায়গা "
                              "থেকে গায়েব হয়ে যাবে — শুধু আপনিই /hidden "
                              "থেকে ফিরিয়ে আনতে পারবেন।",
                              "explorer_hide", {"path": sel})
            return
        if action == "rename":
            name = sel.replace("\\", "/").split("/")[-1]
            state["await"] = {"kind": "rename", "path": sel}
            self._save_explorer_state(state)
            self.send(f"✏️ নতুন নাম লিখুন: {name} "
                      "(বাতিল করতে 'বাতিল' লিখুন)")
            return
        if action == "delete":
            name = sel.replace("\\", "/").split("/")[-1]
            self._ask_confirm(f"🗑️ {name} মুছে ফেলব? ফাইল/ফোল্ডারটা "
                              "সুরক্ষায় (quarantine) রাখা হবে — /deleted "
                              "থেকে ফেরানো যাবে, আর সেখানে হ্যাঁ চাপলে "
                              "তবেই চিরতরে মুছবে।",
                              "explorer_delete", {"path": sel})
            return
        self.send("❓ এই কাজটা চিনলাম না। /help লিখুন।")

    def _explorer_await(self, text: str) -> bool:
        """Handle a reply the explorer asked for: a new name, a new
        folder name, or a search word. Returns True when the text
        was consumed as the answer."""
        if self.file_explorer is None:
            return False
        state = self._explorer_state()
        await_ = state.get("await") or {}
        kind = await_.get("kind", "")
        if not kind:
            return False
        target = await_.get("path") or ""
        if kind == "rename":
            from pathlib import Path as _Path
            from .file_explorer import valid_name

            ok, err = valid_name(text)
            if not ok:
                self.send(f"✏️ {err} আবার লিখুন (বাতিল করতে 'বাতিল' লিখুন)।")
                return True
            src = _Path(target)
            if not src.exists():
                state["await"] = {}
                self._save_explorer_state(state)
                self.send("✏️ এটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।")
                return True
            dest = src.parent / text.strip()
            if dest.exists():
                self.send(f"✏️ {text.strip()} নামে এখানে আগেই কিছু আছে — "
                          "অন্য নাম লিখুন (বাতিল করতে 'বাতিল' লিখুন)।")
                return True
            try:
                import shutil as _shutil

                _shutil.move(str(src), str(dest))
            except Exception as exc:
                state["await"] = {}
                self._save_explorer_state(state)
                self.send(f"✏️ নাম বদলানো যায়নি: {exc}")
                return True
            state["await"] = {}
            state["sel"] = str(dest)
            self._save_explorer_state(state)
            self.send(f"✏️ নাম বদলানো হয়েছে: {src.name} → {dest.name}")
            self.show_explorer()
            return True
        if kind == "mkdir":
            from pathlib import Path as _Path
            from .file_explorer import valid_name

            ok, err = valid_name(text)
            if not ok:
                self.send(f"📁 {err} আবার লিখুন (বাতিল করতে 'বাতিল' লিখুন)।")
                return True
            folder = _Path(target) / text.strip()
            if folder.exists():
                self.send(f"📁 {text.strip()} নামে এখানে আগেই কিছু আছে — "
                          "অন্য নাম লিখুন (বাতিল করতে 'বাতিল' লিখুন)।")
                return True
            try:
                folder.mkdir(parents=False)
            except Exception as exc:
                state["await"] = {}
                self._save_explorer_state(state)
                self.send(f"📁 ফোল্ডার বানানো যায়নি: {exc}")
                return True
            state["await"] = {}
            self._save_explorer_state(state)
            self.send(f"📁 ফোল্ডার বানানো হয়েছে: {text.strip()}")
            self.show_explorer()
            return True
        if kind == "search":
            import os as _os
            from pathlib import Path as _Path

            word = text.strip().lower()
            if not word:
                self.send("🔎 শব্দটা খালি — আবার লিখুন "
                          "(বাতিল করতে 'বাতিল' লিখুন)।")
                return True
            base = _Path(target) if target else _Path(state.get("cwd") or "")
            found: list[str] = []
            scanned = 0
            try:
                for root, dirs, files in _os.walk(base):
                    for name in dirs + files:
                        scanned += 1
                        if scanned > 500:
                            break
                        if word in name.lower():
                            found.append(str(_Path(root) / name))
                            if len(found) >= 20:
                                break
                    if scanned > 500 or len(found) >= 20:
                        break
            except Exception:
                pass
            state["await"] = {}
            self._save_explorer_state(state)
            if not found:
                self.send(f"🔎 '{text.strip()}' — {base} এর ভেতরে কিছু "
                          "পাওয়া যায়নি।")
                return True
            lines = [f"🔎 '{text.strip()}' — {len(found)}টি পাওয়া গেছে:"]
            for f in found:
                mark = "📁" if _Path(f).is_dir() else "📄"
                lines.append(f"  {mark} {f}")
            if len(found) >= 20:
                lines.append("  (প্রথম ২০টি দেখানো হলো।)")
            self.send_long("\n".join(lines))
            return True
        state["await"] = {}
        self._save_explorer_state(state)
        return True

    def _explorer_choose_dest(self, state: dict) -> None:
        mode = state.get("mode") or ""
        src = state.get("src") or ""
        dest = state.get("cwd") or ""
        if mode not in ("copy", "move") or not src or not dest:
            self.send("📍 আগে একটা ফাইল বেছে নিয়ে কপি/মুভ চাপুন।")
            return
        verb = "কপি" if mode == "copy" else "মুভ"
        name = src.replace("\\", "/").split("/")[-1]
        detail = ""
        try:
            from pathlib import Path as _Path
            from .file_explorer import _fmt_size as _ex_size
            from .file_explorer import dir_size as _ex_dir_size

            if _Path(src).is_dir():
                size, done = _ex_dir_size(src)
                detail = f" (ফোল্ডার, মোট {_ex_size(size)}{'±' if not done else ''})"
        except Exception:
            detail = ""
        self._ask_confirm(f"📍 {name}{detail} — {verb} করব এই ফোল্ডারে?\n{dest}",
                          f"explorer_{mode}", {"src": src, "dest": dest})

    def _do_explorer_copy(self, src: str, dest: str) -> str:
        from pathlib import Path as _Path

        from .file_explorer import copy_dir, copy_file

        if _Path(src).is_dir():
            ok, msg, _target = copy_dir(src, dest)
        else:
            ok, msg, _target = copy_file(src, dest)
        state = self._explorer_state()
        state.update(mode="", src="", sel="")
        self._save_explorer_state(state)
        self.show_explorer()
        return msg if ok else f"📋 {msg}"

    def _do_explorer_move(self, src: str, dest: str) -> str:
        from pathlib import Path as _Path

        from .file_explorer import move_dir, move_file

        if _Path(src).is_dir():
            ok, msg, _target = move_dir(src, dest)
        else:
            ok, msg, _target = move_file(src, dest)
        state = self._explorer_state()
        state.update(mode="", src="", sel="")
        self._save_explorer_state(state)
        self.show_explorer()
        return msg if ok else f"➡️ {msg}"

    def _do_explorer_hide(self, path: str) -> str:
        if self.file_explorer is None:
            return "📂 ফাইল এক্সপ্লোরার এই PC-তে চালু নেই।"
        try:
            ok, msg, _item = self.file_explorer.hide(path)
        except Exception as exc:
            return f"👁️ হাইড করা যায়নি: {exc}"
        state = self._explorer_state()
        state.update(sel="", mode="", src="")
        self._save_explorer_state(state)
        self.show_explorer()
        return msg

    def _do_explorer_delete(self, path: str) -> str:
        """Delete from the explorer: into the delete-protect
        quarantine, so /deleted can restore it (v12.11)."""
        if self.delete_protector is None:
            return "🗑️ ডিলিট সুরক্ষা এই PC-তে চালু নেই।"
        try:
            _ok, msg = self.delete_protector.quarantine_user(path)
        except Exception as exc:
            return f"🗑️ মুছে ফেলা যায়নি: {exc}"
        state = self._explorer_state()
        state.update(sel="", mode="", src="")
        self._save_explorer_state(state)
        self.show_explorer()
        return msg

    def _do_explorer_unhide(self, hid: int) -> str:
        if self.file_explorer is None:
            return "📂 ফাইল এক্সপ্লোরার এই PC-তে চালু নেই।"
        try:
            msg = self.file_explorer.unhide(int(hid))
        except Exception as exc:
            return f"🫥 ফেরানো যায়নি: {exc}"
        if msg.startswith("↩️"):
            state = self._explorer_state()
            self._send_hidden_buttons(state.get("hpage", 0), refresh=True)
        return msg

    def send_hidden_list(self) -> None:
        """The /hidden list, with a 'ফিরিয়ে দাও' button per file,
        10 per page."""
        from .utils import today_str

        self.send_long(build_hidden_text(self.db, today_str()))
        self._send_hidden_buttons(0, refresh=False)

    def _send_hidden_buttons(self, page: int, refresh: bool) -> None:
        """The restore buttons for one /hidden page. When refresh is
        True, the existing buttons message is edited in place (pager
        taps, or after a restore); otherwise a fresh one is sent."""
        try:
            held = self.db.hidden_held(limit=200)
        except Exception:
            held = []
        pages = max(1, (len(held) + 9) // 10)
        page = max(0, min(int(page), pages - 1))
        rows: list[list[dict]] = []
        for row in held[page * 10:(page + 1) * 10]:
            name = (row["file_name"] or "?")[:40]
            rows.append([{"text": f"↩️ {name} — ফিরিয়ে দাও",
                          "callback_data": f"x:uh:{row['id']}"}])
        nav: list[dict] = []
        if page > 0:
            nav.append({"text": "⬅️ আগের", "callback_data": f"x:hp:{page - 1}"})
        if page + 1 < pages:
            nav.append({"text": "পরের ➡️", "callback_data": f"x:hp:{page + 1}"})
        if nav:
            rows.append(nav)
        if not held:
            return
        text = (f"👇 ফিরিয়ে দিতে চাইলে বোতাম চাপুন — "
                f"পাতা {page + 1}/{pages}:")
        markup = {"inline_keyboard": rows}
        state = self._explorer_state()
        state["hpage"] = page
        if refresh and state.get("hmsg") \
                and self.edit_markup(state["hmsg"], text, markup):
            self._save_explorer_state(state)
            return
        if refresh and not state.get("hmsg"):
            return  # the list was never shown — nothing to refresh
        msg_id = self.send_markup_id(text, markup)
        if msg_id:
            state["hmsg"] = msg_id
            self._save_explorer_state(state)

    # -- command routing ---------------------------------------------------
    def handle_text(self, text: str) -> None:
        """Answer one message from the owner. Unknown text gets the
        help hint; a bare হ্যাঁ/না answers a pending confirmation."""
        from .utils import today_str

        text = (text or "").strip()
        if not text:
            return
        low = text.lower()
        # An answer the explorer asked for (a new name, a new folder
        # name, or a search word). A "/" command always wins (it
        # clears the wait below); a live confirmation wins over the
        # wait too.
        if not text.startswith("/"):
            if low in self._CANCEL_WORDS and self._get_pending() is None:
                if self._explorer_cancel_await():
                    self.send("❌ বাতিল করা হয়েছে।")
                    self.show_explorer()
                    return
            elif (low not in self._CONFIRM_WORDS
                    and low not in self._CANCEL_WORDS
                    and self._get_pending() is None):
                if self._explorer_await(text):
                    return
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
            self._clear_explorer_await()
        day = today_str()
        if cmd in ("/start", "/help"):
            self.send(HELP_TEXT)
        elif cmd == "/menu":
            self.send_markup(
                "📋 DeskAudit মেনু — চাপলেই কাজ হবে:\n"
                "📊 দেখা | 🎛️ নিয়ন্ত্রণ | 💾 ডেটা ও PC\n"
                "(বন্ধ/মোছা/রিসেটের মতো কাজে আগে নিশ্চিতকরণ চাইবে — "
                "এক চাপে কিছু মুছবে না।)",
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
            self.send_deleted_list()
        elif cmd == "/explore":
            self.explore_open(" ".join(args) if args else None)
        elif cmd == "/hidden":
            self.send_hidden_list()
        elif cmd == "/alerts":
            self.send_long(build_alerts_text(self.db, day))
        elif cmd == "/files":
            self.send_long(build_files_text(self.db, day))
        elif cmd == "/search":
            if not args:
                self.send("🔎 /search-এর পরে শব্দ দিন — যেমন /search রিপোর্ট")
            else:
                # A DB error here used to fail silently (v12.13, T7):
                # build_search_text had no try/except, the error only
                # reached poll_commands' catch, and the owner got no reply.
                try:
                    self.send_long(build_search_text(self.db, day, " ".join(args)))
                except Exception as exc:
                    self.send(f"🔎 খোঁজা যায়নি: {exc}")
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
                # T5: the raw "white"/"black" value used to leak into
                # the Bengali reply ("স্ক্রিন এখন white আছে") — map it.
                state = {"white": "সাদা ঢাকা", "black": "কালো ঢাকা"}.get(current)
                state = f"এখন {state} আছে" if state else "এখন খোলা আছে"
                self.send(f"⬜ স্ক্রিন {state}। বদলাতে: /screen white, /screen black অথবা /screen off")
            elif args[0].lower() in ("white", "সাদা"):
                self.send(self.set_screen("white"))
            elif args[0].lower() in ("black", "কালো"):
                self.send(self.set_screen("black"))
            elif args[0].lower() in ("off", "খোলা"):
                self.send(self.set_screen("off"))
            else:
                self.send("⬜ ব্যবহার: /screen white, /screen black অথবা /screen off")
        elif cmd == "/record":
            if not args:
                self.show_record_menu()
            elif args[0].lower() in ("stop", "থামাও"):
                self.stop_recording()
            elif args[0].lower() in ("status", "অবস্থা"):
                self.recording_status()
            else:
                self.start_recording(args[0])
        elif cmd == "/live":
            if not args:
                live = self.live_viewer
                if live is not None and live.status()["running"]:
                    self.live_status()
                else:
                    self.start_live()
            elif args[0].lower() in ("stop", "থামাও"):
                self.stop_live()
            elif args[0].lower() in ("status", "অবস্থা"):
                self.live_status()
            else:
                self.send("🔴 ব্যবহার: /live — দেখা শুরু | /live stop — থামানো | /live status — অবস্থা")
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
        if data.startswith("dq:"):
            # dq:yes:<qid>:<token> / dq:no:<qid>:<token> — a decision
            # on ONE quarantined file (v12.13, M9). Each file's
            # buttons carry their own token, so a second delete never
            # kills the first file's buttons.
            bits = data.split(":")
            verdict = bits[1] if len(bits) > 1 else ""
            try:
                qid = int(bits[2]) if len(bits) > 2 else 0
            except ValueError:
                qid = 0
            token = bits[3] if len(bits) > 3 else None
            pending = self._get_delete_pending(qid) if qid else None
            if pending is None or pending.get("token") != token:
                self.send("⌛ এই নিশ্চিতকরণটি আর কার্যকর নয় — /deleted লিখে আবার দেখুন।")
            else:
                self._clear_delete_pending(qid)
                if verdict == "yes":
                    self.send(self._do_delete_purge(qid))
                elif verdict == "no":
                    self.send(self._do_delete_restore(qid))
                else:
                    self.send("❓ অচেনা বোতাম — /help লিখুন।")
            return
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
        if data.startswith("x:"):
            # A tap on the explorer's browsing message.
            self._handle_explorer_callback(data[2:])
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
                self.send_deleted_list()
            elif cmd == "explore":
                self.show_explorer()
            elif cmd == "hidden":
                self.send_hidden_list()
            elif cmd == "alerts":
                self.send_long(build_alerts_text(self.db, day))
            elif cmd == "files":
                self.send_long(build_files_text(self.db, day))
            elif cmd == "search":
                # Needs a word — the button only opens the question.
                self.send("🔎 কী খুঁজব? /search <শব্দ> লিখুন — যেমন /search রিপোর্ট")
            elif cmd == "report":
                self.send_report(day)
            elif cmd == "shot":
                self.send_screenshot()
            elif cmd == "live":
                self.start_live()
            elif cmd == "live_stop":
                self.stop_live()
            elif cmd == "browser":
                self.send_long(build_browser_text(self.db, day))
            elif cmd == "screen":
                self.send_markup("⬜ স্ক্রিন কী করব?", screen_keyboard())
            elif cmd == "screen_white":
                self.send(self.set_screen("white"))
            elif cmd == "screen_black":
                self.send(self.set_screen("black"))
            elif cmd == "screen_off":
                self.send(self.set_screen("off"))
            elif cmd == "record":
                self.show_record_menu()
            elif cmd.startswith("record_"):
                arg = cmd[len("record_"):]
                if arg == "stop":
                    self.stop_recording()
                elif arg == "status":
                    self.recording_status()
                else:
                    self.start_recording(arg)
            elif cmd == "lock":
                self.send(self.lock_pc())
            elif cmd == "close":
                self.send("❌ কোন অ্যাপ বন্ধ করব? /close <অ্যাপ> লিখুন — "
                          "যেমন /close chrome")
            elif cmd == "freeze":
                self.send("🧊 কোন অ্যাপ স্থগিত রাখব? /freeze <অ্যাপ> লিখুন — "
                          "যেমন /freeze chrome (আবার চালু করতে /unfreeze <অ্যাপ>)")
            elif cmd == "pause":
                self.send(self.pause_audit())
            elif cmd == "resume":
                self.send(self.resume_audit())
            elif cmd == "mute":
                # One tap = quiet for 60 minutes (reversible with /unmute).
                self.send(self.mute(60))
            elif cmd == "unmute":
                self.send(self.unmute())
            elif cmd == "digest":
                current = self.settings.get("digest_minutes", 60)
                state = "বন্ধ" if not current else f"প্রতি {current} মিনিটে"
                self.send(f"🕐 ডাইজেস্ট এখন {state} আসে। বদলাতে: "
                          "/digest 30 অথবা /digest off লিখুন।")
            elif cmd == "goal":
                self.send("🎯 কোন প্রজেক্টের লক্ষ্য বদলাব? "
                          "/goal <প্রজেক্ট> <মিনিট> লিখুন — যেমন /goal Office 240")
            elif cmd == "backup":
                self.send(self.backup_now())
            elif cmd == "clear":
                self.send("🗑 কোন দিনের ডেটা মুছব? /clear <তারিখ> লিখুন — "
                          "যেমন /clear 2026-11-24 (আজকের জন্য শুধু /clear)")
            elif cmd == "shutdown":
                # Only asks — the owner still confirms with ✅ first.
                self.request_shutdown(restart=False)
            elif cmd == "restart":
                self.request_shutdown(restart=True)
            elif cmd == "abort":
                self.send(self.abort_shutdown())
            elif cmd == "reset":
                # Only asks — the owner still confirms with ✅ first.
                self.request_reset()
            elif cmd == "menu":
                self.send_markup(
                    "📋 DeskAudit মেনু — চাপলেই কাজ হবে:\n"
                    "📊 দেখা | 🎛️ নিয়ন্ত্রণ | 💾 ডেটা ও PC\n"
                    "(বন্ধ/মোছা/রিসেটের মতো কাজে আগে নিশ্চিতকরণ চাইবে — "
                    "এক চাপে কিছু মুছবে না।)",
                    menu_keyboard())
            elif cmd == "noop":
                # v12.13: intentionally silent — a button that does
                # nothing (e.g. a disabled-looking row). Without this
                # branch the tap fell into the "unknown button" reply.
                pass
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
        # A corrupt offset used to brick polling forever (v12.13, T8):
        # int() raised on every poll, only a log line, no self-heal.
        try:
            offset_raw = self.db.get_state("telegram_update_offset")
            offset = int(offset_raw) if offset_raw else None
        except (TypeError, ValueError):
            self.log.warning("Telegram offset was corrupt — resetting it.")
            try:
                self.db.set_state("telegram_update_offset", "")
            except Exception:
                pass
            offset = None
        try:
            updates = get_updates(self.token, offset)
        except Exception as exc:
            self.log.warning("Telegram poll failed: %s", exc)
            return 0
        handled = 0
        max_id = None
        for upd in updates:
            try:
                max_id = upd.get("update_id", max_id)
                uid = upd.get("update_id")
                # v12.14: never answer the same update twice in this
                # process — a failed offset save (or a double delivery)
                # re-fetches the same updates on the next poll.
                if uid is not None:
                    if uid in self._handled_update_ids:
                        continue
                    self._handled_update_ids.add(uid)
                    if len(self._handled_update_ids) > 1000:
                        self._handled_update_ids.clear()
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
            except Exception as exc:
                # v12.14: a silent failure here used to re-answer every
                # command on the next poll (the offset never advanced).
                # The in-memory _handled_update_ids set is the safety
                # net for this process, but the log line makes the
                # failure visible instead of silent.
                self.log.warning("Could not save Telegram offset: %s", exc)
        return handled
