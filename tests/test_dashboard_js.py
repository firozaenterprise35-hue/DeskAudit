"""End-to-end test of the dashboard page's JavaScript — the real thing.

Starts the DashboardServer with seeded data, fetches the page, and runs
its <script> in Node with a minimal DOM shim and real fetch() calls to
the live server. Then asserts that KPIs, charts, tables, narrative,
weekly review and Settings actually render — the class of bug that a
dead script (e.g. one stray brace) causes, which endpoint-only tests
never catch.

Requires Node (skipped gracefully without it).

Run:  python3 tests/test_dashboard_js.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deskaudit.dashboard import STATIC_DIR, DashboardServer
from deskaudit.projects import Project, Tagger
from test_dashboard import seed_db

HARNESS = r"""
const fs = require('fs');
const base = process.argv[2];       // e.g. http://127.0.0.1:12345
const htmlPath = process.argv[3];
const seedDate = process.argv[4];
const html = fs.readFileSync(htmlPath, 'utf-8');
let script = html.split('<script>')[1].split('</script>')[0];
// We drive load()/loadSettings() ourselves, so drop their auto-run.
script = script.replace(/^\s*load\(\);\s*$/m, '').replace(/^\s*loadSettings\(\);\s*$/m, '');

// --- minimal DOM shim: getElementById returns stable element objects ---
const els = {};
function makeEl(id) {
  const el = {
    id, _html: '', _text: '', value: '', checked: false,
    dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    options: [],
    addEventListener() {}, appendChild() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      this.options = [...String(v).matchAll(/<option[^>]*>(.*?)<\/option>/gs)]
        .map(m => ({ value: m[1].replace(/<[^>]+>/g, ''), text: m[1] }));
    },
  });
  Object.defineProperty(el, 'textContent', {
    get() { return this._text; },
    set(v) { this._text = String(v); },
  });
  return el;
}
const rangeBtns = ['7', '14', '30'].map(d => {
  const b = makeEl('range' + d);
  b.dataset = { days: d };
  return b;
});
globalThis.document = {
  getElementById(id) { return els[id] || (els[id] = makeEl(id)); },
  querySelectorAll(sel) { return sel === '.rangeBtn' ? rangeBtns : []; },
};
// The page uses relative URLs; point fetch at the live server.
const realFetch = fetch;
globalThis.fetch = (url, opts) => realFetch(base + url, opts);

(async () => {
  eval(script + '\n;globalThis.__t = { load, loadSettings, saveSettings, state, $ };');
  const t = globalThis.__t;
  t.state.date = seedDate;   // deterministic: the seeded day
  await t.load();
  await t.loadSettings();
  const out = [];
  const check = (name, ok, extra = '') =>
    out.push((ok ? 'PASS ' : 'FAIL ') + name + (extra ? ' — ' + extra : ''));
  const kpis = t.$('kpis').innerHTML;
  check('14 KPI cards', (kpis.match(/class="kpi"/g) || []).length === 14,
    (kpis.match(/class="kpi"/g) || []).length + ' found');
  check('hourly has 24 columns', (t.$('hourly').innerHTML.match(/class="col"/g) || []).length === 24);
  check('donut rendered', t.$('donut').innerHTML.includes('<svg'));
  check('project bars rendered', t.$('projectBars').innerHTML.includes('Office'));
  check('app bars rendered', t.$('appBars').innerHTML.includes('WINWORD'));
  check('domain bars rendered', t.$('domainBars').innerHTML.includes('coursera.org'));
  check('category bars rendered', t.$('categoryBars').innerHTML.includes('learning'));
  check('file bars rendered', t.$('fileBars').innerHTML.includes('modified'));
  check('trend rows rendered', (t.$('trendBars').innerHTML.match(/class="bar-row"/g) || []).length === 7);
  check('trend shows top project', t.$('trendBars').innerHTML.includes('Office'));
  check('project table has Office row', t.$('projectTable').innerHTML.includes('Office'));
  check('app table has WINWORD row', t.$('appTable').innerHTML.includes('WINWORD'));
  check('domain table has coursera row', t.$('domainTable').innerHTML.includes('coursera.org'));
  check('search list shows query', t.$('searchList').innerHTML.includes('python sqlite'));
  check('download list shows file', t.$('downloadList').innerHTML.includes('report.pdf'));
  check('recent files table', t.$('fileTable').innerHTML.includes('Q4 report.xlsx'));
  check('alerts section rendered', !t.$('alertList').innerHTML.includes('Loading'));
  check('narrative rendered', t.$('narrative').innerHTML.includes('মূল কাজ'));
  check('weekly rendered', t.$('weekly').innerHTML.includes('মোট হিসাব'));
  check('glance rendered', t.$('glance').innerHTML.includes('active'));
  check('transfers section rendered', t.$('transferSummary').innerHTML.includes('USB'));
  check('transfer table has USB copy', t.$('transferTable').innerHTML.includes('report.xlsx'));
  check('transfer table has cloud domain', t.$('transferTable').innerHTML.includes('drive.google.com'));
  check('transfer count badge', t.$('transferCount').textContent.includes('2'));
  check('transfers export link set', t.$('expTransfers').href.includes('format=transfers'));
  check('transfers settings loaded', String(t.$('set_transfers_poll_seconds').value) === '60');
  check('settings loaded (model filled)', t.$('set_llm_model').value.length > 0);
  check('settings loaded (poll interval)', String(t.$('set_poll_interval_seconds').value) === '5');
  check('export links set',
    t.$('expMd').href.includes('/api/export?date=') && t.$('expCsv').href.includes('format=csv'));
  await t.saveSettings();
  check('settings save reports Saved', t.$('setStatus').textContent.includes('Saved'));
  console.log(out.join('\n'));
  if (out.some(l => l.startsWith('FAIL'))) process.exit(1);
  console.log('DASHBOARD JS E2E PASS');
})();
"""


def test_dashboard_js_e2e():
    node = shutil.which("node")
    if not node:
        print("dashboard JS e2e skipped (node not installed)")
        return
    tmp = Path(tempfile.mkdtemp())
    db, date = seed_db(tmp)
    db.close()
    (tmp / "config.yaml").write_text(
        "poll_interval_seconds: 5\nllm:\n  provider: gemini\n"
        "  model: gemini-3.5-flash-lite\n  api_key: ''\n",
        encoding="utf-8")
    tagger = Tagger(projects=[Project(name="Office", daily_goal_minutes=240),
                              Project(name="Study", daily_goal_minutes=120)])
    server = DashboardServer(tmp / "test.db", host="127.0.0.1", port=0,
                             tagger=tagger, project_root=tmp,
                             config_path=tmp / "config.yaml")
    url = server.start()
    try:
        harness = tmp / "harness.js"
        harness.write_text(HARNESS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), url.rstrip("/"), str(STATIC_DIR / "index.html"), date],
            capture_output=True, text=True, timeout=60)
        print(proc.stdout)
        if proc.returncode != 0:
            print(proc.stderr)
            raise AssertionError("dashboard JS e2e harness failed")
        assert "DASHBOARD JS E2E PASS" in proc.stdout
    finally:
        server.stop()
    print("dashboard JS e2e test passed")


if __name__ == "__main__":
    test_dashboard_js_e2e()
