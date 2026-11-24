# DeskAudit Tab Reporter (optional, for exact active-tab URLs)

History polling alone gives you the URL of every visit, but not the exact
second-by-second active tab. This tiny extension closes that gap.

## Install (Chrome / Edge)

1. Open `chrome://extensions/` (Edge: `edge://extensions/`).
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this `extension/chrome/` folder.
4. In DeskAudit `config.yaml`, set:
   ```yaml
   browser_receiver_enabled: true
   browser_receiver_port: 8765
   ```
5. Restart DeskAudit (`run.bat`). The extension posts the active tab to
   `http://127.0.0.1:8765/tab` every 5 seconds.

## What it sends

Only: browser name, tab URL, tab title. No page content, no passwords,
no cookies, no form data. The receiver listens on localhost only.

## Alternative (no extension)

Launch Chrome with `--remote-debugging-port=9222` and set
`browser_cdp_port: 9222` in `config.yaml`. DeskAudit then asks Chrome
directly for the active tab.

## Uninstall

Remove the extension from `chrome://extensions/` and set
`browser_receiver_enabled: false`. History polling keeps working.
