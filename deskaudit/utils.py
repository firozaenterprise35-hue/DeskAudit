"""Small helpers shared by the trackers and the report generator."""

from __future__ import annotations

from datetime import datetime, timezone


def format_duration(seconds: float) -> str:
    """3599 -> '59m', 7320 -> '2h 2m', 45 -> '45s'."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")
