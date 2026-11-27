"""DeskAudit entry point.

Usage:
    python main.py                 # run the agent until Ctrl+C
    python main.py --report        # today's summary (+ reports/YYYY-MM-DD.md/.csv)
    python main.py --report --date 2026-11-23
    python main.py --once          # one poll cycle, useful for testing
    python main.py --browser-audit # import ALL browser history once, then exit
    python main.py --browser-audit --date 2026-11-23  # (date is ignored, kept for symmetry)
    python main.py --projects      # list projects + rules from projects.yaml
    python main.py --project-add "Client A" --folder "%USERPROFILE%/Documents/ClientA" --goal 120
    python main.py --project-tag domain github.com --project Study
    python main.py --retag         # re-tag ALL stored rows with current projects.yaml
    python main.py --retag --date 2026-11-23
    python main.py --suggest-projects [--date 2026-11-23]
    python main.py --dashboard [--port 8000] [--host 127.0.0.1]
    python main.py --alerts [--date 2026-11-23] [--notify]
    python main.py --alerts-list [--date 2026-11-23] [--unread]
    python main.py --transfers [--date 2026-11-23]  # data transfers today
    python main.py --ai-summary [--date 2026-11-23] [--weekly] [--llm]
    python main.py --backup | --backup-list | --restore <file> | --restore --latest
    python main.py --evening-job          # backup + alerts + daily summary (9pm job)
    python main.py --schedule-summary     # create the Windows 21:00 task
    python main.py --unschedule-summary   # remove it

Pause: create an empty file named PAUSE next to main.py to pause tracking.
Browser tracking: history polling runs every browser_poll_seconds while the
agent runs. For an exact active-tab URL, either launch Chrome with
--remote-debugging-port=9222 (set browser_cdp_port in config.yaml) or
install extension/chrome/ and set browser_receiver_enabled: true.
Projects: edit projects.yaml, then run --retag so old rows get the new tags.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

def _app_dir() -> Path:
    """Folder holding the app: the exe's folder when frozen by PyInstaller
    (DeskAudit.exe), otherwise the folder holding main.py."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


sys.path.insert(0, str(_app_dir()))

from deskaudit.app_tracker import AppTracker
from deskaudit.browser_receiver import BrowserReceiver
from deskaudit.browser_tracker import BrowserTracker
from deskaudit.config import Config
from deskaudit.database import Database
from deskaudit.file_watcher import FileWatcher
from deskaudit.projects import Tagger
from deskaudit.report import generate_report
from deskaudit.utils import format_duration, today_str


def _refresh_before_summary(db: Database, config: Config, tagger: Tagger) -> None:
    """Poll browser history + scan transfers before an alerts check or a
    summary, so a one-off command sees today's data even when the agent
    isn't running (otherwise --alerts / --evening-job report an empty day
    with no warning). Best-effort: failures are logged, never fatal."""
    if config.browser_enabled:
        try:
            browser = BrowserTracker(db, config, tagger=tagger)
            browser.poll_history()
        except Exception as exc:
            print(f"(Browser history poll failed: {exc})")
    if (config.transfers or {}).get("enabled", True):
        try:
            from deskaudit.transfers import TransferMonitor

            TransferMonitor(db, config.transfers).scan_today()
        except Exception as exc:
            print(f"(Transfer scan failed: {exc})")


def load_tagger(config: Config) -> Tagger:
    """Load projects.yaml next to config.yaml; fall back to empty tagger."""
    projects_path = Path(config.project_root) / "projects.yaml"
    tagger = Tagger.load(projects_path) if projects_path.exists() else Tagger.empty()
    return tagger


def sync_projects_to_db(db: Database, tagger: Tagger) -> None:
    for p in tagger.projects:
        db.upsert_project(p.name, p.description, p.color, p.parent, p.daily_goal_minutes)


def merge_db_overrides(db: Database, tagger: Tagger) -> None:
    """DB overrides (set with --project-tag) must survive a restart, so
    load them into the tagger before any tagging / retagging happens."""
    try:
        for row in db.list_project_overrides():
            tags = [t for t in (row["tags"] or "").split(",") if t]
            tagger.add_override(row["match_type"], row["match_value"], row["project"], tags)
    except Exception:
        pass


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return logging.getLogger("deskaudit")


def run_agent(config: Config, log: logging.Logger) -> int:
    _hide_console()  # background mode: no CMD window on the desktop
    db = Database(config.db_path)
    tagger = load_tagger(config)
    sync_projects_to_db(db, tagger)
    merge_db_overrides(db, tagger)
    # Auto-backup once a day, on agent start (skipped if today's backup exists).
    if config.backup_on_start:
        try:
            from deskaudit.backup import latest_backup, make_backup

            today = today_str()
            latest = latest_backup(config.project_root)
            if latest is None or today not in latest.name:
                dest = make_backup(config.db_path, config.project_root,
                                   keep_days=config.backup_keep_days,
                                   keep_count=config.backup_keep_count)
                log.info("Daily backup saved: %s", dest)
        except Exception as exc:
            log.warning("Auto-backup failed (agent keeps running): %s", exc)
    log.info("Projects loaded: %s", ", ".join(p.name for p in tagger.projects) or "none")
    tracker = AppTracker(db, config.idle_threshold_seconds, tagger=tagger)
    watcher = FileWatcher(db, config, tagger=tagger)
    browser = BrowserTracker(db, config, tagger=tagger)
    receiver = None
    dashboard_server = None
    gate = None
    transfer_monitor = None
    pause_file = config.project_root / "PAUSE"
    last_browser_poll = 0.0
    last_alert_check = 0.0
    last_transfer_scan = 0.0
    last_delete_scan = 0.0
    last_telegram_poll = 0.0
    last_telegram_digest = 0.0
    last_email = 0.0
    telegram_bot = None
    # Coerce once at startup: a bad poll_seconds used to raise inside
    # the loop condition every 5 s and kill the agent silently.
    try:
        transfer_poll = int((config.transfers or {}).get("poll_seconds", 60))
    except (TypeError, ValueError):
        log.warning("Bad transfers.poll_seconds — using 60s.")
        transfer_poll = 60
    if transfer_poll < 5:
        transfer_poll = 5

    def _safe_stop(name, fn):
        try:
            fn()
        except Exception as exc:
            log.warning("Stop %s failed: %s", name, exc)

    def shutdown(*_args):
        log.info("Shutting down, closing current session...")
        _safe_stop("tracker", tracker.stop)
        _safe_stop("browser", browser.stop)
        if receiver is not None:
            _safe_stop("receiver", receiver.stop)
        if gate is not None:
            _safe_stop("gate", gate.stop)
        if dashboard_server is not None:
            _safe_stop("dashboard", dashboard_server.stop)
        _safe_stop("watcher", watcher.stop)
        _safe_stop("db", db.close)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    try:
        watcher_started = watcher.start()
    except Exception as exc:
        log.warning("File watcher failed to start (agent keeps running): %s", exc)
        watcher_started = False
    if watcher_started:
        log.info("Watching folders: %s", ", ".join(watcher.watched))
    else:
        log.warning("File watcher did not start — check watch_folders / watchdog install.")

    if config.browser_enabled:
        log.info("Browser tracking on: profiles found: %s",
                 ", ".join(f"{p.browser} ({p.profile_name})" for p in browser.profiles) or "none yet")
        if config.browser_receiver_enabled:
            receiver = BrowserReceiver(browser, config.browser_receiver_port, log)
            receiver.start()
        if config.browser_cdp_port:
            log.info("Browser CDP enabled on port %s", config.browser_cdp_port)
    else:
        log.info("Browser tracking off (browser_enabled: false).")

    # Data Transfer Monitor (organisation): USB / cloud / email / message /
    # print / sensitive folders / large moves. Metadata only.
    if (config.transfers or {}).get("enabled", True):
        try:
            from deskaudit.transfers import TransferMonitor

            transfer_monitor = TransferMonitor(db, config.transfers, log)
            log.info("Data Transfer Monitor on: sensitive folders: %s",
                     ", ".join(transfer_monitor.sensitive_folders) or "none set")
        except Exception as exc:  # transfers must never crash the agent
            log.warning("Data Transfer Monitor did not start: %s", exc)

    # New-program watch (v12.5): snapshot the processes running NOW, so
    # only programs opened AFTER the agent starts are reported.
    app_watch = None
    if (config.app_watch or {}).get("enabled", True):
        try:
            from deskaudit.app_watch import AppWatch

            app_watch = AppWatch(config.app_watch, log)
            log.info("New-program watch on: baseline %s processes.",
                     app_watch.baseline())
        except Exception as exc:  # the watch must never crash the agent
            log.warning("New-program watch did not start: %s", exc)

    # File explorer (v12.9): browse drives/folders from Telegram and
    # copy / move / send / hide files. Created before the bot so the
    # bot can offer it; /hidden restores live here too.
    file_explorer = None
    if (config.explorer or {}).get("enabled", True):
        try:
            from deskaudit.file_explorer import FileExplorer

            file_explorer = FileExplorer(
                db, config.explorer, log,
                project_root=config.project_root)
            log.info("File explorer on: hidden folder at %s.",
                     file_explorer.hidden_root)
        except Exception as exc:  # the explorer must never crash the agent
            log.warning("File explorer did not start: %s", exc)

    # Delete protection (v12.8): a deleted file is moved to a hidden
    # quarantine (another drive, when configured) instead of being
    # lost — the owner decides on Telegram (হ্যাঁ = purge, না = restore).
    delete_protector = None
    if (config.delete_protect or {}).get("enabled", True):
        try:
            from deskaudit.delete_protect import DeleteProtector

            delete_protector = DeleteProtector(
                db, config.delete_protect, log,
                project_root=config.project_root,
                protected_folders=config.effective_watch_folders())
            log.info("Delete protection on: quarantine at %s.",
                     delete_protector.quarantine_root)
        except Exception as exc:  # protection must never crash the agent
            log.warning("Delete protection did not start: %s", exc)

    # Telegram bot: outbound status/digests + critical alerts to the
    # owner's chat. Disabled until bot_token + chat_id are set.
    if (config.telegram or {}).get("enabled", False):
        try:
            from deskaudit.telegram import TelegramBot

            telegram_bot = TelegramBot(
                db, config.telegram, log,
                project_root=config.project_root,
                config_path=config.project_root / "config.yaml",
                projects_path=config.project_root / "projects.yaml",
                tagger=tagger,
                delete_protector=delete_protector,
                file_explorer=file_explorer)
            if telegram_bot.configured:
                log.info("Telegram bot on: digests every %s min, commands polled.",
                         telegram_bot.settings.get("digest_minutes", 60))
                if delete_protector is not None:
                    delete_protector.notify = telegram_bot.notify_deleted
            else:
                log.warning("Telegram enabled but bot_token/chat_id missing — "
                            "set them in Dashboard Settings -> Telegram.")
        except Exception as exc:  # the bot must never crash the agent
            log.warning("Telegram bot did not start: %s", exc)
    if (config.email or {}).get("enabled", False):
        from deskaudit.emailer import configured as _email_configured

        if _email_configured(config.email):
            log.info("Hourly email digest on: every %s min to %s.",
                     config.email.get("interval_minutes", 60), config.email.get("to_addr"))
        else:
            log.warning("Email enabled but smtp_user/smtp_password/to_addr missing — "
                        "set them in Dashboard Settings -> Email.")

    # The agent also serves the dashboard, so the secret gate can open it
    # any time without a second process or console window.
    if config.dashboard_on_start:
        try:
            from deskaudit.dashboard import DashboardServer

            dashboard_server = DashboardServer(
                config.db_path, host="127.0.0.1", port=8000, tagger=tagger,
                project_root=config.project_root, alerts_settings=config.alerts,
                config_path=config.project_root / "config.yaml",
                transfers_settings=config.transfers,
                lock_code=str((config.secret_gate or {}).get("code", "1122")),
                lock_enabled=bool(config.dashboard_lock))
            url = dashboard_server.start()
            log.info("Dashboard serving at %s", url)
        except OSError as exc:
            log.warning("Dashboard could not start (already running?): %s", exc)
    if (config.secret_gate or {}).get("enabled", True):
        try:
            from deskaudit.secret_gate import start_secret_gate

            gate = start_secret_gate(config.secret_gate, "http://127.0.0.1:8000/", log)
            if gate is not None:
                # Never log the code itself (README promises nothing
                # about the gate is logged).
                log.info("Secret gate on: %s desktop clicks, then the code.", gate.clicks_needed)
        except Exception as exc:  # the gate must never crash the agent
            log.warning("Secret gate did not start: %s", exc)

    log.info("DeskAudit running. Poll every %ss, idle after %ss. Ctrl+C to stop.",
             config.poll_interval_seconds, config.idle_threshold_seconds)
    try:
        while True:
            if pause_file.exists():
                # Paused: no tracking, no digest, no email — but the
                # bot still answers commands, so /resume can wake the
                # agent. The digest timer is reset so resuming does
                # not immediately send an empty digest.
                if telegram_bot is not None:
                    try:
                        telegram_bot.poll_commands()
                    except Exception as exc:
                        log.warning("Telegram poll failed: %s", exc)
                last_telegram_digest = time.time()
                time.sleep(config.poll_interval_seconds)
                continue
            window = None
            browser_info = None
            try:
                window = tracker.tick(config.poll_interval_seconds)
            except Exception as exc:
                # A DB error here used to kill the whole agent silently
                # (windowed exe: traceback goes to devnull). Log and
                # keep polling — the next tick usually succeeds.
                log.warning("App tracking tick failed: %s", exc)
            try:
                from deskaudit.idle import is_idle

                browser_info = browser.tick(window, is_idle(config.idle_threshold_seconds),
                                            config.poll_interval_seconds) if window else None
            except Exception as exc:
                log.warning("Browser tick failed: %s", exc)
            now_ts = time.time()
            if config.browser_enabled and now_ts - last_browser_poll >= config.browser_poll_seconds:
                last_browser_poll = now_ts
                try:
                    counts = browser.poll_history()
                except Exception as exc:
                    counts = {"visits": 0, "downloads": 0, "searches": 0}
                    log.warning("Browser history poll failed: %s", exc)
                if counts["visits"] or counts["downloads"]:
                    log.info("Browser history: %s new visits, %s downloads, %s searches",
                             counts["visits"], counts["downloads"], counts["searches"])
            # Data transfers: scan every poll_seconds (USB / print / browser / files).
            if (transfer_monitor is not None
                    and now_ts - last_transfer_scan >= transfer_poll):
                last_transfer_scan = now_ts
                try:
                    counts = transfer_monitor.scan_today()
                    new = sum(counts.values())
                    if new:
                        log.info("Transfers detected: %s", counts)
                except Exception as exc:  # transfers must never crash the agent
                    log.warning("Transfer scan failed: %s", exc)
            # Delete protection: Recycle Bins + protected-folder mirror.
            if delete_protector is not None:
                try:
                    delete_poll = int((config.delete_protect or {}).get("poll_seconds", 30) or 30)
                except (TypeError, ValueError):
                    delete_poll = 30
                delete_poll = max(delete_poll, 10)
                if now_ts - last_delete_scan >= delete_poll:
                    last_delete_scan = now_ts
                    try:
                        quarantined = delete_protector.scan()
                        if quarantined:
                            log.info("Delete protection: %s file(s) quarantined: %s",
                                     len(quarantined),
                                     ", ".join(i["file_name"] for i in quarantined))
                    except Exception as exc:
                        log.warning("Delete-protect scan failed: %s", exc)
            # Alerts: re-check every 30 minutes while the agent runs.
            if config.alerts.get("enabled", True) and now_ts - last_alert_check >= 1800:
                last_alert_check = now_ts
                try:
                    from deskaudit.alerts import check_alerts, notify_windows, save_alerts

                    found = check_alerts(db, today_str(), settings=config.alerts, tagger=tagger,
                                         transfer_settings=config.transfers)
                    new = save_alerts(db, found)
                    for a in new:
                        log.warning("ALERT [%s] %s", a.severity, a.title)
                    if new and config.alerts.get("notify", True):
                        notify_windows(new)
                    if new and telegram_bot is not None:
                        # Critical alerts go to Telegram immediately;
                        # warnings/info ride along in the hourly digest.
                        for a in new:
                            if a.severity == "critical":
                                try:
                                    telegram_bot.notify_alert(a)
                                except Exception as exc:
                                    log.warning("Telegram alert failed: %s", exc)
                except Exception as exc:  # alerts must never crash the agent
                    log.warning("Alert check failed: %s", exc)
            # Telegram: answer /status etc. + scheduled digest.
            if telegram_bot is not None:
                try:
                    telegram_poll = int(telegram_bot.settings.get("poll_seconds", 30) or 30)
                except (TypeError, ValueError):
                    telegram_poll = 30
                telegram_poll = max(telegram_poll, 10)
                if now_ts - last_telegram_poll >= telegram_poll:
                    last_telegram_poll = now_ts
                    try:
                        telegram_bot.poll_commands()
                    except Exception as exc:
                        log.warning("Telegram poll failed: %s", exc)
                # New programs opened since the last tick go to Telegram
                # immediately (v12.5) — which program, its window, when.
                if (app_watch is not None and app_watch.enabled
                        and telegram_bot.configured):
                    try:
                        new_apps = app_watch.check()
                        if new_apps:
                            sent = telegram_bot.notify_new_app(new_apps)
                            if sent:
                                log.info("New program alert sent: %s",
                                         ", ".join(a["name"] for a in new_apps[:sent]))
                    except Exception as exc:
                        log.warning("New-program alert failed: %s", exc)
                try:
                    digest_minutes = int(telegram_bot.settings.get("digest_minutes", 60) or 60)
                except (TypeError, ValueError):
                    digest_minutes = 60
                if digest_minutes > 0 and now_ts - last_telegram_digest >= digest_minutes * 60:
                    last_telegram_digest = now_ts
                    try:
                        if telegram_bot.send_digest():
                            log.info("Telegram digest sent.")
                    except Exception as exc:
                        log.warning("Telegram digest failed: %s", exc)
            # Email: one digest every interval_minutes.
            if (config.email or {}).get("enabled", False):
                try:
                    interval = int(config.email.get("interval_minutes", 60) or 60)
                except (TypeError, ValueError):
                    interval = 60
                if interval > 0 and now_ts - last_email >= interval * 60:
                    last_email = now_ts
                    try:
                        from deskaudit.emailer import (build_hourly_email, configured,
                                                       send_email)

                        if configured(config.email):
                            subject, text, html = build_hourly_email(
                                db, today_str(), interval)
                            ok, err = send_email(config.email, subject, text, html)
                            if ok:
                                log.info("Hourly email sent.")
                            else:
                                log.warning("Hourly email failed: %s", err)
                    except Exception as exc:
                        log.warning("Hourly email failed: %s", exc)
            if browser_info:
                log.info("Browser: %s — %s [%s]", browser_info["browser"],
                         browser_info["title"] or "(no title)",
                         browser_info["domain"] or "title only")
            elif window is not None:
                log.info("Active: %s — %s", window.app_name, window.title or "(no title)")
            time.sleep(config.poll_interval_seconds)
    except SystemExit:
        return 0
    finally:
        _safe_stop("tracker", tracker.stop)
        _safe_stop("browser", browser.stop)
        if receiver is not None:
            _safe_stop("receiver", receiver.stop)
        if gate is not None:
            _safe_stop("gate", gate.stop)
        if dashboard_server is not None:
            _safe_stop("dashboard", dashboard_server.stop)
        _safe_stop("watcher", watcher.stop)
        _safe_stop("db", db.close)
    return 0


def _ensure_std_streams() -> None:
    """Windowed exe (console=False, v10.4) has no console: PyInstaller
    sets sys.stdout/sys.stderr to None. Point them at a file next to the
    log (data/deskaudit-stdout.log) instead of devnull — one-shot CLI
    output (--report, --alerts, --transfers, ...) used to vanish
    completely, and the README's claim that it went "to the log file"
    was false for printed output."""
    if sys.stdout is None or sys.stderr is None:
        try:
            out_path = _app_dir() / "data" / "deskaudit-stdout.log"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            stream = open(out_path, "a", encoding="utf-8")
        except Exception:
            import os

            stream = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def _fix_console_encoding() -> None:
    """Windows consoles default to cp1252/cp437, which cannot print the
    Bengali daily summary — printing it then crashes with
    UnicodeEncodeError. Switch stdout/stderr to UTF-8 (replacing anything
    still unencodable) so --ai-summary works in any console."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _hide_console() -> None:
    """Hide this process's console window (Windows only, best-effort).

    Safety net for running from source (python main.py --hidden): the
    frozen exe is built windowed (console=False, v10.4), so it has no
    console window at all and this is a no-op there. One-shot CLI
    commands run from a real console keep theirs. The log file keeps
    working either way."""
    if sys.platform != "win32":
        return
    try:
        import win32con
        import win32gui

        hwnd = win32gui.GetConsoleWindow()
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
    except Exception:
        pass


def _stop_running_agent() -> int:
    """Stop a running DeskAudit.exe (Windows only). The --stop process
    is itself named DeskAudit.exe, so it terminates every other process
    with that name and leaves itself alone."""
    if sys.platform != "win32":
        print("--stop works on Windows only (this is not a Windows machine).")
        return 1
    from deskaudit.utils import run_hidden

    mine = os.getpid()
    stopped = 0
    try:
        out = run_hidden(["tasklist", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=30)
        for line in out.stdout.splitlines():
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "deskaudit.exe":
                try:
                    pid = int(parts[1])
                except ValueError:
                    continue
                if pid != mine:
                    r = run_hidden(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, timeout=30)
                    if r.returncode == 0:
                        stopped += 1
    except Exception as exc:
        print(f"Could not stop DeskAudit: {exc}")
        return 1
    if stopped:
        print("DeskAudit stopped.")
    else:
        print("DeskAudit is not running (or could not be stopped — try running as administrator).")
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()  # windowed exe: stdout/stderr are None
    _fix_console_encoding()
    parser = argparse.ArgumentParser(description="DeskAudit — desktop audit agent")
    parser.add_argument("--report", action="store_true", help="Generate a daily report")
    parser.add_argument("--date", default=None, help="Report date, YYYY-MM-DD")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--browser-audit", action="store_true",
                        help="Import all browser history once and exit")
    parser.add_argument("--projects", action="store_true", help="List projects and rules")
    parser.add_argument("--project-add", default=None, metavar="NAME",
                        help="Add a project (use with --folder/--domain/--app/--goal)")
    parser.add_argument("--folder", default=None, help="Folder rule for --project-add")
    parser.add_argument("--domain", default=None, help="Domain rule for --project-add")
    parser.add_argument("--app", default=None, help="App rule for --project-add")
    parser.add_argument("--goal", type=int, default=0, help="Daily goal in minutes")
    parser.add_argument("--project-tag", nargs=2, default=None, metavar=("TYPE", "VALUE"),
                        help="Manual override: --project-tag domain github.com --project Study")
    parser.add_argument("--project", default=None, help="Project name for --project-tag")
    parser.add_argument("--retag", action="store_true",
                        help="Re-tag stored rows with current projects.yaml")
    parser.add_argument("--suggest-projects", action="store_true",
                        help="Suggest rules for untagged top apps/domains/titles")
    parser.add_argument("--alerts", action="store_true",
                        help="Check today's alerts, save new ones, print them")
    parser.add_argument("--alerts-list", action="store_true",
                        help="List stored alerts")
    parser.add_argument("--unread", action="store_true", help="With --alerts-list, unread only")
    parser.add_argument("--transfers", action="store_true",
                        help="Scan for data transfers and list today's (USB / cloud / email / print)")
    parser.add_argument("--telegram-test", action="store_true",
                        help="Send a test message to the configured Telegram chat")
    parser.add_argument("--telegram-now", action="store_true",
                        help="Send the current status to Telegram now")
    parser.add_argument("--screenshot", default=None, metavar="PATH",
                        help="Capture the screen as a PNG (Windows only) and save it to PATH")
    parser.add_argument("--screen-test", default=None, metavar="COLOR",
                        help="Show the fullscreen white/black overlay for --screen-seconds (test only)")
    parser.add_argument("--screen-seconds", type=int, default=5,
                        help="Seconds for --screen-test (default 5)")
    parser.add_argument("--app-watch-test", action="store_true",
                        help="List currently running non-system programs with window titles (test only)")
    parser.add_argument("--delete-protect-test", action="store_true",
                        help="Scan Recycle Bins + protected folders once and quarantine fresh deletes (test only)")
    parser.add_argument("--quarantine-list", action="store_true",
                        help="List files currently held in quarantine")
    parser.add_argument("--explorer-test", nargs="?", const="", default=None,
                        metavar="PATH",
                        help="List drives, or one folder's entries")
    parser.add_argument("--hidden-list", action="store_true",
                        help="List files hidden from Telegram")
    parser.add_argument("--email-test", action="store_true",
                        help="Send a test email to the configured address")
    parser.add_argument("--email-now", action="store_true",
                        help="Send the hourly email digest now")
    parser.add_argument("--notify", action="store_true",
                        help="With --alerts, also try a Windows notification")
    parser.add_argument("--ai-summary", action="store_true",
                        help="Daily Bengali summary (local, no API key)")
    parser.add_argument("--weekly", action="store_true",
                        help="With --ai-summary, make the 7-day review instead")
    parser.add_argument("--llm", action="store_true",
                        help="With --ai-summary, use the LLM text if a key is configured")
    parser.add_argument("--backup", action="store_true", help="Back up the database")
    parser.add_argument("--backup-list", action="store_true", help="List database backups")
    parser.add_argument("--restore", nargs="?", const="__latest__", default=None,
                        metavar="FILE", help="Restore a backup (file, or --latest)")
    parser.add_argument("--latest", action="store_true", help="With --restore, use newest backup")
    parser.add_argument("--evening-job", action="store_true",
                        help="9pm job: backup + alerts + daily summary")
    parser.add_argument("--schedule-summary", action="store_true",
                        help="Create the Windows 21:00 daily task")
    parser.add_argument("--unschedule-summary", action="store_true",
                        help="Remove the Windows 21:00 daily task")
    parser.add_argument("--dashboard", action="store_true",
                        help="Start the local web dashboard (127.0.0.1:8000)")
    parser.add_argument("--open-dashboard", action="store_true",
                        help="Start the dashboard if needed and open it in the browser")
    parser.add_argument("--hidden", action="store_true",
                        help="Hide this process's console window (background mode)")
    parser.add_argument("--stop", action="store_true",
                        help="Stop a running DeskAudit agent (Windows only)")
    parser.add_argument("--port", type=int, default=8000, help="Dashboard port")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard host (default 127.0.0.1)")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    # Frozen exe: the default config.yaml lives next to DeskAudit.exe,
    # not in whatever folder the shortcut was launched from.
    config_path = args.config
    if getattr(sys, "frozen", False) and config_path == "config.yaml":
        config_path = str(_app_dir() / "config.yaml")
    config = Config.load(config_path)
    log = setup_logging(config.log_path)
    tagger = load_tagger(config)

    if args.hidden:
        _hide_console()

    if args.stop:
        return _stop_running_agent()

    if args.open_dashboard:
        _hide_console()
        from deskaudit.dashboard import DashboardServer

        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
        finally:
            db.close()
        url = f"http://{args.host}:{args.port}/"
        server = None
        try:
            server = DashboardServer(config.db_path, host=args.host, port=args.port,
                                     tagger=tagger, project_root=config.project_root,
                                     alerts_settings=config.alerts,
                                     config_path=Path(config_path),
                                     transfers_settings=config.transfers,
                                     lock_code=str((config.secret_gate or {}).get("code", "1122")),
                                     lock_enabled=bool(config.dashboard_lock))
            url = server.start()
            print(f"DeskAudit dashboard: {url}")
        except OSError:
            print("Dashboard already running — opening it.")
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:
            pass
        if server is not None:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\nDashboard stopped.")
        return 0

    if args.dashboard:
        _hide_console()
        from deskaudit.dashboard import DashboardServer

        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
        finally:
            db.close()
        server = DashboardServer(config.db_path, host=args.host, port=args.port,
                                 tagger=tagger, project_root=config.project_root,
                                 alerts_settings=config.alerts,
                                 config_path=Path(config_path),
                                 transfers_settings=config.transfers,
                                 lock_code=str((config.secret_gate or {}).get("code", "1122")),
                                 lock_enabled=bool(config.dashboard_lock))
        try:
            url = server.start()
        except OSError:
            print(f"Dashboard could not start on port {args.port} — already running? "
                  "Open http://127.0.0.1:8000/ directly, or use another port with --port.")
            return 1
        print(f"DeskAudit dashboard: {url}")
        print("Local only — press Ctrl+C to stop.")
        if args.host not in ("127.0.0.1", "localhost"):
            print("Warning: dashboard is not on localhost only. Use 127.0.0.1 on untrusted networks.")
        if not args.no_open:
            try:
                import webbrowser

                webbrowser.open(url)
            except Exception:
                pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
        return 0

    if args.backup:
        from deskaudit.backup import make_backup

        try:
            dest = make_backup(config.db_path, config.project_root,
                               keep_days=config.backup_keep_days,
                               keep_count=config.backup_keep_count)
            print(f"Backup saved: {dest}")
        except FileNotFoundError:
            print("No database yet — nothing to back up. Run the agent once first.")
            return 1
        except (ValueError, OSError) as exc:
            print(f"Backup failed: {exc}")
            return 1
        return 0

    if args.backup_list:
        from deskaudit.backup import list_backups

        rows = list_backups(config.project_root)
        if not rows:
            print("No backups yet. Run: python main.py --backup")
        else:
            for r in rows:
                print(f"{r['name']}  ({r['size_bytes'] // 1024} KB, {r['created']})")
        return 0

    if args.restore is not None:
        from deskaudit.backup import latest_backup, restore_backup

        backup_path = latest_backup(config.project_root) if (args.restore == "__latest__" or args.latest) else Path(args.restore)
        if backup_path is None:
            print("No backups found.")
            return 1
        print("Stop the DeskAudit agent first if it is running, then restore.")
        try:
            safety = restore_backup(config.db_path, backup_path, config.project_root)
            print(f"Restored from: {backup_path}")
            if safety:
                print(f"Current DB saved first as: {safety}")
        except FileNotFoundError:
            print(f"Backup not found: {backup_path}")
            return 1
        except ValueError:
            print(f"Not a DeskAudit database: {backup_path}")
            return 1
        except PermissionError:
            print("Restore failed — is the agent still running? Stop it first, then restore.")
            return 1
        except OSError as exc:
            print(f"Restore failed: {exc}")
            return 1
        return 0

    if args.schedule_summary:
        if sys.platform != "win32":
            print("This creates a Windows Task Scheduler task. On Windows, double-click")
            print("schedule_summary.bat, or run: python main.py --schedule-summary")
            return 0
        from deskaudit.utils import run_hidden

        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}" --evening-job'
        else:
            cmd = (f'"{sys.executable}" "{Path(config.project_root) / "main.py"}" '
                   f"--evening-job")
        r = run_hidden(["schtasks", "/create", "/tn", "DeskAudit Daily Summary",
                        "/tr", cmd, "/sc", "daily", "/st", "21:00", "/f"],
                       capture_output=True, text=True)
        print(r.stdout or r.stderr)
        return 0 if r.returncode == 0 else 1

    if args.unschedule_summary:
        if sys.platform != "win32":
            print("On Windows, double-click unschedule_summary.bat.")
            return 0
        from deskaudit.utils import run_hidden

        r = run_hidden(["schtasks", "/delete", "/tn", "DeskAudit Daily Summary", "/f"],
                       capture_output=True, text=True)
        print(r.stdout or r.stderr)
        return 0 if r.returncode == 0 else 1

    if args.evening_job:
        # 9pm job: backup + alerts + daily summary (+ weekly on Sundays).
        from deskaudit.alerts import check_alerts, save_alerts
        from deskaudit.backup import make_backup
        from deskaudit.summary import save_daily_summary, save_weekly_summary

        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            # Fresh data first: poll browser history + scan transfers, or
            # the job reports an empty day when the agent isn't running.
            _refresh_before_summary(db, config, tagger)
            try:
                dest = make_backup(config.db_path, config.project_root,
                                   keep_days=config.backup_keep_days,
                                   keep_count=config.backup_keep_count)
                print(f"Backup saved: {dest}")
            except FileNotFoundError:
                print("No database yet — backup skipped.")
            except (ValueError, OSError) as exc:
                print(f"Backup failed (job continues): {exc}")
            found = check_alerts(db, today_str(), settings=config.alerts, tagger=tagger,
                                 transfer_settings=config.transfers)
            new = save_alerts(db, found)
            print(f"Alerts: {len(found)} found, {len(new)} new.")
            result, path = save_daily_summary(
                db, None, reports_dir=Path(config.project_root) / "reports", tagger=tagger)
            print(f"Daily summary saved: {path}")
            if datetime.now().weekday() == 6:  # Sunday
                _, wpath = save_weekly_summary(
                    db, reports_dir=Path(config.project_root) / "reports", tagger=tagger)
                print(f"Weekly review saved: {wpath}")
        finally:
            db.close()
        return 0

    if args.alerts:
        from deskaudit.alerts import check_alerts, notify_windows, save_alerts

        date = args.date or today_str()
        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            # Fresh data first (same reason as the evening job).
            _refresh_before_summary(db, config, tagger)
            found = check_alerts(db, date, settings=config.alerts, tagger=tagger,
                                 transfer_settings=config.transfers)
            new = save_alerts(db, found)
        finally:
            db.close()
        if not found:
            print(f"No alerts for {date}.")
        else:
            for a in found:
                mark = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(a.severity, "•")
                print(f"{mark} [{a.severity}] {a.title}\n   {a.message}")
            print(f"\n{len(found)} alert(s), {len(new)} new.")
            if (args.notify or config.alerts.get("notify")) and new:
                print("Notification sent." if notify_windows(new) else
                      "(Windows notification not available here — alerts are saved in the DB and shown on the dashboard.)")
        return 0

    if args.transfers:
        from deskaudit.transfers import TransferMonitor

        date = args.date or today_str()
        db = Database(config.db_path)
        try:
            monitor = TransferMonitor(db, config.transfers)
            counts = monitor.scan_today() if date == today_str() else {"browser": 0, "files": 0}
            if date != today_str():
                monitor.scan_browser_channels(date)
                monitor.scan_file_events(date)
            rows = db.transfers_for_date(date, limit=100)
            tcounts = db.transfer_counts(date)
        finally:
            db.close()
        total = sum(c.get("count", 0) for c in tcounts.values())
        if not total:
            print(f"No data transfers recorded for {date}.")
        else:
            print(f"Data transfers for {date}: {total} event(s)")
            for kind, c in sorted(tcounts.items()):
                print(f"  {kind}: {c['count']}")
            for r in rows:
                size = f" ({round(r['size_bytes'] / 1024 / 1024, 1)} MB)" if r["size_bytes"] else ""
                print(f"  {r['occurred_at'][11:16]} {r['transfer_type']} — "
                      f"{r['file_name'] or r['channel']} -> {r['destination']}{size}")
        return 0

    if args.screenshot:
        from datetime import datetime as _dt

        from deskaudit.screenshot import capture_screen

        path = args.screenshot
        if Path(path).is_dir() or not Path(path).suffix:
            path = str(Path(path) / f"shot-{_dt.now().strftime('%Y%m%d-%H%M%S')}.png")
        try:
            out = capture_screen(path)
        except Exception as exc:
            print(f"Screenshot failed: {exc}")
            return 1
        print(f"Screenshot saved: {out} ({out.stat().st_size} bytes)")
        return 0

    if args.screen_test:
        from deskaudit.screen_blank import start_blank, stop_blank

        ok, msg = start_blank(args.screen_test)
        print(f"Screen {args.screen_test}: ok={ok} ({msg})")
        if not ok:
            return 1
        time.sleep(max(1, args.screen_seconds))
        stop_blank()
        print("Screen overlay stopped.")
        return 0

    if args.delete_protect_test:
        from deskaudit.delete_protect import DeleteProtector

        db = Database(config.db_path)
        try:
            protector = DeleteProtector(
                db, config.delete_protect, log,
                project_root=config.project_root,
                protected_folders=config.effective_watch_folders())
            found = protector.scan()
        finally:
            db.close()
        print(f"Quarantine: {protector.quarantine_root}")
        if not found:
            print("No fresh deletes found (Recycle Bins + protected folders).")
        for item in found:
            print(f"  🗑 {item['file_name']} — was: {item['original_path']} "
                  f"({item['source']}) -> held #{item['id']}")
        return 0

    if args.quarantine_list:
        db = Database(config.db_path)
        try:
            rows = db.quarantined_held(limit=50)
        finally:
            db.close()
        if not rows:
            print("No files held in quarantine.")
        for row in rows:
            print(f"  #{row['id']} {row['file_name']} — was: {row['original_path']} "
                  f"[{row['source']}, {row['deleted_at']}]")
        return 0

    if args.explorer_test is not None:
        from deskaudit.file_explorer import FileExplorer, list_dir

        db = Database(config.db_path)
        try:
            explorer = FileExplorer(db, config.explorer, log,
                                    project_root=config.project_root)
            if not args.explorer_test:
                roots = explorer.roots()
                print("Drives/roots:")
                for root in roots:
                    print(f"  {root}")
                print(f"Hidden folder: {explorer.hidden_root}")
                return 0
            result = list_dir(args.explorer_test, 0, explorer.page_size)
            if result.get("error"):
                print(f"Cannot open {args.explorer_test}: {result['error']}")
                return 1
            print(f"{result['path']} — {result['total']} entries "
                  f"(page 1/{result['pages']}):")
            for e in result["entries"]:
                kind = "DIR " if e["is_dir"] else "FILE"
                size = f" ({e['size']} B)" if e["size"] is not None else ""
                print(f"  {kind} {e['name']}{size}")
            return 0
        finally:
            db.close()

    if args.hidden_list:
        db = Database(config.db_path)
        try:
            rows = db.hidden_held(limit=50)
        finally:
            db.close()
        if not rows:
            print("No files hidden.")
        for row in rows:
            print(f"  #{row['id']} {row['file_name']} — was: {row['original_path']} "
                  f"[hidden {row['hidden_at']}]")
        return 0

    if args.app_watch_test:
        from deskaudit.app_watch import AppWatch, _SYSTEM_PROCS, _iter_processes, window_title_for_pid

        procs = _iter_processes()
        watch = AppWatch()
        watch.baseline(procs)
        print(f"Running processes: {len(procs)} (baseline: {len(watch._seen)})")
        shown = 0
        for p in sorted(procs, key=lambda q: (q.name or "").lower()):
            if (p.name or "").lower() in _SYSTEM_PROCS:
                continue
            title = window_title_for_pid(p.pid)
            print(f"  {p.name} (pid {p.pid}){(' — ' + title[:60]) if title else ''}")
            shown += 1
            if shown >= 15:
                break
        return 0

    if args.telegram_test or args.telegram_now:
        from deskaudit.telegram import TelegramBot, send_message

        db = Database(config.db_path)
        try:
            bot = TelegramBot(db, config.telegram, log)
            if not bot.configured:
                print("Telegram is not configured yet — set bot_token + chat_id in "
                      "Dashboard Settings -> Telegram (or config.local.yaml), then retry.")
                return 1
            if args.telegram_test:
                ok = send_message(bot.token, bot.chat_id,
                                  "✅ DeskAudit Telegram — পরীক্ষা বার্তা। বট সংযুক্ত আছে।")
                print("Test message sent." if ok else
                      "Could not send — check the bot token and chat id, "
                      "and make sure you sent /start to the bot.")
                return 0 if ok else 1
            ok = bot.send_status()
            print("Status sent to Telegram." if ok else "Could not send the status.")
            return 0 if ok else 1
        finally:
            db.close()

    if args.email_test or args.email_now:
        from deskaudit.emailer import build_hourly_email, configured, send_email

        db = Database(config.db_path)
        try:
            if not configured(config.email):
                print("Email is not configured yet — set smtp_user, smtp_password and "
                      "to_addr in Dashboard Settings -> Email (or config.local.yaml), "
                      "then retry.")
                return 1
            if args.email_test:
                ok, err = send_email(config.email, "DeskAudit — পরীক্ষা ইমেইল",
                                     "DeskAudit ইমেইল পরীক্ষা — এই ঠিকানায় ঘণ্টায় "
                                     "একবার আপডেট আসবে।")
                print("Test email sent." if ok else f"Could not send email: {err}")
                return 0 if ok else 1
            subject, text, html = build_hourly_email(
                db, today_str(), int(config.email.get("interval_minutes", 60) or 60))
            ok, err = send_email(config.email, subject, text, html)
            print("Digest email sent." if ok else f"Could not send email: {err}")
            return 0 if ok else 1
        finally:
            db.close()

    if args.alerts_list:
        db = Database(config.db_path)
        try:
            rows = db.list_alerts(date=args.date, unread_only=args.unread)
        finally:
            db.close()
        if not rows:
            print("No stored alerts.")
        else:
            for r in rows:
                mark = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(r["severity"], "•")
                read = "" if r["is_read"] else " (unread)"
                print(f"{mark} {r['date']} [{r['severity']}] {r['title']}{read}\n   {r['message']}")
        return 0

    if args.ai_summary:
        from deskaudit.summary import (generate_daily_summary,
                                       generate_weekly_summary,
                                       render_daily_markdown,
                                       render_weekly_markdown,
                                       save_daily_summary, save_weekly_summary)

        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            if args.weekly:
                result, path = save_weekly_summary(
                    db, end=args.date, reports_dir=Path(config.project_root) / "reports",
                    tagger=tagger)
                source = "local"
                if args.llm:
                    from deskaudit.llm_summary import llm_narrative

                    text, source = llm_narrative(result, config.llm, kind="weekly", explicit=True)
                    # Append the LLM text as its own section — the old
                    # code overwrote the file and destroyed the detailed
                    # local sections (projects/files/browser/transfers).
                    # Only when the LLM actually answered: on fallback
                    # the text IS the local narrative, already in the file.
                    if source.startswith("llm"):
                        path = Path(config.project_root) / "reports" / f"{result['end']}-weekly.md"
                        existing = path.read_text(encoding="utf-8") if path.exists() else ""
                        path.write_text(
                            existing.rstrip() + "\n\n## LLM সামারি\n\n"
                            f"{text}\n\n---\n\n*Source: {source}. Only aggregated stats were sent to the LLM.*\n",
                            encoding="utf-8")
                        result["narrative"] = result["narrative"] + "\n\n## LLM সামারি\n\n" + text
            else:
                result, path = save_daily_summary(
                    db, args.date, reports_dir=Path(config.project_root) / "reports",
                    tagger=tagger)
                source = "local"
                if args.llm:
                    from deskaudit.llm_summary import llm_narrative

                    text, source = llm_narrative(result, config.llm, kind="daily", explicit=True)
                    if source.startswith("llm"):
                        path = Path(config.project_root) / "reports" / f"{result['date']}-summary.md"
                        existing = path.read_text(encoding="utf-8") if path.exists() else ""
                        path.write_text(
                            existing.rstrip() + "\n\n## LLM সামারি\n\n"
                            f"{text}\n\n---\n\n*Source: {source}. Only aggregated stats were sent to the LLM.*\n",
                            encoding="utf-8")
                        result["narrative"] = result["narrative"] + "\n\n## LLM সামারি\n\n" + text
        finally:
            db.close()
        print(result["narrative"])
        print(f"\nSource: {source}")
        print(f"Saved: {path}")
        return 0

    if args.projects:
        print(f"Default project: {tagger.default_project}")
        for p in tagger.projects:
            print(f"\n{p.name} (priority {p.priority}, goal {p.daily_goal_minutes}m, color {p.color or '-'})")
            if p.description:
                print(f"  {p.description}")
            if p.folders:
                print(f"  folders: {', '.join(p.folders)}")
            if p.apps:
                print(f"  apps: {', '.join(p.apps)}")
            if p.domains:
                print(f"  domains: {', '.join(p.domains)}")
            if p.window_keywords:
                print(f"  window keywords: {', '.join(p.window_keywords)}")
            if p.file_patterns:
                print(f"  file patterns: {', '.join(p.file_patterns)}")
            if p.tags:
                print(f"  tags: {', '.join(p.tags)}")
        if tagger.overrides:
            print("\nOverrides:")
            for ov in tagger.overrides:
                print(f"  {ov.match_type}={ov.match_value} -> {ov.project}")
        print("\nEdit projects.yaml to change rules, then run: python main.py --retag")
        return 0

    if args.project_add:
        # Append a minimal project to projects.yaml (keeps existing file intact).
        # Built with yaml.safe_dump so a quote in the name/folder cannot
        # produce invalid YAML that wipes all projects on the next load.
        import yaml  # type: ignore

        projects_path = Path(config.project_root) / "projects.yaml"
        entry: dict = {"name": args.project_add, "priority": 80}
        if args.goal:
            entry["daily_goal_minutes"] = args.goal
        if args.folder:
            entry["folders"] = [args.folder]
        if args.domain:
            entry["domains"] = [args.domain]
        if args.app:
            entry["apps"] = [args.app]
        dumped = yaml.safe_dump([entry], allow_unicode=True,
                                default_flow_style=False, sort_keys=False)
        indented = "\n".join("  " + line if line else line
                             for line in dumped.rstrip("\n").split("\n"))
        text = projects_path.read_text(encoding="utf-8") if projects_path.exists() else "default_project: \"Untagged\"\n\nprojects:\n"
        if "projects:" not in text:
            text += "\nprojects:\n"
        projects_path.write_text(text.rstrip() + "\n" + indented + "\n", encoding="utf-8")
        print(f"Added project '{args.project_add}' to projects.yaml. Run --retag to apply it to old rows.")
        return 0

    if args.project_tag:
        if not args.project:
            print("Usage: python main.py --project-tag domain github.com --project Study")
            return 2
        match_type, match_value = args.project_tag
        db = Database(config.db_path)
        try:
            db.set_project_override(match_type, match_value, args.project)
            tagger.add_override(match_type, match_value, args.project)
            counts = db.retag_all(tagger)
        finally:
            db.close()
        print(f"Override saved: {match_type}={match_value} -> {args.project}")
        print(f"Retagged: {counts}")
        return 0

    if args.retag:
        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            counts = db.retag_all(tagger, date=args.date)
        finally:
            db.close()
        print(f"Retagged (date={args.date or 'all'}): {counts}")
        print("Run: python main.py --report  to see the new project totals.")
        return 0

    if args.suggest_projects:
        db = Database(config.db_path)
        try:
            date = args.date or today_str()
            untagged = db.untagged_top(date, limit=5)
            items = []
            for a in untagged.get("apps", []):
                items.append({"kind": "app", "value": a["value"], "seconds": a["seconds"]})
            for d in untagged.get("domains", []):
                items.append({"kind": "domain", "value": d["value"], "seconds": d["seconds"]})
            for t in untagged.get("titles", []):
                items.append({"kind": "title", "value": t["value"], "seconds": t["seconds"]})
            suggestions = tagger.suggest_for_untagged(items)
            if not suggestions:
                print(f"No untagged activity on {date}. Nothing to suggest.")
            else:
                print(f"Untagged on {date} — add one of these to projects.yaml:")
                for s in suggestions:
                    print(f"\n- {s['kind']}: {s['value']}")
                    print(f"  {s['yaml']}")
                    print(f"  # {s['hint']}")
                print("\nThen run: python main.py --retag")
        finally:
            db.close()
        return 0

    if args.browser_audit:
        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            browser = BrowserTracker(db, config, tagger=tagger)
            counts = browser.audit_now()
        finally:
            db.close()
        print(f"Browser audit: {counts['profiles']} profiles, "
              f"{counts['visits']} visits, {counts['searches']} searches, "
              f"{counts['downloads']} downloads imported, "
              f"{counts.get('bookmarks', 0)} bookmarks found.")
        print("Run: python main.py --report  to see them in the daily report.")
        return 0

    if args.report:
        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            text, md_path, csv_path = generate_report(
                db, args.date or today_str(), Path(config.project_root) / "reports",
                tagger=tagger,
            )
        finally:
            db.close()
        print(text)
        print(f"\nSaved: {md_path}\nSaved: {csv_path}")
        print(f"Saved: {Path(config.project_root) / 'reports' / (args.date or today_str())}-browser.csv")
        print(f"Saved: {Path(config.project_root) / 'reports' / (args.date or today_str())}-projects.csv")
        print(f"Saved: {Path(config.project_root) / 'reports' / (args.date or today_str())}-transfers.csv")
        return 0

    if args.once:
        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
            tracker = AppTracker(db, config.idle_threshold_seconds, tagger=tagger)
            window = tracker.tick(config.poll_interval_seconds)
            tracker.stop()
            print(f"Active: {window.app_name} — {window.title}")
            if config.browser_enabled:
                browser = BrowserTracker(db, config, tagger=tagger)
                counts = browser.poll_history()
                print(f"Browser history: {counts['visits']} new visits, "
                      f"{counts['downloads']} downloads, {counts['searches']} searches")
                browser.stop()
            if (config.transfers or {}).get("enabled", True):
                from deskaudit.transfers import TransferMonitor

                tcounts = TransferMonitor(db, config.transfers).scan_today()
                print(f"Transfers: {tcounts}")
        finally:
            db.close()
        return 0

    return run_agent(config, log)


if __name__ == "__main__":
    raise SystemExit(main())
