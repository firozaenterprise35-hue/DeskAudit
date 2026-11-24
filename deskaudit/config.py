"""Load config.yaml with safe defaults.

If PyYAML is missing, or the file is missing / broken, the agent still
starts with the defaults below instead of crashing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = {
    "poll_interval_seconds": 5,
    "idle_threshold_seconds": 300,
    "db_path": "data/deskaudit.db",
    "log_path": "data/deskaudit.log",
    "watch_folders": ["%USERPROFILE%/Desktop", "%USERPROFILE%/Documents"],
    "exclude_extensions": [".tmp", ".log", ".lock", ".part", ".crdownload"],
    "exclude_folders": ["data", "reports", ".venv", "__pycache__"],
    "browser_enabled": True,
    "browser_poll_seconds": 60,
    "browser_cdp_port": 0,
    "browser_receiver_enabled": False,
    "browser_receiver_port": 8765,
    "track_downloads": True,
    "track_searches": True,
    "productive_domains": [],
    "distracting_domains": [],
    "exclude_domains": [],
}


def expand_path(raw: str) -> Path:
    """Expand %VAR%, $VAR and ~ in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(raw)))


@dataclass
class Config:
    poll_interval_seconds: int = 5
    idle_threshold_seconds: int = 300
    db_path: Path = Path("data/deskaudit.db")
    log_path: Path = Path("data/deskaudit.log")
    watch_folders: list[Path] = field(default_factory=list)
    exclude_extensions: set[str] = field(default_factory=set)
    exclude_folders: set[str] = field(default_factory=set)
    browser_enabled: bool = True
    browser_poll_seconds: int = 60
    browser_cdp_port: int = 0
    browser_receiver_enabled: bool = False
    browser_receiver_port: int = 8765
    track_downloads: bool = True
    track_searches: bool = True
    productive_domains: set[str] = field(default_factory=set)
    distracting_domains: set[str] = field(default_factory=set)
    exclude_domains: set[str] = field(default_factory=set)
    alerts: dict = field(default_factory=dict)
    backup_keep_days: int = 30
    backup_keep_count: int = 30
    backup_on_start: bool = True
    llm: dict = field(default_factory=dict)
    project_root: Path = Path(".")

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> "Config":
        raw: dict = {}
        cfg_path = Path(path)
        if cfg_path.exists():
            try:
                import yaml  # type: ignore

                with cfg_path.open(encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception:
                raw = {}
        merged = {**DEFAULTS, **raw}
        root = cfg_path.resolve().parent if cfg_path.exists() else Path(".").resolve()
        return cls(
            poll_interval_seconds=int(merged.get("poll_interval_seconds", 5)),
            idle_threshold_seconds=int(merged.get("idle_threshold_seconds", 300)),
            db_path=(root / expand_path(str(merged.get("db_path", "data/deskaudit.db"))))
            if not expand_path(str(merged.get("db_path", "data/deskaudit.db"))).is_absolute()
            else expand_path(str(merged.get("db_path", "data/deskaudit.db"))),
            log_path=(root / expand_path(str(merged.get("log_path", "data/deskaudit.log"))))
            if not expand_path(str(merged.get("log_path", "data/deskaudit.log"))).is_absolute()
            else expand_path(str(merged.get("log_path", "data/deskaudit.log"))),
            watch_folders=[
                expand_path(str(p)) for p in merged.get("watch_folders", [])
            ],
            exclude_extensions={
                str(e).lower() for e in merged.get("exclude_extensions", [])
            },
            exclude_folders={
                str(e).lower() for e in merged.get("exclude_folders", [])
            },
            browser_enabled=bool(merged.get("browser_enabled", True)),
            browser_poll_seconds=int(merged.get("browser_poll_seconds", 60)),
            browser_cdp_port=int(merged.get("browser_cdp_port", 0) or 0),
            browser_receiver_enabled=bool(merged.get("browser_receiver_enabled", False)),
            browser_receiver_port=int(merged.get("browser_receiver_port", 8765)),
            track_downloads=bool(merged.get("track_downloads", True)),
            track_searches=bool(merged.get("track_searches", True)),
            productive_domains={str(e).lower() for e in merged.get("productive_domains", [])},
            distracting_domains={str(e).lower() for e in merged.get("distracting_domains", [])},
            exclude_domains={str(e).lower() for e in merged.get("exclude_domains", [])},
            alerts=dict(merged.get("alerts", {}) or {}),
            backup_keep_days=int(merged.get("backup_keep_days", 30)),
            backup_keep_count=int(merged.get("backup_keep_count", 30)),
            backup_on_start=bool(merged.get("backup_on_start", True)),
            llm=dict(merged.get("llm", {}) or {}),
            project_root=root,
        )

    def is_excluded(self, path: Path) -> bool:
        """True if a file/folder should not be logged."""
        lowered = str(path).lower()
        if path.suffix.lower() in self.exclude_extensions:
            return True
        return any(part.lower() in self.exclude_folders for part in Path(lowered).parts)
