"""Google Drive auto-upload for finished screen recordings (v12.16).

When a recording ends, the video is sent to Telegram as before — and,
when Drive is connected, it is ALSO uploaded to a "DeskAudit
Recordings" folder in the owner's Google Drive, so the video survives
even if the PC is wiped. The owner gets the Drive link in the chat.

Drive uploads need OAuth (an API key cannot upload). The one-time
setup is the same pattern as the Telegram bot token and the Gemini
key: the owner creates a Google Cloud OAuth client (Desktop app) and
puts its Client ID + Secret in Dashboard Settings -> Drive (or sends
them to Muse to install). Then /drive auth sends her the sign-in
link; she opens it on her phone, signs in, copies the code from the
address bar and sends /drive code <কোড> to the bot. The bot
exchanges it for a refresh token (stored in config.local.yaml, never
shown) and uploads with it from then on. Scope is drive.file — the
uploader can only touch files it created itself, never the whole
Drive.

Stdlib only (urllib) — no Google client library is installed on the
agent PC. Network is fully mocked in tests/test_drive_upload.py.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_DRIVE = {
    "enabled": True,
    "client_id": "",
    "client_secret": "",
    "refresh_token": "",
    "folder_name": "DeskAudit Recordings",
}

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"
SCOPE = "https://www.googleapis.com/auth/drive.file"
# Loopback redirect: Google allows http://127.0.0.1 without registering
# it. The owner signs in on her phone; the page cannot load there, but
# the code is in the address bar (?code=...) and she pastes it to the bot.
REDIRECT_URI = "http://127.0.0.1:8765/"
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveError(Exception):
    """A Drive API step failed (auth, refresh, find/create folder,
    upload). The message is safe to show the owner."""


def build_auth_url(client_id: str) -> str:
    """The sign-in link for /drive auth. access_type=offline asks for
    a refresh token; prompt=consent makes Google issue one even if
    this account authorised the app before."""
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    return f"{AUTH_URL}?{params}"


def _post_form(url: str, fields: dict, timeout: int = 60) -> dict:
    """POST application/x-www-form-urlencoded; returns the decoded
    JSON. Raises DriveError on any failure (HTTP error body included
    when Google explains it)."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = (detail.get("error_description")
                   or (detail.get("error") or {}).get("message")
                   or detail.get("error") or str(exc))
        except Exception:
            msg = str(exc)
        raise DriveError(str(msg)) from exc
    except Exception as exc:
        raise DriveError(str(exc)) from exc


def _api_json(method: str, url: str, access_token: str,
              payload: dict | None = None, timeout: int = 60) -> dict:
    """One Drive API call with a JSON body (or none for GET)."""
    data = None
    headers = {"Authorization": f"Bearer {access_token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _http_drive_error(exc) from exc
    except Exception as exc:
        raise DriveError(str(exc)) from exc


def _http_drive_error(exc: urllib.error.HTTPError) -> DriveError:
    try:
        detail = json.loads(exc.read().decode("utf-8"))
        msg = (detail.get("error") or {}).get("message") or str(exc)
    except Exception:
        msg = str(exc)
    err = DriveError(msg)
    err.status = exc.code  # type: ignore[attr-defined]
    return err


def exchange_code(client_id: str, client_secret: str,
                  code: str) -> dict:
    """Trade the pasted auth code for tokens. Returns the token dict
    (access_token, refresh_token, expires_in). Raises DriveError."""
    code = (code or "").strip()
    if not code:
        raise DriveError("কোডটি খালি — /drive code <কোড> লিখুন।")
    result = _post_form(TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    if "access_token" not in result or "refresh_token" not in result:
        raise DriveError("Google টোকেন দেয়নি — কোডটি ভুল বা মেয়াদ শেষ। "
                         "আবার /drive auth লিখে নতুন কোড নিন।")
    return result


def refresh_access_token(client_id: str, client_secret: str,
                         refresh_token: str) -> dict:
    """Trade a refresh token for a fresh access token."""
    result = _post_form(TOKEN_URL, {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    if "access_token" not in result:
        raise DriveError("Google নতুন টোকেন দেয়নি — আবার /drive auth করে "
                         "নতুন করে যুক্ত করুন।")
    return result


def _find_folder(access_token: str, folder_name: str) -> str | None:
    """The id of the recordings folder, or None when it is missing."""
    query = (f"name = '{folder_name.replace(chr(39), chr(92) + chr(39))}' "
             f"and mimeType = '{FOLDER_MIME}' and trashed = false")
    url = FILES_URL + "?" + urllib.parse.urlencode(
        {"q": query, "fields": "files(id,name)", "pageSize": 1})
    result = _api_json("GET", url, access_token)
    files = result.get("files") or []
    return files[0]["id"] if files else None


def _create_folder(access_token: str, folder_name: str) -> str:
    result = _api_json("POST", FILES_URL, access_token, {
        "name": folder_name, "mimeType": FOLDER_MIME})
    folder_id = result.get("id")
    if not folder_id:
        raise DriveError("Drive ফোল্ডার বানানো যায়নি।")
    return folder_id


def _ensure_folder(access_token: str, folder_name: str) -> str:
    return _find_folder(access_token, folder_name) or _create_folder(
        access_token, folder_name)


def upload_file(access_token: str, path: str | Path,
                folder_name: str = "DeskAudit Recordings") -> dict:
    """Upload one file into the recordings folder (creating the
    folder when it is missing). Returns the file dict (id,
    webViewLink, name). Raises DriveError. A 48 MB recording fits in
    one multipart request; the timeout covers slow shop internet."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except Exception as exc:
        raise DriveError(f"ফাইল পড়া যায়নি: {exc}") from exc
    folder_id = _ensure_folder(access_token, folder_name)
    boundary = "----DeskAuditDrive"
    body = bytearray()
    meta = json.dumps({"name": path.name, "parents": [folder_id]},
                      ensure_ascii=False).encode("utf-8")
    body += f"--{boundary}\r\n".encode()
    body += ("Content-Type: application/json; charset=UTF-8\r\n\r\n").encode()
    body += meta + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += ("Content-Type: application/octet-stream\r\n\r\n").encode()
    body += data
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        UPLOAD_URL, data=bytes(body),
        headers={"Authorization": f"Bearer {access_token}",
                 "Content-Type": f"multipart/related; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _http_drive_error(exc) from exc
    except Exception as exc:
        raise DriveError(str(exc)) from exc
    if not result.get("id"):
        raise DriveError("Drive আপলোডের জবাব আসেনি।")
    return result


class DriveUploader:
    """Uploads finished recordings to the owner's Drive.

    `settings` is the drive section (DEFAULT_DRIVE overlaid). The
    access token is cached in memory and refreshed when it expires;
    a 401 retries once after a refresh. `upload()` returns
    (True, webViewLink) or (False, error message) and never raises.
    """

    def __init__(self, settings: dict | None, log=None):
        self.settings = {**DEFAULT_DRIVE, **(settings or {})}
        self.log = log
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        """Client ID + Secret + refresh token are all present."""
        return bool(self.settings.get("client_id")
                    and self.settings.get("client_secret")
                    and self.settings.get("refresh_token"))

    @property
    def enabled(self) -> bool:
        """Auto-upload runs only when it is switched on AND connected."""
        return bool(self.settings.get("enabled", True)) and self.configured

    def auth_url(self) -> str | None:
        """The sign-in link, or None when the client ID is missing."""
        client_id = (self.settings.get("client_id") or "").strip()
        return build_auth_url(client_id) if client_id else None

    def _get_access_token(self) -> str:
        """A live access token, refreshing when it is missing or
        about to expire (5 minutes of margin)."""
        if (self._token is not None
                and time.time() < self._token_expires_at - 300):
            return self._token
        result = refresh_access_token(
            self.settings["client_id"], self.settings["client_secret"],
            self.settings["refresh_token"])
        self._token = result["access_token"]
        self._token_expires_at = time.time() + int(
            result.get("expires_in") or 3600)
        return self._token

    def upload(self, path: str | Path) -> tuple[bool, str]:
        """Upload one file. (True, link) on success, (False, error)
        otherwise. Never raises."""
        if not self.enabled:
            return False, "Drive আপলোড চালু নেই।"
        try:
            token = self._get_access_token()
        except DriveError as exc:
            return False, str(exc)
        folder = str(self.settings.get("folder_name")
                     or DEFAULT_DRIVE["folder_name"])
        try:
            result = upload_file(token, path, folder)
        except DriveError as exc:
            if getattr(exc, "status", None) == 401:
                # The cached token died between the check and the
                # upload — refresh once and retry.
                try:
                    self._token = None
                    token = self._get_access_token()
                    result = upload_file(token, path, folder)
                except DriveError as exc2:
                    return False, str(exc2)
            else:
                return False, str(exc)
        link = result.get("webViewLink") or result.get("id") or ""
        if self.log is not None:
            self.log.info("Drive upload ok: %s -> %s", path, link)
        return True, link
