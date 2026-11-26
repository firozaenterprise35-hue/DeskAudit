"""Tests for the dashboard PIN lock (v12.6).

The dashboard shows a plain white lock page until the owner types the
code (default 1122) and presses Enter. Every data API needs the
session cookie, so the lock protects the data, not just the page.
"""

import http.cookiejar
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deskaudit.dashboard import DashboardServer
from deskaudit.projects import Project, Tagger


def _seed(tmp: Path) -> str:
    from datetime import datetime, timedelta
    from deskaudit.database import Database
    db = Database(tmp / "test.db")
    now = datetime.now().astimezone().replace(hour=10, minute=5, second=0, microsecond=0)
    sid = db.start_app_session("notepad.exe", r"C:\Windows\notepad.exe", "notes.txt",
                               now, project="Office", tags=["work"])
    db.close_app_session(sid, now + timedelta(minutes=30), 1800, 1500, 300)
    db.close()
    return now.strftime("%Y-%m-%d")


def _req(url, method="GET", data=None, cookie=None):
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read(), r.headers.get("Set-Cookie")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Set-Cookie")


def _start(tmp, **kw):
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240)])
    server = DashboardServer(tmp / "test.db", host="127.0.0.1", port=0,
                             tagger=tagger, project_root=tmp, **kw)
    return server, server.start()


def test_lock_flow():
    tmp = Path(tempfile.mkdtemp())
    day = _seed(tmp)
    server, url = _start(tmp, lock_code="1122", lock_enabled=True)
    try:
        # Locked: the root serves a BLANK white page — no field, no
        # button, no text at all (v12.7: anonymous typing, 1122 + Enter).
        code, body, _ = _req(url)
        assert code == 200
        text = body.decode("utf-8")
        assert "<input" not in text and "<form" not in text and "<button" not in text
        assert "খুলুন" not in text and "কোড" not in text
        assert "set_telegram_enabled" not in text  # no dashboard content
        assert "keydown" in text and "/api/unlock" in text  # key capture
        # Locked: data APIs refuse without a session.
        code, body, _ = _req(url + "api/summary?date={day}")
        assert code == 401
        assert json.loads(body)["error"] == "locked"
        code, body, _ = _req(url + "api/settings")
        assert code == 401
        # Health stays open (liveness only).
        code, body, _ = _req(url + "api/health")
        assert code == 200
        # Wrong code: still locked.
        code, body, _ = _req(url + "api/unlock", method="POST", data={"code": "0000"})
        assert code == 401
        code, body, _ = _req(url + "api/summary?date={day}")
        assert code == 401
        # Right code: session cookie, then the dashboard + APIs open.
        code, body, set_cookie = _req(url + "api/unlock", method="POST", data={"code": "1122"})
        assert code == 200
        assert json.loads(body)["ok"] is True
        assert set_cookie and "deskaudit_session=" in set_cookie
        cookie = set_cookie.split(";")[0]
        code, body, _ = _req(url, cookie=cookie)
        assert code == 200
        assert "set_telegram_enabled" in body.decode("utf-8")
        code, body, _ = _req(url + "api/summary?date={day}", cookie=cookie)
        assert code == 200
        assert json.loads(body)["kpis"]["active_seconds"] == 1500
        # Lock again: the session dies, the lock page returns.
        code, body, _ = _req(url + "api/lock", method="POST", data={}, cookie=cookie)
        assert code == 200
        code, body, _ = _req(url, cookie=cookie)
        assert "set_telegram_enabled" not in body.decode("utf-8")
        code, body, _ = _req(url + "api/summary?date={day}", cookie=cookie)
        assert code == 401
    finally:
        server.stop()
    print("lock flow passed")


def test_lockout_after_failures():
    tmp = Path(tempfile.mkdtemp())
    day = _seed(tmp)
    server, url = _start(tmp, lock_code="1122", lock_enabled=True)
    try:
        for _ in range(5):
            code, _, _ = _req(url + "api/unlock", method="POST", data={"code": "0000"})
            assert code == 401
        # 6th attempt (even the right code) is locked out for 30s.
        code, body, _ = _req(url + "api/unlock", method="POST", data={"code": "1122"})
        assert code == 429
        assert "৩০ সেকেন্ড" in body.decode("utf-8")
    finally:
        server.stop()
    print("lockout after failures passed")


def test_lock_disabled():
    tmp = Path(tempfile.mkdtemp())
    day = _seed(tmp)
    server, url = _start(tmp, lock_enabled=False)
    try:
        code, body, _ = _req(url)
        assert code == 200
        assert "set_telegram_enabled" in body.decode("utf-8")
        code, body, _ = _req(url + "api/summary?date={day}")
        assert code == 200
    finally:
        server.stop()
    print("lock disabled passed")


def test_lock_settings_roundtrip():
    from deskaudit.dashboard import get_settings, save_settings
    tmp = Path(tempfile.mkdtemp())
    cfg = tmp / "config.yaml"
    cfg.write_text("dashboard_lock: true\nsecret_gate:\n  code: '1122'\n", encoding="utf-8")
    s = get_settings(cfg)
    assert s["agent"]["dashboard_lock"] is True
    saved = save_settings(cfg, {"agent": {"dashboard_lock": False}})
    assert saved["agent"]["dashboard_lock"] is False
    assert get_settings(cfg)["agent"]["dashboard_lock"] is False
    print("lock settings roundtrip passed")


def test_lock_keys_js():
    """The lock page's key capture, for real: simulate typing in Node.

    1122 + Enter -> unlock request + reload; a wrong code -> request
    but no reload; a stray letter clears what was typed. Skipped
    gracefully when Node is unavailable.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        print("lock keys JS test skipped (no node)")
        return
    from deskaudit.dashboard import STATIC_DIR
    harness = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf-8');
const script = html.split('<script>')[1].split('</script>')[0];
let listener = null;
const calls = [];
let reloaded = 0;
global.document = { addEventListener: (type, fn) => { listener = fn; } };
global.location = { reload: () => { reloaded++; } };
global.fetch = (url, opts) => {
  calls.push(JSON.parse(opts.body));
  const ok = JSON.parse(opts.body).code === '1122';
  return Promise.resolve({ json: () => Promise.resolve({ ok }) });
};
eval(script);
function key(k) { listener({ key: k, ctrlKey: false, altKey: false, metaKey: false }); }
async function run() {
  ['1', '1', '2', '2', 'Enter'].forEach(key);            // right code
  await new Promise(r => setTimeout(r, 50));
  ['1', '1', '1', '1', 'Enter'].forEach(key);            // wrong code
  await new Promise(r => setTimeout(r, 50));
  ['1', '1', 'x', '2', '2', 'Enter'].forEach(key);       // letter clears buffer
  await new Promise(r => setTimeout(r, 50));
  console.log(JSON.stringify({ calls, reloaded }));
}
run();
"""
    tmp = Path(tempfile.mkdtemp())
    js = tmp / "lock_keys.js"
    js.write_text(harness, encoding="utf-8")
    out = subprocess.run([node, str(js), str(STATIC_DIR / "lock.html")],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["calls"] == [{"code": "1122"}, {"code": "1111"}, {"code": "22"}], result
    assert result["reloaded"] == 1, result
    print("lock keys JS test passed")


if __name__ == "__main__":
    test_lock_flow()
    test_lockout_after_failures()
    test_lock_disabled()
    test_lock_settings_roundtrip()
    test_lock_keys_js()
    print("ALL DASHBOARD LOCK TESTS PASSED")
