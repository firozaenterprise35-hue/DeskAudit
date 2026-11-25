"""Load config.yaml with safe defaults.

If PyYAML is missing, or the file is missing / broken, the agent still
starts with the defaults below instead of crashing.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("deskaudit.config")

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
    "dashboard_on_start": True,
    "secret_gate": {
        "enabled": True,
        "clicks": 4,
        "code": "1122",
        "click_window_seconds": 2,
        "arm_seconds": 10,
    },
}

# Imported lazily to avoid a cycle (secret_gate imports nothing from config).
DEFAULT_SECRET_GATE = {
    "enabled": True,
    "clicks": 4,
    "code": "1122",
    "click_window_seconds": 2,
    "arm_seconds": 10,
}


def expand_path(raw: str) -> Path:
    """Expand %VAR%, $VAR and ~ in a path string."""
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _coerce_int(value, default: int) -> int:
    """int() that falls back to `default` with a warning instead of
    crashing every command on a typo like poll_interval_seconds: "fast"."""
    try:
        return int(value)
    except (TypeError, ValueError):
        _log.warning("Bad config value %r — using default %s.", value, default)
        return default


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
    dashboard_on_start: bool = True
    secret_gate: dict = field(default_factory=dict)
    transfers: dict = field(default_factory=dict)
    telegram: dict = field(default_factory=dict)
    email: dict = field(default_factory=dict)
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
            except Exception as exc:
                # Loud, not silent: a YAML syntax error used to fall back
                # to defaults with no message anywhere.
                _log.warning("config.yaml could not be parsed (%s) — using defaults.", exc)
                print(f"WARNING: config.yaml could not be parsed ({exc}) — using defaults.",
                      file=sys.stderr)
                raw = {}
        # config.local.yaml (same folder, git-ignored) overrides config.yaml.
        # This is where secrets belong — e.g. llm.api_key — because
        # config.yaml is tracked by git and config.local.yaml is not.
        # Nested sections (llm, alerts) merge key-by-key, so a local
        # llm.api_key does not wipe the other llm settings.
        local_path = cfg_path.with_name("config.local.yaml")
        if local_path.exists():
            try:
                import yaml  # type: ignore

                with local_path.open(encoding="utf-8") as f:
                    local = yaml.safe_load(f) or {}
                for key, value in local.items():
                    if isinstance(value, dict) and isinstance(raw.get(key), dict):
                        raw[key] = {**raw[key], **value}
                    else:
                        raw[key] = value
            except Exception as exc:
                _log.warning("config.local.yaml could not be parsed (%s) — ignoring it.", exc)
                pass
        merged = {**DEFAULTS, **raw}
        root = cfg_path.resolve().parent if cfg_path.exists() else Path(".").resolve()
        # Transfers: defaults from transfers.py, overlaid by config.yaml /
        # config.local.yaml (nested merge above already handled the local file).
        from .transfers import DEFAULT_TRANSFERS

        transfers = {**DEFAULT_TRANSFERS, **(merged.get("transfers", {}) or {})}
        # Telegram + email: same pattern. Secrets (bot_token, smtp_password)
        # belong in config.local.yaml, never config.yaml (tracked by git).
        from .emailer import DEFAULT_EMAIL
        from .telegram import DEFAULT_TELEGRAM

        telegram = {**DEFAULT_TELEGRAM, **(merged.get("telegram", {}) or {})}
        email = {**DEFAULT_EMAIL, **(merged.get("email", {}) or {})}
        return cls(
            poll_interval_seconds=_coerce_int(merged.get("poll_interval_seconds", 5), 5),
            idle_threshold_seconds=_coerce_int(merged.get("idle_threshold_seconds", 300), 300),
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
            browser_poll_seconds=_coerce_int(merged.get("browser_poll_seconds", 60), 60),
            browser_cdp_port=_coerce_int(merged.get("browser_cdp_port", 0) or 0, 0),
            browser_receiver_enabled=bool(merged.get("browser_receiver_enabled", False)),
            browser_receiver_port=_coerce_int(merged.get("browser_receiver_port", 8765), 8765),
            track_downloads=bool(merged.get("track_downloads", True)),
            track_searches=bool(merged.get("track_searches", True)),
            productive_domains={str(e).lower() for e in merged.get("productive_domains", [])},
            distracting_domains={str(e).lower() for e in merged.get("distracting_domains", [])},
            exclude_domains={str(e).lower() for e in merged.get("exclude_domains", [])},
            alerts=dict(merged.get("alerts", {}) or {}),
            backup_keep_days=_coerce_int(merged.get("backup_keep_days", 30), 30),
            backup_keep_count=_coerce_int(merged.get("backup_keep_count", 30), 30),
            backup_on_start=bool(merged.get("backup_on_start", True)),
            llm=dict(merged.get("llm", {}) or {}),
            dashboard_on_start=bool(merged.get("dashboard_on_start", True)),
            secret_gate={**DEFAULT_SECRET_GATE, **(merged.get("secret_gate", {}) or {})},
            transfers=transfers,
            telegram=telegram,
            email=email,
            project_root=root,
        )

    def is_excluded(self, path: Path) -> bool:
        """True if a file/folder should not be logged.

        Folder exclusions apply ONLY under the app's own folder
        (project_root/data, project_root/reports, ...). The old check
        matched ANY path component, so Documents\\data\\invoice.xlsx and
        Documents\\reports\\Q4.xlsx were never logged — and transfer
        detection was blind for those folders too.
        """
        lowered = str(path).lower()
        if path.suffix.lower() in self.exclude_extensions:
            return True
        p = lowered.replace("\\", "/").rstrip("/")
        root = str(self.project_root).replace("\\", "/").lower().rstrip("/")
        for folder in self.exclude_folders:
            f = f"{root}/{str(folder).lower().strip('/')}"
            if p == f or p.startswith(f + "/"):
                return True
        return False

    def effective_watch_folders(self) -> list[Path]:
        """Watch folders + sensitive folders (for the Data Transfer
        Monitor). A sensitive folder outside Desktop/Documents still
        needs watching, or no file events exist to scan for it."""
        folders: list[Path] = list(self.watch_folders)
        seen = {str(p).lower().replace("\\", "/") for p in folders}
        for raw in (self.transfers or {}).get("sensitive_folders") or []:
            try:
                p = expand_path(str(raw))
            except Exception:
                continue
            if str(p).lower().replace("\\", "/") not in seen:
                seen.add(str(p).lower().replace("\\", "/"))
                folders.append(p)
        return folders
