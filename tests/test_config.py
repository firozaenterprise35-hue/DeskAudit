"""Config tests: defaults, config.yaml values, config.local.yaml overlay.

config.local.yaml is git-ignored and is where secrets belong (llm.api_key).
A local llm.api_key must NOT wipe the other llm settings from config.yaml.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.config import Config


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_config_local_overlay():
    tmp = Path(tempfile.mkdtemp())
    _write(tmp / "config.yaml", (
        "poll_interval_seconds: 7\n"
        "llm:\n"
        "  enabled: false\n"
        "  provider: gemini\n"
        "  model: gemini-3.5-flash-lite\n"
        "  api_key: ''\n"
    ))
    _write(tmp / "config.local.yaml", (
        "llm:\n"
        "  api_key: 'AIzaLocalTestKey'\n"
    ))
    cfg = Config.load(tmp / "config.yaml")
    assert cfg.poll_interval_seconds == 7
    assert cfg.llm["provider"] == "gemini", cfg.llm
    assert cfg.llm["model"] == "gemini-3.5-flash-lite", cfg.llm
    assert cfg.llm["api_key"] == "AIzaLocalTestKey", cfg.llm
    # Without a local file, defaults + config.yaml apply unchanged.
    tmp2 = Path(tempfile.mkdtemp())
    _write(tmp2 / "config.yaml", "poll_interval_seconds: 9\n")
    cfg2 = Config.load(tmp2 / "config.yaml")
    assert cfg2.poll_interval_seconds == 9
    assert cfg2.llm == {}
    print("config local overlay tests passed")


def test_effective_watch_folders_includes_protected():
    # v12.13 (M10): delete_protect.protected_folders (set in Dashboard
    # Settings) was never read anywhere — a Shift+Delete there was
    # unrecoverable, with no warning.
    tmp = Path(tempfile.mkdtemp())
    accounts = tmp / "Accounts"
    accounts.mkdir()
    _write(tmp / "config.yaml", (
        "watch_folders:\n"
        f"  - {tmp / 'Desktop'}\n"
        "transfers:\n"
        "  sensitive_folders: []\n"
        "delete_protect:\n"
        "  protected_folders:\n"
        f"  - {accounts}\n"
    ))
    cfg = Config.load(tmp / "config.yaml")
    folders = [str(p) for p in cfg.effective_watch_folders()]
    assert str(accounts) in folders, folders
    print("effective watch folders include protected passed")


def test_coerce_bool_and_min_int():
    """v12.13 (C-m6/C-m7): quoted "false" in YAML must not become True,
    and a 0 poll interval must not busy-loop."""
    import tempfile
    from pathlib import Path

    from deskaudit.config import Config

    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text(
        'browser_enabled: "false"\npoll_interval_seconds: 0\n'
        'idle_threshold_seconds: 1\nbrowser_poll_seconds: 0\n',
        encoding="utf-8")
    cfg = Config.load(tmp / "config.yaml")
    assert cfg.browser_enabled is False
    assert cfg.poll_interval_seconds == 1
    assert cfg.idle_threshold_seconds == 10
    assert cfg.browser_poll_seconds == 5
    print("coerce bool and min int tests passed")



if __name__ == "__main__":
    test_config_local_overlay()
    test_effective_watch_folders_includes_protected()
    test_coerce_bool_and_min_int()

