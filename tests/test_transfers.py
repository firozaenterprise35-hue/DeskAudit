"""Data Transfer Monitor tests — classification, USB snapshot diff,
sensitive folders, browser/file scans, transfer alerts, dashboard API
and Settings round-trip. Synthetic data only.

Run:  python tests/test_transfers.py
"""

import json
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.alerts import check_alerts
from deskaudit.database import Database
from deskaudit.transfers import (
    DEFAULT_TRANSFERS, TransferMonitor, classify_transfer_domain,
    diff_snapshots, is_sensitive_path, list_print_jobs,
    list_removable_drives, snapshot_drive,
)


def test_classify_transfer_domain():
    assert classify_transfer_domain("drive.google.com") == "cloud"
    assert classify_transfer_domain("www.dropbox.com") == "cloud"
    assert classify_transfer_domain("wetransfer.com") == "cloud"
    assert classify_transfer_domain("mail.google.com") == "email"
    assert classify_transfer_domain("outlook.live.com") == "email"
    assert classify_transfer_domain("web.whatsapp.com") == "message"
    assert classify_transfer_domain("web.telegram.org") == "message"
    assert classify_transfer_domain("teams.microsoft.com") == "message"
    assert classify_transfer_domain("github.com") is None
    assert classify_transfer_domain("youtube.com") is None
    assert classify_transfer_domain("") is None
    print("classify_transfer_domain tests passed")


def test_is_sensitive_path():
    folders = [r"C:\Users\F\Documents\CompanyData", "/home/user/Accounts"]
    assert is_sensitive_path(r"C:\Users\F\Documents\CompanyData\secret.xlsx", folders)
    assert is_sensitive_path("/home/user/Accounts/ledger.xlsx", folders)
    assert is_sensitive_path(r"c:\users\f\documents\companydata\a.txt", folders)  # case-insensitive
    assert not is_sensitive_path(r"C:\Users\F\Documents\Other\a.txt", folders)
    assert not is_sensitive_path("/home/user/Documents/a.txt", folders)
    assert not is_sensitive_path("/home/user/Accounts2/a.txt", folders)  # prefix, not parent
    print("is_sensitive_path tests passed")


def test_snapshot_diff():
    tmp = Path(tempfile.mkdtemp())
    drive = tmp / "usb"
    (drive / "docs").mkdir(parents=True)
    (drive / "docs" / "a.txt").write_text("hello", encoding="utf-8")
    snap1 = snapshot_drive(str(drive))
    assert "docs/a.txt" in snap1
    time.sleep(0.01)
    (drive / "docs" / "b.txt").write_text("copied file", encoding="utf-8")
    (drive / "top.pdf").write_text("x", encoding="utf-8")
    snap2 = snapshot_drive(str(drive))
    added = diff_snapshots(snap1, snap2)
    names = {rel for rel, _ in added}
    assert names == {"docs/b.txt", "top.pdf"}, names
    assert diff_snapshots(snap2, snap2) == []
    print("snapshot diff tests passed")


def test_off_windows_noops():
    # On this Linux box both must return [] and never raise.
    assert list_removable_drives() == []
    assert list_print_jobs() == []
    print("off-Windows no-op tests passed")


def test_log_transfer_dedup():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    date = datetime.now().strftime("%Y-%m-%d")
    when = datetime.now().astimezone().isoformat(timespec="seconds")
    assert db.log_transfer(date, "usb_copy", "E:", "a.xlsx", r"E:\a.xlsx",
                           1234, "E:", occurred_at=when) is True
    # Same (date, type, channel, file, time) -> ignored.
    assert db.log_transfer(date, "usb_copy", "E:", "a.xlsx", r"E:\a.xlsx",
                           1234, "E:", occurred_at=when) is False
    assert db.has_transfer(date, "usb_copy", r"E:\a.xlsx") is True
    assert db.has_transfer(date, "usb_copy", r"E:\other.xlsx") is False
    # Same file NAME in another folder is a different file (full-path match).
    assert db.has_transfer(date, "usb_copy", r"F:\a.xlsx") is False
    counts = db.transfer_counts(date)
    assert counts["usb_copy"] == {"count": 1, "bytes": 1234}, counts
    rows = db.transfers_for_date(date)
    assert len(rows) == 1 and rows[0]["file_name"] == "a.xlsx"
    db.close()
    print("log_transfer dedup tests passed")


def seed_browser_and_files(tmp: Path):
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")
    # Cloud session 20 min active (>= 60s minimum) -> cloud row
    bsid = db.start_browser_session("Chrome", "https://drive.google.com/drive/my-drive",
                                    "drive.google.com", "My Drive", "work", True,
                                    base, project="Office")
    db.close_browser_session(bsid, base + timedelta(minutes=20), 1200, 1200, 0)
    # Gmail session 30 s (< 60s minimum) -> NO row — and the history
    # fallback must NOT log it either, even though a visit row exists
    # for the same domain (the session covers the domain first).
    bsid = db.start_browser_session("Chrome", "https://mail.google.com/mail/u/0/",
                                    "mail.google.com", "Gmail", "email", True,
                                    base + timedelta(hours=1), project="Office")
    db.close_browser_session(bsid, base + timedelta(hours=1), 30, 30, 0)
    db.log_browser_visit("Chrome", "https://mail.google.com/mail/u/0/",
                         "mail.google.com", "Gmail",
                         base + timedelta(hours=1, minutes=1), project="Office")
    # WhatsApp Web visit only (history fallback) -> message row
    db.log_browser_visit("Chrome", "https://web.whatsapp.com/", "web.whatsapp.com",
                         "WhatsApp", base + timedelta(hours=2), project="Office")
    # Sensitive folder move-out -> sensitive_copy row
    sensitive = tmp / "CompanyData"
    sensitive.mkdir()
    db.log_file_event("moved", str(sensitive / "secret.xlsx"),
                      dest_path=str(tmp / "Desktop" / "secret.xlsx"),
                      occurred_at=base + timedelta(hours=3), project="Office")
    # Move INSIDE the sensitive folder -> no row
    db.log_file_event("moved", str(sensitive / "a.xlsx"),
                      dest_path=str(sensitive / "sub" / "a.xlsx"),
                      occurred_at=base + timedelta(hours=3, minutes=1), project="Office")
    # Big move 150 MB (>= 100 MB default) -> large_transfer row
    db.log_file_event("moved", "/home/user/Documents/big.zip",
                      dest_path="/home/user/Downloads/big.zip",
                      size_bytes=150 * 1024 * 1024,
                      occurred_at=base + timedelta(hours=4), project="Office")
    return db, date, sensitive


def test_monitor_scans():
    tmp = Path(tempfile.mkdtemp())
    db, date, sensitive = seed_browser_and_files(tmp)
    monitor = TransferMonitor(db, {
        **DEFAULT_TRANSFERS,
        "sensitive_folders": [str(sensitive)],
    })
    found = monitor.scan_browser_channels(date)
    kinds = {t.transfer_type for t in found}
    assert "cloud" in kinds, kinds
    assert "message" in kinds, kinds
    assert "email" not in kinds, kinds  # 30 s session is below the minimum
    # Second scan: same rows, deduped -> nothing new.
    assert monitor.scan_browser_channels(date) == []
    found = monitor.scan_file_events(date)
    kinds = {t.transfer_type for t in found}
    assert "sensitive_copy" in kinds, kinds
    assert "large_transfer" in kinds, kinds
    assert monitor.scan_file_events(date) == []
    counts = db.transfer_counts(date)
    assert counts["cloud"]["count"] == 1
    assert counts["message"]["count"] == 1
    assert counts["sensitive_copy"]["count"] == 1
    assert counts["large_transfer"]["count"] == 1
    assert counts["large_transfer"]["bytes"] == 150 * 1024 * 1024
    db.close()
    print("monitor scan tests passed")


def test_transfer_alerts():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    base = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    date = base.strftime("%Y-%m-%d")

    def at(minutes):
        return (base + timedelta(minutes=minutes)).isoformat(timespec="seconds")

    db.log_transfer(date, "usb_insert", "E:", occurred_at=at(0))
    db.log_transfer(date, "usb_copy", "E:", "report.xlsx", r"E:\report.xlsx",
                    2 * 1024 * 1024, "E:", occurred_at=at(1))
    db.log_transfer(date, "usb_copy", "E:", "data.csv", r"E:\data.csv",
                    512 * 1024, "E:", occurred_at=at(2))
    db.log_transfer(date, "cloud", "drive.google.com", occurred_at=at(10),
                    details=json.dumps({"seconds": 1200}))
    db.log_transfer(date, "email", "mail.google.com", occurred_at=at(20),
                    details=json.dumps({"seconds": 1200}))
    db.log_transfer(date, "message", "web.whatsapp.com", occurred_at=at(30),
                    details=json.dumps({"seconds": 900}))
    db.log_transfer(date, "print", "HP LaserJet", "Invoice.docx", occurred_at=at(40),
                    details=json.dumps({"pages": 3}))
    db.log_transfer(date, "sensitive_copy", "Desktop", "secret.xlsx",
                    occurred_at=at(50))
    db.log_transfer(date, "large_transfer", "Downloads", "big.zip",
                    150 * 1024 * 1024, occurred_at=at(60))
    alerts = check_alerts(db, date)
    types = {a.alert_type for a in alerts}
    for expected in ("usb_insert", "usb_file_copy", "cloud_upload",
                     "communication_use", "print_job", "sensitive_copy",
                     "large_transfer"):
        assert expected in types, types
    sev = {a.alert_type: a.severity for a in alerts}
    assert sev["sensitive_copy"] == "critical"
    # No transfers at all -> no transfer alerts.
    tmp2 = Path(tempfile.mkdtemp())
    db2 = Database(tmp2 / "test.db")
    alerts2 = check_alerts(db2, date)
    assert not ({a.alert_type for a in alerts2}
                & {"usb_insert", "usb_file_copy", "cloud_upload", "communication_use",
                   "print_job", "sensitive_copy", "large_transfer"}), [a.to_dict() for a in alerts2]
    db.close(); db2.close()
    print("transfer alert tests passed")


def test_dashboard_transfers_api():
    import json as _json
    import urllib.request

    from deskaudit.dashboard import DashboardServer, transfer_summary

    tmp = Path(tempfile.mkdtemp())
    db, date, _ = seed_browser_and_files(tmp)
    monitor = TransferMonitor(db, {**DEFAULT_TRANSFERS, "sensitive_folders": [str(_)]})
    monitor.scan_browser_channels(date)
    monitor.scan_file_events(date)
    s = transfer_summary(db, date)
    assert s["total"] == 4, s
    assert s["counts"]["cloud"]["count"] == 1
    assert s["cloud_domains"] == ["drive.google.com"]
    db.close()
    server = DashboardServer(tmp / "test.db", host="127.0.0.1", port=0,
                             project_root=tmp, lock_enabled=False)
    url = server.start()
    try:
        with urllib.request.urlopen(url + f"api/transfers?date={date}", timeout=5) as r:
            data = _json.loads(r.read())
            assert data["total"] == 4
            assert any(row["type"] == "sensitive_copy" for row in data["rows"])
        with urllib.request.urlopen(url + f"api/summary?date={date}", timeout=5) as r:
            data = _json.loads(r.read())
            assert data["transfers"]["total"] == 4
            assert data["kpis"]["transfers_total"] == 4
            assert data["kpis"]["usb_copies"] == 0
    finally:
        server.stop()
    print("dashboard transfers API tests passed")


def test_dashboard_transfers_settings():
    import os

    from deskaudit.dashboard import get_settings, save_settings

    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    old_env = os.environ.pop("DESKAUDIT_LLM_API_KEY", None)
    try:
        view = get_settings(tmp / "config.yaml")
        assert view["transfers"]["enabled"] is True
        assert view["transfers"]["poll_seconds"] == 60
        assert view["transfers"]["large_transfer_mb"] == 100
        assert view["transfers"]["sensitive_folders"] == []
        saved = save_settings(tmp / "config.yaml", {
            "transfers": {
                "poll_seconds": 120,
                "large_transfer_mb": 250,
                "sensitive_folders": "%USERPROFILE%/Documents/CompanyData, D:/Accounts",
                "usb_alert": False,
            },
        })
        assert saved["transfers"]["poll_seconds"] == 120
        assert saved["transfers"]["large_transfer_mb"] == 250
        assert saved["transfers"]["usb_alert"] is False
        assert saved["transfers"]["sensitive_folders"] == [
            "%USERPROFILE%/Documents/CompanyData", "D:/Accounts"]
        local_text = (tmp / "config.local.yaml").read_text(encoding="utf-8")
        assert "CompanyData" in local_text
        # Bad value raises and writes nothing.
        try:
            save_settings(tmp / "config.yaml", {"transfers": {"poll_seconds": 99999}})
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range poll_seconds must raise ValueError")
        assert get_settings(tmp / "config.yaml")["transfers"]["poll_seconds"] == 120
    finally:
        if old_env is not None:
            os.environ["DESKAUDIT_LLM_API_KEY"] = old_env
    print("dashboard transfers settings tests passed")


if __name__ == "__main__":
    test_classify_transfer_domain()
    test_is_sensitive_path()
    test_snapshot_diff()
    test_off_windows_noops()
    test_log_transfer_dedup()
    test_monitor_scans()
    test_transfer_alerts()
    test_dashboard_transfers_api()
    test_dashboard_transfers_settings()
