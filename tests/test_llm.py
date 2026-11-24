"""LLM summary tests — no network, no real key.

The LLM path must ALWAYS fall back to the local narrative when the key
is missing or the call fails, and must never send titles/paths/URLs.
"""

import json
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.database import Database
from deskaudit.llm_summary import _stats_payload, llm_configured, llm_narrative
from deskaudit.summary import generate_daily_summary


class _FakeResponse:
    """Stands in for urllib's response object in tests (no network)."""

    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_llm_fallback_and_privacy():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone()
    sid = db.start_app_session("WINWORD.EXE", "", "Secret Salary 2026.xlsx", now, project="Office")
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1800, 0)
    result = generate_daily_summary(db, now.strftime("%Y-%m-%d"))
    # No key -> local fallback, and the local text is returned unchanged.
    assert not llm_configured({})
    assert not llm_configured({"enabled": True})
    text, source = llm_narrative(result, {}, kind="daily")
    assert text == result["narrative"]
    assert source.startswith("local")
    # Payload sent to the LLM must not contain titles, paths or URLs.
    payload = _stats_payload(result)
    blob = str(payload)
    assert "Secret Salary" not in blob
    assert "WINWORD" not in blob
    assert "http" not in blob
    assert payload["active_minutes"] == 30
    db.close()
    print("llm fallback/privacy tests passed")


def test_gemini_call_mocked():
    """Google AI Studio (Gemini) provider: correct endpoint, header and
    parsing — with the network call mocked, so no real key is needed."""
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone()
    sid = db.start_app_session("WINWORD.EXE", "", "Doc", now, project="Office")
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1800, 0)
    result = generate_daily_summary(db, now.strftime("%Y-%m-%d"))
    seen = {}

    def fake_urlopen(req, timeout=60):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"candidates": [{"content": {"parts": [
            {"text": "আজ তুমি Office প্রজেক্টে ৩০ মিনিট কাজ করেছো।"}]}}]})

    real = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        cfg = {"enabled": True, "provider": "gemini",
               "api_key": "AIzaFakeKey123", "model": "gemini-3.5-flash-lite"}
        text, source = llm_narrative(result, cfg, kind="daily")
    finally:
        urllib.request.urlopen = real
    assert text == "আজ তুমি Office প্রজেক্টে ৩০ মিনিট কাজ করেছো।", text
    assert source.startswith("llm (gemini/"), source
    assert seen["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                           "gemini-3.5-flash-lite:generateContent"), seen["url"]
    assert seen["headers"]["x-goog-api-key"] == "AIzaFakeKey123"
    assert "Secret" not in json.dumps(seen["body"], ensure_ascii=False)
    db.close()
    print("gemini mocked-call test passed")


if __name__ == "__main__":
    test_llm_fallback_and_privacy()
    test_gemini_call_mocked()
