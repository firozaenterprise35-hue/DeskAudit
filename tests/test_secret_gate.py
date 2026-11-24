"""Secret gate tests — pure state machine, no Windows hooks.

Run:  python tests/test_secret_gate.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskaudit.secret_gate import SecretGate


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def advance(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


def test_four_clicks_then_code_opens():
    clock = FakeClock()
    fired = []
    gate = SecretGate(clicks=4, code="1122", clock=clock, on_trigger=lambda: fired.append(True))
    assert gate.click(True) is False
    assert gate.click(True) is False
    assert gate.click(True) is False
    assert gate.click(True) is True  # 4th click arms it
    assert gate.is_armed()
    assert gate.key("1") is False
    assert gate.key("1") is False
    assert gate.key("2") is False
    assert gate.key("2") is True
    assert fired == [True]
    assert not gate.is_armed()  # one-shot: disarms after firing


def test_wrong_key_disarms():
    clock = FakeClock()
    gate = SecretGate(clock=clock)
    for _ in range(4):
        gate.click(True)
    assert gate.key("1") is False
    assert gate.key("9") is False  # wrong digit ends the attempt
    assert not gate.is_armed()
    assert gate.key("2") is False  # typing after disarm does nothing


def test_clicks_must_be_close_together():
    clock = FakeClock()
    gate = SecretGate(click_window_seconds=2, clock=clock)
    gate.click(True)
    clock.advance(3)  # too slow — the streak is gone
    gate.click(True)
    gate.click(True)
    assert gate.click(True) is False  # only 3 in the window, not armed
    assert gate.click(True) is True


def test_non_desktop_click_breaks_the_streak():
    clock = FakeClock()
    gate = SecretGate(clock=clock)
    gate.click(True)
    gate.click(True)
    gate.click(False)  # clicked inside an app window — streak resets
    gate.click(True)
    gate.click(True)
    assert gate.click(True) is False
    assert gate.click(True) is True


def test_code_must_be_typed_in_time():
    clock = FakeClock()
    gate = SecretGate(arm_seconds=10, clock=clock)
    for _ in range(4):
        gate.click(True)
    clock.advance(11)  # waited too long
    assert not gate.is_armed()
    assert gate.key("1") is False


def test_config_defaults():
    from deskaudit.config import Config

    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.yaml").write_text("poll_interval_seconds: 5\n", encoding="utf-8")
    cfg = Config.load(tmp / "config.yaml")
    assert cfg.dashboard_on_start is True
    assert cfg.secret_gate["enabled"] is True
    assert cfg.secret_gate["clicks"] == 4
    assert cfg.secret_gate["code"] == "1122"


if __name__ == "__main__":
    test_four_clicks_then_code_opens()
    test_wrong_key_disarms()
    test_clicks_must_be_close_together()
    test_non_desktop_click_breaks_the_streak()
    test_code_must_be_typed_in_time()
    test_config_defaults()
    print("secret gate tests passed")
