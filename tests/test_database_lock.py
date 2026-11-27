"""Database concurrency tests (v12.13, C3): one SQLite connection is
shared by the watchdog thread, the main loop and the Telegram poller —
writes from several threads must never be lost or raise
InterfaceError / 'cannot start a transaction within a transaction'.
"""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.database import Database  # noqa: E402


def test_concurrent_writes():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "stress.db")
    errors = []

    def writer(n):
        try:
            for i in range(200):
                db.log_file_event("created", f"/tmp/w{n}/f{i}.txt")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    count = db.conn.execute("SELECT COUNT(*) c FROM file_events").fetchone()["c"]
    db.close()
    assert not errors, errors
    assert count == 800, f"only {count}/800 rows survived concurrent writes"
    print("concurrent writes passed")


def test_concurrent_mixed_read_write():
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "mixed.db")
    errors = []
    stop = threading.Event()

    def writer():
        try:
            i = 0
            while not stop.is_set():
                db.log_file_event("modified", f"/tmp/m/f{i}.txt")
                i += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            while not stop.is_set():
                db.conn.execute("SELECT COUNT(*) c FROM file_events").fetchone()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer),
               threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for t in threads:
        t.start()
    import time
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join()
    db.close()
    assert not errors, errors
    print("concurrent mixed read/write passed")


if __name__ == "__main__":
    test_concurrent_writes()
    test_concurrent_mixed_read_write()
    print("ALL DATABASE LOCK TESTS PASSED")
