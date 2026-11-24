"""Data Transfer Monitor — DeskAudit v11 (organisation / per-PC edition).

Watches for data LEAVING the computer, so an admin can see whether
managers or staff transfer company data anywhere:

  * USB / pen drive: inserted, files copied onto it, removed
  * Cloud upload sites: Google Drive, OneDrive, Dropbox, WeTransfer, ...
  * Email & messaging channels: Gmail / Outlook / Yahoo mail, WhatsApp Web,
    Telegram Web, Messenger, Teams, Slack, Discord
  * Printing: every print job (document name, pages, printer)
  * Sensitive folders: a file moved/copied OUT of a folder the admin marked
  * Large transfers: any single file move/copy over the size limit

What is recorded is METADATA ONLY — the same rule DeskAudit has had from
day one: file name, size, destination (drive letter / domain / printer),
time and app. NEVER the content of what was typed or sent: no keystroke
logging, no message / email text, no passwords, no clipboard content.
A browser session on web.whatsapp.com is logged as "message channel used
for 12 minutes", never as what was written there. That is the whole point:
the transfer is visible and preserved, the private content is not read.

How detection works (all local, no internet):
  * USB: Windows drive polling (GetDriveType == DRIVE_REMOVABLE) plus a
    snapshot diff of the drive's files — a file that appears on the drive
    was copied there. Off-Windows this is a no-op. Note: external USB
    hard drives that Windows reports as FIXED drives are not detected —
    pen drives and memory cards are.
  * Cloud / email / message: the browser tracker already records sessions
    and visits; a session on a known cloud / mail / messaging domain (with
    at least ``cloud_min_seconds`` / ``communication_min_seconds`` of
    active time) becomes a transfer row. Visits alone (history-only import)
    give one row per domain per day as a fallback.
  * Print: the Windows print spooler (win32print.EnumJobs) is polled; each
    new job id becomes a row. Off-Windows this is a no-op. A job that
    prints and finishes between two polls is never seen.
  * Sensitive folders + large transfers: scanned from the file_events the
    file watcher already logs — a "moved" event whose source is inside a
    sensitive folder and whose destination is outside it is a copy out;
    a "created" event outside a sensitive folder whose same-named,
    same-sized original still sits inside one is a copy too; a moved or
    copied file over ``large_transfer_mb`` is a large transfer.

All rows live in the ``transfers`` table (see database.py) and are
deduplicated on (date, type, channel, file, time), so repeated scans
never duplicate them. Alerts for these rows live in alerts.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .utils import today_str

DEFAULT_TRANSFERS = {
    "enabled": True,
    "poll_seconds": 60,              # how often the agent scans (USB/print/browser/file)
    "sensitive_folders": [],         # e.g. ["%USERPROFILE%/Documents/CompanyData"]
    "cloud_min_seconds": 60,         # a cloud session shorter than this is not logged
    "communication_min_seconds": 60,  # same for email / messaging channels
    "cloud_alert_minutes": 15,       # alert when cloud use passes this per day
    "communication_alert_minutes": 30,
    "large_transfer_mb": 100,        # a moved file at least this big alerts
    "usb_alert": True,
    "print_alert": True,
    "sensitive_copy_alert": True,
}

# Domain substring -> transfer channel. Checked in order: a domain that is
# both cloud and mail (none today) would take the first match. Substring
# matching is the same style as browser_utils.CATEGORY_MAP.
CLOUD_DOMAINS: tuple[str, ...] = (
    "drive.google.com",
    "onedrive.live.com",
    "onedrive.com",
    "dropbox.com",
    "box.com",
    "mega.nz",
    "mediafire.com",
    "wetransfer.com",
    "sendspace.com",
    "file.io",
    "send-anywhere.com",
    "filemail.com",
    "smash.com",
    "transferxl.com",
    "pcloud.com",
    "sync.com",
    "icloud.com",
    "4shared.com",
    "degoo.com",
    "terabox.com",
)

EMAIL_DOMAINS: tuple[str, ...] = (
    "mail.google.com",
    "gmail.com",
    "outlook.live.com",
    "outlook.office.com",
    "outlook.office365.com",
    "mail.yahoo.com",
    "mail.proton.me",
    "proton.me",
    "tutanota.com",
    "mail.zoho.com",
)

MESSAGE_DOMAINS: tuple[str, ...] = (
    "web.whatsapp.com",
    "web.telegram.org",
    "telegram.org",
    "messenger.com",
    "teams.microsoft.com",
    "slack.com",
    "discord.com",
    "skype.com",
)


def classify_transfer_domain(domain: str) -> str | None:
    """'cloud' | 'email' | 'message' | None, for one domain.

    Email is checked before message, cloud before both — a domain can only
    be one channel, and the order keeps the result stable. Matching is on
    whole labels (exact or a subdomain): "file.io" must not match
    "profile.io", and "box.com" must not match "mybox.com".
    """
    d = (domain or "").lower().strip()
    if not d:
        return None

    def _match(items) -> bool:
        return any(d == item or d.endswith("." + item) for item in items)

    if _match(CLOUD_DOMAINS):
        return "cloud"
    if _match(EMAIL_DOMAINS):
        return "email"
    if _match(MESSAGE_DOMAINS):
        return "message"
    return None


def normalize_path(raw: str) -> str:
    """Lower-case, forward slashes — so C:\\Users\\X and C:/Users/X match."""
    return str(raw or "").replace("\\", "/").lower()


def is_sensitive_path(path: str, sensitive_folders) -> bool:
    """True when `path` is inside (or is) one of the sensitive folders."""
    p = normalize_path(path)
    for folder in (sensitive_folders or []):
        f = normalize_path(folder).rstrip("/")
        if f and (p == f or p.startswith(f + "/")):
            return True
    return False


# ---------------------------------------------------------------------------
# USB drives (Windows only — everything here returns [] off Windows)
# ---------------------------------------------------------------------------

def list_removable_drives() -> list[str]:
    """Drive roots of currently connected removable drives, e.g. ['E:\\']."""
    if os.name != "nt":
        return []
    try:
        import win32api
        import win32file
    except Exception:
        return []
    drives = []
    try:
        for drive in win32api.GetLogicalDriveStrings().split("\x00"):
            if not drive:
                continue
            try:
                if win32file.GetDriveType(drive) == win32file.DRIVE_REMOVABLE:
                    drives.append(drive)
            except Exception:
                continue
    except Exception:
        return []
    return drives


def snapshot_drive(root: str, max_files: int = 20000, max_depth: int = 6) -> dict[str, tuple[int, int]]:
    """{relative posix path: (size_bytes, mtime)} for files on a drive.

    Bounded (file count + depth + hidden/system folders skipped) so a
    big USB stick cannot stall the agent loop. Hidden FILES are included
    (a copied .env must be visible); only hidden/system folders are
    skipped. The cap is high enough that truncation is not a practical
    concern on a pen drive.
    """
    out: dict[str, tuple[int, int]] = {}
    root_path = Path(root)
    if not root_path.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root_path):
        rel_dir = Path(dirpath).relative_to(root_path)
        if len(rel_dir.parts) > max_depth:
            dirnames[:] = []
            continue
        # Skip hidden / system folders ($RECYCLE.BIN, System Volume Information, .Trash).
        dirnames[:] = [d for d in dirnames
                       if not d.startswith((".", "$")) and d.lower() not in ("system volume information",)]
        for name in filenames:
            if len(out) >= max_files:
                return out
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            rel = (rel_dir / name).as_posix() if str(rel_dir) != "." else name
            out[rel] = (int(st.st_size), int(st.st_mtime))
    return out


def diff_snapshots(old: dict, new: dict) -> list[tuple[str, int]]:
    """Files that appeared on the drive — i.e. what was copied onto it.

    Only NEW files count. A file that merely changed size/mtime was
    edited in place on the stick, not copied onto it.
    """
    added = []
    for rel, (size, mtime) in new.items():
        if rel not in old:
            added.append((rel, size))
    return added


# ---------------------------------------------------------------------------
# Print jobs (Windows only)
# ---------------------------------------------------------------------------

def list_print_jobs() -> list[dict]:
    """Current jobs in the local print spooler.

    Each dict: {job_id, document, printer, user, pages, size_bytes}.
    Returns [] off Windows, when no printers exist, or on any error —
    printing must never crash the agent.
    """
    if os.name != "nt":
        return []
    try:
        import win32print
    except Exception:
        return []
    jobs = []
    try:
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)
    except Exception:
        return []
    for printer in printers:
        name = printer.get("pPrinterName") if isinstance(printer, dict) else None
        if not name:
            continue
        handle = None
        try:
            handle = win32print.OpenPrinter(name)
            for job in win32print.EnumJobs(handle, 0, 100, 1):
                try:
                    submitted = job.get("Submitted") or ()
                    jobs.append({
                        "job_id": int(job.get("JobId") or 0),
                        "document": str(job.get("pDocument") or ""),
                        "printer": str(job.get("pPrinterName") or name),
                        "user": str(job.get("pUserName") or ""),
                        "pages": int(job.get("TotalPages") or 0),
                        "size_bytes": int(job.get("Size") or 0),
                        "submitted": submitted,
                    })
                except Exception:
                    continue
        except Exception:
            continue
        finally:
            if handle is not None:
                try:
                    win32print.ClosePrinter(handle)
                except Exception:
                    pass
    return jobs


# ---------------------------------------------------------------------------
# Transfer row
# ---------------------------------------------------------------------------

@dataclass
class Transfer:
    transfer_type: str  # usb_insert | usb_remove | usb_copy | cloud | email |
    # message | print | sensitive_copy | large_transfer
    channel: str = ""       # drive letter, domain, printer name, folder
    file_name: str = ""
    file_path: str = ""
    size_bytes: int | None = None
    destination: str = ""   # where the data went
    app: str = ""
    occurred_at: str = ""
    details: dict = field(default_factory=dict)
    date: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.transfer_type, "channel": self.channel,
            "file": self.file_name, "path": self.file_path,
            "size_bytes": self.size_bytes, "destination": self.destination,
            "app": self.app, "at": self.occurred_at, "details": self.details,
            "date": self.date,
        }


class TransferMonitor:
    """Polls the machine for transfers and logs them to the database.

    Created once by the agent loop; ``scan_today()`` is called every
    ``poll_seconds``. Every log call is INSERT OR IGNORE, so running the
    same scan twice never duplicates a row.
    """

    def __init__(self, db, settings: dict | None = None, log=None):
        self.db = db
        self.settings = {**DEFAULT_TRANSFERS, **(settings or {})}
        self.log = log
        # Sensitive folders: expand %VAR% / ~, then normalise.
        folders = []
        for raw in (self.settings.get("sensitive_folders") or []):
            try:
                folders.append(str(Path(os.path.expandvars(os.path.expanduser(str(raw))))))
            except Exception:
                continue
        self.sensitive_folders = folders
        self._usb_snapshots: dict[str, dict] = {}
        self._seen_usb: set[str] = set()
        self._usb_seeded = False
        self._seen_print_jobs: set[tuple[str, int]] = set()

    # -- helpers ---------------------------------------------------------
    def _log(self, transfer: Transfer) -> bool:
        try:
            return self.db.log_transfer(
                transfer.date or today_str(), transfer.transfer_type,
                transfer.channel, transfer.file_name, transfer.file_path,
                transfer.size_bytes, transfer.destination, transfer.app,
                transfer.occurred_at or datetime.now().astimezone().isoformat(timespec="seconds"),
                json.dumps(transfer.details, ensure_ascii=False) if transfer.details else "",
            )
        except Exception as exc:
            # A failing transfers table used to lose rows with no trace
            # anywhere while scan_today reported success. Log it.
            import logging

            logging.getLogger("deskaudit.transfers").warning(
                "Transfer not logged (%s %s): %s",
                transfer.transfer_type, transfer.file_name, exc)
            return False

    def _now(self) -> datetime:
        return datetime.now().astimezone()

    def _stamp(self, now: datetime, index: int) -> str:
        """Per-event timestamp: several events found in one poll must not
        share the same occurred_at, or the transfers UNIQUE key silently
        drops all but the first (two print jobs of one document, two USB
        copies of one name)."""
        from datetime import timedelta

        # Millisecond precision matters: two events in one poll stamped
        # with timespec="seconds" collide in the transfers UNIQUE key.
        return (now + timedelta(milliseconds=index)).isoformat(timespec="milliseconds")

    def _seed_usb_seen(self, date: str) -> None:
        """On the first poll, treat drives with a usb_insert row TODAY as
        already seen — otherwise every agent restart logs a spurious
        usb_insert for a drive that was never unplugged."""
        if self._usb_seeded:
            return
        self._usb_seeded = True
        try:
            rows = self.db.conn.execute(
                "SELECT channel FROM transfers WHERE date = ? AND transfer_type = 'usb_insert'",
                (date,)).fetchall()
            current = set(list_removable_drives())
            for r in rows:
                if r["channel"] in current:
                    self._seen_usb.add(r["channel"])
                    self._usb_snapshots[r["channel"]] = snapshot_drive(r["channel"])
        except Exception:
            pass

    # -- USB --------------------------------------------------------------
    def poll_usb(self, now: datetime | None = None) -> list[Transfer]:
        """Detect inserted / removed drives and files copied onto them."""
        now = now or self._now()
        date = now.strftime("%Y-%m-%d")
        found: list[Transfer] = []
        self._seed_usb_seen(date)
        current = set(list_removable_drives())
        # Removed drives (were seen, now gone).
        for drive in sorted(self._seen_usb - current):
            t = Transfer("usb_remove", channel=drive, destination=drive,
                         occurred_at=now.isoformat(timespec="seconds"), date=date)
            if self._log(t):
                found.append(t)
            self._usb_snapshots.pop(drive, None)
        for drive in sorted(current):
            snap = snapshot_drive(drive)
            if drive not in self._seen_usb:
                t = Transfer("usb_insert", channel=drive, destination=drive,
                             occurred_at=now.isoformat(timespec="seconds"), date=date)
                if self._log(t):
                    found.append(t)
            else:
                old = self._usb_snapshots.get(drive, {})
                for i, (rel, size) in enumerate(diff_snapshots(old, snap)):
                    dest_path = str(Path(drive) / rel)
                    # Skip a file already logged from a "moved" file event
                    # (matched by full path — the same name in another
                    # folder is a different file).
                    try:
                        if self.db.has_transfer(date, "usb_copy", dest_path):
                            continue
                    except Exception:
                        pass
                    t = Transfer("usb_copy", channel=drive,
                                 file_name=Path(rel).name,
                                 file_path=dest_path,
                                 size_bytes=size, destination=drive,
                                 occurred_at=self._stamp(now, i),
                                 date=date)
                    if self._log(t):
                        found.append(t)
            self._usb_snapshots[drive] = snap
        self._seen_usb = current
        return found

    # -- Print ------------------------------------------------------------
    def poll_printers(self, now: datetime | None = None) -> list[Transfer]:
        """Log each new spooler job once.

        Job IDs are per-printer, so the seen key is (printer, job_id) —
        Printer B's job 5 must not be skipped because Printer A's job 5
        was seen. Note the spooler is polled: a job that prints and
        finishes between two polls is never seen (see README).
        """
        now = now or self._now()
        date = now.strftime("%Y-%m-%d")
        found: list[Transfer] = []
        for i, job in enumerate(list_print_jobs()):
            job_id = job.get("job_id") or 0
            printer = job.get("printer", "")
            if not job_id or (printer, job_id) in self._seen_print_jobs:
                continue
            self._seen_print_jobs.add((printer, job_id))
            t = Transfer("print", channel=printer,
                         file_name=job.get("document", ""),
                         size_bytes=job.get("size_bytes") or None,
                         destination=printer,
                         occurred_at=self._stamp(now, i),
                         details={"pages": job.get("pages", 0),
                                  "user": job.get("user", "")},
                         date=date)
            if self._log(t):
                found.append(t)
        return found

    # -- Browser channels --------------------------------------------------
    def scan_browser_channels(self, date: str | None = None) -> list[Transfer]:
        """Cloud / email / message domain use, from browser sessions + visits."""
        date = date or today_str()
        found: list[Transfer] = []
        min_cloud = int(self.settings.get("cloud_min_seconds", 60))
        min_comm = int(self.settings.get("communication_min_seconds", 60))
        # Live sessions (have a duration): log each qualifying session once.
        try:
            sessions = self.db.conn.execute(
                """SELECT browser, domain, started_at, active_seconds
                   FROM browser_sessions WHERE substr(started_at, 1, 10) = ?
                   AND domain != ''""", (date,)).fetchall()
        except Exception:
            sessions = []
        session_domains: set[str] = set()
        for r in sessions:
            kind = classify_transfer_domain(r["domain"] or "")
            if not kind:
                continue
            # Mark the domain as session-covered BEFORE the minimum check:
            # a 30 s Gmail session must not fall through to the history
            # fallback and get logged anyway as a zero-second row.
            session_domains.add(r["domain"])
            seconds = float(r["active_seconds"] or 0)
            minimum = min_cloud if kind == "cloud" else min_comm
            if seconds < minimum:
                continue
            t = Transfer(kind, channel=r["domain"], app=r["browser"] or "",
                         occurred_at=r["started_at"],
                         details={"seconds": int(seconds)}, date=date)
            if self._log(t):
                found.append(t)
        # History-only fallback: one row per domain per day, when no live
        # session covered that domain (e.g. the browser was closed).
        try:
            visits = self.db.conn.execute(
                """SELECT domain, MIN(visited_at) AS first_seen
                   FROM browser_visits WHERE substr(visited_at, 1, 10) = ?
                   AND domain != '' GROUP BY domain""", (date,)).fetchall()
        except Exception:
            visits = []
        for r in visits:
            domain = r["domain"] or ""
            if domain in session_domains:
                continue
            kind = classify_transfer_domain(domain)
            if not kind:
                continue
            t = Transfer(kind, channel=domain, occurred_at=r["first_seen"],
                         details={"source": "history"}, date=date)
            if self._log(t):
                found.append(t)
        return found

    def _sensitive_original_exists(self, file_name: str, size) -> bool:
        """True when a file with this name (and size, when known) still
        sits inside a sensitive folder — i.e. the created file outside
        it is a copy of that original, not a new file of the same name."""
        for folder in self.sensitive_folders:
            candidate = Path(folder) / file_name
            try:
                if candidate.is_file():
                    if size is None or candidate.stat().st_size == int(size):
                        return True
            except (OSError, ValueError):
                continue
        return False

    def _usb_drive_for(self, dest: str, removable: list[str]) -> str:
        """The removable drive root `dest` sits on, or ''."""
        dest_norm = normalize_path(dest)
        for d in removable:
            prefix = normalize_path(d).rstrip("/") + "/"
            if (dest_norm + "/").startswith(prefix):
                return d
        return ""

    # -- File events: sensitive copies + large moves ------------------------
    def scan_file_events(self, date: str | None = None) -> list[Transfer]:
        """Copies out of sensitive folders + large file moves/copies, from
        file_events. A plain Ctrl+C/Ctrl+V copy produces a `created` event
        at the destination (no `moved` event) — that is the most common
        way data leaves, so `created` events are scanned too."""
        date = date or today_str()
        found: list[Transfer] = []
        large_bytes = int(self.settings.get("large_transfer_mb", 100)) * 1024 * 1024
        removable = list_removable_drives()
        try:
            rows = self.db.conn.execute(
                """SELECT event_type, path, dest_path, size_bytes, occurred_at
                   FROM file_events WHERE substr(occurred_at, 1, 10) = ?""",
                (date,)).fetchall()
        except Exception:
            rows = []
        for r in rows:
            src = r["path"] or ""
            dest = r["dest_path"] or ""
            size = r["size_bytes"]
            name = Path(src.replace("\\", "/")).name
            # A move out of a sensitive folder to anywhere else.
            if (r["event_type"] == "moved" and dest
                    and is_sensitive_path(src, self.sensitive_folders)
                    and not is_sensitive_path(dest, self.sensitive_folders)):
                t = Transfer("sensitive_copy", channel=Path(dest.replace("\\", "/")).parent.as_posix(),
                             file_name=name, file_path=src, size_bytes=size,
                             destination=dest,
                             occurred_at=r["occurred_at"], date=date)
                if self._log(t):
                    found.append(t)
            # A plain copy out: a `created` event outside a sensitive
            # folder, for a file whose same-named, same-sized original
            # still sits inside one.
            if (r["event_type"] == "created"
                    and not is_sensitive_path(src, self.sensitive_folders)
                    and self._sensitive_original_exists(name, size)):
                t = Transfer("sensitive_copy",
                             channel=Path(src.replace("\\", "/")).parent.as_posix(),
                             file_name=name, file_path=src, size_bytes=size,
                             destination=src,
                             occurred_at=r["occurred_at"],
                             details={"source": "created"}, date=date)
                if self._log(t):
                    found.append(t)
            # A big file moved OR copied anywhere (a copy out), incl. onto
            # a USB drive.
            if size and int(size) >= large_bytes and r["event_type"] in ("moved", "created"):
                target = dest if (r["event_type"] == "moved" and dest) else src
                usb_drive = self._usb_drive_for(target, removable)
                if usb_drive:
                    try:
                        if self.db.has_transfer(date, "usb_copy", target):
                            continue
                    except Exception:
                        pass
                    t = Transfer("usb_copy", channel=usb_drive,
                                 file_name=name, file_path=src, size_bytes=int(size),
                                 destination=target,
                                 occurred_at=r["occurred_at"], date=date)
                else:
                    t = Transfer("large_transfer",
                                 channel=Path(target.replace("\\", "/")).parent.as_posix(),
                                 file_name=name, file_path=src, size_bytes=int(size),
                                 destination=target,
                                 occurred_at=r["occurred_at"],
                                 details={"source": r["event_type"]}, date=date)
                if self._log(t):
                    found.append(t)
        return found

    def scan_today(self) -> dict[str, int]:
        """One polling round: USB + printers + browser channels + file events."""
        if not self.settings.get("enabled", True):
            return {"usb": 0, "print": 0, "browser": 0, "files": 0}
        now = self._now()
        counts = {
            "usb": len(self.poll_usb(now)),
            "print": len(self.poll_printers(now)),
            "browser": len(self.scan_browser_channels(now.strftime("%Y-%m-%d"))),
            "files": len(self.scan_file_events(now.strftime("%Y-%m-%d"))),
        }
        return counts
