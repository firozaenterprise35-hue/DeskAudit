"""File watcher using watchdog.

Logs created / modified / deleted / moved events to SQLite. Debounces
rapid repeated 'modified' events for the same file (e.g. an app saving
every few seconds) so the DB is not flooded.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .database import Database

_log = logging.getLogger("deskaudit.file_watcher")

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - watchdog is in requirements.txt
    FileSystemEvent = object  # type: ignore
    FileSystemEventHandler = object  # type: ignore
    Observer = None  # type: ignore


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, db: Database, config: Config, tagger=None):
        super().__init__()
        self.db = db
        self.config = config
        self.tagger = tagger
        self._last_modified: dict[str, float] = {}

    def _should_log(self, path: str, dest: str | None = None) -> bool:
        """Log the event unless BOTH source and destination are excluded.
        A file moved OUT of an excluded folder (or INTO a watched one)
        must still be logged — only a move inside excluded space is hidden."""
        if not self.config.is_excluded(Path(path)):
            return True
        if dest and not self.config.is_excluded(Path(dest)):
            return True
        return False

    def _log(self, event_type: str, src: str, dest: str | None = None,
             is_dir: bool = False) -> None:
        try:
            self._log_inner(event_type, src, dest, is_dir)
        except Exception as exc:
            # A DB error here used to kill the whole observer thread
            # (watchdog only catches queue.Empty) — file tracking then
            # died silently. Log and keep watching instead.
            _log.warning("File event not logged (%s %s): %s", event_type, src, exc)

    def _log_inner(self, event_type: str, src: str, dest: str | None = None,
                   is_dir: bool = False) -> None:
        if not self._should_log(src, dest):
            return
        size = None
        try:
            # For a move, the SOURCE no longer exists — stat the
            # destination instead, or every moved row has size NULL and
            # large-transfer detection can never fire.
            target = dest if (event_type == "moved" and dest) else src
            p = Path(target)
            size = p.stat().st_size if p.is_file() else None
        except OSError:
            size = None
        project, tags = ("Untagged", [])
        if self.tagger is not None:
            project, tags = self.tagger.tag_file(src, dest or "")
        self.db.log_file_event(event_type, src, dest, is_dir, size,
                               datetime.now().astimezone(),
                               project=project, tags=tags)

    def on_created(self, event):  # type: ignore[no-untyped-def]
        self._log("created", event.src_path, is_dir=event.is_directory)

    def on_modified(self, event):  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        now = time.time()
        # Debounce: same file modified again within 10s -> skip.
        if now - self._last_modified.get(event.src_path, 0) < 10:
            return
        self._last_modified[event.src_path] = now
        # The debounce cache is never evicted otherwise — prune entries
        # older than an hour once it grows past a few thousand.
        if len(self._last_modified) > 5000:
            cutoff = now - 3600
            self._last_modified = {k: v for k, v in self._last_modified.items()
                                   if v >= cutoff}
        self._log("modified", event.src_path)

    def on_deleted(self, event):  # type: ignore[no-untyped-def]
        self._log("deleted", event.src_path, is_dir=event.is_directory)

    def on_moved(self, event):  # type: ignore[no-untyped-def]
        self._log("moved", event.src_path, dest=event.dest_path,
                  is_dir=event.is_directory)


class FileWatcher:
    """Starts one watchdog observer per watched folder that exists."""

    def __init__(self, db: Database, config: Config, tagger=None):
        self.db = db
        self.config = config
        self.tagger = tagger
        self.observer = None
        self.watched: list[str] = []

    def start(self) -> bool:
        if Observer is None:
            return False
        handler = _Handler(self.db, self.config, self.tagger)
        self.observer = Observer()
        # Sensitive folders are watched too, even when they sit outside
        # Desktop/Documents — otherwise no file events exist to scan and
        # sensitive-copy / large-transfer detection can never fire.
        for folder in self.config.effective_watch_folders():
            if folder.exists():
                self.observer.schedule(handler, str(folder), recursive=True)
                self.watched.append(str(folder))
        if not self.watched:
            return False
        self.observer.start()
        return True

    def stop(self) -> None:
        if self.observer is not None:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
