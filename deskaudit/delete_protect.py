"""Delete protection (v12.8) — a deleted file looks deleted to the
user, but its content is preserved.

How it works, honestly:
- A normal Explorer delete sends the file to the Recycle Bin — Windows
  itself preserves the content there. The protector watches every
  drive's $Recycle.Bin and, the moment a file lands in it, MOVES it to
  a hidden quarantine folder (another drive, when one is configured)
  and tells the owner on Telegram with হ্যাঁ / না buttons.
  হ্যাঁ = delete the quarantine copy for good. না = move it back to
  exactly where it was.
- A Shift+Delete (or a delete from cmd / another app) skips the bin,
  so for folders the owner marks as protected (Desktop, Documents,
  sensitive folders) the protector keeps a live MIRROR copy of every
  file. When such a file disappears, the mirror copy is quarantined
  the same way. This is the only way a permanent delete can be
  undone — the content has to exist somewhere BEFORE the delete.
- A permanent delete OUTSIDE the protected folders cannot be undone:
  by the time the agent sees the delete event, the content is already
  gone. Those deletes are only logged (name, path, size, time).

TEMP / %TEMP% files, browser caches, the Recycle Bin itself, the
quarantine folder and DeskAudit's own data are never protected.

The quarantine is never emptied on its own: held items are removed
only when the owner taps হ্যাঁ (purge) on Telegram. Mirror copies
are different — they are just a safety copy, so old ones are pruned
when the mirror grows past its cap or retention.
"""

import hashlib
import json
import os
import secrets
import shutil
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_DELETE_PROTECT = {
    "enabled": True,
    "quarantine_path": "",  # "" = <project_root>/data/quarantine
    "poll_seconds": 30,
    "mirror_max_mb": 500,  # mirror only files up to this size
    "quarantine_max_gb": 10,  # cap for the MIRROR only; held items never auto-purged
    "mirror_retention_days": 30,
    "protected_folders": [],  # extra folders, beyond Desktop/Documents/sensitive
}

# Per-scan mirror budget, so the first sync never hogs the disk.
_MIRROR_MAX_FILES_PER_SCAN = 20
_MIRROR_MAX_BYTES_PER_SCAN = 200 * 1024 * 1024

_SEEN_KEY = "delete_protect_seen"
_MIRROR_KEY = "delete_protect_mirror"
_SEEN_CAP = 5000

# FILETIME (100ns ticks since 1601-01-01) -> Unix epoch offset.
_FILETIME_EPOCH = 116444736000000000


def filetime_to_datetime(value: int) -> datetime:
    """Convert a Windows FILETIME int to a naive local datetime."""
    try:
        seconds = (int(value) - _FILETIME_EPOCH) / 10_000_000
        return datetime.fromtimestamp(seconds)
    except Exception:
        return datetime.now()


def parse_recycle_info(path: str | Path) -> dict | None:
    """Parse a Recycle Bin $I metadata file.

    Layout: 8-byte header, 8-byte original size (int64), 8-byte
    deletion time (FILETIME), then the original path as UTF-16LE.
    Returns {"original_path", "size_bytes", "deleted_at"} or None.
    """
    try:
        data = Path(path).read_bytes()
    except Exception:
        return None
    if len(data) < 24:
        return None
    try:
        size = struct.unpack_from("<q", data, 8)[0]
        deleted_at = filetime_to_datetime(struct.unpack_from("<q", data, 16)[0])
        raw_path = data[24:]
        original = raw_path.decode("utf-16-le", errors="ignore").split("\x00")[0].strip()
    except Exception:
        return None
    if not original:
        return None
    return {"original_path": original, "size_bytes": max(int(size), 0),
            "deleted_at": deleted_at}


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").lower().rstrip("/")


def is_temp_path(path: str, quarantine_root: str = "",
                 project_root: str = "") -> bool:
    """True for paths that must never be protected: TEMP files,
    browser caches, the Recycle Bin itself, the quarantine folder,
    and DeskAudit's own data/reports/backups folders."""
    p = _norm(path)
    if "$recycle.bin" in p:
        return True
    if quarantine_root and p.startswith(_norm(quarantine_root)):
        return True
    if project_root:
        root = _norm(project_root)
        for folder in ("data", "reports", "backups"):
            if p.startswith(f"{root}/{folder}"):
                return True
    for env in ("TEMP", "TMP"):
        temp = os.environ.get(env, "")
        if temp and p.startswith(_norm(temp)):
            return True
    if "windows/temp" in p:
        return True
    if "appdata/local/temp" in p:
        return True
    # A folder literally named Temp right under a drive root, e.g. D:/Temp.
    parts = [seg for seg in p.split("/") if seg]
    if len(parts) >= 2 and parts[1] == "temp" and len(parts[0]) == 2 \
            and parts[0][1] == ":":
        return True
    if "/cache/" in p and ("appdata" in p or "local settings" in p):
        return True
    return False


def parse_drive_strings(raw: str) -> list[str]:
    """Drive roots from a double-null-terminated drive list, e.g.
    "C:\\\\0D:\\\\0E:\\\\0" -> ["C:\\\\", "D:\\\\", "E:\\\\"]."""
    drives = []
    for chunk in raw.split("\x00"):
        if len(chunk) >= 2 and chunk[1] == ":":
            drives.append(chunk[:2] + "\\")
    return drives


def list_drives() -> list[str]:
    """Drive roots, e.g. ['C:\\\\', 'D:\\\\']. Windows only in
    practice; elsewhere just the filesystem root.

    The trailing backslash matters (v12.11 audit): on real Windows,
    plain "C:" is NOT the root — it is the process's current
    directory on drive C, so browsing/copying through "C:" lands in
    the wrong place and Path("C:").parent is ".". "C:\\\\" is the
    real root, and its parent is itself.

    And the buffer matters (v12.12 audit): GetLogicalDriveStringsW
    returns a DOUBLE-null-terminated list ("C:\\\\0D:\\\\0..."), but
    ctypes' buf.value stops at the FIRST null — so the old code
    returned only the first drive. The explorer showed only C: and
    the delete protector only watched C:'s Recycle Bin. Read the
    full buffer with wstring_at(buf, n) instead."""
    if sys.platform != "win32":
        return ["/"]
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(256)
        n = ctypes.windll.kernel32.GetLogicalDriveStringsW(256, buf)
        if n and n < 256:
            drives = parse_drive_strings(ctypes.wstring_at(buf, n))
            if drives:
                return drives
    except Exception:
        pass
    return [f"{letter}:\\" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
            if os.path.isdir(f"{letter}:\\")]


def find_recycle_bins(drives: list[str] | None = None) -> list[Path]:
    """$Recycle.Bin folders that actually exist, one per drive."""
    bins = []
    for drive in drives or list_drives():
        root = Path(drive) if drive == "/" else Path(f"{drive}\\")
        bin_path = root / "$Recycle.Bin"
        try:
            if bin_path.is_dir():
                bins.append(bin_path)
        except Exception:
            continue
    return bins


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} GB"


class DeleteProtector:
    """Watches Recycle Bins + mirrors protected folders into a
    hidden quarantine. Never raises out of a scan."""

    def __init__(self, db, settings: dict | None, log,
                 project_root: str | Path = ".",
                 protected_folders: list | None = None,
                 notify=None):
        self.db = db
        self.settings = dict(DEFAULT_DELETE_PROTECT)
        if settings:
            self.settings.update({k: v for k, v in settings.items() if v is not None})
        self.log = log
        self.project_root = Path(project_root)
        self.protected_folders = [Path(p) for p in (protected_folders or [])]
        self.notify = notify  # callable(item) -> bool, e.g. bot.notify_deleted
        self.enabled = bool(self.settings.get("enabled", True))
        self._settings_stamp: tuple | None = None

    def _reload_settings(self) -> None:
        """Re-read the delete_protect settings from config.yaml (+
        config.local.yaml overrides) when the files changed since
        the last load — so a quarantine-drive change in Dashboard
        Settings -> ডিলিট সুরক্ষা takes effect within one poll
        interval, without restarting the agent (v12.12 audit: the
        running agent used to keep its startup settings forever).
        Only reloads when a config file exists and changed; a torn
        or unparseable file is ignored and the old settings kept."""
        try:
            cfg_path = self.project_root / "config.yaml"
            local_path = self.project_root / "config.local.yaml"
            if not cfg_path.exists():
                return
            stamp = []
            for p in (cfg_path, local_path):
                try:
                    stamp.append(p.stat().st_mtime_ns)
                except Exception:
                    stamp.append(0)
            stamp = tuple(stamp)
            if stamp == self._settings_stamp:
                return
            import yaml

            for p in (cfg_path, local_path):
                if p.exists():
                    with p.open(encoding="utf-8") as f:
                        yaml.safe_load(f)  # parse check only
            from .config import Config

            cfg = Config.load(cfg_path)
            self.settings = {**DEFAULT_DELETE_PROTECT,
                             **(cfg.delete_protect or {})}
            self.enabled = bool(self.settings.get("enabled", True))
            self.protected_folders = list(cfg.effective_watch_folders())
            self._settings_stamp = stamp
        except Exception as exc:
            self.log.warning("Delete-protect settings reload failed: %s", exc)

    # -- paths ------------------------------------------------------
    @property
    def quarantine_root(self) -> Path:
        raw = str(self.settings.get("quarantine_path") or "").strip()
        if raw:
            p = Path(os.path.expandvars(os.path.expanduser(raw)))
            if not p.is_absolute():
                # A relative path means "inside the install folder",
                # not "inside wherever the process happened to start".
                p = self.project_root / p
            return p
        return self.project_root / "data" / "quarantine"

    @property
    def held_dir(self) -> Path:
        return self.quarantine_root / "held"

    @property
    def mirror_dir(self) -> Path:
        return self.quarantine_root / "mirror"

    def _ensure_dirs(self) -> None:
        try:
            self.held_dir.mkdir(parents=True, exist_ok=True)
            self.mirror_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.log.warning("Quarantine folders could not be created: %s", exc)

    # -- kv helpers ---------------------------------------------------
    def _seen(self) -> list:
        try:
            raw = self.db.get_state(_SEEN_KEY)
            return json.loads(raw) if raw else []
        except Exception:
            return []

    def _save_seen(self, seen: list) -> None:
        try:
            self.db.set_state(_SEEN_KEY, json.dumps(seen[-_SEEN_CAP:]))
        except Exception:
            pass

    def _mirror_manifest(self) -> dict:
        try:
            raw = self.db.get_state(_MIRROR_KEY)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        try:
            self.db.set_state(_MIRROR_KEY, json.dumps(manifest))
        except Exception:
            pass

    # -- quarantine store ---------------------------------------------
    def _quarantine(self, source: Path, file_name: str, original_path: str,
                    size_bytes: int, source_kind: str,
                    deleted_at: datetime | None) -> dict | None:
        """Move `source` (file or dir) into held/<token>/ and record it.
        Returns the item dict, or None on failure."""
        self._ensure_dirs()
        token = secrets.token_hex(4)
        dest_dir = self.held_dir / token
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / file_name
            if dest.exists():
                dest = dest_dir / f"{token}_{file_name}"
            shutil.move(str(source), str(dest))
        except Exception as exc:
            self.log.warning("Quarantine move failed for %s: %s", source, exc)
            return None
        try:
            from .utils import today_str

            qid = self.db.log_quarantined(
                date=today_str(), file_name=file_name,
                original_path=str(original_path), quarantine_path=str(dest),
                size_bytes=int(size_bytes or 0), source=source_kind,
                deleted_at=(deleted_at or datetime.now()).isoformat(timespec="seconds"))
        except Exception as exc:
            self.log.warning("Quarantined DB log failed: %s", exc)
            qid = 0
        item = {"id": qid, "file_name": file_name,
                "original_path": str(original_path),
                "quarantine_path": str(dest),
                "size_bytes": int(size_bytes or 0), "source": source_kind,
                "deleted_at": (deleted_at or datetime.now()).isoformat(timespec="seconds")}
        if self.notify:
            try:
                self.notify(item)
            except Exception as exc:
                self.log.warning("Delete-protect notify failed: %s", exc)
        return item

    def quarantine_user(self, path: str | Path) -> tuple[bool, str]:
        """Quarantine one file or folder the owner deleted from the
        Telegram explorer (v12.11). Same quarantine + the same
        হ্যাঁ/না Telegram message as a Recycle Bin delete, so the
        file/folder can be restored from /deleted. System paths and
        the quarantine folder itself are refused."""
        self._reload_settings()
        from .file_explorer import dir_size, is_system_path

        target = Path(path)
        if not target.exists():
            return False, "এটা আর নেই — হয়তো মুছে গেছে বা সরানো হয়েছে।"
        if is_system_path(target):
            return False, "সিস্টেম ফোল্ডারের কিছু মুছে ফেলা যাবে না।"
        try:
            if os.path.commonpath([target.resolve(), self.held_dir.resolve()]) \
                    == str(self.held_dir.resolve()):
                return False, "এটা ইতিমধ্যে সুরক্ষায় লুকিয়ে আছে।"
        except Exception:
            pass
        if target.is_dir():
            size, _done = dir_size(target)
        else:
            try:
                size = target.stat().st_size
            except Exception:
                size = 0
        item = self._quarantine(target, target.name, str(target), size,
                                "telegram", None)
        if item is None:
            return False, "মুছে ফেলা যায়নি — অনুমতি নেই বা ডিস্কের সমস্যা।"
        return True, (f"🗑️ মুছে ফেলা হয়েছে (সুরক্ষায় রাখা আছে): "
                      f"{target.name} — ফেরাতে /deleted দেখুন।")

    # -- Recycle Bin ----------------------------------------------------
    def scan_recycle_bins(self, bins: list[Path] | None = None) -> list[dict]:
        """Move fresh Recycle Bin items to quarantine. One scan pass."""
        found: list[dict] = []
        if not self.enabled:
            return found
        seen = self._seen()
        seen_set = set(seen)
        changed = False
        for bin_path in bins if bins is not None else find_recycle_bins():
            try:
                sid_dirs = [p for p in bin_path.iterdir() if p.is_dir()]
            except Exception:
                continue
            for sid in sid_dirs:
                try:
                    infos = [p for p in sid.iterdir()
                             if p.is_file() and p.name.startswith("$I")]
                except Exception:
                    continue
                for info_path in infos:
                    key = f"{info_path}"
                    if key in seen_set:
                        continue
                    parsed = parse_recycle_info(info_path)
                    if parsed is None:
                        # Unreadable metadata: mark seen so a broken
                        # $I is not retried forever.
                        seen.append(key)
                        seen_set.add(key)
                        changed = True
                        continue
                    original = parsed["original_path"]
                    if is_temp_path(original, str(self.quarantine_root),
                                    str(self.project_root)):
                        seen.append(key)
                        seen_set.add(key)
                        changed = True
                        continue
                    recycle_file = info_path.with_name("$R" + info_path.name[2:])
                    if not recycle_file.exists():
                        # Already emptied from the bin.
                        seen.append(key)
                        seen_set.add(key)
                        changed = True
                        continue
                    try:
                        size = parsed["size_bytes"] or (
                            recycle_file.stat().st_size if recycle_file.is_file() else 0)
                    except Exception:
                        size = parsed["size_bytes"]
                    name = Path(original.replace("\\", "/")).name or recycle_file.name
                    item = self._quarantine(recycle_file, name, original, size,
                                            "recycle_bin", parsed["deleted_at"])
                    if item is not None:
                        # Only now is it seen: if the quarantine move
                        # failed (disk full, locked file), the $I is
                        # left unmarked so the next scan retries it
                        # instead of forgetting the delete (v12.12).
                        seen.append(key)
                        seen_set.add(key)
                        changed = True
                        found.append(item)
                        # The $I entry is now orphaned — drop it so the
                        # bin does not show a broken entry.
                        try:
                            info_path.unlink()
                        except Exception:
                            pass
                    else:
                        self.log.warning(
                            "Delete protection: could not quarantine %s "
                            "(will retry next scan).", original)
        if changed:
            self._save_seen(seen)
        return found

    # -- protected-folder mirror -----------------------------------------
    def _mirror_name(self, source: Path) -> str:
        digest = hashlib.sha1(str(source).encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{digest}_{source.name}"

    def _prune_mirror(self) -> None:
        """Prune mirror copies that have NO manifest entry (orphans:
        their source vanished without a quarantine, e.g. the DB was
        reset). A mirror copy whose source still exists is NEVER
        pruned by age or size (v12.12 audit): copy2 preserves the
        source's mtime, so the old retention check deleted the
        mirror of any file untouched for 30 days — and the next
        Shift+Delete of that file was then silently unrecoverable.
        The cap is enforced when copying (scan_protected), not by
        deleting live protection."""
        try:
            cap_bytes = int(self.settings.get("quarantine_max_gb", 10) or 10) * 1024 ** 3
        except (TypeError, ValueError):
            return
        if not self.mirror_dir.is_dir():
            return
        try:
            manifest = self._mirror_manifest()
            live = {str(v[2]) for v in manifest.values() if len(v) >= 3}
            entries = []
            for p in self.mirror_dir.iterdir():
                if p.name in live:
                    continue  # still protecting a live file — keep it
                try:
                    st = p.stat()
                    entries.append((st.st_mtime, st.st_size, p))
                except Exception:
                    continue
            total = self._mirror_total_bytes()
            for mtime, _size, p in sorted(entries):
                if total <= cap_bytes:
                    break
                try:
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink()
                    total -= _size
                except Exception:
                    continue
        except Exception:
            pass

    def _find_move_target(self, source: str) -> str | None:
        """A RECENT 'moved' file event from the agent's own file
        watcher, so a rename is not mistaken for a delete. Only
        moves from the last hour count (v12.12 audit): the old code
        took the latest event of ANY age, so a file moved months ago
        could mask a real delete of a new file at the same path."""
        try:
            rows = self.db.conn.execute(
                """SELECT dest_path, occurred_at FROM file_events
                   WHERE event_type = 'moved' AND path = ?
                   ORDER BY occurred_at DESC LIMIT 1""",
                (source,)).fetchall()
        except Exception:
            return None
        if not rows:
            return None
        dest = (rows[0]["dest_path"] or "").strip()
        if not dest:
            return None
        try:
            when = datetime.fromisoformat(str(rows[0]["occurred_at"]))
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.now().astimezone().tzinfo)
            if (datetime.now().astimezone() - when).total_seconds() > 3600:
                return None  # an old move — not this disappearance
        except Exception:
            return None  # unreadable time: do not trust it as a rename
        return dest

    def _mirror_total_bytes(self) -> int:
        """Current size of the mirror folder (0 when it is missing)."""
        try:
            if not self.mirror_dir.is_dir():
                return 0
            total = 0
            for p in self.mirror_dir.iterdir():
                try:
                    total += p.stat().st_size if p.is_file() else 0
                except Exception:
                    continue
            return total
        except Exception:
            return 0

    def scan_protected(self) -> list[dict]:
        """Sync the mirror, then quarantine mirror copies whose source
        file was deleted. Returns newly quarantined items."""
        found: list[dict] = []
        if not self.enabled:
            return found
        self._ensure_dirs()
        manifest = self._mirror_manifest()
        try:
            max_mb = int(self.settings.get("mirror_max_mb", 500) or 500)
        except (TypeError, ValueError):
            max_mb = 500
        max_bytes = max_mb * 1024 * 1024
        try:
            cap_bytes = int(self.settings.get("quarantine_max_gb", 10) or 10) * 1024 ** 3
        except (TypeError, ValueError):
            cap_bytes = 10 * 1024 ** 3
        copied_files = 0
        copied_bytes = 0
        cap_warned = False
        # 1) Copy new / changed files into the mirror (budgeted).
        for folder in self.protected_folders:
            if not folder.is_dir():
                continue
            try:
                walk = list(folder.rglob("*"))
            except Exception:
                continue
            for path in walk:
                if copied_files >= _MIRROR_MAX_FILES_PER_SCAN \
                        or copied_bytes >= _MIRROR_MAX_BYTES_PER_SCAN:
                    break
                try:
                    if not path.is_file():
                        continue
                except Exception:
                    continue
                if is_temp_path(str(path), str(self.quarantine_root),
                                str(self.project_root)):
                    continue
                try:
                    st = path.stat()
                except Exception:
                    continue
                if st.st_size > max_bytes:
                    continue
                key = str(path)
                old = manifest.get(key)
                if old and old[0] == st.st_mtime_ns and old[1] == st.st_size:
                    continue  # unchanged
                # Cap: when the mirror is already full, do not copy
                # new files (and do not delete live protection to make
                # room — v12.12). The file stays unmirrored until the
                # owner frees space or raises the cap.
                if self._mirror_total_bytes() + st.st_size > cap_bytes:
                    if not cap_warned:
                        cap_warned = True
                        self.log.warning(
                            "Delete protection: mirror is full (%s cap) — "
                            "%s is not mirrored until space is freed.",
                            _fmt_size(cap_bytes), path)
                    continue
                mirror_name = self._mirror_name(path)
                dest = self.mirror_dir / mirror_name
                try:
                    shutil.copy2(str(path), str(dest))
                    manifest[key] = [st.st_mtime_ns, st.st_size, mirror_name]
                    copied_files += 1
                    copied_bytes += st.st_size
                except Exception:
                    continue
        # 2) Sources that vanished: rename, or a real delete.
        for source, (mtime_ns, size, mirror_name) in list(manifest.items()):
            if Path(source).exists():
                continue
            target = self._find_move_target(source)
            if target and Path(target).exists():
                # Renamed, not deleted: the mirror follows the file.
                mirror_path = self.mirror_dir / mirror_name
                new_name = self._mirror_name(Path(target))
                try:
                    if mirror_path.exists():
                        mirror_path.rename(self.mirror_dir / new_name)
                except Exception:
                    pass
                manifest[target] = [Path(target).stat().st_mtime_ns,
                                    Path(target).stat().st_size, new_name]
                del manifest[source]
                continue
            mirror_path = self.mirror_dir / mirror_name
            if not mirror_path.exists():
                del manifest[source]
                continue
            item = self._quarantine(mirror_path, Path(source).name, source,
                                    size, "mirror", datetime.now())
            if item is not None:
                del manifest[source]
                found.append(item)
            else:
                # The quarantine move failed (disk full, locked file):
                # keep the manifest entry so the next scan retries
                # instead of forgetting the delete (v12.12).
                self.log.warning(
                    "Delete protection: could not quarantine %s "
                    "(will retry next scan).", source)
        self._save_manifest(manifest)
        self._prune_mirror()
        return found

    def scan(self) -> list[dict]:
        """One full pass: Recycle Bins, then the protected mirror."""
        self._reload_settings()
        try:
            found = self.scan_recycle_bins()
        except Exception as exc:
            self.log.warning("Recycle Bin scan failed: %s", exc)
            found = []
        try:
            found += self.scan_protected()
        except Exception as exc:
            self.log.warning("Protected-folder scan failed: %s", exc)
        return found

    # -- owner decisions ---------------------------------------------------
    def restore(self, qid: int) -> str:
        """Move a held item back to its original path."""
        row = self.db.get_quarantined(qid) if self.db else None
        if row is None:
            return f"🗑 #{qid} বলে কোনো সুরক্ষিত ফাইল পাওয়া যায়নি।"
        if row["status"] != "held":
            return f"🗑 {row['file_name']} — এটা আর সুরক্ষায় নেই ({row['status']})।"
        src = Path(row["quarantine_path"])
        if not src.exists():
            return f"🗑 {row['file_name']} — কোয়ারেন্টাইনে ফাইলটা আর নেই।"
        dest = Path(row["original_path"])
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                dest = dest.with_name(f"{dest.stem} (ফেরত {stamp}){dest.suffix}")
            shutil.move(str(src), str(dest))
            try:
                src.parent.rmdir()  # drop the empty held/<token>/ dir
            except Exception:
                pass
            self.db.set_quarantine_status(qid, "restored")
            # Re-mirror at once, so a second delete right after the
            # restore is protected too — without waiting for the next
            # scan to notice the file is back. Only when the file fits
            # the mirror cap and is not a temp file (v12.12: the old
            # code re-mirrored any size, bypassing the cap).
            try:
                self._ensure_dirs()
                max_mb = int(self.settings.get("mirror_max_mb", 500) or 500)
                if (dest.is_file() and dest.stat().st_size <= max_mb * 1024 * 1024
                        and not is_temp_path(str(dest), str(self.quarantine_root),
                                            str(self.project_root))):
                    mirror_name = self._mirror_name(dest)
                    shutil.copy2(str(dest), str(self.mirror_dir / mirror_name))
                    st = dest.stat()
                    manifest = self._mirror_manifest()
                    manifest[str(dest)] = [st.st_mtime_ns, st.st_size, mirror_name]
                    self._save_manifest(manifest)
            except Exception:
                pass
        except Exception as exc:
            return f"🗑 ফেরানো যায়নি: {exc}"
        return f"↩️ {row['file_name']} আবার আগের জায়গায় ফিরে এসেছে — {dest}"

    def purge(self, qid: int) -> str:
        """Delete a held item for good — only ever on the owner's হ্যাঁ."""
        row = self.db.get_quarantined(qid) if self.db else None
        if row is None:
            return f"🗑 #{qid} বলে কোনো সুরক্ষিত ফাইল পাওয়া যায়নি।"
        if row["status"] != "held":
            return f"🗑 {row['file_name']} — এটা আর সুরক্ষায় নেই ({row['status']})।"
        src = Path(row["quarantine_path"])
        try:
            if src.is_dir():
                shutil.rmtree(src, ignore_errors=True)
            elif src.exists():
                src.unlink()
            try:
                src.parent.rmdir()
            except Exception:
                pass
            self.db.set_quarantine_status(qid, "purged")
        except Exception as exc:
            return f"🗑 পারমেনেন্টলি মোছা যায়নি: {exc}"
        return f"🗑 {row['file_name']} পারমেনেন্টলি মুছে ফেলা হয়েছে।"

    def held_items(self, limit: int = 50) -> list:
        try:
            return self.db.quarantined_held(limit=limit)
        except Exception:
            return []
