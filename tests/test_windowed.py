"""Regression tests for the windowed build (v10.4).

The frozen exe is built with console=False, so on Windows it has no
console window at all — not even the brief flash a console exe shows
before Python can hide it. PyInstaller then sets sys.stdout/sys.stderr
to None, and main.py must survive that: _ensure_std_streams() points
them at devnull, _fix_console_encoding() skips None streams, and the
log file keeps working.

Run:  python3 tests/test_windowed.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as main_mod


def test_spec_is_windowed() -> None:
    spec = (Path(__file__).resolve().parent.parent / "DeskAudit.spec").read_text()
    assert "console=False" in spec, "DeskAudit.spec must build windowed (console=False)"
    assert "console=True" not in spec, "DeskAudit.spec still has console=True"


def test_windowed_startup_survives_no_stdout() -> None:
    real_out, real_err = sys.stdout, sys.stderr
    try:
        # Simulate a windowed PyInstaller exe: both streams are None.
        sys.stdout = None
        sys.stderr = None
        main_mod._ensure_std_streams()
        assert sys.stdout is not None and sys.stderr is not None
        main_mod._fix_console_encoding()  # must not raise on devnull streams
        log_path = Path(tempfile.mkdtemp()) / "test.log"
        log = main_mod.setup_logging(log_path)
        log.info("windowed test line")
        print("printed to devnull without crashing")
        assert "windowed test line" in log_path.read_text(encoding="utf-8")
    finally:
        sys.stdout, sys.stderr = real_out, real_err


if __name__ == "__main__":
    test_spec_is_windowed()
    print("spec is windowed OK")
    test_windowed_startup_survives_no_stdout()
    print("windowed startup OK")
