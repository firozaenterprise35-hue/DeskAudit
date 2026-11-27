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
from datetime import datetime, timedelta
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


def list_drives() -> list[str]:
    """Drive letters with a root, e.g. ['C:', 'D:']. Windows only
    in practice; elsewhere just the filesystem root."""
    if sys.platform != "win32":
        return ["/"]
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(256)
        n = ctypes.windll.kernel32.GetLogicalDriveStringsW(256, buf)
        drives = []
        for chunk in buf.value.split("\x00"):
            if len(chunk) >= 2 and chunk[1] == ":":
                drives.append(chunk[:2])
        if drives:
            return drives
    except Exception:
        pass
    return [f"{letter}:" for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
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

    # -- paths ------------------------------------------------------
    @property
    def quarantine_root(self) -> Path:
        raw = str(self.settings.get("quarantine_path") or "").strip()
        if raw:
            return Path(os.path.expandvars(os.path.expanduser(raw)))
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
                    seen.append(key)
                    seen_set.add(key)
                    changed = True
                    parsed = parse_recycle_info(info_path)
                    if parsed is None:
                        continue
                    original = parsed["original_path"]
                    if is_temp_path(original, str(self.quarantine_root),
                                    str(self.project_root)):
                        continue
                    recycle_file = info_path.with_name("$R" + info_path.name[2:])
                    if not recycle_file.exists():
                        continue  # already emptied from the bin
                    try:
                        size = parsed["size_bytes"] or (
                            recycle_file.stat().st_size if recycle_file.is_file() else 0)
                    except Exception:
                        size = parsed["size_bytes"]
                    name = Path(original.replace("\\", "/")).name or recycle_file.name
                    item = self._quarantine(recycle_file, name, original, size,
                                            "recycle_bin", parsed["deleted_at"])
                    if item is not None:
                        found.append(item)
                        # The $I entry is now orphaned — drop it so the
                        # bin does not show a broken entry.
                        try:
                            info_path.unlink()
                        except Exception:
                            pass
        if changed:
            self._save_seen(seen)
        return found

    # -- protected-folder mirror -----------------------------------------
    def _mirror_name(self, source: Path) -> str:
        digest = hashlib.sha1(str(source).encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{digest}_{source.name}"

    def _prune_mirror(self) -> None:
        """Prune old mirror copies past retention / cap. Held items
        are never touched — only the owner can purge those."""
        try:
            cap_bytes = int(self.settings.get("quarantine_max_gb", 10) or 10) * 1024 ** 3
            retention = timedelta(days=int(self.settings.get("mirror_retention_days", 30) or 30))
        except (TypeError, ValueError):
            return
        if not self.mirror_dir.is_dir():
            return
        try:
            entries = []
            for p in self.mirror_dir.iterdir():
                try:
                    st = p.stat()
                    entries.append((st.st_mtime, st.st_size, p))
                except Exception:
                    continue
            now = time.time()
            total = sum(size for _, size, _ in entries)
            for mtime, _size, p in sorted(entries):
                over_cap = total > cap_bytes
                too_old = now - mtime > retention.total_seconds()
                if not (over_cap or too_old):
                    continue
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
        """A 'moved' file event from the agent's own file watcher, so
        a rename is not mistaken for a delete."""
        try:
            rows = self.db.conn.execute(
                """SELECT dest_path FROM file_events
                   WHERE event_type = 'moved' AND path = ?
                   ORDER BY occurred_at DESC LIMIT 1""",
                (source,)).fetchall()
        except Exception:
            return None
        if not rows:
            return None
        dest = (rows[0]["dest_path"] or "").strip()
        return dest or None

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
        copied_files = 0
        copied_bytes = 0
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
            del manifest[source]
            if item is not None:
                found.append(item)
        self._save_manifest(manifest)
        self._prune_mirror()
        return found

    def scan(self) -> list[dict]:
        """One full pass: Recycle Bins, then the protected mirror."""
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
            # scan to notice the file is back.
            try:
                self._ensure_dirs()
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
