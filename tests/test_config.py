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


if __name__ == "__main__":
    test_config_local_overlay()
