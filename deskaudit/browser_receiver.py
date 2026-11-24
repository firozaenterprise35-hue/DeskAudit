"""Local receiver for the DeskAudit browser extension.

Listens ONLY on 127.0.0.1 (localhost), never on the network. The Chrome /
Edge extension in extension/chrome/ POSTs the active tab every few seconds:

    POST /tab  {"browser": "Chrome", "url": "...", "title": "..."}

No auth token is needed because the port is localhost-only and the
extension is installed by the user themself. Do not expose this port.

Start it from main.py when browser_receiver_enabled is true in config.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(tracker, log):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, payload: dict):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path not in ("/tab", "/visit"):
                self._send(404, {"ok": False, "error": "unknown path"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception as exc:
                self._send(400, {"ok": False, "error": str(exc)})
                return
            url = (data.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._send(400, {"ok": False, "error": "url must be http(s)"})
                return
            try:
                tracker.record_extension_tab(
                    data.get("browser") or "Chrome",
                    url,
                    data.get("title") or "",
                    datetime.now().astimezone(),
                )
            except Exception as exc:  # never crash the server on bad data
                log.warning("extension tab failed: %s", exc)
                self._send(500, {"ok": False, "error": str(exc)})
                return
            self._send(200, {"ok": True})

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"ok": True, "service": "deskaudit-browser-receiver"})
                return
            self._send(404, {"ok": False, "error": "unknown path"})

    return Handler


class BrowserReceiver:
    def __init__(self, tracker, port: int, log):
        self.tracker = tracker
        self.port = port
        self.log = log
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            handler = make_handler(self.tracker, self.log)
            self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        except OSError as exc:
            self.log.warning("Browser receiver could not start on port %s: %s", self.port, exc)
            return False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.log.info("Browser extension receiver listening on 127.0.0.1:%s", self.port)
        return True

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
