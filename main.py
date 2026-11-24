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
    pause_file = config.project_root / "PAUSE"
    last_browser_poll = 0.0
    last_alert_check = 0.0

    def shutdown(*_args):
        log.info("Shutting down, closing current session...")
        tracker.stop()
        browser.stop()
        if receiver is not None:
            receiver.stop()
        watcher.stop()
        db.close()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    if watcher.start():
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

    log.info("DeskAudit running. Poll every %ss, idle after %ss. Ctrl+C to stop.",
             config.poll_interval_seconds, config.idle_threshold_seconds)
    try:
        while True:
            if pause_file.exists():
                time.sleep(config.poll_interval_seconds)
                continue
            window = tracker.tick(config.poll_interval_seconds)
            from deskaudit.idle import is_idle

            browser_info = browser.tick(window, is_idle(config.idle_threshold_seconds),
                                        config.poll_interval_seconds)
            now_ts = time.time()
            if config.browser_enabled and now_ts - last_browser_poll >= config.browser_poll_seconds:
                last_browser_poll = now_ts
                counts = browser.poll_history()
                if counts["visits"] or counts["downloads"]:
                    log.info("Browser history: %s new visits, %s downloads, %s searches",
                             counts["visits"], counts["downloads"], counts["searches"])
            # Alerts: re-check every 30 minutes while the agent runs.
            if config.alerts.get("enabled", True) and now_ts - last_alert_check >= 1800:
                last_alert_check = now_ts
                try:
                    from deskaudit.alerts import check_alerts, notify_windows, save_alerts

                    found = check_alerts(db, today_str(), settings=config.alerts, tagger=tagger)
                    new = save_alerts(db, found)
                    for a in new:
                        log.warning("ALERT [%s] %s", a.severity, a.title)
                    if new and config.alerts.get("notify", True):
                        notify_windows(new)
                except Exception as exc:  # alerts must never crash the agent
                    log.warning("Alert check failed: %s", exc)
            if browser_info:
                log.info("Browser: %s — %s [%s]", browser_info["browser"],
                         browser_info["title"] or "(no title)",
                         browser_info["domain"] or "title only")
            else:
                log.info("Active: %s — %s", window.app_name, window.title or "(no title)")
            time.sleep(config.poll_interval_seconds)
    except SystemExit:
        return 0
    finally:
        tracker.stop()
        browser.stop()
        if receiver is not None:
            receiver.stop()
        watcher.stop()
        db.close()
    return 0


def _fix_console_encoding() -> None:
    """Windows consoles default to cp1252/cp437, which cannot print the
    Bengali daily summary — printing it then crashes with
    UnicodeEncodeError. Switch stdout/stderr to UTF-8 (replacing anything
    still unencodable) so --ai-summary works in any console."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
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

    if args.dashboard:
        from deskaudit.dashboard import DashboardServer

        db = Database(config.db_path)
        try:
            sync_projects_to_db(db, tagger)
            merge_db_overrides(db, tagger)
        finally:
            db.close()
        server = DashboardServer(config.db_path, host=args.host, port=args.port,
                                 tagger=tagger, project_root=config.project_root,
                                 alerts_settings=config.alerts)
        url = server.start()
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
        return 0

    if args.schedule_summary:
        if sys.platform != "win32":
            print("This creates a Windows Task Scheduler task. On Windows, double-click")
            print("schedule_summary.bat, or run: python main.py --schedule-summary")
            return 0
        import subprocess

        if getattr(sys, "frozen", False):
            cmd = f'"{sys.executable}" --evening-job'
        else:
            cmd = (f'"{sys.executable}" "{Path(config.project_root) / "main.py"}" '
                   f"--evening-job")
        r = subprocess.run(["schtasks", "/create", "/tn", "DeskAudit Daily Summary",
                            "/tr", cmd, "/sc", "daily", "/st", "21:00", "/f"],
                           capture_output=True, text=True)
        print(r.stdout or r.stderr)
        return 0 if r.returncode == 0 else 1

    if args.unschedule_summary:
        if sys.platform != "win32":
            print("On Windows, double-click unschedule_summary.bat.")
            return 0
        import subprocess

        r = subprocess.run(["schtasks", "/delete", "/tn", "DeskAudit Daily Summary", "/f"],
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
            try:
                dest = make_backup(config.db_path, config.project_root,
                                   keep_days=config.backup_keep_days,
                                   keep_count=config.backup_keep_count)
                print(f"Backup saved: {dest}")
            except FileNotFoundError:
                print("No database yet — backup skipped.")
            found = check_alerts(db, today_str(), settings=config.alerts, tagger=tagger)
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
            found = check_alerts(db, date, settings=config.alerts, tagger=tagger)
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
                    path = Path(config.project_root) / "reports" / f"{result['end']}-weekly.md"
                    path.write_text(
                        f"# DeskAudit — সাপ্তাহিক রিভিউ ({result['start']} থেকে {result['end']})\n\n"
                        f"{text}\n\n---\n\n*Source: {source}. Local rule-based sections are in the same file's data; "
                        "only aggregated stats were sent to the LLM.*\n",
                        encoding="utf-8")
                    result["narrative"] = text
            else:
                result, path = save_daily_summary(
                    db, args.date, reports_dir=Path(config.project_root) / "reports",
                    tagger=tagger)
                source = "local"
                if args.llm:
                    from deskaudit.llm_summary import llm_narrative

                    text, source = llm_narrative(result, config.llm, kind="daily", explicit=True)
                    path = Path(config.project_root) / "reports" / f"{result['date']}-summary.md"
                    path.write_text(
                        f"# DeskAudit — ডেইলি সামারি ({result['date']})\n\n"
                        f"{text}\n\n---\n\n*Source: {source}. Only aggregated stats were sent to the LLM.*\n",
                        encoding="utf-8")
                    result["narrative"] = text
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
        projects_path = Path(config.project_root) / "projects.yaml"
        entry = [f'  - name: "{args.project_add}"']
        if args.goal:
            entry.append(f"    daily_goal_minutes: {args.goal}")
        entry.append("    priority: 80")
        if args.folder:
            entry.append(f'    folders:\n      - "{args.folder}"')
        if args.domain:
            entry.append(f'    domains:\n      - "{args.domain}"')
        if args.app:
            entry.append(f'    apps:\n      - "{args.app}"')
        text = projects_path.read_text(encoding="utf-8") if projects_path.exists() else "default_project: \"Untagged\"\n\nprojects:\n"
        if "projects:" not in text:
            text += "\nprojects:\n"
        projects_path.write_text(text.rstrip() + "\n" + "\n".join(entry) + "\n", encoding="utf-8")
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
        from deskaudit.projects import Tagger as _Tagger

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
              f"{counts['downloads']} downloads imported.")
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
        finally:
            db.close()
        return 0

    return run_agent(config, log)


if __name__ == "__main__":
    raise SystemExit(main())
