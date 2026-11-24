"""Daily AI summary — local, rule-based Bengali narrative. No LLM, no API key,
no internet. Everything is computed from build_summary() + range_summary().

    python main.py --ai-summary                 # today, saves reports/YYYY-MM-DD-summary.md
    python main.py --ai-summary --date 2026-11-23
    python main.py --ai-summary --weekly         # 7-day review, saves ...-weekly.md
    python main.py --ai-summary --weekly --date 2026-11-23   # week ending that date

The daily narrative has these sections, none skipped when data exists:
  headline, main work, productivity, projects & goals, files, browser,
  untagged, yesterday comparison, tomorrow's suggestion.
The weekly review has: totals & average, best / weakest day, top project,
top app, productivity trend, goals met, what to improve next week.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from .dashboard import range_summary
from .report import build_summary
from .utils import format_duration, today_str


def _pct(part: float, whole: float) -> int:
    return round(100 * part / whole) if whole else 0


def _delta_text(now: float, before: float, label: str) -> str:
    if not before:
        return ""
    diff = _pct(now - before, before)
    if diff > 5:
        return f" গতকালের তুলনায় {label} {diff}% বেশি।"
    if diff < -5:
        return f" গতকালের তুলনায় {label} {-diff}% কম।"
    return f" গতকালের মতোই {label}।"


def generate_daily_summary(db, date: str | None = None, tagger=None) -> dict:
    date = date or today_str()
    s = build_summary(db, date, tagger=tagger)
    yesterday = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    prev = build_summary(db, yesterday, tagger=tagger)
    b = s["browser"]
    active, idle = s["active_seconds"], s["idle_seconds"]
    total = active + idle
    productive, distracting = b["productive_seconds"], b["distracting_seconds"]
    browser_total = productive + distracting
    top_app = s["top_apps"][0] if s["top_apps"] else None
    top_project = s["projects"][0] if s["projects"] else None
    top_domain = b["top_domains"][0] if b["top_domains"] else None
    fc = s["file_counts"]

    headline = (
        f"{date} — আপনি {format_duration(active)} অ্যাক্টিভ ছিলেন"
        + (f" এবং {format_duration(idle)} আইডল ছিলেন" if idle else "")
        + f", মোট {s['apps_used']}টি অ্যাপ ব্যবহার করেছেন।"
    )
    sections: list[dict] = []
    insights: list[str] = []

    # Main work
    main = []
    if top_project:
        main.append(f"সবচেয়ে বেশি সময় গেছে {top_project['project']} প্রজেক্টে — {format_duration(top_project['active_seconds'])}।")
    if top_app:
        main.append(f"সবচেয়ে বেশি ব্যবহৃত অ্যাপ {top_app['app']} ({format_duration(top_app['active_seconds'])}), "
                    + (f"উইন্ডো: {top_app['title']}।" if top_app["title"] else ""))
    if top_domain:
        main.append(f"ব্রাউজারে সবচেয়ে বেশি {top_domain['domain']} ({top_domain['category']})।")
    if not main:
        main.append("আজ কোনো অ্যাক্টিভিটি রেকর্ড হয়নি — এজেন্ট চালু ছিল কি না দেখে নিন।")
    sections.append({"title": "মূল কাজ", "text": " ".join(main)})

    # Productivity
    prod_text = (
        f"ব্রাউজারে প্রোডাক্টিভ সময় {format_duration(productive)}, ডিস্ট্রাক্টিং সময় {format_duration(distracting)}"
        + (f" — ব্রাউজার সময়ের {_pct(productive, browser_total)}% প্রোডাক্টিভ।" if browser_total else "।")
        + _delta_text(active, prev["active_seconds"], "অ্যাক্টিভ সময়")
    )
    if browser_total and distracting > productive:
        insights.append("ডিস্ট্রাক্টিং ব্রাউজিং প্রোডাক্টিভ সময়ের চেয়ে বেশি — আগামীকাল ডিস্ট্রাক্টিং সাইটের সময় সীমা ঠিক করে নিতে পারেন।")
    if top_app and active and top_app["active_seconds"] / active > 0.5:
        insights.append(f"{top_app['app']}-এ দিনের অর্ধেকের বেশি সময় গেছে — একটানা এক অ্যাপে এত সময় স্বাভাবিক কি না দেখে নিন।")
    sections.append({"title": "প্রোডাক্টিভিটি", "text": prod_text})

    # Projects & goals
    if s["projects"]:
        lines = []
        for p in s["projects"]:
            goal = f", গোল {format_duration(p['goal_minutes'] * 60)} এর {p.get('goal_percent', 0)}%" if p.get("goal_minutes") else ""
            mark = " ✓" if p.get("goal_met") else ""
            lines.append(f"{p['project']}: {format_duration(p['active_seconds'])}{goal}{mark}")
            if p.get("goal_minutes") and not p.get("goal_met") and p.get("goal_percent", 0) < 50:
                insights.append(f"{p['project']} প্রজেক্টের গোলের {p.get('goal_percent', 0)}% মাত্র পূরণ হয়েছে।")
        sections.append({"title": "প্রজেক্ট ও গোল", "text": " | ".join(lines)})
    else:
        sections.append({"title": "প্রজেক্ট ও গোল", "text": "আজ কোনো প্রজেক্টে ট্যাগ করা কাজ হয়নি।"})

    # Files
    file_text = (
        f"ফাইল: {fc.get('created', 0)}টি তৈরি, {fc.get('modified', 0)}টি পরিবর্তন, "
        f"{fc.get('deleted', 0)}টি ডিলিট, {fc.get('moved', 0)}টি সরানো হয়েছে।"
    )
    if fc.get("deleted", 0) >= 5:
        insights.append(f"আজ {fc['deleted']}টি ফাইল ডিলিট হয়েছে — ভুল করে ডিলিট হলো কি না অ্যালার্ট দেখে নিন।")
    sections.append({"title": "ফাইল", "text": file_text})

    # Browser
    browser_lines = []
    if b["searches"]:
        browser_lines.append("টপ সার্চ: " + ", ".join(f"“{x['query']}” (×{x['count']})" for x in b["searches"][:3]) + "।")
    if b["downloads"]:
        browser_lines.append("ডাউনলোড: " + ", ".join(f"{d['file']} [{d['state']}]" for d in b["downloads"][:3]) + "।")
    browser_lines.append(f"মোট ভিজিট {b['visits']}টি, ডোমেইন {b['domains_used']}টি।")
    sections.append({"title": "ব্রাউজার", "text": " ".join(browser_lines)})

    # Untagged
    untagged_seconds = sum(float(a.get("seconds", 0)) for a in s["untagged"].get("apps", []))
    if untagged_seconds:
        sections.append({"title": "আনট্যাগড", "text": f"{format_duration(untagged_seconds)} সময় কোনো প্রজেক্টে ট্যাগ হয়নি।"})
        insights.append("আনট্যাগড সময় কমাতে `python main.py --suggest-projects` চালিয়ে রুল যোগ করুন।")
    else:
        sections.append({"title": "আনট্যাগড", "text": "সব কাজই ট্যাগ করা আছে — আনট্যাগড কিছু নেই।"})

    # Data transfers (organisation)
    tr = s.get("transfers", {})
    if tr.get("total"):
        counts = tr.get("counts", {})
        parts = []
        labels = {"usb_copy": "USB-তে কপি", "usb_insert": "USB লাগানো",
                  "cloud": "ক্লাউড", "email": "ইমেইল", "message": "মেসেজ",
                  "print": "প্রিন্ট", "sensitive_copy": "গুরুত্বপূর্ণ ফোল্ডার থেকে বাইরে",
                  "large_transfer": "বড় ট্রান্সফার", "usb_remove": "USB খোলা"}
        for kind in ("usb_copy", "usb_insert", "cloud", "email", "message",
                     "print", "sensitive_copy", "large_transfer"):
            c = counts.get(kind, {})
            if c.get("count"):
                parts.append(f"{labels[kind]} {c['count']}টি")
        sections.append({"title": "ডেটা ট্রান্সফার",
                         "text": f"আজ মোট {tr['total']}টি ট্রান্সফার ইভেন্ট: " + ", ".join(parts) + "।"})
        if counts.get("sensitive_copy", {}).get("count"):
            insights.append("গুরুত্বপূর্ণ ফোল্ডার থেকে ফাইল বাইরে গেছে — অ্যালার্ট দেখে এখনই যাচাই করুন।")
        if counts.get("usb_copy", {}).get("count"):
            insights.append("USB ড্রাইভে ফাইল কপি হয়েছে — কী গেল ট্রান্সফার তালিকায় দেখে নিন।")
    else:
        sections.append({"title": "ডেটা ট্রান্সফার",
                         "text": "আজ কোনো ডেটা ট্রান্সফার ধরা পড়েনি — USB কপি, ক্লাউড আপলোড, প্রিন্ট কিছুই হয়নি।"})

    # Tomorrow
    if not active:
        suggestions = ["আজ কোনো অ্যাক্টিভিটি রেকর্ড হয়নি — এজেন্ট চালু ছিল কি না দেখে নিন।"]
    else:
        suggestions = insights if insights else ["আজকের ধারা ভালো — আগামীকাল একইভাবে চালিয়ে যান।"]
    sections.append({"title": "আগামীকালের জন্য", "text": " ".join(suggestions)})

    narrative = headline + "\n\n" + "\n\n".join(
        f"## {sec['title']}\n{sec['text']}" for sec in sections)
    return {
        "date": date, "kind": "daily", "headline": headline,
        "sections": sections, "insights": insights,
        "narrative": narrative,
        "stats": {
            "active_seconds": active, "idle_seconds": idle,
            "active_percent": _pct(active, total),
            "apps_used": s["apps_used"],
            "productive_seconds": productive, "distracting_seconds": distracting,
            "productive_percent": _pct(productive, browser_total),
            "projects_worked": len(s["projects"]),
            "goals_met": sum(1 for p in s["projects"] if p.get("goal_met")),
            "file_events": sum(fc.values()),
            "visits": b["visits"],
            "untagged_seconds": untagged_seconds,
            "active_vs_yesterday_percent": _pct(active - prev["active_seconds"], prev["active_seconds"]) if prev["active_seconds"] else 0,
        },
        "summary": s,
    }


def generate_weekly_summary(db, end: str | None = None, days: int = 7,
                            tagger=None) -> dict:
    end = end or today_str()
    rows = range_summary(db, days, end=end, tagger=tagger)
    total_active = sum(r["active_seconds"] for r in rows)
    total_idle = sum(r["idle_seconds"] for r in rows)
    total_browser = sum(r["browser_seconds"] for r in rows)
    total_prod = sum(r["productive_seconds"] for r in rows)
    total_dist = sum(r["distracting_seconds"] for r in rows)
    best = max(rows, key=lambda r: r["active_seconds"]) if rows else None
    weakest = min(rows, key=lambda r: r["active_seconds"]) if rows else None
    avg = total_active / len(rows) if rows else 0
    # Top project of the week: sum project_totals across the days.
    project_seconds: dict[str, float] = {}
    for r in rows:
        for p in db.project_totals(r["date"]):
            project_seconds[p["project"]] = project_seconds.get(p["project"], 0) + float(p["active"] or 0)
    top_project = max(project_seconds.items(), key=lambda kv: kv[1]) if project_seconds else ("", 0)
    # Trend: second half vs first half
    half = len(rows) // 2 or 1
    first = sum(r["active_seconds"] for r in rows[:half])
    second = sum(r["active_seconds"] for r in rows[half:])
    trend = _pct(second - first, first) if first else 0

    start = rows[0]["date"] if rows else end
    # Data transfers across the week (organisation).
    try:
        week_transfers = db.transfer_count_between(start, end)
        week_transfer_total = sum(c.get("count", 0) for c in week_transfers.values())
    except Exception:
        week_transfers, week_transfer_total = {}, 0
    headline = (
        f"{start} থেকে {end} — {days} দিনে মোট {format_duration(total_active)} অ্যাক্টিভ, "
        f"দিনে গড়ে {format_duration(avg)}।"
    )
    sections = [
        {"title": "মোট হিসাব",
         "text": f"অ্যাক্টিভ {format_duration(total_active)}, আইডল {format_duration(total_idle)}, "
                 f"ব্রাউজার {format_duration(total_browser)} (প্রোডাক্টিভ {format_duration(total_prod)}, "
                 f"ডিস্ট্রাক্টিং {format_duration(total_dist)}), মোট ভিজিট {sum(r['visits'] for r in rows)}টি, "
                 f"ফাইল ইভেন্ট {sum(r['file_events'] for r in rows)}টি।"},
        {"title": "সেরা ও দুর্বল দিন",
         "text": (f"সবচেয়ে অ্যাক্টিভ দিন {best['date']} ({format_duration(best['active_seconds'])})। "
                  f"সবচেয়ে কম {weakest['date']} ({format_duration(weakest['active_seconds'])})."
                  if best and weakest else "কোনো ডেটা নেই।")},
        {"title": "টপ প্রজেক্ট",
         "text": f"সপ্তাহের টপ প্রজেক্ট {top_project[0] or '—'} — {format_duration(top_project[1])}।" if top_project[0] else "কোনো প্রজেক্টে কাজ হয়নি।"},
        {"title": "ডেটা ট্রান্সফার",
         "text": (f"সপ্তাহে মোট {week_transfer_total}টি ট্রান্সফার ইভেন্ট: "
                  + ", ".join(f"{k} {c['count']}টি" for k, c in sorted(week_transfers.items()) if c["count"])
                  + "।"
                  if week_transfer_total else "সপ্তাহে কোনো ডেটা ট্রান্সফার ধরা পড়েনি।")},
        {"title": "ট্রেন্ড",
         "text": (f"সপ্তাহের দ্বিতীয় অর্ধে অ্যাক্টিভ সময় {abs(trend)}% {'বেড়েছে' if trend >= 0 else 'কমেছে'}।"
                  if trend else "সপ্তাহজুড়ে অ্যাক্টিভ সময় প্রায় একই ছিল।")},
        {"title": "আগামী সপ্তাহের জন্য",
         "text": ("ডিস্ট্রাক্টিং সময় বেশি থাকলে ব্রাউজারের সীমা কমিয়ে নিন; আনট্যাগড সময় থাকলে প্রজেক্ট রুল যোগ করুন; "
                  "গোল মিস হওয়া প্রজেক্টে পরের সপ্তাহে একটু বেশি সময় রাখুন।"
                  if total_dist > total_prod else "এই ধারা ধরে রাখুন — প্রোডাক্টিভ সময় ডিস্ট্রাক্টিংয়ের চেয়ে বেশি।")},
    ]
    narrative = headline + "\n\n" + "\n\n".join(
        f"## {sec['title']}\n{sec['text']}" for sec in sections)
    return {
        "start": start, "end": end, "days": days, "kind": "weekly",
        "headline": headline, "sections": sections, "narrative": narrative,
        "stats": {
            "total_active_seconds": total_active, "avg_active_seconds": avg,
            "total_idle_seconds": total_idle, "total_browser_seconds": total_browser,
            "productive_seconds": total_prod, "distracting_seconds": total_dist,
            "best_day": best["date"] if best else "", "weakest_day": weakest["date"] if weakest else "",
            "top_project": top_project[0], "top_project_seconds": top_project[1],
            "trend_percent": trend,
        },
        "rows": rows,
    }


def render_daily_markdown(result: dict) -> str:
    lines = [f"# DeskAudit — ডেইলি সামারি ({result['date']})", "", result["headline"], ""]
    for sec in result["sections"]:
        lines += [f"## {sec['title']}", "", sec["text"], ""]
    lines += ["---", "", "*লোকাল রুল-ভিত্তিক সামারি — কোনো AI API কি বা ইন্টারনেট লাগে না।*"]
    return "\n".join(lines) + "\n"


def render_weekly_markdown(result: dict) -> str:
    lines = [f"# DeskAudit — সাপ্তাহিক রিভিউ ({result['start']} থেকে {result['end']})", "",
             result["headline"], ""]
    for sec in result["sections"]:
        lines += [f"## {sec['title']}", "", sec["text"], ""]
    lines += ["## দিনভিত্তিক হিসাব", "", "| তারিখ | অ্যাক্টিভ | ব্রাউজার | ভিজিট | ফাইল |",
              "|---|---|---:|---:|---:|"]
    for r in result["rows"]:
        lines.append(f"| {r['date']} | {format_duration(r['active_seconds'])} | "
                     f"{format_duration(r['browser_seconds'])} | {r['visits']} | {r['file_events']} |")
    lines += ["", "*লোকাল রুল-ভিত্তিক রিভিউ — কোনো AI API কি বা ইন্টারনেট লাগে না।*"]
    return "\n".join(lines) + "\n"


def save_daily_summary(db, date: str | None = None, reports_dir: str | Path = "reports",
                       tagger=None) -> tuple[dict, Path]:
    result = generate_daily_summary(db, date, tagger=tagger)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{result['date']}-summary.md"
    path.write_text(render_daily_markdown(result), encoding="utf-8")
    return result, path


def save_weekly_summary(db, end: str | None = None, days: int = 7,
                        reports_dir: str | Path = "reports", tagger=None) -> tuple[dict, Path]:
    result = generate_weekly_summary(db, end=end, days=days, tagger=tagger)
    out = Path(reports_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{result['end']}-weekly.md"
    path.write_text(render_weekly_markdown(result), encoding="utf-8")
    return result, path
