"""Telegram file explorer (v12.9): browse drives and folders from the
owner's Telegram chat, and copy / move / send / hide files.

Design notes
------------
* Browsing is button-driven: the bot keeps one small "explorer state"
  (current folder, page, selected file, copy/move mode) in kv_state,
  and the inline buttons carry only short codes (x:drv:C, x:o:3,
  x:f:5, x:pg:2, ...) — Telegram callback_data is limited to 64
  bytes, so full paths never travel in a button.
* Only FILES can be copied, moved, sent or hidden in v12.9. Folders
  can be opened and navigated, nothing more.
* v12.11 (360° audit): folders can be copied, moved, hidden,
  renamed and deleted too, and sent as a zip (within the send
  limit); files and folders can be renamed, a new folder can be
  created, anything can be deleted into the delete-protect
  quarantine, and the current folder can be searched by name.
* Send = the file goes to the owner's chat as a Telegram document.
  Telegram's Bot API accepts at most ~50 MB per file, so bigger files
  can only be copied/moved to another drive, never sent.
* Hide = the file is MOVED into a hidden folder (default
  <project_root>/data/hidden, or the configured hidden_path) and
  recorded in the hidden_files table — the strong hide, like v12.8's
  quarantine. The weak Windows "hidden" attribute is NOT used: anyone
  who turns on "show hidden files" would see the file again.
* Mutations (copy / move / hide / unhide) are blocked inside system
  folders (C:\\Windows, Program Files, ...) so a wrong tap can never
  break Windows. Browsing those folders is still allowed.
* The explorer never raises out of a listing or an action: every
  failure comes back as a Bengali message instead.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

_log = logging.getLogger("deskaudit.file_explorer")

# A folder copy/move that has to copy data (not a same-drive rename)
# is refused above this size: the bot's poll loop would block for
# minutes on a multi-GB tree, and the owner gets a clear message
# instead of a frozen bot.
FOLDER_COPY_MAX_BYTES = 1 * 1024 * 1024 * 1024

_DRIVE_RE = re.compile(r"^[A-Za-z]:$")

DEFAULT_EXPLORER = {
    "enabled": True,
    "allowed_roots": [],      # empty = every drive is browsable
    "hidden_path": "",        # empty = <project_root>/data/hidden
    "page_size": 20,          # entries shown per page
    "max_send_mb": 50,        # Telegram Bot API send limit
}

# First folder under a drive root that marks a system folder. Checked
# case-insensitively, on the path's own components, so it works for
# Windows-style paths even when the code runs on Linux (tests).
SYSTEM_FIRST_FOLDERS = {
    "windows", "program files", "program files (x86)", "programdata",
    "perflogs", "$recycle.bin", "system volume information", "recovery",
    "boot", "documents and settings",
}


def _parts(path: str) -> list[str]:
    """Path components, split on both / and \\ (Windows paths may be
    inspected on any platform)."""
    return [p for p in str(path).replace("\\", "/").split("/") if p]


def is_system_path(path: str | Path) -> bool:
    """True when the path sits inside a Windows system folder, e.g.
    C:\\Windows\\... or D:\\Program Files\\.... The drive root itself
    (C:\\) is not a system path — browsing it is fine."""
    parts = _parts(path)
    if len(parts) < 2:
        return False
    return parts[1].lower() in SYSTEM_FIRST_FOLDERS


def norm_root(path: str | Path) -> str:
    """Make a drive root a REAL root: "C:" -> "C:\\\\". On real
    Windows, plain "C:" is the process's current directory on drive
    C, not the root (v12.11 audit) — browsing or copying through it
    lands in the wrong place. Anything that is not a bare drive
    letter is returned unchanged."""
    raw = str(path)
    if _DRIVE_RE.match(raw.strip()):
        return raw.strip() + "\\"
    return raw


def dir_size(path: str | Path, cap_bytes: int = FOLDER_COPY_MAX_BYTES,
             cap_files: int = 5000) -> tuple[int, bool]:
    """Total size of a folder's files, bounded: the walk stops at
    cap_bytes / cap_files and reports complete=False, so a huge
    tree can never hang the bot. Never raises."""
    total = 0
    seen = 0
    try:
        for _root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(_root, name))
                except Exception:
                    continue
                seen += 1
                if seen >= cap_files or total >= cap_bytes:
                    return total, False
    except Exception:
        return total, False
    return total, True


def drive_free(path: str | Path) -> str:
    """Free space on a drive, e.g. "123.4 GB" — for the explorer's
    drives view, so the owner can see where a big copy fits. Empty
    string when it cannot be read. Never raises."""
    try:
        return _fmt_size(shutil.disk_usage(str(path)).free)
    except Exception:
        return ""


def valid_name(name: str) -> tuple[bool, str]:
    """A new file/folder name typed in Telegram: no path separators
    or Windows-forbidden characters, not empty, not . / .."""
    name = (name or "").strip()
    if not name or name in (".", ".."):
        return False, "নামটা খালি রাখা যাবে না।"
    if any(c in name for c in '\\/:*?"<>|'):
        return False, "নামে \\ / : * ? \" < > | — এগুলো থাকা যাবে না।"
    if any(ord(c) < 32 for c in name):
        return False, "নামে অদৃশ্য অক্ষর থাকা যাবে না।"
    return True, ""


def _fmt_size(size: int | None) -> str:
    if size is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%d-%m %H:%M")
    except Exception:
        return ""


def list_dir(path: str | Path, page: int = 0,
             page_size: int = 20) -> dict:
    """One page of a folder's entries, folders first, then files,
    each sorted by name. Never raises: errors come back as
    {"error": "..."}."""
    try:
        folder = Path(path)
        if not folder.is_dir():
            return {"path": str(path), "entries": [], "page": 0,
                    "pages": 0, "total": 0,
                    "error": "ফোল্ডারটা পাওয়া যায়নি।"}
        rows = []
        for child in folder.iterdir():
            try:
                st = child.stat()
            except Exception:
                continue
            rows.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
                "size": None if child.is_dir() else int(st.st_size),
                "mtime": float(st.st_mtime),
            })
        rows.sort(key=lambda r: (not r["is_dir"], r["name"].lower()))
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(int(page), pages - 1))
        start = page * page_size
        return {"path": str(folder), "entries": rows[start:start + page_size],
                "page": page, "pages": pages, "total": total}
    except PermissionError:
        return {"path": str(path), "entries": [], "page": 0,
                "pages": 0, "total": 0,
                "error": "এই ফোল্ডার খোলার অনুমতি নেই।"}
    except Exception as exc:
        return {"path": str(path), "entries": [], "page": 0,
                "pages": 0, "total": 0, "error": f"খোলা যায়নি: {exc}"}


def copy_file(src: str | Path, dest_dir: str | Path) -> tuple[bool, str, str]:
    """Copy one file into dest_dir. Returns (ok, message, dest_path).
    Refuses system destinations and copying a file onto itself."""
    src_p, dest_p = Path(src), Path(dest_dir)
    if not src_p.is_file():
        return False, "ফাইলটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", ""
    if is_system_path(dest_p):
        return False, "সিস্টেম ফোল্ডারে কপি করা যাবে না (Windows ভেঙে যেতে পারে)।", ""
    if not dest_p.is_dir():
        return False, "গন্তব্য ফোল্ডারটা পাওয়া যায়নি।", ""
    if _parts(src_p.parent) == _parts(dest_p):
        return False, "ফাইলটা এই ফোল্ডারেই আছে — অন্য ফোল্ডার বেছে নিন।", ""
    target = dest_p / src_p.name
    n = 2
    while target.exists():
        target = dest_p / f"{src_p.stem} ({n}){src_p.suffix}"
        n += 1
    try:
        shutil.copy2(src_p, target)
    except PermissionError:
        return False, "কপি করা যায়নি — অনুমতি নেই।", ""
    except Exception as exc:
        return False, f"কপি করা যায়নি: {exc}", ""
    return True, f"📋 কপি হয়েছে: {src_p.name} → {target}", str(target)


def move_file(src: str | Path, dest_dir: str | Path) -> tuple[bool, str, str]:
    """Move one file into dest_dir. Same guards as copy_file, plus
    the source itself must not sit in a system folder."""
    src_p, dest_p = Path(src), Path(dest_dir)
    if not src_p.is_file():
        return False, "ফাইলটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", ""
    if is_system_path(src_p):
        return False, "সিস্টেম ফোল্ডার থেকে ফাইল সরানো যাবে না।", ""
    if is_system_path(dest_p):
        return False, "সিস্টেম ফোল্ডারে মুভ করা যাবে না (Windows ভেঙে যেতে পারে)।", ""
    if not dest_p.is_dir():
        return False, "গন্তব্য ফোল্ডারটা পাওয়া যায়নি।", ""
    if _parts(src_p.parent) == _parts(dest_p):
        return False, "ফাইলটা এই ফোল্ডারেই আছে — অন্য ফোল্ডার বেছে নিন।", ""
    target = dest_p / src_p.name
    n = 2
    while target.exists():
        target = dest_p / f"{src_p.stem} ({n}){src_p.suffix}"
        n += 1
    try:
        shutil.move(str(src_p), str(target))
    except PermissionError:
        return False, "মুভ করা যায়নি — অনুমতি নেই।", ""
    except Exception as exc:
        return False, f"মুভ করা যায়নি: {exc}", ""
    return True, f"➡️ মুভ হয়েছে: {src_p.name} → {target}", str(target)


def _unique_dir(dest_dir: Path, name: str) -> Path:
    """A free folder name inside dest_dir: Name, Name (2), ..."""
    target = dest_dir / name
    n = 2
    while target.exists():
        target = dest_dir / f"{name} ({n})"
        n += 1
    return target


def copy_dir(src: str | Path, dest_dir: str | Path) -> tuple[bool, str, str]:
    """Copy one folder (whole tree) into dest_dir. Refuses system
    destinations, copying a folder into itself, and trees over
    FOLDER_COPY_MAX_BYTES."""
    src_p, dest_p = Path(src), Path(dest_dir)
    if not src_p.is_dir():
        return False, "ফোল্ডারটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", ""
    if is_system_path(dest_p):
        return False, "সিস্টেম ফোল্ডারে কপি করা যাবে না (Windows ভেঙে যেতে পারে)।", ""
    if not dest_p.is_dir():
        return False, "গন্তব্য ফোল্ডারটা পাওয়া যায়নি।", ""
    try:
        if os.path.commonpath([src_p.resolve(), dest_p.resolve()]) \
                == str(src_p.resolve()):
            return False, "একটা ফোল্ডারকে তার নিজের ভেতরে কপি করা যাবে না।", ""
    except Exception:
        pass
    size, _done = dir_size(src_p)
    if size >= FOLDER_COPY_MAX_BYTES:
        return False, (f"ফোল্ডারটা অনেক বড় ({_fmt_size(size)}) — টেলিগ্রাম "
                       "থেকে কপি করা যাবে না, ভেতরের ফাইলগুলো আলাদা করে কপি করুন।"), ""
    target = _unique_dir(dest_p, src_p.name)
    try:
        shutil.copytree(src_p, target)
    except PermissionError:
        return False, "কপি করা যায়নি — অনুমতি নেই।", ""
    except Exception as exc:
        return False, f"কপি করা যায়নি: {exc}", ""
    return True, f"📋 কপি হয়েছে: {src_p.name} → {target}", str(target)


def move_dir(src: str | Path, dest_dir: str | Path) -> tuple[bool, str, str]:
    """Move one folder into dest_dir. A same-drive move is a rename
    (instant, any size); a cross-drive move copies the data, so the
    1 GB guard applies there."""
    src_p, dest_p = Path(src), Path(dest_dir)
    if not src_p.is_dir():
        return False, "ফোল্ডারটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", ""
    if is_system_path(src_p):
        return False, "সিস্টেম ফোল্ডার থেকে ফোল্ডার সরানো যাবে না।", ""
    if is_system_path(dest_p):
        return False, "সিস্টেম ফোল্ডারে মুভ করা যাবে না (Windows ভেঙে যেতে পারে)।", ""
    if not dest_p.is_dir():
        return False, "গন্তব্য ফোল্ডারটা পাওয়া যায়নি।", ""
    try:
        if os.path.commonpath([src_p.resolve(), dest_p.resolve()]) \
                == str(src_p.resolve()):
            return False, "একটা ফোল্ডারকে তার নিজের ভেতরে মুভ করা যাবে না।", ""
    except Exception:
        pass
    try:
        same_drive = src_p.resolve().anchor == dest_p.resolve().anchor
    except Exception:
        same_drive = False
    if not same_drive:
        size, _done = dir_size(src_p)
        if size >= FOLDER_COPY_MAX_BYTES:
            return False, (f"ফোল্ডারটা অনেক বড় ({_fmt_size(size)}) — অন্য ড্রাইভে "
                           "মুভ করা যাবে না, ভেতরের ফাইলগুলো আলাদা করে মুভ করুন।"), ""
    target = _unique_dir(dest_p, src_p.name)
    try:
        shutil.move(str(src_p), str(target))
    except PermissionError:
        return False, "মুভ করা যায়নি — অনুমতি নেই।", ""
    except Exception as exc:
        return False, f"মুভ করা যায়নি: {exc}", ""
    return True, f"➡️ মুভ হয়েছে: {src_p.name} → {target}", str(target)


def zip_dir(src: str | Path, max_bytes: int) -> tuple[bool, str, str]:
    """Zip one folder into a temp file for sending on Telegram.
    Returns (ok, message, zip_path); the caller deletes the zip
    after sending. Refuses trees whose raw size is far above the
    send limit, and zips that still come out too big."""
    src_p = Path(src)
    if not src_p.is_dir():
        return False, "ফোল্ডারটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", ""
    size, _done = dir_size(src_p, cap_bytes=max_bytes * 4)
    if size >= max_bytes * 4:
        return False, (f"ফোল্ডারটা অনেক বড় ({_fmt_size(size)}) — জিপ করলেও "
                       f"{_fmt_size(max_bytes)}-এর বেশি হবে, পাঠানো যাবে না।"), ""
    tmp = tempfile.mkdtemp(prefix="deskaudit-zip-")
    try:
        base = os.path.join(tmp, src_p.name)
        zip_path = shutil.make_archive(base, "zip", src_p.parent, src_p.name)
        actual = os.path.getsize(zip_path)
    except Exception as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"জিপ করা যায়নি: {exc}", ""
    if actual > max_bytes:
        shutil.rmtree(tmp, ignore_errors=True)
        return False, (f"জিপ করার পরও {_fmt_size(actual)} — "
                       f"{_fmt_size(max_bytes)}-এর বেশি, পাঠানো যাবে না।"), ""
    return True, "", zip_path


class FileExplorer:
    """The hide/unhide side of the explorer, backed by the
    hidden_files table. Copy/move/send are the module functions
    above; hide moves the file into a hidden folder and records
    where it came from, so it can be restored to the same place."""

    def __init__(self, db, settings: dict | None, log,
                 project_root: str | Path = "."):
        self.db = db
        self.settings = dict(DEFAULT_EXPLORER)
        if settings:
            self.settings.update({k: v for k, v in settings.items()
                                  if v is not None})
        self.log = log
        self.project_root = Path(project_root)
        self.enabled = bool(self.settings.get("enabled", True))

    # -- paths ------------------------------------------------------
    @property
    def hidden_root(self) -> Path:
        raw = str(self.settings.get("hidden_path") or "").strip()
        if raw:
            p = Path(os.path.expandvars(os.path.expanduser(raw)))
            if not p.is_absolute():
                # A relative path means "inside the install folder",
                # not "inside wherever the process happened to start".
                p = self.project_root / p
            return p
        return self.project_root / "data" / "hidden"

    @property
    def page_size(self) -> int:
        try:
            return max(5, min(50, int(self.settings.get("page_size", 20))))
        except (TypeError, ValueError):
            return 20

    @property
    def max_send_bytes(self) -> int:
        try:
            mb = max(1, min(50, int(self.settings.get("max_send_mb", 50))))
        except (TypeError, ValueError):
            mb = 50
        return mb * 1024 * 1024

    def roots(self) -> list[str]:
        """Browsable roots: the configured allowed_roots, or every
        drive when none are configured. Drive letters are normalised
        to real roots ("C:" -> "C:\\\\") — see norm_root."""
        configured = [str(r).strip() for r in
                      (self.settings.get("allowed_roots") or []) if str(r).strip()]
        if configured:
            return [norm_root(r) for r in configured if Path(r).is_dir()]
        from .delete_protect import list_drives

        return list_drives()

    def can_send(self, size: int | None) -> bool:
        return size is not None and 0 < size <= self.max_send_bytes

    # -- hide / unhide ----------------------------------------------
    def hide(self, path: str | Path) -> tuple[bool, str, dict | None]:
        """Move one file or folder into the hidden folder. Returns
        (ok, message, item-dict). The item carries the DB row id so
        Telegram can offer 'ফিরিয়ে দাও' for exactly this file."""
        from .utils import today_str

        src = Path(path)
        if not src.exists():
            return False, "এটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।", None
        if is_system_path(src):
            return False, "সিস্টেম ফোল্ডারের কিছু হাইড করা যাবে না।", None
        try:
            hidden_abs = os.path.abspath(str(self.hidden_root))
            src_abs = os.path.abspath(str(src))
            if src_abs == hidden_abs or src_abs.startswith(hidden_abs + os.sep):
                return False, "এটা আগেই লুকানো আছে।", None
        except Exception:
            pass
        import secrets

        try:
            size = src.stat().st_size if src.is_file() else dir_size(src)[0]
        except Exception:
            size = None
        token = secrets.token_hex(4)
        dest_dir = self.hidden_root / token
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            shutil.move(str(src), str(dest))
        except PermissionError:
            return False, "হাইড করা যায়নি — অনুমতি নেই।", None
        except Exception as exc:
            return False, f"হাইড করা যায়নি: {exc}", None
        try:
            hid = self.db.log_hidden(
                today_str(), src.name, str(src), str(dest), size)
        except Exception as exc:
            # v12.13 (P7): the file is already moved but the DB row
            # failed — the item would carry id 0 and its Telegram
            # restore button could never work. Move the file BACK and
            # report the failure instead of leaving an unfindable file.
            _log.warning("hide: DB log failed for %s: %s", src, exc)
            try:
                shutil.move(str(dest), str(src))
            except Exception:
                _log.warning("hide: could not move %s back to %s", dest, src)
            return False, "হাইড করা যায়নি — রেকর্ড লেখা যায়নি, ফাইল আগের জায়গাতেই আছে।", None
        item = {"id": hid, "file_name": src.name,
                "original_path": str(src), "hidden_path": str(dest),
                "size_bytes": size}
        return True, f"👁️ হাইড হয়েছে: {src.name} — আগের জায়গায় আর দেখা যাবে না।", item

    def unhide(self, hid: int) -> str:
        """Restore a hidden file or folder to its original path. A
        name clash at the original place gets a ' (ফেরত ...)' suffix
        instead of overwriting anything. If the hidden copy is gone,
        the row is marked purged so it stops appearing in /hidden."""
        row = self.db.get_hidden(int(hid))
        if row is None:
            return "🫥 এই লুকানো ফাইলটা পাওয়া যায়নি।"
        if row["status"] != "hidden":
            return f"🫥 {row['file_name']} — আগেই {row['status']} করা হয়েছে।"
        src = Path(row["hidden_path"])
        if not src.exists():
            try:
                self.db.set_hidden_status(int(hid), "purged")
            except Exception:
                pass
            return (f"🫥 {row['file_name']} — লুকানো কপিটা আর নেই, "
                    "তাই তালিকা থেকে সরিয়ে দেওয়া হয়েছে।")
        dest = Path(row["original_path"])
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return f"🫥 ফেরানো যায়নি: {exc}"
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if src.is_dir():
                dest = dest.parent / f"{dest.name} (ফেরত {stamp})"
            else:
                dest = dest.parent / f"{dest.stem} (ফেরত {stamp}){dest.suffix}"
        try:
            shutil.move(str(src), str(dest))
        except PermissionError:
            return "🫥 ফেরানো যায়নি — অনুমতি নেই।"
        except Exception as exc:
            return f"🫥 ফেরানো যায়নি: {exc}"
        try:
            self.db.set_hidden_status(int(hid), "restored")
        except Exception as exc:
            _log.warning("unhide: DB update failed for #%s: %s", hid, exc)
        return f"↩️ ফিরিয়ে দেওয়া হয়েছে: {row['file_name']} → {dest}"

    def held_items(self, limit: int = 50) -> list:
        try:
            return self.db.hidden_held(limit=limit)
        except Exception:
            return []
