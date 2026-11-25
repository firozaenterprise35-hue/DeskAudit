"""Hourly email digest — plain-text + simple HTML via SMTP.

The agent sends one digest email every `interval_minutes` (default
60) to the owner's own address. Gmail's SMTP is the documented route:
turn on 2-Step Verification, create an App Password for DeskAudit,
and put the address + 16-character app password in Dashboard
Settings -> Email (stored in config.local.yaml only, never in git).

The digest is metadata only — app names, window titles, file names,
domains, transfer counts — never typed content or message text.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

_log = logging.getLogger("deskaudit.emailer")

DEFAULT_EMAIL = {
    "enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "from_addr": "",
    "to_addr": "",
    "interval_minutes": 60,
    "use_tls": True,
}


def configured(settings: dict | None) -> bool:
    s = settings or {}
    return bool(s.get("smtp_user") and s.get("smtp_password") and s.get("to_addr"))


def _fmt_min(seconds) -> str:
    try:
        minutes = int(float(seconds or 0) // 60)
    except Exception:
        minutes = 0
    if minutes >= 60:
        return f"{minutes // 60}ঘ {minutes % 60}মি"
    return f"{minutes}মি"


def _since_rows(db, since: datetime, limit: int = 6) -> list:
    try:
        return db.conn.execute(
            """SELECT app_name, COALESCE(active_seconds, 0) AS active
               FROM app_sessions
               WHERE started_at >= ?
               ORDER BY started_at DESC LIMIT ?""",
            (since.isoformat(timespec="seconds"), limit),
        ).fetchall()
    except Exception:
        return []


def build_hourly_email(db, day: str, since_minutes: int = 60) -> tuple[str, str, str]:
    """Return (subject, plain-text body, HTML body) for the last hour."""
    since = datetime.now() - timedelta(minutes=since_minutes)
    now = datetime.now().strftime("%H:%M")
    subject = f"DeskAudit — গত {since_minutes} মিনিটের আপডেট ({day} {now})"
    rows = _since_rows(db, since)
    lines = [f"DeskAudit — গত {since_minutes} মিনিটের আপডেট", f"তারিখ: {day}, সময়: {now}", ""]
    html_rows = []
    if rows:
        by_app: dict[str, float] = {}
        for r in rows:
            by_app[r["app_name"]] = by_app.get(r["app_name"], 0) + float(r["active"] or 0)
        lines.append("ব্যবহৃত অ্যাপ:")
        for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  • {app} — {_fmt_min(secs)}")
            html_rows.append((app, _fmt_min(secs)))
    else:
        lines.append("এই সময়ে কোনো নতুন অ্যাপ সেশন শুরু হয়নি।")
    try:
        files = db.conn.execute(
            """SELECT event_type, path FROM file_events
               WHERE occurred_at >= ? ORDER BY occurred_at DESC LIMIT 5""",
            (since.isoformat(timespec="seconds"),),
        ).fetchall()
    except Exception:
        files = []
    if files:
        lines.append("ফাইল কাজ:")
        for f in files:
            name = (f["path"] or "").replace("\\", "/").split("/")[-1]
            lines.append(f"  • {f['event_type']}: {name}")
    try:
        counts = db.transfer_counts(day)
    except Exception:
        counts = {}
    total_transfers = sum(v.get("count", 0) for v in counts.values())
    if total_transfers:
        lines.append(f"আজ মোট ডেটা ট্রান্সফার: {total_transfers}টি")
    try:
        alerts = db.list_alerts(date=day, limit=5)
    except Exception:
        alerts = []
    if alerts:
        lines.append("আজকের অ্যালার্ট:")
        for a in alerts:
            lines.append(f"  • [{a['severity']}] {a['title']}")
    lines.append("")
    lines.append("— DeskAudit (মেটাডেটা শুধু: অ্যাপ/ফাইল/ডোমেইনের নাম ও সময়; "
                 "টাইপ করা লেখা বা মেসেজের ভেতরের কথা এতে থাকে না)")
    text = "\n".join(lines)
    app_rows = "".join(f"<tr><td>{a}</td><td>{m}</td></tr>" for a, m in html_rows) or \
        "<tr><td colspan='2'>এই সময়ে কোনো নতুন অ্যাপ সেশন শুরু হয়নি।</td></tr>"
    transfer_line = (f"<p>আজ মোট ডেটা ট্রান্সফার: {total_transfers}টি</p>"
                     if total_transfers else "")
    alert_line = ("<p>আজকের অ্যালার্ট:<br>" +
                  "<br>".join(f"[{a['severity']}] {a['title']}" for a in alerts) +
                  "</p>") if alerts else ""
    html = (f"<html><body><h2>DeskAudit — গত {since_minutes} মিনিটের আপডেট</h2>"
            f"<p>তারিখ: {day}, সময়: {now}</p>"
            f"<table border='1' cellpadding='6'><tr><th>অ্যাপ</th><th>সময়</th></tr>"
            f"{app_rows}</table>{transfer_line}{alert_line}"
            f"<p><small>মেটাডেটা শুধু — টাইপ করা লেখা বা মেসেজের ভেতরের কথা "
            f"এতে থাকে না।</small></p></body></html>")
    return subject, text, html


def send_email(settings: dict | None, subject: str, text_body: str,
               html_body: str | None = None) -> tuple[bool, str]:
    """Send one email via SMTP. Returns (True, '') or (False, reason).
    Never raises — the agent loop must never die on a mail failure."""
    s = {**DEFAULT_EMAIL, **(settings or {})}
    if not configured(s):
        return False, "email not configured (smtp_user / smtp_password / to_addr)"
    host = str(s.get("smtp_host") or "smtp.gmail.com")
    try:
        port = int(s.get("smtp_port") or 587)
    except (TypeError, ValueError):
        return False, "bad smtp_port"
    sender = str(s.get("from_addr") or s.get("smtp_user"))
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = str(s.get("to_addr"))
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        use_tls = bool(s.get("use_tls", True))
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        try:
            server.ehlo()
            if use_tls and port != 465:
                server.starttls()
                server.ehlo()
            server.login(str(s.get("smtp_user")), str(s.get("smtp_password")))
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return True, ""
    except Exception as exc:
        _log.warning("Email send failed: %s", exc)
        return False, str(exc)
