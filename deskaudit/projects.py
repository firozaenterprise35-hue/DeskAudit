"""Project tagging — the rule engine.

A project is a named bucket of work (Office, Study, Personal, DeskAudit,
a client, a course...). Every tracked thing — an app session, a file
event, a browser visit / session / download / search — gets:

    primary project : exactly one, e.g. "Office"
    tags            : zero or more secondary labels, e.g. ["work", "urgent"]

How a primary project is chosen, in order:
1. Manual override (exact match, from projects.yaml `overrides` or the
   DB `project_overrides` table, set with --project-tag). Overrides always win.
2. Rules, highest `priority` first. A rule matches if ANY of its
   conditions match (folders, file patterns, apps, window keywords /
   regex, domains, URL parts, search keywords). All matching rules also
   contribute their `tags` as secondary tags.
3. `default_project` (default "Untagged") when nothing matches.

Rule fields (all optional, all lists unless noted):
    folders          — path prefix, e.g. "%USERPROFILE%/Documents/Office"
    file_patterns    — fnmatch on the file name, e.g. "*.xlsx", "*report*"
    file_extensions  — e.g. [".py", ".docx"] (matched on the file name)
    apps             — exe name, e.g. "WINWORD.EXE" (case-insensitive)
    window_keywords  — substring in the window title
    window_regex     — regex on the window title
    domains          — substring in the domain, e.g. "github.com"
    url_contains     — substring in the full URL
    url_regex        — regex on the full URL
    search_keywords  — substring in the search query
    tags             — secondary tags this project adds when it matches
    priority         — higher wins (default 50)
    color            — for future dashboards, e.g. "#2563eb"
    daily_goal_minutes — for the daily report, e.g. 240
    parent           — parent project name, e.g. "Office" for "Office / Client A"
    description      — free text

Matching is case-insensitive everywhere except regex (which the user
controls). Nothing here reads file contents — only paths, names, titles,
domains and URLs that DeskAudit already logs.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _norm_path(raw: str) -> str:
    """Normalise a file path for comparison: expand %VAR% / ~, lower-case,
    forward slashes, no trailing slash. Windows paths use backslashes,
    rule files use forward slashes — both must match the same way."""
    expanded = os.path.expandvars(os.path.expanduser(str(raw or "")))
    return expanded.replace("\\", "/").strip().lower().rstrip("/")


def _safe_int(value, default: int) -> int:
    """int() that falls back to `default` instead of crashing on a typo."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _expand(raw: str) -> str:
    return os.path.expandvars(os.path.expanduser(str(raw or "")))


def _match_any(needles: list[str], haystack: str, *, regex: bool = False) -> bool:
    hay = _norm(haystack)
    if not hay:
        return False
    for needle in needles or []:
        if not needle:
            continue
        if regex:
            try:
                if re.search(str(needle), haystack or "", re.IGNORECASE):
                    return True
            except re.error:
                continue
        elif _norm(needle) in hay:
            return True
    return False


@dataclass
class Project:
    name: str
    description: str = ""
    color: str = ""
    parent: str = ""
    daily_goal_minutes: int = 0
    priority: int = 50
    folders: list[str] = field(default_factory=list)
    file_patterns: list[str] = field(default_factory=list)
    file_extensions: list[str] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    window_keywords: list[str] = field(default_factory=list)
    window_regex: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    url_contains: list[str] = field(default_factory=list)
    url_regex: list[str] = field(default_factory=list)
    search_keywords: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> "Project":
        raw = raw or {}
        return cls(
            name=str(raw.get("name", "Untagged")),
            description=str(raw.get("description", "") or ""),
            color=str(raw.get("color", "") or ""),
            parent=str(raw.get("parent", "") or ""),
            daily_goal_minutes=_safe_int(raw.get("daily_goal_minutes", 0) or 0, 0),
            priority=_safe_int(raw.get("priority", 50) or 0, 50),
            folders=[_expand(p) for p in raw.get("folders", []) or []],
            file_patterns=list(raw.get("file_patterns", []) or []),
            file_extensions=[e.lower() for e in raw.get("file_extensions", []) or []],
            apps=[a.lower() for a in raw.get("apps", []) or []],
            window_keywords=list(raw.get("window_keywords", []) or []),
            window_regex=list(raw.get("window_regex", []) or []),
            domains=[d.lower() for d in raw.get("domains", []) or []],
            url_contains=list(raw.get("url_contains", []) or []),
            url_regex=list(raw.get("url_regex", []) or []),
            search_keywords=list(raw.get("search_keywords", []) or []),
            tags=list(raw.get("tags", []) or []),
        )

    def matches(self, *, path: str = "", file_name: str = "", app: str = "",
                title: str = "", domain: str = "", url: str = "",
                search_query: str = "") -> bool:
        """True if ANY condition matches. A project with no conditions
        never matches — it can only be the default project."""
        if self.folders and path:
            p = _norm_path(path)
            for folder in self.folders:
                f = _norm_path(folder)
                # Prefix match only, on one separator: a rule
                # "%USERPROFILE%/Documents/Office" must match
                # "C:\Users\X\Documents\Office\report.xlsx", and a rule
                # "Office" must NOT match "...\MyOffice\file.txt".
                if f and (p == f or p.startswith(f + "/")):
                    return True
        if file_name:
            name = Path(file_name).name
            for pattern in self.file_patterns:
                if fnmatch.fnmatch(name.lower(), pattern.lower()):
                    return True
            ext = Path(name).suffix.lower()
            if ext and ext in self.file_extensions:
                return True
            if _match_any(self.file_patterns, name):
                return True
        if app and _norm(app) in [a.lower() for a in self.apps]:
            return True
        if title and (_match_any(self.window_keywords, title)
                      or _match_any(self.window_regex, title, regex=True)):
            return True
        if domain and _match_any(self.domains, domain):
            return True
        if url and (_match_any(self.url_contains, url)
                    or _match_any(self.url_regex, url, regex=True)):
            return True
        if search_query and _match_any(self.search_keywords, search_query):
            return True
        return False


@dataclass
class Override:
    match_type: str  # domain | url | file | app | title
    match_value: str
    project: str
    tags: list[str] = field(default_factory=list)


class Tagger:
    """Loads projects.yaml and answers: what project is this thing?"""

    def __init__(self, projects: list[Project] | None = None,
                 overrides: list[Override] | None = None,
                 default_project: str = "Untagged"):
        self.projects: list[Project] = sorted(
            projects or [], key=lambda p: (-p.priority, p.name))
        self.by_name = {p.name: p for p in self.projects}
        self.overrides: list[Override] = overrides or []
        self.default_project = default_project or "Untagged"

    @classmethod
    def load(cls, path: str | Path = "projects.yaml") -> "Tagger":
        raw: dict = {}
        p = Path(path)
        if p.exists():
            try:
                import yaml  # type: ignore

                with p.open(encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            except Exception as exc:
                # LOUD, not silent: a broken projects.yaml used to wipe
                # all rules with no message anywhere. Warn in the log and
                # on stderr so the user can fix the file.
                import logging
                import sys

                msg = (f"projects.yaml could not be parsed ({exc}) — "
                       "project tagging is off until the file is fixed.")
                logging.getLogger("deskaudit.projects").warning(msg)
                print(f"WARNING: {msg}", file=sys.stderr)
                raw = {}
        projects = [Project.from_dict(item) for item in raw.get("projects", []) or []]
        overrides = [
            Override(
                match_type=str(o.get("match_type", "")),
                match_value=str(o.get("match_value", "")),
                project=str(o.get("project", "")),
                tags=list(o.get("tags", []) or []),
            )
            for o in raw.get("overrides", []) or []
        ]
        return cls(projects, overrides,
                   str(raw.get("default_project", "Untagged") or "Untagged"))

    @classmethod
    def empty(cls) -> "Tagger":
        return cls([], [], "Untagged")


    # -- overrides ------------------------------------------------------
    def _override_for(self, *, path: str = "", app: str = "", title: str = "",
                      domain: str = "", url: str = "") -> Override | None:
        for ov in self.overrides:
            value = _norm(ov.match_value)
            if not value:
                continue
            if ov.match_type == "domain" and value in _norm(domain):
                return ov
            if ov.match_type == "url" and value in _norm(url):
                return ov
            if ov.match_type == "file" and value in _norm(_expand(path)):
                return ov
            if ov.match_type == "app" and value == _norm(app):
                return ov
            if ov.match_type == "title" and value in _norm(title):
                return ov
        return None

    def add_override(self, match_type: str, match_value: str, project: str,
                     tags: list[str] | None = None) -> None:
        self.overrides = [
            o for o in self.overrides
            if not (o.match_type == match_type and _norm(o.match_value) == _norm(match_value))
        ]
        self.overrides.append(Override(match_type, match_value, project, tags or []))

    # -- main entry point ------------------------------------------------
    def tag(self, *, path: str = "", file_name: str = "", app: str = "",
            title: str = "", domain: str = "", url: str = "",
            search_query: str = "") -> tuple[str, list[str]]:
        """Return (primary_project, secondary_tags)."""
        ov = self._override_for(path=path, app=app, title=title, domain=domain, url=url)
        if ov:
            return ov.project, list(ov.tags)

        matched = [
            p for p in self.projects
            if p.matches(path=path, file_name=file_name or Path(path).name if path else "",
                         app=app, title=title, domain=domain, url=url,
                         search_query=search_query)
        ]
        if not matched:
            return self.default_project, []
        primary = matched[0]
        tags: list[str] = []
        for p in matched:
            for t in p.tags:
                if t not in tags:
                    tags.append(t)
            # A secondary project name is also a useful tag.
            if p is not primary and p.name not in tags:
                tags.append(p.name)
        return primary.name, tags

    # -- convenience wrappers, one per tracked thing ---------------------
    def tag_app(self, app: str, exe_path: str = "", title: str = "") -> tuple[str, list[str]]:
        return self.tag(app=app, title=title, path=exe_path)

    def tag_file(self, path: str, dest_path: str = "") -> tuple[str, list[str]]:
        project, tags = self.tag(path=path, file_name=Path(path).name)
        if dest_path:
            p2, t2 = self.tag(path=dest_path, file_name=Path(dest_path).name)
            if p2 != self.default_project and project == self.default_project:
                return p2, list(dict.fromkeys(tags + t2))
        return project, tags

    def tag_browser(self, url: str = "", domain: str = "", title: str = "",
                    search_query: str = "") -> tuple[str, list[str]]:
        return self.tag(url=url, domain=domain, title=title, search_query=search_query)

    def tag_download(self, url: str = "", domain: str = "", file_path: str = "") -> tuple[str, list[str]]:
        project, tags = self.tag(url=url, domain=domain, path=file_path,
                                 file_name=Path(file_path).name if file_path else "")
        return project, tags

    def tag_search(self, query: str, url: str = "", domain: str = "") -> tuple[str, list[str]]:
        return self.tag(url=url, domain=domain, search_query=query)

    # -- suggestions ------------------------------------------------------
    def suggest_for_untagged(self, items: list[dict]) -> list[dict]:
        """Turn untagged top items into copy-paste YAML rule suggestions.
        Each item: {"kind": "app"|"domain"|"folder", "value": str, "seconds": float}
        Values are dumped with yaml.safe_dump so a quote in a window title
        cannot produce a malformed suggestion.
        """
        import yaml  # type: ignore

        def _yaml_line(key: str, value: str) -> str:
            dumped = yaml.safe_dump({key: [value]}, allow_unicode=True,
                                    default_flow_style=True).strip()
            return f"    {dumped}"

        suggestions = []
        for item in items:
            value = item.get("value", "")
            kind = item.get("kind", "")
            if not value:
                continue
            if kind == "domain":
                suggestions.append({
                    "value": value, "kind": kind,
                    "yaml": _yaml_line("domains", value),
                    "hint": "Add this domain to a project in projects.yaml",
                })
            elif kind == "folder":
                suggestions.append({
                    "value": value, "kind": kind,
                    "yaml": _yaml_line("folders", value),
                    "hint": "Add this folder to a project in projects.yaml",
                })
            elif kind == "app":
                suggestions.append({
                    "value": value, "kind": kind,
                    "yaml": _yaml_line("apps", value),
                    "hint": "Add this app to a project in projects.yaml",
                })
            elif kind == "title":
                suggestions.append({
                    "value": value, "kind": kind,
                    "yaml": _yaml_line("window_keywords", value),
                    "hint": "Add this title keyword to a project in projects.yaml",
                })
        return suggestions


def update_project_goal(path: str | Path, name: str, minutes: int) -> str:
    """Set a project's daily_goal_minutes in projects.yaml (used by
    Telegram /goal). Returns "updated" when the project existed,
    "created" when a minimal entry was added. Raises ValueError on a
    bad goal and FileNotFoundError when the file does not exist.
    Like --project-add, the file is rewritten with yaml.safe_dump."""
    import yaml  # type: ignore

    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
        raise ValueError("goal must be 0 or more minutes")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"projects file not found: {path}")
    with p.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    projects = raw.get("projects", []) or []
    found = False
    for item in projects:
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            item["daily_goal_minutes"] = minutes
            found = True
            break
    if not found:
        projects.append({"name": name, "daily_goal_minutes": minutes})
        raw["projects"] = projects
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True)
    return "updated" if found else "created"
