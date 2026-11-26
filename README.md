# DeskAudit — Windows Desktop Audit Agent (MVP)

DeskAudit is a local-first Python agent that quietly audits what happens on a
Windows desktop: which apps you use, which window is in front, which files
are created / edited / renamed / deleted, and when you are idle.

It does **not** log keystrokes, passwords, clipboard contents, or the text
inside your files. It logs metadata only: app name, window title, file path,
event type, and timestamps.

## What the MVP does

- **App / window tracking** — polls the foreground window every few seconds
  (`psutil` + `pywin32`) and stores one session per app + window title.
- **File watching** — watches Desktop and Documents with `watchdog` and
  records created / modified / deleted / moved events.
- **Idle detection** — uses Windows `GetLastInputInfo`. After 5 minutes
  (configurable) with no input, time is counted as idle, not active work.
- **Browser tracking (full)** — see below.
- **Project tagging (full)** — see below.
- **Web dashboard (full)** — see below. Stdlib only, no extra install.
- **Alerts (full)** — see below.
- **Daily AI summary + weekly review (full)** — see below. Local Bengali narrative, no API key.
- **Local storage** — everything goes to SQLite at `data/deskaudit.db`.
  No cloud upload, no account, no telemetry.
- **Daily report** — `python main.py --report` prints a summary and writes
  `reports/YYYY-MM-DD.md`, `reports/YYYY-MM-DD.csv`,
  `reports/YYYY-MM-DD-browser.csv`, `reports/YYYY-MM-DD-projects.csv`
  and `reports/YYYY-MM-DD-transfers.csv`.

## Browser tracking — everything it covers

No extension is needed for the default layer. While the agent runs it
polls browser history every `browser_poll_seconds` (default 60s).

| Feature | How it works | Where it lands |
|---|---|---|
| Browsers | Chrome, Edge, Brave, Opera, Vivaldi (Chromium) + Firefox | auto-discovered profiles |
| History / visits | Reads `History` / `places.sqlite` — copied first, because the browser locks the original | `browser_visits` |
| Active tab + time per site | Foreground window title + history match, per browser + domain + title session, active vs idle | `browser_sessions` |
| Exact active-tab URL (optional) | 1) Extension in `extension/chrome/` → localhost receiver on 127.0.0.1:8765, or 2) CDP: launch Chrome with `--remote-debugging-port=9222` | same session, with URL |
| Domain + category | 60+ built-in domains (coding, work, email, learning, search, social, video, shopping, news) + your `productive_domains` / `distracting_domains` in `config.yaml` | `category`, `is_productive` |
| Time per domain | Productive vs distracting time, domains used, top domains | daily report |
| Search queries | Extracted from the URL: Google/Bing/DuckDuckGo `q=`, Yahoo `p=`, YouTube `search_query=`, Brave/Ecosia `q=`, Baidu `wd`, Yandex `text` | `browser_searches` |
| Downloads | Chromium `downloads` table: file name, path, size, state (complete / cancelled / interrupted). Firefox downloads are not tracked (Firefox does not keep them in places.sqlite in a stable form). | `browser_downloads` |
| Tab switches | Counted when the foreground browser title changes to another tab | `browser_sessions.tab_switches` |
| Bookmarks | Chromium `Bookmarks` JSON + Firefox `moz_bookmarks` (reader functions, used by `--browser-audit` tooling) | `browser_history.read_*_bookmarks()` |
| Dedup | `UNIQUE(browser, url, visited_at)` + a per-profile last-seen watermark in `kv_state`, so re-running never double-counts | — |
| Exclusions | `exclude_domains` in `config.yaml` is never logged; sensitive URL params (`token`, `password`, `api_key`, `code`, …) are stripped by `redact_url()` | — |

What is **not** tracked: incognito / private history (browsers never write
it), passwords, cookies, form data, saved logins, clipboard, keystrokes,
or page content.

One-off import of everything already in history:

```bat
python main.py --browser-audit
python main.py --report
```

## Project tagging — everything it covers

Every app session, file event, browser visit / session / download / search
gets one primary project + optional secondary tags, stored in the `project`
and `tags` columns of every table. Projects are defined in `projects.yaml`
(shipped with Office, Study, DeskAudit, Personal, Freelance examples).

| Feature | How it works |
|---|---|
| Rule types | `folders` (path prefix), `file_patterns` (fnmatch, e.g. `*report*`), `file_extensions` (`.xlsx`, `.py`), `apps` (exe name), `window_keywords`, `window_regex`, `domains`, `url_contains`, `url_regex`, `search_keywords` |
| Priority | Highest `priority` wins for the primary project; all other matching projects add their name + `tags` as secondary tags |
| Manual override | Exact match, always wins: `overrides` in `projects.yaml`, or `python main.py --project-tag domain github.com --project Study` (saved in DB, survives restart) |
| Parent / subproject | `parent: "Office"` on a project, e.g. `Office / Client A` — report still shows the child, parent is kept for dashboards |
| Daily goal | `daily_goal_minutes` per project — report shows `goal 4h, 56%` and a ✓ when met |
| Color | `color: "#2563eb"` per project, for future dashboards |
| Untagged | Nothing matches → `default_project` (`Untagged`). Report lists untagged time + top untagged apps / domains / titles |
| Suggestions | `python main.py --suggest-projects` turns the top untagged items into copy-paste YAML rules |
| Retag old data | Edit `projects.yaml`, then `python main.py --retag` (or `--retag --date 2026-11-23`) — recomputes every stored row, no data loss |
| Add a project | `python main.py --projects` to list, `python main.py --project-add "Client A" --folder "%USERPROFILE%/Documents/ClientA" --goal 120` to append |
| Report | Per-project active time (app + browser split), file events, visits, goal % — in text, Markdown, and `reports/YYYY-MM-DD-projects.csv` |

Project time is never double-counted: browser time is a subset of app
time, so a project's active time = app time + browser time − the part
where they overlap in the same project (measured session by session).
Browser time whose app session was tagged to a different project still
counts, because it is genuinely extra time for that project.

Example `projects.yaml` rule:

```yaml
- name: "Office"
  daily_goal_minutes: 240
  priority: 100
  folders: ["%USERPROFILE%/Documents/Office"]
  apps: ["WINWORD.EXE", "EXCEL.EXE"]
  domains: ["teams.microsoft.com"]
  window_keywords: ["Meeting"]
  tags: ["work"]
```

## Web dashboard — everything it covers

Start it, then it opens in your browser:

```bat
python main.py --dashboard
REM or double-click dashboard.bat
REM http://127.0.0.1:8000/  (localhost only, Ctrl+C to stop)
```

No extra package is needed — the server is Python's stdlib
`http.server`, and the page uses vanilla JS + CSS, no CDN, so it works
offline. All data comes from `data/deskaudit.db` on this computer.

| Section | What you see |
|---|---|
| Header | Date picker, Prev / Next day, Today, trend range 7 / 14 / 30 days, Refresh, Auto-refresh 30s, Export (Markdown, Apps CSV, Browser CSV, Projects CSV) |
| KPI cards (14) | Active, Idle, Apps used, Browser time, Productive, Distracting, Projects worked, File events, Visits, Searches, Downloads, Untagged time — each with its sub-note (%, goals met, …) |
| Today at a glance | One sentence: active / idle, apps, browser productive vs distracting, projects worked |
| Hourly activity | Bar chart, 0–23h, active minutes per hour (browser is a subset, never added on top; hover shows browser minutes) |
| Productive vs distracting | Donut chart with % + legend |
| Projects | Bar + goal progress per project: active, app / browser split, files, visits, goal %, ✓ when met |
| Top apps | Horizontal bars, top 10 by active time |
| Top domains | Horizontal bars, top 10 by active time / visits |
| Browser categories | Bars by category (coding, work, social, video, …) |
| File events by type | Bars: created / modified / deleted / moved |
| Trend | Active minutes per day for the last 7 / 14 / 30 days |
| Projects table | Project, Active, App, Browser, Files, Visits, Goal, Progress — with project dropdown filter + text search |
| App sessions table | App, window title, project, active, sessions — with text search |
| Browser tables | Top domains, top searches, downloads |
| Recent file events | Last 20: when, event, path, project |
| Untagged | Top untagged apps / domains / titles, with a hint to add a rule + `--retag` |
| Alerts | Today's alerts with severity badges (🔴 critical / 🟡 warning / 🔵 info) |
| Daily summary | Bengali narrative for the day, all 8 sections (incl. Data transfers) |
| Weekly review | 7-day review: totals, best / weakest day, top project, trend |
| ⚙️ Settings | AI (LLM) provider / model / API key, agent, secret gate, alerts, backup and Data Transfer settings — saved to `config.local.yaml` (`config.yaml` is never changed, the key is never shown back). Restart the agent and dashboard to apply. |

API (JSON, local only): `GET /api/health`, `/api/summary?date=YYYY-MM-DD`,
`/api/range?days=7&end=YYYY-MM-DD`, `/api/alerts?date=YYYY-MM-DD`,
`/api/narrative?date=YYYY-MM-DD`, `/api/weekly?end=YYYY-MM-DD&days=7`,
`/api/export?date=YYYY-MM-DD&format=md|csv|browser|projects|transfers`,
`/api/transfers?date=YYYY-MM-DD`, `GET /api/settings`, `POST /api/settings`.

Options: `python main.py --dashboard --port 8001 --host 127.0.0.1 --no-open`.
Do not use `--host 0.0.0.0` on an untrusted network — the page shows
window titles and URLs.

## Alerts — everything it covers

Fully local rule engine, no internet, no API key. The agent re-checks
every 30 minutes while it runs; you can also check manually.

```bat
python main.py --alerts            # check today, save new ones, print them
python main.py --alerts --date 2026-11-23
python main.py --alerts --notify   # also try a Windows notification
python main.py --alerts-list       # show stored alerts
python main.py --alerts-list --unread
```

| Rule | Severity | When it fires |
|---|---|---|
| `deletion_burst` | 🔴 critical | 5+ files deleted within 10 minutes (defaults in `config.yaml`) |
| `deletion_folder_burst` | 🟡 warning | 3+ deletions from one folder in a day |
| `unknown_app_night` | 🔴 critical | An app never seen before THAT session, running 00:00–05:00. An app already used earlier (even the same day) does not count as unknown at night. |
| `new_app` | 🔵 info | An app seen for the very first time today, only if it is not a well-known safe app (Chrome, Word, … in `config.yaml`) and it was used at least 5 minutes today. Short launches do not alert. |
| `distracting_over` | 🟡 warning | Distracting browser time over 60 min/day |
| `project_goal_met` | 🔵 info | A project reached its daily goal |
| `project_goal_missed` | 🟡 warning | A project reached < 50% of its goal (past days only — today isn't over yet) |
| `untagged_spike` | 🟡 warning | Untagged time over 60 min AND over 50% of active time |
| `idle_over` | 🔵 info | Idle time over 120 min/day |
| `large_download` | 🟡 warning | A single download over 500 MB |

All thresholds live under `alerts:` in `config.yaml`. Alerts are stored
in the `alerts` table, deduped on (date, type, title), so re-running
never duplicates them — only NEW alerts are notified. Notification tries
`plyer` if installed, otherwise a PowerShell toast; on Linux it simply
skips notification (alerts are still saved and shown on the dashboard).
The dashboard has an Alerts card (`/api/alerts?date=...`) with severity badges.

Data-transfer alerts (thresholds under `transfers:` in `config.yaml`):

| Rule | Severity | When it fires |
|---|---|---|
| `usb_insert` | 🟡 warning | A USB drive was connected |
| `usb_file_copy` | 🟡 warning | Files were copied onto a USB drive (count + total size) |
| `cloud_upload` | 🟡 warning | Cloud-drive use over 15 min/day (Drive / Dropbox / WeTransfer …) |
| `communication_use` | 🔵 info | Email / messaging channel use over 30 min/day |
| `print_job` | 🔵 info | A document was printed (document name, printer) |
| `sensitive_copy` | 🔴 critical | A file was copied OUT of a sensitive folder |
| `large_transfer` | 🟡 warning | A single moved file over 100 MB |

## Data Transfer Monitor — organisation edition (v11)

For an office: every staff PC runs its own DeskAudit agent, and each PC
has its own dashboard at `http://127.0.0.1:8000/` (per-PC — there is no
central server). The agent watches for data LEAVING the PC and logs every
event with file name, size, destination, time and app:

```bat
python main.py --transfers            # scan now + list today's transfers
python main.py --transfers --date 2026-11-23
```

| Channel | What is detected |
|---|---|
| **USB / pen drive** | Drive inserted, files copied onto it (snapshot diff), drive removed. Pen drives and memory cards — external USB hard drives that Windows reports as fixed drives are not detected. |
| **Cloud upload** | Sessions on drive.google.com, OneDrive, Dropbox, Box, MEGA, MediaFire, WeTransfer, SendSpace, file.io, pCloud, iCloud, … (a session under 60 s is not logged) |
| **Email** | Gmail, Outlook, Yahoo mail, Proton, Zoho (channel used + duration) |
| **Messaging** | WhatsApp Web, Telegram Web, Messenger, Teams, Slack, Discord, Skype |
| **Printing** | Print jobs seen in the Windows spooler (polled every 60 s — a job that prints and finishes between two polls is missed): document name, pages, printer |
| **Sensitive folders** | A file moved/copied OUT of a folder you mark in `transfers.sensitive_folders` — e.g. `%USERPROFILE%/Documents/CompanyData` |
| **Large transfers** | Any single file move or copy over `large_transfer_mb` (default 100 MB) |

Everything is shown on the dashboard (🔒 ডেটা ট্রান্সফার section + two
KPI cards + `/api/transfers?date=...`), in the daily report
(`reports/YYYY-MM-DD-transfers.csv` and a Data transfers section in the
`.md`), and in the daily Bengali summary (ডেটা ট্রান্সফার section).

**What is NOT recorded:** the content of what anyone typed or sent.
No keystroke logging, no message / email text, no passwords, no
clipboard content. A 12-minute WhatsApp Web session is logged as
"message channel used for 12 minutes" — never as what was written.
DeskAudit is metadata-only, by design, from day one.

**Making transfers harder, not just visible:** run
`usb_write_protect.bat` (as administrator) to block WRITING to USB
drives on that PC — reading still works, and the agent still logs every
USB insert. A copy that the write-block stops leaves no file event,
so a *blocked* attempt itself is not logged. Run `usb_write_allow.bat`
to undo it.
This does not block phones (MTP) or cloud uploads — those are caught
and logged by the monitor instead. Staff should be told that office
computers are monitored.

Configure in `config.yaml` under `transfers:` (or from the dashboard
Settings page — saved to `config.local.yaml`): `enabled`, `poll_seconds`
(how often the agent scans, default 60 s), `sensitive_folders`,
`cloud_min_seconds`, `communication_min_seconds`, `cloud_alert_minutes`,
`communication_alert_minutes`, `large_transfer_mb`, `usb_alert`,
`print_alert`, `sensitive_copy_alert`.

## Telegram bot + hourly email (v12, full data in v12.1, remote control in v12.2, screenshots + screen + power in v12.3, audit fixes in v12.4, new-program alerts in v12.5, dashboard PIN lock in v12.6)

The agent can report to you while you are away — in Bengali, on your
phone and in your inbox. Both are off until you configure them in
Dashboard Settings (saved to `config.local.yaml` only, never git).

**Telegram bot** — the bot tells you, with the same detail as the
dashboard (v12.1; `/status`, `/today` and the hourly digest all use the
shared report summary, so Telegram and the dashboard can never drift):
- **/status** (message the bot): what is open RIGHT NOW — every visible
  window grouped by app, the foreground app; today's totals (active,
  idle, first/last work time, apps used); projects with goal progress
  (e.g. Office 1ঘ 20মি / লক্ষ্য 4ঘ, 33%) and untagged time; top apps
  with window titles; browser time split productive vs distracting,
  top domains, search queries and downloads with sizes; recent files
  with times; data transfers with file names, destinations and sizes;
  today's alerts. Long reports are split into several messages.
- **Scheduled digest** every `digest_minutes` (default 60): apps and
  projects used in the last hour, latest window, files with times,
  searches, downloads and transfer rows.
- **Critical alerts immediately** (e.g. a file copied out of a
  sensitive folder) — warnings and info ride in the digest.
- **/today** — the full-day summary (same sections as /status, without
  the live "open now" part). **/help** — the command list.
  Only your chat id may command the bot; other chats are ignored.
  When the day has no recorded work yet, /status says so plainly and
  reminds you to check that the agent is running.

**Remote control (v12.2)** — the same bot also controls the agent.
Only the owner's chat id may use these; others are ignored. The agent
must be running on the PC (it polls Telegram every ~30 s).
- Read-only: **/week** (7-day summary), **/apps**, **/projects**,
  **/transfers**, **/alerts**, **/files**, **/search <word>**,
  **/report** (today's report sent as a file), **/menu** (inline
  buttons for the safe commands — nothing destructive is one tap away).
- Pause / silence: **/pause** (tracking stops; the bot still answers,
  so /resume wakes it), **/resume**, **/mute [minutes]** / **/unmute**
  (silence alerts + digests), **/digest <minutes|off>** (change the
  digest interval, saved to config.local.yaml).
- PC control: **/lock** (lock Windows), **/close <app>**, **/freeze
  <app>** / **/unfreeze** (suspend / resume a running app, Windows
  only). DeskAudit itself and Windows system processes can never be
  closed or frozen.
- Data: **/clear [date]** (delete one day), **/reset** (delete all
  recorded data — a backup is taken first; projects and settings
  stay), **/backup** (back up now), **/goal <project> <minutes>**
  (daily project goal, saved to projects.yaml + DB).
- Safety: /close, /freeze, /clear and /reset always ask “নিশ্চিত?”
  first and only run after you reply হ্যাঁ/yes or tap ✅ (the
  confirmation expires after 5 minutes; reply না to cancel).

**Screenshots, browser control, screen blank, power (v12.3)** — the
admin controls. You are the admin/controller of your company's PCs,
so these run from your Telegram chat only.
- **/screenshot** (or /shot): captures the PC's whole screen right
  now and sends it as a Telegram photo. On demand only — never
  automatic — and the file is deleted after sending. Note: a
  screenshot shows screen content (typed text, messages), unlike the
  usual metadata-only reports; tell your staff the PCs are monitored.
- **/browser**: which sites are open now — the live tab (browser,
  domain, title, URL, start time), today's domains with productive /
  distracting marks, and the latest visits. **/browser close** closes
  every browser (Chrome, Edge, Brave, Firefox, Opera, Vivaldi) after
  confirmation. **/browser block <domain>** blocks a site for the
  whole PC via the Windows hosts file (needs DeskAudit to run as
  admin); **/browser unblock <domain>** lifts it.
- **/screen white** / **/screen black**: covers every monitor with a
  solid white or black fullscreen window (always on top, no close
  box) until **/screen off** lifts it. If the agent stops, the
  overlay dies with it.
- **/shutdown** / **/restart**: the PC powers off / restarts 60
  seconds after you confirm (so work can be saved); **/abort**
  cancels it while the countdown runs. All three need confirmation
  first, like the other destructive commands.

**Audit fixes (v12.4)** — Firoza reported the v12.3 controls were
cross-firing and the screen never turned white/black. Two real bugs
were found by reading the code:
- The overlay set `WNDCLASS.hbrBackground` to a raw colour number
  (0xFFFFFF / 0x000000). That field must hold a BRUSH handle — a
  colour number is an invalid handle (and 0 is NULL, "do not
  erase"), so Windows never painted the window and it stayed
  invisible. Fixed with a real `CreateSolidBrush`, one window class
  per colour, and an explicit fill on WM_ERASEBKGND / WM_PAINT.
- Confirmations were not tied to their action: every ✅ button said
  plain `confirm:yes` and ran whatever happened to be pending when
  tapped, a stale pending survived a new command, and a casual
  "ok" counted as yes. Fixed: each confirmation carries a one-time
  token (old buttons are refused), a new command or menu tap drops
  a stale pending, and only an explicit yes ("হ্যাঁ"/"yes"/"confirm")
  confirms.

**New-program alerts + silent launches (v12.5)** — the owner asked
for an instant Telegram message whenever a new program is opened,
and for the PowerShell window that flashed every so often to stay
silent.
- **Instant new-program alerts**: the agent snapshots the running
  processes once at startup; any program opened after that triggers
  an immediate Telegram message — which program, its exe path, its
  open window title, when it opened, and its project tag. System
  and background processes never alert, the same program is not
  re-reported within `cooldown_minutes` (default 10), and at most
  `max_per_tick` messages go out per tick. Config in `app_watch:`
  and Dashboard Settings → নতুন প্রোগ্রাম অ্যালার্ট.
- **Silent launches**: every child process DeskAudit starts
  (PowerShell alert toasts, ipconfig /flushdns, shutdown, tasklist /
  taskkill, schtasks) now runs through `utils.run_hidden()` —
  CREATE_NO_WINDOW + a hidden STARTUPINFO — so no console window
  ever flashes. The toast also gets `-WindowStyle Hidden`.
- **What is NOT recorded — a deliberate boundary**: DeskAudit never
  logs keystrokes and never records what anyone types or writes —
  no message or email text, no passwords, no clipboard. The
  new-program alert says WHICH program and WHICH window/chat is open
  (the title can show e.g. "WhatsApp — Rina"), never the words typed
  there. To see what is on screen right now, the owner can use
  /screenshot on demand (v12.3, only to her chat).

**Dashboard PIN lock (v12.6)** — the owner asked that the dashboard
stay white/blank until she types 1122 and presses Enter.
- Opening http://127.0.0.1:8000/ now shows a plain **white lock
  page** (PIN field + "খুলুন" button). Typing the code (the
  secret-gate code, default 1122) and pressing Enter unlocks the
  dashboard for 8 hours.
- The lock protects the **data, not just the page**: every data API
  (/api/summary, /api/range, /api/alerts, /api/transfers,
  /api/narrative, /api/weekly, /api/export, /api/settings,
  /api/telegram-test, /api/email-test) answers 401 while locked.
  /api/health stays open (liveness only).
- Unlocking mints a random session token (HttpOnly cookie, never the
  code itself); sessions die when the agent restarts. A "🔒 লক করুন"
  button in the dashboard header locks again immediately. 5 wrong
  codes in 5 minutes lock unlocking out for 30 seconds.
- Config: `dashboard_lock: true` (top-level, Dashboard Settings →
  Agent → "ড্যাশবোর্ড খুলতে 1122 কোড চাইবে"); changing
  secret_gate.code changes the dashboard PIN too, since it is the
  same code.

**Installer fix (v12.6.1)** — upgrading while the old agent was
still running failed with "Error opening file for writing:
...DeskAudit.exe", because the installer's one stop attempt in
.onInit could miss an agent that started afterwards. The installer
now stops the agent again right before copying files and *verifies*
the exe is really free (up to 3 tries); if it is still locked it
asks to stop DeskAudit first and Retry, instead of failing mid-way.
The uninstaller got the same check. No code change in the agent
itself — only the installer script and the version number.

Setup: in Telegram, talk to **@BotFather** → `/newbot` → copy the token.
Send `/start` to your new bot, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and read
`chat.id` (or ask **@userinfobot** for your id). Paste both in Dashboard
Settings → Telegram, press "📩 Telegram পরীক্ষা বার্তা পাঠান", restart
the agent. CLI: `DeskAudit.exe --telegram-test` / `--telegram-now`.

**Hourly email** — one digest every `interval_minutes` (default 60):
apps used, file work, transfers, alerts — plain text + a small HTML
table. Gmail route: turn on 2-Step Verification, then create an App
Password for DeskAudit (Google Account → Security → App passwords) and
paste the address + 16-character app password in Dashboard Settings →
Email, press "✉️ পরীক্ষা ইমেইল পাঠান", restart the agent.
CLI: `DeskAudit.exe --email-test` / `--email-now`.

**What is NOT in these messages:** the content of what anyone typed
or sent. The bot can say "Word — রিপোর্ট.docx - Word" (which document
is open) and "12 minutes on WhatsApp Web" — never the text that was
typed or written. No keystroke logging, no message/email text, no
passwords, no clipboard. Same metadata-only rule as the rest of
DeskAudit. If the bot or the mail server is unreachable, the agent
logs a warning and keeps running — notifications never crash it.

## Daily AI summary + weekly review — everything it covers

Local, rule-based Bengali narrative — no LLM, no API key, no internet.
It is computed from the same `build_summary()` data as the reports.

```bat
python main.py --ai-summary                 # today -> reports/YYYY-MM-DD-summary.md
python main.py --ai-summary --date 2026-11-23
python main.py --ai-summary --weekly         # 7-day review -> reports/YYYY-MM-DD-weekly.md
```

Daily summary sections (none skipped when data exists): headline, মূল কাজ
(top project / app / domain), প্রোডাক্টিভিটি (productive vs distracting +
yesterday comparison), প্রজেক্ট ও গোল (goal % and ✓), ফাইল (created /
modified / deleted / moved), ব্রাউজার (top searches, downloads, visits),
আনট্যাগড, আগামীকালের জন্য (insights: distracting > productive, one app >
50% of the day, goal < 50%, 5+ deletions, untagged time).

Weekly review sections: মোট হিসাব (total / average active, idle, browser,
visits, files), সেরা ও দুর্বল দিন, টপ প্রজেক্ট, ডেটা ট্রান্সফার (weekly
transfer counts), ট্রেন্ড (second half vs first half), আগামী সপ্তাহের
জন্য — plus a day-by-day table.

Both appear on the dashboard too (Daily Summary and Weekly Review
sections, `/api/narrative?date=...` and `/api/weekly?end=...&days=7`).
An optional LLM-written version exists too — see "LLM-written summary
(optional)" below (Gemini, OpenAI or Anthropic, needs a key you
provide). The local version stays the default.

## Folder structure

```text
DeskAudit/
├── main.py                  # Entry point: run the agent or make a report
├── config.yaml              # Poll interval, idle threshold, watched folders
├── projects.yaml            # Your projects + rules + overrides (edit this)
├── requirements.txt
├── run.bat                  # Double-click to start on Windows (from source)
├── dashboard.bat            # Double-click to open the dashboard (from source)
├── schedule_summary.bat     # Double-click to schedule the 9pm job (also shipped in the exe folder)
├── unschedule_summary.bat   # Remove the 9pm job (also shipped in the exe folder)
├── usb_write_protect.bat    # Block writing to USB drives (admin; also shipped in the exe folder)
├── usb_write_allow.bat      # Undo the USB write block (also shipped in the exe folder)
├── build_exe.bat            # Build dist\DeskAudit\DeskAudit.exe (PyInstaller, Windows)
├── DeskAudit.spec           # PyInstaller spec (copies configs + the 4 .bat files into dist/)
├── deskaudit/
│   ├── __init__.py
│   ├── config.py            # Loads config.yaml, with safe defaults
│   ├── database.py          # SQLite schema + insert / query helpers
│   ├── app_tracker.py       # Foreground window + process tracking
│   ├── file_watcher.py      # watchdog handler for file events
│   ├── idle.py              # GetLastInputInfo wrapper
│   ├── browser_utils.py     # Domain, category, search, URL redaction, time conversion
│   ├── browser_history.py   # Chromium + Firefox history / download / bookmark readers
│   ├── browser_tracker.py   # Active tab, history polling, CDP, search, downloads
│   ├── browser_receiver.py  # Localhost receiver for the browser extension
│   ├── projects.py          # Project rule engine: Tagger, Project, Override
│   ├── alerts.py            # Alerts rule engine + Windows notification
│   ├── summary.py           # Daily Bengali summary + weekly review
│   ├── llm_summary.py       # Optional LLM-written summary (Gemini / OpenAI / Anthropic)
│   ├── backup.py            # SQLite online backup / restore / pruning
│   ├── secret_gate.py       # 4 desktop clicks + code opens the dashboard
│   ├── transfers.py         # Data Transfer Monitor: USB / cloud / email / print / sensitive folders
│   ├── telegram.py          # Telegram bot: status / digest / critical alerts, /status /today commands
│   ├── app_watch.py         # Real-time new-program watch: baseline + new process alerts (v12.5)
│   ├── screenshot.py        # Screen capture (ctypes BitBlt + stdlib PNG encoder, Windows only)
│   ├── screen_blank.py      # Fullscreen white/black overlay window (Windows only)
│   ├── emailer.py           # Hourly email digest via SMTP (plain text + HTML)
│   ├── dashboard.py         # Web dashboard: data API + stdlib HTTP server
│   ├── dashboard_static/
│   │   └── index.html       # Dashboard page: cards, charts, tables, filters
│   ├── report.py            # Daily summary: Markdown + CSV (incl. browser + projects + transfers)
│   └── utils.py             # Duration formatting, path helpers
├── installer/
│   ├── DeskAudit.nsi        # NSIS installer (built on Linux with makensis)
│   ├── DeskAudit.iss        # Inno Setup installer (built on Windows)
│   ├── build_installer.bat  # Build Setup.exe with Inno Setup 6 (Windows)
│   └── deskaudit.ico        # App icon (monitor + magnifier + checkmark)
├── .github/workflows/
│   └── build-windows.yml    # GitHub Actions build (exe + Setup.exe on every push)
├── extension/chrome/        # Optional Tab Reporter extension (Manifest V3)
├── data/                    # deskaudit.db + deskaudit.log (created on run)
├── reports/                 # Daily reports (created on run)
├── backups/                 # Database backups (created on run)
└── tests/
    ├── test_smoke.py        # Import + DB + report smoke test, no Windows needed
    ├── test_browser.py      # Synthetic Chrome + Firefox DB tests
    ├── test_projects.py     # Rule engine + retag + project report tests
    ├── test_dashboard.py    # Dashboard summary + live HTTP server tests
    ├── test_dashboard_js.py # Real JS e2e in Node against the live server
    ├── test_alerts.py       # Every alert rule + dedup + save/list tests
    ├── test_summary.py      # Daily + weekly Bengali summary tests
    ├── test_config.py       # Config load / coercion / exclusion tests
    ├── test_backup.py       # Backup / restore / pruning tests
    ├── test_llm.py          # LLM payload + provider call tests (mocked network)
    ├── test_secret_gate.py  # Secret gate state machine tests
    ├── test_transfers.py    # Data Transfer Monitor tests
    ├── test_audit_v11_1.py  # v11.1 re-audit regression tests (19 tests)
    ├── test_telegram.py     # Telegram bot + hourly email tests (mocked network)
    ├── test_telegram_commands.py  # Telegram remote control tests (v12.2, mocked network)
    ├── test_telegram_control.py   # Screenshot/browser/screen/power commands (v12.3)
    ├── test_screenshot.py   # PNG encoder + screen overlay + hosts block tests (v12.3)
    ├── test_app_watch.py    # New-program watch + instant alerts (v12.5)
    ├── test_silent.py       # Silent child-process launches, no console flash (v12.5)
    ├── test_dashboard_lock.py  # Dashboard PIN lock (v12.6)
    └── test_windowed.py     # Windowed-exe (no console) checks
```

## Quick start (Windows)

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.yaml config.local.yaml
REM Edit config.local.yaml if you want different folders, then:
run.bat
```

Or from a terminal:

```bat
python main.py
```

Stop with `Ctrl+C`. The current app session is closed cleanly on exit.

Pause tracking any time by creating an empty file named `PAUSE` in the
project folder, and delete it to resume. Useful for private work.

## Windows .exe & installer (no Python needed)

**Easiest:** download `DeskAudit-Setup-12.6.1.exe` (Google Drive link from
Muse) and double-click it. It installs per-user to
`%LOCALAPPDATA%\DeskAudit` (no admin needed), adds Start Menu shortcuts
(DeskAudit, DeskAudit Dashboard, Schedule 9pm daily job, Stop DeskAudit),
and can start the agent with Windows. Uninstall from Windows Settings like any other app.
The exe and the installer carry the DeskAudit icon (monitor + magnifier
+ checkmark, `installer/deskaudit.ico`).
Upgrading: the installer stops a running agent first, so just run the
new Setup.exe over the old install. Uninstall keeps your data: `data/`
(the database), `reports/`, `backups/` and `config.local.yaml` (with the
Gemini key) stay in the install folder — delete them yourself if you
really want them gone.

**Installed app = same commands:** every `python main.py --x` command in
this README also works as `DeskAudit.exe --x` from the install folder —
`--report`, `--ai-summary`, `--alerts`, `--transfers`, `--retag`,
`--suggest-projects`, `--backup`, `--restore`, `--schedule-summary`.
One-shot output is written to `data/deskaudit-stdout.log` (the windowed
exe has no console window to print into); the reports themselves land in
`reports/` and the dashboard as usual.

**Background mode — no console window, ever:** the exe is built
windowed (`console=False`), so Windows never creates a CMD/PowerShell
window for it — not even the brief flash a console exe shows before
Python can hide it. The agent runs invisibly in the background; check
Task Manager for `DeskAudit.exe` to see it is running. Stop it from
the Start Menu ("Stop DeskAudit") or with `DeskAudit.exe --stop`.
One-shot commands (`--report`, `--ai-summary`, ...) still work from
the Python source (`python main.py --ai-summary`); the frozen exe
writes its output to the log file and the dashboard instead.

**Secret gate — open the dashboard without any window:** click 4 times
on any empty spot of the desktop, then type `1122` on the keyboard —
the dashboard opens in the browser (`http://127.0.0.1:8000/`).
The agent serves the dashboard itself while it runs, so no separate
`--dashboard` process is needed. Change the clicks/code (or turn the
gate off) in `config.yaml` under `secret_gate:` or from the dashboard
Settings page. Nothing about the gate is logged.

**Portable (no install):** unzip the `DeskAudit-v12.6.1-Windows-exe.zip`
and run `DeskAudit\DeskAudit.exe` directly — no Python needed.

**Build it yourself on Windows:**
```bat
build_exe.bat            REM builds dist\DeskAudit\DeskAudit.exe (PyInstaller)
installer\build_installer.bat   REM builds installer\Output\DeskAudit-Setup-12.6.1.exe (needs Inno Setup 6)
```
On Linux the same exe is built with Wine (Windows Python + PyInstaller)
and the installer with NSIS (`makensis installer\DeskAudit.nsi`, which
produces the same `installer\Output\DeskAudit-Setup-12.6.1.exe`).
A GitHub Actions workflow (`.github/workflows/build-windows.yml`) also
builds both files on every push to `main` and attaches them to
Release v10.0 — it needs the workflow file added to the repo first
(the Muse GitHub App cannot create `.github/workflows/` files via API).

The exe finds its own `config.yaml` next to itself, so shortcuts and
the 9pm scheduled task work from any folder. `config.local.yaml`
(same folder, git-ignored) overrides `config.yaml` — put the Gemini
API key there, never in `config.yaml`. The Bengali daily summary also
prints correctly in a Windows console (the exe forces UTF-8 output).

## Version history (git) & rollback

The project is a git repository (`main` branch), also pushed to GitHub
as one snapshot commit per version
(https://github.com/firozaenterprise35-hue/DeskAudit — local history is
full, the remote holds snapshots). Useful commands:

```bash
git log --oneline          # history
git status                 # what changed since the last commit
git diff                   # see the changes
git commit -am "what changed"   # save a new version
git checkout <commit> -- <file> # restore one file from an old version
git revert <commit>        # undo a commit safely (keeps history)
git reset --hard <commit>  # go back to a version (discards later changes)
```

`data/`, `reports/`, `backups/` and `*.db` are git-ignored — your private
activity data never goes into git. Only code, config and tests are tracked.

## Database backup & restore

```bash
python main.py --backup              # backups/deskaudit-YYYY-MM-DD-HHMMSS.db
python main.py --backup-list         # list backups with size + date
python main.py --restore <file>      # restore a backup
python main.py --restore --latest    # restore the newest backup
```

- Backups use sqlite3's online backup (safe while the agent is running).
- The agent makes one backup automatically each day on start
  (`backup_on_start` in `config.yaml`); old backups are pruned after
  `backup_keep_days` / `backup_keep_count` (default 30 / 30).
- **Restore first saves your current DB** as `backups/deskaudit-before-restore-....db`,
  so a bad restore can itself be undone. Stop the agent before restoring.

## 9pm automatic job (backup + alerts + daily summary)

On Windows, double-click **`schedule_summary.bat`** (in the install
folder — it is shipped with Setup.exe and the portable zip — or Start
Menu → "Schedule 9pm daily job"), or run
`DeskAudit.exe --schedule-summary` (from source:
`python main.py --schedule-summary`). It creates a Task Scheduler task
"DeskAudit Daily Summary" that runs **every day at 21:00**:

```
python main.py --evening-job
```

That one command does: database backup → alerts check → daily summary
→ weekly review on Sundays. To remove the task, double-click
`unschedule_summary.bat` or run `DeskAudit.exe --unschedule-summary`.
The job first polls browser history and scans transfers, so it works
even if the agent was not running that day.

## LLM-written summary (optional)

The local rule-based Bengali summary is always the default and needs no
key. For an LLM-written version, give an API key, then run:

```bash
python main.py --ai-summary --llm            # daily, LLM text
python main.py --ai-summary --llm --weekly   # 7-day review, LLM text
```

**Google AI Studio (Gemini) — recommended, free tier:** get a key at
https://aistudio.google.com/app/apikey ("Create API key"; keys start with
`AIza...` or `AQ....`). Default provider is `gemini`, model
`gemini-3.5-flash-lite` (verified working 2026-11-24).

**OpenAI:** provider `openai`, model `gpt-4o-mini`, key starts with `sk-...`.
**Anthropic:** provider `anthropic`, model `claude-3-5-haiku-latest`.

Give the key via the `DESKAUDIT_LLM_API_KEY` env var (recommended) or
`llm.api_key` in `config.local.yaml` (git-ignored). **Never put the key
in `config.yaml` — that file is tracked by git.** The `--llm` flag itself
is the opt-in, so `llm.enabled` does not also need to be set.

Only **aggregated stats** are sent to the LLM (active/idle minutes, top
projects with goal %, file/visit counts) — no window titles, file paths
or URLs. If the key is missing or the LLM call fails, it falls back to
the local summary and says so in the output.

## Make a report

```bat
python main.py --report
python main.py --report --date 2026-11-23
```

Example output:

```text
DeskAudit — 2026-11-23
Active: 5h 12m | Idle: 48m | Apps used: 7
Top apps:
  chrome.exe — 2h 41m
  Code.exe — 1h 35m
  WINWORD.EXE — 42m
File events: 18 created, 34 modified, 2 deleted, 1 moved
Browser: 2h 41m active | 12 domains | 84 visits | productive 1h 58m | distracting 31m
Top domains:
  github.com — 1h 12m [work]
  youtube.com — 24m [video]
Top searches:
  python sqlite (x3)
Downloads:
  Chrome: report.pdf [complete]
Projects:
  Office — 2h 15m [app 1h 40m, browser 35m, 12 files, 8 visits] (goal 4h, 56%)
  Study — 1h 05m [app 20m, browser 45m, 3 files, 14 visits] (goal 2h, 54%)
  Untagged — 18m in apps — run: python main.py --suggest-projects
```

## Configuration

`config.yaml` (all optional, defaults are used when a key is missing):

```yaml
poll_interval_seconds: 5
idle_threshold_seconds: 300
db_path: "data/deskaudit.db"
watch_folders:
  - "%USERPROFILE%/Desktop"
  - "%USERPROFILE%/Documents"
exclude_extensions: [".tmp", ".log", ".lock"]
exclude_folders: ["data", "reports", ".venv"]
```

Paths support `%USERPROFILE%`, `%USERNAME%` and `~` expansion.

## Privacy notes

- Data never leaves the machine in the MVP.
- Window titles can contain document names. If you share a report, check
  the titles first.
- To stop file watching for a folder, remove it from `watch_folders`.
- Delete history with: delete `data/deskaudit.db` (the agent recreates it).

## Browser configuration

```yaml
browser_enabled: true
browser_poll_seconds: 60
track_downloads: true
track_searches: true
browser_cdp_port: 0              # 9222 if Chrome launched with --remote-debugging-port=9222
browser_receiver_enabled: false  # true after installing extension/chrome/
browser_receiver_port: 8765
productive_domains: ["github.com"]
distracting_domains: ["facebook.com", "youtube.com"]
exclude_domains: []              # never log these domains
```

## Next steps after the MVP

Done in v7: git version history, database backup/restore, optional
LLM-written summary (needs an API key you provide), and the 9pm
automatic job (backup + alerts + daily summary, Task Scheduler).

Remaining: run it for a real day on Windows and check the first real
daily summary against your actual work.
