"""File watcher using watchdog.

Logs created / modified / deleted / moved events to SQLite. Debounces
rapid repeated 'modified' events for the same file (e.g. an app saving
every few seconds) so the DB is not flooded.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .database import Database

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

    def _should_log(self, path: str) -> bool:
        p = Path(path)
        return not self.config.is_excluded(p)

    def _log(self, event_type: str, src: str, dest: str | None = None,
             is_dir: bool = False) -> None:
        if not self._should_log(src):
            return
        size = None
        try:
            p = Path(src)
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
        for folder in self.config.watch_folders:
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
