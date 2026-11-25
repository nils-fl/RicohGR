"""Tests for tools/recipes/recipe_card.py solo runs (run: pytest tools/tests)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes")]
import recipe_card as rc  # noqa: E402


def batches_of(script: bytes):
    return re.findall(r"b <<'END_B'\n(.*?)END_B\n", script.decode(), re.S)


def test_solo_batches_fit_one_sector_with_room_for_the_pad():
    steps = [("log", "shot 1 standard at 0, +2.0 EV")]
    steps += [("cmd", f"UserData Set StillImageControlNo1ParamStandardContrastHighLight P{i % 4}", 300)
              for i in range(30)]
    steps += [("log", "press"), ("cmd", "SW_sws 1", 1500), ("cmd", "SW_swr 1", 300)]
    script = rc.solo_script("TEST", steps)
    bodies = batches_of(script)
    assert len(bodies) > 3
    for body in bodies:
        assert rc.solo_frame_bytes() + len(body) <= rc.RELAY_SIZE - 24
    sent = [l for body in bodies for l in body.splitlines() if l.startswith("sendln")]
    assert len(sent) == 32
    # a log step ends a batch: the press is in a batch of its own
    assert bodies[-1].startswith("sendln 'SW_sws 1'")


def test_solo_refuses_unsafe_text():
    with pytest.raises(ValueError):
        rc.solo_script("T", [("log", "a'b")])
    with pytest.raises(ValueError):
        rc.solo_script("T", [("cmd", "UserData Set X 'y'", 300)])


def test_solo_sh_steps_run_between_batches():
    steps = [("cmd", "Af_Led_On", 300), ("sh", "$w/B.BIN; log patch B $?"), ("cmd", "Af_Led_Off", 300)]
    script = rc.solo_script("T", steps).decode()
    assert len(batches_of(script.encode())) == 2
    assert script.index("END_B\n$w/B.BIN; log patch B $?\nb <<'END_B'") > 0
    with pytest.raises(ValueError):
        rc.solo_script("T", [("sh", "a\nb")])


def test_remote_sh_and_solo_parse():
    files = rc.card_files()
    remote = files[f"{rc.CARD_DIR}/REMOTE.SH"].decode()
    assert "solo ;;" in remote and "SOLO.LIB" in remote
    assert "for s in SMOKE SESSION SESSION2;" in remote   # the first armed one runs
    for text in (remote, rc.solo_lib(), rc.solo_script("T", [("cmd", "Af_Led_On", 300)]).decode()):
        r = subprocess.run(["sh", "-n"], input=text, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
