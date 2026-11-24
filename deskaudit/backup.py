"""Database backup & restore for DeskAudit.

Backups are timestamped copies of the SQLite database, made with
sqlite3's online backup API (safe even while the agent is running —
never copy the .db file by hand while it is open).

    python main.py --backup                 # backups/deskaudit-YYYY-MM-DD-HHMMSS.db
    python main.py --backup-list            # list backups with size + date
    python main.py --restore backups/deskaudit-2026-11-24-210000.db
    python main.py --restore --latest       # restore the newest backup

Safety:
  * Restore first copies the CURRENT database to
    backups/deskaudit-before-restore-....db, so a bad restore can itself
    be undone. Restore refuses to run while the agent is holding the DB
    open in the same process — run it with the agent stopped.
  * Retention: keep the newest 30 backups by default (config.yaml:
    backup_keep_days / backup_keep_count). Old ones are pruned on backup.
  * Backups live in backups/ and are git-ignored — they contain your
    private activity data, so they never go into git or a zip.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_BACKUP_DIR = "backups"
DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_COUNT = 30


def backup_dir(project_root: str | Path) -> Path:
    return Path(project_root) / DEFAULT_BACKUP_DIR


def _timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")


def _unique_path(out_dir: Path, name: str) -> Path:
    """A backup name that never overwrites an existing file — two backups
    (or a restore's safety copy) in the same second must not clobber
    each other, which would destroy the very file being restored."""
    candidate = out_dir / name
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while candidate.exists():
        candidate = out_dir / f"{stem}-{i}{suffix}"
        i += 1
    return candidate


def make_backup(db_path: str | Path, project_root: str | Path,
                keep_days: int = DEFAULT_KEEP_DAYS,
                keep_count: int = DEFAULT_KEEP_COUNT) -> Path:
    """Online backup of the SQLite DB. Returns the backup file path."""
    db_path = Path(db_path)
    out_dir = backup_dir(project_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(out_dir, f"deskaudit-{_timestamp()}.db")
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    # Verify the backup opens and has the expected tables.
    check = sqlite3.connect(dest)
    try:
        tables = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "app_sessions" not in tables:
            dest.unlink(missing_ok=True)
            raise ValueError(f"Backup verification failed: {dest}")
    finally:
        check.close()
    prune_backups(out_dir, keep_days=keep_days, keep_count=keep_count)
    return dest


def list_backups(project_root: str | Path) -> list[dict]:
    out_dir = backup_dir(project_root)
    if not out_dir.exists():
        return []
    rows = []
    for p in sorted(out_dir.glob("deskaudit-*.db"), reverse=True):
        stat = p.stat()
        rows.append({
            "path": p,
            "name": p.name,
            "size_bytes": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        })
    return rows


def latest_backup(project_root: str | Path) -> Path | None:
    rows = list_backups(project_root)
    return rows[0]["path"] if rows else None


def prune_backups(out_dir: Path, keep_days: int = DEFAULT_KEEP_DAYS,
                  keep_count: int = DEFAULT_KEEP_COUNT) -> list[Path]:
    """Delete backups older than keep_days, then keep only keep_count newest.
    Returns the deleted paths."""
    from datetime import timedelta

    deleted: list[Path] = []
    # Only dated backup files (deskaudit-YYYY-MM-DD-....db) — the restore
    # safety copies (deskaudit-before-restore-*.db) must NOT be pruned,
    # or the "undo a restore" net expires under normal retention.
    files = sorted(out_dir.glob("deskaudit-2*.db"), key=lambda p: p.stat().st_mtime)
    cutoff = (datetime.now().astimezone() - timedelta(days=keep_days)).timestamp()
    for p in files:
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            deleted.append(p)
    files = [p for p in sorted(out_dir.glob("deskaudit-2*.db"),
                               key=lambda p: p.stat().st_mtime) if p.exists()]
    while len(files) > keep_count:
        oldest = files.pop(0)
        oldest.unlink(missing_ok=True)
        deleted.append(oldest)
    return deleted


def restore_backup(db_path: str | Path, backup_path: str | Path,
                   project_root: str | Path) -> Path:
    """Restore a backup over the live DB. First saves the current DB as a
    before-restore backup, so the restore itself can be undone. Returns
    the safety-copy path."""
    db_path = Path(db_path)
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")
    # Verify the backup before touching the live DB.
    check = sqlite3.connect(backup_path)
    try:
        tables = {r[0] for r in check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "app_sessions" not in tables:
            raise ValueError(f"Not a DeskAudit database: {backup_path}")
    finally:
        check.close()
    out_dir = backup_dir(project_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    safety = _unique_path(out_dir, f"deskaudit-before-restore-{_timestamp()}.db")
    if db_path.exists():
        shutil.copy2(db_path, safety)
    else:
        safety = None  # type: ignore[assignment]
    shutil.copy2(backup_path, db_path)
    return safety  # type: ignore[return-value]
