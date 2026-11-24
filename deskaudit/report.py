"""Daily report: prints a summary and writes Markdown + CSV files."""

from __future__ import annotations

import csv
from pathlib import Path

from .database import Database
from .utils import format_duration, today_str


def build_summary(db: Database, date: str, tagger=None) -> dict:
    totals = db.totals(date)
    rows = db.app_totals(date)
    files = db.file_counts(date)
    # Projects: goal comes from projects.yaml (tagger) when available,
    # otherwise from the DB projects table.
    goals: dict[str, int] = {}
    if tagger is not None:
        goals = {p.name: p.daily_goal_minutes for p in tagger.projects}
    else:
        try:
            goals = {r["name"]: int(r["daily_goal_minutes"] or 0)
                     for r in db.list_projects()}
        except Exception:
            goals = {}
    project_rows = [
        {
            "project": r["project"],
            "app_seconds": float(r["app_active"] or 0),
            "browser_seconds": float(r["browser_active"] or 0),
            "active_seconds": float(r["active"] or 0),
            "file_events": int(r["file_events"] or 0),
            "visits": int(r["visits"] or 0),
            "goal_minutes": int(goals.get(r["project"], 0) or 0),
        }
        for r in db.project_totals(date)
    ]
    for p in project_rows:
        goal_seconds = p["goal_minutes"] * 60
        p["goal_percent"] = round(100 * p["active_seconds"] / goal_seconds) if goal_seconds else 0
        p["goal_met"] = bool(goal_seconds and p["active_seconds"] >= goal_seconds)
    untagged = db.untagged_top(date, limit=5)
    # Data transfers (organisation): counts by type + the latest rows.
    try:
        tcounts = db.transfer_counts(date)
        trows = [
            {
                "type": r["transfer_type"],
                "channel": r["channel"] or "",
                "file": r["file_name"] or "",
                "size": r["size_bytes"],
                "destination": r["destination"] or "",
                "app": r["app"] or "",
                "at": r["occurred_at"] or "",
            }
            for r in db.transfers_for_date(date, limit=20)
        ]
    except Exception:
        tcounts, trows = {}, []
    top = [
        {
            "app": r["app_name"],
            "title": r["window_title"] or "",
            "active_seconds": float(r["active"] or 0),
            "total_seconds": float(r["total"] or 0),
            "sessions": int(r["sessions"] or 0),
        }
        for r in rows
    ]
    b_totals = db.browser_totals(date)
    domain_rows = db.browser_domain_totals(date)
    if not domain_rows:
        # History-only import: no live sessions yet, show visit counts.
        domain_rows = db.browser_visit_domain_totals(date)
    b_domains = [
        {
            "domain": r["domain"],
            "category": r["category"] or "other",
            "productive": bool(r["is_productive"]),
            "active_seconds": float(r["active"] or 0),
            "sessions": int(r["sessions"] or 0),
        }
        for r in domain_rows
    ]
    return {
        "date": date,
        "active_seconds": float(totals["active"] or 0),
        "idle_seconds": float(totals["idle"] or 0),
        "total_seconds": float(totals["total"] or 0),
        "apps_used": int(totals["apps"] or 0),
        "top_apps": top[:10],
        "file_counts": files,
        "projects": project_rows,
        "untagged": untagged,
        "transfers": {
            "total": sum(c.get("count", 0) for c in tcounts.values()),
            "counts": tcounts,
            "rows": trows,
        },
        "browser": {
            "active_seconds": float(b_totals["active"] or 0),
            "productive_seconds": float(b_totals["productive"] or 0),
            "distracting_seconds": float(b_totals["distracting"] or 0),
            "domains_used": int(b_totals["domains"] or 0),
            "visits": db.browser_visit_count(date),
            "top_domains": b_domains[:10],
            "searches": [
                {"query": r["query"], "count": int(r["n"] or 0)}
                for r in db.top_searches(date)
            ],
            "downloads": [
                {
                    "browser": r["browser"],
                    "file": r["file_name"] or "",
                    "size": r["total_bytes"],
                    "state": r["state"] or "",
                }
                for r in db.downloads_for_date(date)
            ],
        },
    }


def render_text(summary: dict) -> str:
    lines = [
        f"DeskAudit — {summary['date']}",
        f"Active: {format_duration(summary['active_seconds'])}"
        f" | Idle: {format_duration(summary['idle_seconds'])}"
        f" | Apps used: {summary['apps_used']}",
        "Top apps:",
    ]
    for item in summary["top_apps"]:
        title = f" — {item['title']}" if item["title"] else ""
        lines.append(f"  {item['app']} — {format_duration(item['active_seconds'])}{title}")
    if not summary["top_apps"]:
        lines.append("  (no app sessions recorded)")
    fc = summary["file_counts"]
    lines.append(
        "File events: "
        + ", ".join(f"{fc.get(k, 0)} {k}" for k in ("created", "modified", "deleted", "moved"))
    )
    b = summary.get("browser", {})
    lines.append(
        f"Browser: {format_duration(b.get('active_seconds', 0))} active"
        f" | {b.get('domains_used', 0)} domains | {b.get('visits', 0)} visits"
        f" | productive {format_duration(b.get('productive_seconds', 0))}"
        f" | distracting {format_duration(b.get('distracting_seconds', 0))}"
    )
    lines.append("Top domains:")
    for item in b.get("top_domains", []):
        mark = "work" if item["productive"] else item["category"]
        if item["active_seconds"]:
            lines.append(f"  {item['domain']} — {format_duration(item['active_seconds'])} [{mark}]")
        else:
            lines.append(f"  {item['domain']} — {item['sessions']} visits [{mark}]")
    if not b.get("top_domains"):
        lines.append("  (no browser sessions recorded)")
    if b.get("searches"):
        lines.append("Top searches:")
        for s in b["searches"][:5]:
            lines.append(f"  {s['query']} (x{s['count']})")
    if b.get("downloads"):
        lines.append("Downloads:")
        for d in b["downloads"][:5]:
            lines.append(f"  {d['browser']}: {d['file']} [{d['state']}]")
    lines.append("Projects:")
    for p in summary.get("projects", []):
        goal = ""
        if p.get("goal_minutes"):
            goal = (f" (goal {format_duration(p['goal_minutes'] * 60)}, "
                    f"{p.get('goal_percent', 0)}%{' ✓' if p.get('goal_met') else ''})")
        lines.append(
            f"  {p['project']} — {format_duration(p['active_seconds'])}"
            f" [app {format_duration(p['app_seconds'])}, "
            f"browser {format_duration(p['browser_seconds'])}, "
            f"{p['file_events']} files, {p['visits']} visits]{goal}"
        )
    if not summary.get("projects"):
        lines.append("  (no tagged activity recorded)")
    untagged = summary.get("untagged", {})
    untagged_seconds = sum(a.get("seconds", 0) for a in untagged.get("apps", []))
    if untagged_seconds:
        lines.append(f"Untagged: {format_duration(untagged_seconds)} in apps — "
                     "run: python main.py --suggest-projects")
    tr = summary.get("transfers", {})
    if tr.get("total"):
        lines.append(f"Data transfers: {tr['total']} event(s)")
        for kind, c in sorted(tr.get("counts", {}).items()):
            lines.append(f"  {kind}: {c.get('count', 0)}")
        for row in tr.get("rows", [])[:5]:
            size = f" ({round(row['size'] / 1024 / 1024, 1)} MB)" if row.get("size") else ""
            lines.append(f"  {row['at'][11:16]} {row['type']} — {row['file'] or row['channel']}"
                         f" -> {row['destination']}{size}")
    else:
        lines.append("Data transfers: none recorded")
    return "\n".join(lines)


def render_markdown(summary: dict) -> str:
    lines = [
        f"# DeskAudit — {summary['date']}",
        "",
        f"- **Active:** {format_duration(summary['active_seconds'])}",
        f"- **Idle:** {format_duration(summary['idle_seconds'])}",
        f"- **Apps used:** {summary['apps_used']}",
        "",
        "## Top apps",
        "",
        "| App | Window | Active | Sessions |",
        "|---|---|---:|---:|",
    ]
    for item in summary["top_apps"]:
        lines.append(
            f"| {item['app']} | {item['title']} | "
            f"{format_duration(item['active_seconds'])} | {item['sessions']} |"
        )
    lines += ["", "## File events", ""]
    for key in ("created", "modified", "deleted", "moved"):
        lines.append(f"- {key}: {summary['file_counts'].get(key, 0)}")
    b = summary.get("browser", {})
    lines += [
        "", "## Browser", "",
        f"- **Browser active:** {format_duration(b.get('active_seconds', 0))}",
        f"- **Productive:** {format_duration(b.get('productive_seconds', 0))}",
        f"- **Distracting:** {format_duration(b.get('distracting_seconds', 0))}",
        f"- **Domains used:** {b.get('domains_used', 0)}",
        f"- **Visits:** {b.get('visits', 0)}",
        "", "| Domain | Category | Active | Sessions |",
        "|---|---|---:|---:|",
    ]
    for item in b.get("top_domains", []):
        lines.append(
            f"| {item['domain']} | {item['category']} | "
            f"{format_duration(item['active_seconds'])} | {item['sessions']} |"
        )
    if b.get("searches"):
        lines += ["", "### Top searches", ""]
        for s in b["searches"]:
            lines.append(f"- {s['query']} (x{s['count']})")
    if b.get("downloads"):
        lines += ["", "### Downloads", ""]
        for d in b["downloads"]:
            lines.append(f"- {d['browser']}: {d['file']} [{d['state']}]")
    lines += ["", "## Projects", "",
              "| Project | Active | App | Browser | Files | Visits | Goal |",
              "|---|---|---:|---:|---:|---:|---|"]
    for p in summary.get("projects", []):
        goal = f"{format_duration(p['goal_minutes'] * 60)} ({p.get('goal_percent', 0)}%)" if p.get("goal_minutes") else "-"
        lines.append(
            f"| {p['project']} | {format_duration(p['active_seconds'])} | "
            f"{format_duration(p['app_seconds'])} | {format_duration(p['browser_seconds'])} | "
            f"{p['file_events']} | {p['visits']} | {goal} |"
        )
    untagged = summary.get("untagged", {})
    if untagged.get("apps") or untagged.get("domains"):
        lines += ["", "### Untagged — top items to tag", ""]
        for a in untagged.get("apps", [])[:5]:
            lines.append(f"- App: {a['value']} — {format_duration(a['seconds'])}")
        for d in untagged.get("domains", [])[:5]:
            lines.append(f"- Domain: {d['value']} — {format_duration(d['seconds'])}")
        for t in untagged.get("titles", [])[:5]:
            lines.append(f"- Title: {t['value']} — {format_duration(t['seconds'])}")
    tr = summary.get("transfers", {})
    lines += ["", "## Data transfers", ""]
    if tr.get("total"):
        lines.append(f"- **Total events:** {tr['total']}")
        for kind, c in sorted(tr.get("counts", {}).items()):
            lines.append(f"- {kind}: {c.get('count', 0)}")
        lines += ["", "| When | Type | File / Channel | Destination | Size |",
                  "|---|---|---|---|---:|"]
        for row in tr.get("rows", []):
            size = f"{round(row['size'] / 1024 / 1024, 1)} MB" if row.get("size") else "-"
            lines.append(f"| {row['at']} | {row['type']} | {row['file'] or row['channel']} | "
                         f"{row['destination']} | {size} |")
    else:
        lines.append("- No data transfers recorded.")
    return "\n".join(lines) + "\n"


def generate_report(db: Database, date: str | None = None,
                    reports_dir: str | Path = "reports", tagger=None) -> tuple[str, Path, Path]:
    date = date or today_str()
    summary = build_summary(db, date, tagger=tagger)
    text = render_text(summary)
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{date}.md"
    csv_path = out_dir / f"{date}.csv"
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["app", "window_title", "active_seconds", "total_seconds", "sessions"])
        for item in summary["top_apps"]:
            writer.writerow([item["app"], item["title"], int(item["active_seconds"]),
                             int(item["total_seconds"]), item["sessions"]])
    browser_csv = out_dir / f"{date}-browser.csv"
    with browser_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["domain", "category", "productive", "active_seconds", "sessions"])
        for item in summary.get("browser", {}).get("top_domains", []):
            writer.writerow([item["domain"], item["category"], int(item["productive"]),
                             int(item["active_seconds"]), item["sessions"]])
    projects_csv = out_dir / f"{date}-projects.csv"
    with projects_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["project", "active_seconds", "app_seconds", "browser_seconds",
                         "file_events", "visits", "goal_minutes", "goal_percent"])
        for p in summary.get("projects", []):
            writer.writerow([p["project"], int(p["active_seconds"]), int(p["app_seconds"]),
                             int(p["browser_seconds"]), p["file_events"], p["visits"],
                             p["goal_minutes"], p.get("goal_percent", 0)])
    transfers_csv = out_dir / f"{date}-transfers.csv"
    with transfers_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["occurred_at", "type", "channel", "file", "size_bytes",
                         "destination", "app"])
        for row in summary.get("transfers", {}).get("rows", []):
            writer.writerow([row["at"], row["type"], row["channel"], row["file"],
                             row["size"] if row["size"] is not None else "",
                             row["destination"], row["app"]])
    return text, md_path, csv_path
