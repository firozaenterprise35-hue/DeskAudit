"""DeskAudit alerts — fully local rule engine, no internet, no API key.

Rules (all thresholds live in config.yaml under ``alerts:``):
  * deletion_burst — many files deleted in a short window (default 5 in 10 min)
  * deletion_folder_burst — many deletions from ONE folder (default 3)
  * unknown_app_night — an app never seen before THAT session, running
    00:00-05:00. An app already used earlier the same day does NOT count
    as unknown at night.
  * new_app — an app seen for the very first time today (info), only if
    it is not a well-known safe app and it was used at least
    new_app_min_minutes (default 5) today.
  * distracting_over — distracting browser time over the daily limit
  * project_goal_met — a project reached its daily goal (info)
  * project_goal_missed — a project reached < 50% of its goal (past days only)
  * untagged_spike — untagged time over the limit AND over half of active time
  * idle_over — idle time over the daily limit
  * large_download — a single download over the size limit

Data-transfer rules (thresholds live in config.yaml under ``transfers:``,
passed in as ``transfer_settings``):
  * usb_insert — a USB drive was connected
  * usb_file_copy — files were copied onto a USB drive
  * cloud_upload — cloud-drive use over the daily limit
  * communication_use — email / messaging channel use over the daily limit
  * print_job — a document was printed
  * sensitive_copy — a file was copied OUT of a sensitive folder
  * large_transfer — a single moved file over the size limit

Usage:
    python main.py --alerts            # check today, save new ones, print them
    python main.py --alerts --date 2026-11-23
    python main.py --alerts --notify   # also try a Windows notification
    python main.py --alerts-list       # show stored alerts
    python main.py --alerts-list --date 2026-11-23 --unread

Alerts are stored in the ``alerts`` table, deduped on (date, type, title),
so re-running never duplicates them. Only NEW alerts are notified.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .database import Database
from .report import build_summary
from .utils import format_duration, today_str

DEFAULT_ALERTS = {
    "enabled": True,
    "deletion_burst_count": 5,
    "deletion_burst_minutes": 10,
    "deletion_folder_count": 3,
    "night_start_hour": 0,
    "night_end_hour": 5,
    "distracting_minutes": 60,
    "untagged_minutes": 60,
    "untagged_percent": 50,
    "idle_minutes": 120,
    "large_download_mb": 500,
    "notify": True,
    # new_app only fires for a non-safe app used at least this long today,
    # so a 30-second accidental launch does not alert.
    "new_app_min_minutes": 5,
    "known_safe_apps": [
        "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
        "vivaldi.exe", "code.exe", "notepad.exe", "explorer.exe",
        "winword.exe", "excel.exe", "powerpnt.exe", "outlook.exe",
        "teams.exe", "zoom.exe", "slack.exe", "discord.exe",
    ],
}


@dataclass
class Alert:
    alert_type: str
    severity: str  # info | warning | critical
    title: str
    message: str
    details: dict = field(default_factory=dict)
    date: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.alert_type, "severity": self.severity,
            "title": self.title, "message": self.message,
            "details": self.details, "date": self.date,
        }


def _hour(ts: str) -> int:
    try:
        return int(ts[11:13])
    except (ValueError, IndexError, TypeError):
        return -1


def check_alerts(db: Database, date: str | None = None,
                 settings: dict | None = None, tagger=None,
                 transfer_settings: dict | None = None) -> list[Alert]:
    """Evaluate every rule for one day. Pure read — saving is separate."""
    date = date or today_str()
    cfg = {**DEFAULT_ALERTS, **(settings or {})}
    if not cfg.get("enabled", True):
        return []
    summary = build_summary(db, date, tagger=tagger)
    alerts: list[Alert] = []
    today = today_str()

    # -- Deletions: burst + per-folder burst ---------------------------
    deleted = db.conn.execute(
        "SELECT occurred_at, path FROM file_events "
        "WHERE substr(occurred_at, 1, 10) = ? AND event_type = 'deleted' "
        "ORDER BY occurred_at",
        (date,),
    ).fetchall()
    if deleted:
        times = [datetime.fromisoformat(r["occurred_at"]) for r in deleted]
        window = timedelta(minutes=int(cfg["deletion_burst_minutes"]))
        burst = 0
        for i, t in enumerate(times):
            burst = max(burst, sum(1 for u in times if t <= u < t + window))
        if burst >= int(cfg["deletion_burst_count"]):
            alerts.append(Alert(
                "deletion_burst", "critical",
                f"{burst}টি ফাইল {cfg['deletion_burst_minutes']} মিনিটে ডিলিট হয়েছে",
                f"{date} তারিখে {cfg['deletion_burst_minutes']} মিনিটের মধ্যে {burst}টি ফাইল "
                f"ডিলিট হয়েছে (মোট ডিলিট: {len(deleted)}টি)। ভুল করে ডিলিট হলো কি না দেখে নিন।",
                {"count": burst, "total_deleted": len(deleted)}, date))
        folders: dict[str, int] = {}
        for r in deleted:
            folders[str(Path(r["path"]).parent)] = folders.get(str(Path(r["path"]).parent), 0) + 1
        for folder, n in sorted(folders.items(), key=lambda kv: -kv[1]):
            if n >= int(cfg["deletion_folder_count"]):
                alerts.append(Alert(
                    "deletion_folder_burst", "warning",
                    f"এক ফোল্ডার থেকে {n}টি ফাইল ডিলিট হয়েছে",
                    f"ফোল্ডার {folder} থেকে {n}টি ফাইল ডিলিট হয়েছে।",
                    {"folder": folder, "count": n}, date))
                break

    # -- Night + new apps -----------------------------------------------
    # "Known" means seen before THIS session, not just before this date:
    # an app used at 10:00 today is known at 02:00 the same night.
    first_seen = {
        r["app_name"]: r["first_seen"] for r in db.conn.execute(
            "SELECT app_name, MIN(started_at) AS first_seen FROM app_sessions "
            "GROUP BY app_name").fetchall()
    }
    known_before = {
        r[0] for r in db.conn.execute(
            "SELECT DISTINCT app_name FROM app_sessions WHERE substr(started_at, 1, 10) < ?",
            (date,)).fetchall()
    }
    today_apps = db.conn.execute(
        "SELECT app_name, started_at, active_seconds FROM app_sessions "
        "WHERE substr(started_at, 1, 10) = ? ORDER BY started_at",
        (date,)).fetchall()
    # Active time per app today, for the new_app minimum-time filter.
    active_today: dict[str, float] = {}
    for r in today_apps:
        active_today[r["app_name"]] = active_today.get(r["app_name"], 0.0) + float(r["active_seconds"] or 0)
    safe_apps = {str(a).lower() for a in cfg.get("known_safe_apps", [])}
    min_new_seconds = int(cfg.get("new_app_min_minutes", 5)) * 60
    night_unknown = []
    new_apps = []
    for r in today_apps:
        app = r["app_name"]
        if app not in known_before and app not in new_apps:
            # Quiet new_app: skip well-known apps and very short launches.
            if (app.lower() not in safe_apps
                    and active_today.get(app, 0) >= min_new_seconds):
                new_apps.append(app)
        h = _hour(r["started_at"])
        # Unknown at night ONLY if this session is the app's first ever
        # session (first_seen >= this session's start). Same-day daytime
        # use makes it known, so it does not fire.
        first = first_seen.get(app)
        if (int(cfg["night_start_hour"]) <= h < int(cfg["night_end_hour"])
                and app not in night_unknown
                and (first is None or str(first) >= str(r["started_at"]))):
            night_unknown.append(app)
    for app in night_unknown:
        alerts.append(Alert(
            "unknown_app_night", "critical",
            f"রাতে অচেনা অ্যাপ চলেছে: {app}",
            f"রাত {cfg['night_start_hour']}:00–{cfg['night_end_hour']}:00 এর মধ্যে আগে কখনো "
            f"না দেখা অ্যাপ {app} চলেছে। আপনি চালিয়েছিলেন কি না যাচাই করুন।",
            {"app": app}, date))
    if new_apps:
        alerts.append(Alert(
            "new_app", "info",
            f"নতুন অ্যাপ দেখা গেছে: {', '.join(new_apps[:5])}",
            f"আজ প্রথমবার চলেছে: {', '.join(new_apps)}. নতুন সফটওয়্যার ইনস্টল করে থাকলে ঠিক আছে।",
            {"apps": new_apps}, date))

    # -- Distracting browser time ---------------------------------------
    distracting = summary["browser"]["distracting_seconds"]
    if distracting >= int(cfg["distracting_minutes"]) * 60:
        alerts.append(Alert(
            "distracting_over", "warning",
            f"ডিস্ট্রাক্টিং ব্রাউজিং {format_duration(distracting)} ছাড়িয়ে গেছে",
            f"আজ ডিস্ট্রাক্টিং সাইটে {format_duration(distracting)} সময় গেছে "
            f"(সীমা {cfg['distracting_minutes']} মিনিট)।",
            {"seconds": distracting}, date))

    # -- Project goals ---------------------------------------------------
    for p in summary["projects"]:
        if not p.get("goal_minutes"):
            continue
        if p.get("goal_met"):
            alerts.append(Alert(
                "project_goal_met", "info",
                f"গোল পূরণ হয়েছে: {p['project']}",
                f"{p['project']} প্রজেক্টে আজ {format_duration(p['active_seconds'])} কাজ হয়েছে — "
                f"গোল ছিল {format_duration(p['goal_minutes'] * 60)}।",
                {"project": p["project"], "percent": p.get("goal_percent", 0)}, date))
        elif date < today and p.get("goal_percent", 0) < 50:
            alerts.append(Alert(
                "project_goal_missed", "warning",
                f"গোল পূরণ হয়নি: {p['project']} ({p.get('goal_percent', 0)}%)",
                f"{p['project']} প্রজেক্টে {format_duration(p['active_seconds'])} কাজ হয়েছে, "
                f"গোলের {p.get('goal_percent', 0)}% মাত্র।",
                {"project": p["project"], "percent": p.get("goal_percent", 0)}, date))

    # -- Untagged spike --------------------------------------------------
    untagged_seconds = sum(float(a.get("seconds", 0)) for a in summary["untagged"].get("apps", []))
    active = summary["active_seconds"]
    if (untagged_seconds >= int(cfg["untagged_minutes"]) * 60
            and active and 100 * untagged_seconds / active >= int(cfg["untagged_percent"])):
        alerts.append(Alert(
            "untagged_spike", "warning",
            f"আনট্যাগড সময় বেশি: {format_duration(untagged_seconds)}",
            f"আজ {format_duration(untagged_seconds)} সময় কোনো প্রজেক্টে ট্যাগ হয়নি "
            f"(মোট অ্যাক্টিভ সময়ের {round(100 * untagged_seconds / active)}%)। "
            "python main.py --suggest-projects চালিয়ে রুল যোগ করুন।",
            {"seconds": untagged_seconds}, date))

    # -- Idle ------------------------------------------------------------
    idle = summary["idle_seconds"]
    if idle >= int(cfg["idle_minutes"]) * 60:
        alerts.append(Alert(
            "idle_over", "info",
            f"আইডল সময় বেশি: {format_duration(idle)}",
            f"আজ {format_duration(idle)} সময় কম্পিউটার চালু ছিল কিন্তু কোনো ইনপুট ছিল না।",
            {"seconds": idle}, date))

    # -- Large downloads -------------------------------------------------
    limit_bytes = int(cfg["large_download_mb"]) * 1024 * 1024
    for d in summary["browser"]["downloads"]:
        if d.get("size") and d["size"] >= limit_bytes:
            alerts.append(Alert(
                "large_download", "warning",
                f"বড় ডাউনলোড: {d['file']}",
                f"{d['browser']} থেকে {d['file']} ডাউনলোড হয়েছে "
                f"({round(d['size'] / 1024 / 1024, 1)} MB)।",
                {"file": d["file"], "size": d["size"]}, date))

    # -- Data transfers (organisation) ------------------------------------
    # Thresholds come from the transfers: section, not alerts:.
    from .transfers import DEFAULT_TRANSFERS

    tcfg = {**DEFAULT_TRANSFERS, **(transfer_settings or {})}
    if tcfg.get("enabled", True):
        try:
            tcounts = db.transfer_counts(date)
            # Uncapped: thresholds must see the whole day, not just the
            # newest 100 rows (on a busy day the capped fetch undercounted
            # cloud/communication seconds and alerts never fired).
            trows = db.transfers_for_date(date, limit=None)
        except Exception:
            tcounts, trows = {}, []

        def _mb(n: int) -> str:
            return f"{round(n / 1024 / 1024, 1)} MB" if n >= 1024 * 1024 else f"{round(n / 1024, 1)} KB"

        def _seconds_of(kind: str) -> int:
            total = 0
            for r in trows:
                if r["transfer_type"] != kind:
                    continue
                try:
                    total += int(json.loads(r["details"] or "{}").get("seconds", 0) or 0)
                except (ValueError, TypeError):
                    continue
            return total

        def _channels(kind: str) -> list[str]:
            seen: list[str] = []
            for r in trows:
                if r["transfer_type"] == kind and r["channel"] and r["channel"] not in seen:
                    seen.append(r["channel"])
            return seen

        # USB inserted
        if tcfg.get("usb_alert", True):
            inserts = [r for r in trows if r["transfer_type"] == "usb_insert"]
            if inserts:
                drives = ", ".join(sorted({r["channel"] for r in inserts if r["channel"]}))
                alerts.append(Alert(
                    "usb_insert", "warning",
                    f"USB ড্রাইভ লাগানো হয়েছে: {drives}",
                    f"{date} তারিখে USB ড্রাইভ লাগানো হয়েছে ({drives})। কী কপি হলো দেখে নিন।",
                    {"drives": drives, "count": len(inserts)}, date))
        # Files copied onto USB
        usb = tcounts.get("usb_copy", {})
        if usb.get("count"):
            files = [r["file_name"] for r in trows if r["transfer_type"] == "usb_copy"][:5]
            alerts.append(Alert(
                "usb_file_copy", "warning",
                f"USB-তে {usb['count']}টি ফাইল কপি হয়েছে ({_mb(usb['bytes'])})",
                f"USB ড্রাইভে {usb['count']}টি ফাইল কপি হয়েছে, মোট {_mb(usb['bytes'])}। "
                f"ফাইল: {', '.join(files)}।",
                {"count": usb["count"], "bytes": usb["bytes"], "files": files}, date))
        # Cloud use over the daily limit
        cloud_seconds = _seconds_of("cloud")
        if cloud_seconds >= int(tcfg.get("cloud_alert_minutes", 15)) * 60:
            domains = _channels("cloud")
            alerts.append(Alert(
                "cloud_upload", "warning",
                f"ক্লাউডে {format_duration(cloud_seconds)} ব্যবহার হয়েছে",
                f"আজ ক্লাউড সাইটে (ড্রাইভ / ড্রপবক্স / WeTransfer ইত্যাদি) "
                f"{format_duration(cloud_seconds)} সময় গেছে: {', '.join(domains[:5])}। "
                "আপলোড হয়েছে কি না ট্রান্সফার তালিকা দেখে নিন।",
                {"seconds": cloud_seconds, "domains": domains}, date))
        # Email / messaging channel use over the daily limit
        comm_seconds = _seconds_of("email") + _seconds_of("message")
        if comm_seconds >= int(tcfg.get("communication_alert_minutes", 30)) * 60:
            domains = _channels("email") + _channels("message")
            alerts.append(Alert(
                "communication_use", "info",
                f"ইমেইল / মেসেজ চ্যানেলে {format_duration(comm_seconds)} ব্যবহার হয়েছে",
                f"আজ ইমেইল / মেসেজ মাধ্যমে (Gmail, WhatsApp Web ইত্যাদি) "
                f"{format_duration(comm_seconds)} সময় গেছে: {', '.join(domains[:5])}।",
                {"seconds": comm_seconds, "domains": domains}, date))
        # Print jobs
        if tcfg.get("print_alert", True):
            prints = tcounts.get("print", {})
            if prints.get("count"):
                docs = [r["file_name"] for r in trows if r["transfer_type"] == "print"][:5]
                alerts.append(Alert(
                    "print_job", "info",
                    f"{prints['count']}টি ডকুমেন্ট প্রিন্ট হয়েছে",
                    f"আজ {prints['count']}টি ডকুমেন্ট প্রিন্ট হয়েছে। "
                    f"ডকুমেন্ট: {', '.join(docs)}।",
                    {"count": prints["count"], "documents": docs}, date))
        # Sensitive folder copy-out — always critical
        if tcfg.get("sensitive_copy_alert", True):
            sens = tcounts.get("sensitive_copy", {})
            if sens.get("count"):
                files = [r["file_name"] for r in trows if r["transfer_type"] == "sensitive_copy"][:5]
                alerts.append(Alert(
                    "sensitive_copy", "critical",
                    f"গুরুত্বপূর্ণ ফোল্ডার থেকে {sens['count']}টি ফাইল বাইরে গেছে!",
                    f"আপনার ঠিক করা গুরুত্বপূর্ণ ফোল্ডার থেকে {sens['count']}টি ফাইল "
                    f"বাইরে কপি/সরানো হয়েছে: {', '.join(files)}। এখনই যাচাই করুন।",
                    {"count": sens["count"], "files": files}, date))
        # Large single-file move
        large = tcounts.get("large_transfer", {})
        if large.get("count"):
            files = [r["file_name"] for r in trows if r["transfer_type"] == "large_transfer"][:5]
            alerts.append(Alert(
                "large_transfer", "warning",
                f"বড় ট্রান্সফার: {', '.join(files)} ({_mb(large['bytes'])})",
                f"{large['count']}টি বড় ফাইল সরানো/কপি হয়েছে, মোট {_mb(large['bytes'])}। "
                f"ফাইল: {', '.join(files)}।",
                {"count": large["count"], "bytes": large["bytes"], "files": files}, date))
    return alerts


def save_alerts(db: Database, alerts: list[Alert]) -> list[Alert]:
    """Save alerts; return only the NEW ones (deduped by the DB)."""
    new = []
    for a in alerts:
        if db.save_alert(a.date, a.alert_type, a.severity, a.title, a.message,
                         json.dumps(a.details, ensure_ascii=False)):
            new.append(a)
    return new


def notify_windows(alerts: list[Alert]) -> bool:
    """Best-effort Windows toast notification. Returns False on Linux /
    when no notifier is available — never raises.

    Only the PowerShell toast path is used: the old plyer path is gone
    (plyer was never in requirements.txt, so it could never run), and
    the old code returned True unconditionally even when PowerShell
    failed — now the result is actually checked.
    """
    if not alerts or sys.platform != "win32":
        return False
    title = f"DeskAudit: {len(alerts)}টি অ্যালার্ট"
    body = "\n".join(a.title for a in alerts[:3])
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] | Out-Null; "
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$xml.LoadXml(\"<toast><visual><binding template='ToastText02'>"
        f"<text id='1'>{title}</text><text id='2'>{body}</text>"
        "</binding></visual></toast>\"); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('DeskAudit')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($xml))"
    )
    try:
        from .utils import run_hidden

        # -WindowStyle Hidden + CREATE_NO_WINDOW (run_hidden): the toast
        # used to flash a PowerShell console window every time an alert
        # fired (v12.5 audit — Firoza's "every so often a PowerShell
        # screen opens"). Now it never shows.
        result = run_hidden(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                             "-Command", ps],
                            capture_output=True, timeout=15, check=False)
        return result.returncode == 0
    except Exception:
        return False
