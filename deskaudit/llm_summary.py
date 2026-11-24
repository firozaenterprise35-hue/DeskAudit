"""Optional LLM-written daily summary for DeskAudit.

The local rule-based Bengali summary (summary.py) is ALWAYS the default
and needs no key. This module is only used when the user explicitly asks
for LLM text AND provides an API key — it never runs on its own.

    python main.py --ai-summary --llm          # uses the key from config / env
    python main.py --ai-summary --llm --weekly

Providers:
  * "gemini" (default) — Google AI Studio key, free tier available.
    Get one at https://aistudio.google.com/app/apikey ("Create API key").
    Keys may start with "AIza..." or "AQ....". Model default:
    "gemini-3.5-flash-lite" (verified working 2026-11-24; gemini-2.5-flash
    is retired for new users, and gemini-3.8-flash / 3.7-flash returned
    503 high-demand errors in the same test).
  * "openai" — OpenAI key ("sk-..."), model default "gpt-4o-mini".
  * "anthropic" — Anthropic key ("sk-ant-..."), model "claude-3-5-haiku-latest".
  * "openai-compatible" — any OpenAI-compatible provider via base_url.

How to give the key (pick ONE, in this order):
  1. Environment variable DESKAUDIT_LLM_API_KEY (recommended, so the key
     is not stored in a file at all).
  2. config.local.yaml (git-ignored — never config.yaml, which is tracked):
         llm:
           enabled: true
           provider: "gemini"
           api_key: "AIza..."
           model: "gemini-3.5-flash-lite"

Privacy: only the AGGREGATED daily stats are sent to the LLM (the same
numbers the local summary already prints: active/idle time, top apps,
projects with goal %, searches, downloads, file counts). No window titles
with document names, no file paths, no URLs and no raw history are sent.
If the key is missing, --llm falls back to the local summary and says so.
If the LLM call fails (no internet, bad key, rate limit), it also falls
back to the local summary instead of failing the whole command.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from .summary import generate_daily_summary, generate_weekly_summary


def llm_configured(llm_cfg: dict | None, explicit: bool = False) -> bool:
    """True when a key is available. `explicit` is True when the user ran
    --ai-summary --llm — that flag itself is the opt-in, so enabled:true
    in config is not also required."""
    cfg = llm_cfg or {}
    if not explicit and not cfg.get("enabled"):
        return False
    return bool(cfg.get("api_key") or os.environ.get("DESKAUDIT_LLM_API_KEY"))


def _api_key(llm_cfg: dict) -> str:
    return str(llm_cfg.get("api_key") or os.environ.get("DESKAUDIT_LLM_API_KEY") or "")


def _stats_payload(result: dict) -> dict:
    """Only aggregated stats — never titles, paths or URLs.

    Daily results carry active_seconds/idle_seconds/projects; weekly
    results carry total_active_seconds/total_idle_seconds/top_project
    and per-day rows. A weekly payload built from the daily keys sent
    all zeros to the LLM, so the weekly summary described a week in
    which nothing happened — map both shapes here."""
    s = result.get("summary") or result
    stats = result.get("stats", {})
    if "total_active_seconds" in stats:
        # Weekly shape.
        rows = result.get("rows") or []
        projects = []
        if stats.get("top_project"):
            projects.append({
                "project": stats["top_project"],
                "active_minutes": round(float(stats.get("top_project_seconds", 0)) / 60),
                "goal_percent": 0,
            })
        return {
            "end": result.get("end"),
            "start": result.get("start"),
            "days": result.get("days"),
            "active_minutes": round(float(stats.get("total_active_seconds", 0)) / 60),
            "avg_active_minutes": round(float(stats.get("avg_active_seconds", 0)) / 60),
            "idle_minutes": round(float(stats.get("total_idle_seconds", 0)) / 60),
            "productive_minutes": round(float(stats.get("productive_seconds", 0)) / 60),
            "distracting_minutes": round(float(stats.get("distracting_seconds", 0)) / 60),
            "projects": projects,
            "file_events": sum(int(r.get("file_events", 0)) for r in rows),
            "visits": sum(int(r.get("visits", 0)) for r in rows),
            "days_detail": [
                {"date": r.get("date"),
                 "active_minutes": round(float(r.get("active_seconds", 0)) / 60)}
                for r in rows
            ],
        }
    projects = [
        {"project": p["project"], "active_minutes": round(p["active_seconds"] / 60),
         "goal_percent": p.get("goal_percent", 0)}
        for p in (s.get("projects") or [])
    ]
    return {
        "date": result.get("date") or result.get("end"),
        "active_minutes": round(stats.get("active_seconds", 0) / 60),
        "idle_minutes": round(stats.get("idle_seconds", 0) / 60),
        "apps_used": stats.get("apps_used"),
        "productive_minutes": round(stats.get("productive_seconds", 0) / 60),
        "distracting_minutes": round(stats.get("distracting_seconds", 0) / 60),
        "projects": projects,
        "file_events": stats.get("file_events"),
        "visits": stats.get("visits"),
    }


def _prompt(payload: dict, kind: str) -> str:
    period = "today" if kind == "daily" else "this week"
    return (
        "You are DeskAudit, a personal productivity assistant. Write a short "
        f"Bengali summary of the user's computer work {period}, based ONLY on "
        "the aggregated stats below. Do not invent apps, projects or numbers "
        "that are not in the stats. Keep it warm and concrete: what they "
        "mainly did, whether they were productive, which goals were met or "
        "missed, and one suggestion for tomorrow. Maximum 180 words.\n\n"
        f"Stats (JSON): {json.dumps(payload, ensure_ascii=False)}"
    )


def _call_openai_compatible(llm_cfg: dict, prompt: str) -> str:
    """OpenAI-compatible chat completions endpoint (works for OpenAI and
    any OpenAI-compatible provider via base_url)."""
    base_url = str(llm_cfg.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    model = str(llm_cfg.get("model") or "gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 800,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_api_key(llm_cfg)}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data["choices"][0]["message"]["content"]).strip()


def _call_gemini(llm_cfg: dict, prompt: str) -> str:
    """Google Gemini API (Google AI Studio key, starts with 'AIza...').
    POST https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent
    with an x-goog-api-key header."""
    model = str(llm_cfg.get("model") or "gemini-3.5-flash-lite")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 800},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=body,
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": _api_key(llm_cfg)},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def _call_anthropic(llm_cfg: dict, prompt: str) -> str:
    model = str(llm_cfg.get("model") or "claude-3-5-haiku-latest")
    body = json.dumps({
        "model": model, "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json",
                 "x-api-key": _api_key(llm_cfg),
                 "anthropic-version": "2023-06-01"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(b.get("text", "") for b in data.get("content", [])).strip()


def llm_narrative(result: dict, llm_cfg: dict, kind: str = "daily",
                  explicit: bool = False) -> tuple[str, str]:
    """Returns (text, source) where source is 'llm' or 'local' (fallback).
    Never raises — a failed LLM call falls back to the local narrative."""
    if not llm_configured(llm_cfg, explicit=explicit):
        return result["narrative"], "local (no LLM key — set DESKAUDIT_LLM_API_KEY or llm.api_key in config.local.yaml)"
    try:
        provider = str(llm_cfg.get("provider") or "gemini").lower()
        prompt = _prompt(_stats_payload(result), kind)
        if provider in ("gemini", "google"):
            text = _call_gemini(llm_cfg, prompt)
        elif provider == "anthropic":
            text = _call_anthropic(llm_cfg, prompt)
        else:
            text = _call_openai_compatible(llm_cfg, prompt)
        if not text:
            raise ValueError("empty LLM response")
        return text, f"llm ({provider}/{llm_cfg.get('model') or 'default'})"
    except Exception as exc:
        return result["narrative"], f"local (LLM failed, fell back: {exc})"
