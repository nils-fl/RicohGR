"""Focus bracket planner and card (tools/focus/focus_bracket.py); the card tests need the decoded 1.60 image."""
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

import focus_bracket as fb  # noqa: E402
import focus_probe as fp  # noqa: E402
import recipe_card as rc  # noqa: E402


@pytest.mark.parametrize("near,far,f", [(0.25, math.inf, 5.6), (1.0, math.inf, 8.0), (0.2, 0.5, 8.0),
                                        (0.3, 3.0, 2.8), (2.0, math.inf, 11.0)])
def test_plan_covers_near_to_far_with_overlapping_zones(near, far, f):
    ds = fb.plan(near, far, f)
    assert ds == sorted(ds, reverse=True)
    z = fb.zones(ds, f)
    assert z[0][0] >= fb.dioptre(near) - 1e-9           # the near point is sharp
    assert z[-1][1] <= fb.dioptre(far) + 1e-9            # and the far point (0 = infinity)
    for (_, far_edge), (near_edge, _) in zip(z, z[1:]):
        assert near_edge > far_edge                      # each zone overlaps the next


def test_plan_hyperfocal_and_limits():
    h = fb.half_zone(5.6)
    assert 1 / h == pytest.approx(6.08, abs=0.01)        # exiftool gave 6.18 m for these photos
    assert fb.plan(2.0, math.inf, 16.0) == [pytest.approx(0.25)]   # one frame covers it
    with pytest.raises(ValueError):
        fb.plan(0.1, math.inf, 5.6)                      # closer than tested
    with pytest.raises(ValueError):
        fb.plan(1.0, 0.5, 5.6)
    with pytest.raises(ValueError):
        fb.plan(0.2, math.inf, 2.8)                      # too many frames


def test_slot_words_hold_in_the_image():
    assert fb.check() == []
    assert [s for s, _ in fb.SLOTS] == list(fp.MENU_CM)


def test_round_sites_map_each_menu_value_to_one_frame():
    sites = fb.round_sites([261, 278, 299])
    assert sites[:2] == [(0x532AF4D4, 0x03A01F4B, 0x03001105), (0x532AF4F8, 0xE3A01E19, 0xE3001105)]
    assert sites[2] == (0x532AF4C8, 0xE3A01FFA, 0xE3001116)
    assert sites[3] == (0x532AF4B4, 0x030015DC, 0x0300112B)
    with pytest.raises(ValueError):
        fb.round_sites([300] * 8)
    with pytest.raises(ValueError):
        fb.round_sites([50])


def test_card_rounds(tmp_path):
    ds = fb.plan(0.25, math.inf, 5.6)
    files, steps, rows = fb.card_files(ds)
    assert len(rows) == len(ds) == 16
    assert sorted(k for k in files if k.startswith(fb.BRACKET_DIR)) == [f"{fb.BRACKET_DIR}/R{k}.BIN" for k in (1, 2, 3)]
    (tmp_path / "s.sh").write_text(files[f"{rc.CARD_DIR}/SESSION.SH"].decode())
    assert subprocess.run(["sh", "-n", str(tmp_path / "s.sh")]).returncode == 0
    sh = [s[1] for s in steps if s[0] in ("sh", "cmd")]
    # round k: patch, then per frame a menu value, Infinity, Snap, shutter; the stock words back afterwards
    for k in (1, 2, 3):
        p = sh.index(f"{fb.RAM}/R{k}.BIN; log patch R{k}.BIN $?")
        r = sh.index(f"{fb.RAM}/R{k}.BIN restore; log restore R{k}.BIN $?")
        inside = sh[p + 1:r]
        values = [int(m.group(1)) for s in inside for m in [re.fullmatch(r"UserData Set StillSnapDistanceCm (\d+)", s)] if m]
        assert values == [cm for cm, _ in fb.SLOTS][:len(values)]
        assert inside.count("SW_swr 1") == len(values) == sum(r_["round"] == k for r_ in rows)
        assert not any(s.startswith(fb.RAM) for s in inside)
    last_set = len(sh) - 1 - sh[::-1].index("UserData Set StillSnapDistanceCm 250")   # 250 is also a frame slot
    assert sh.index(f"{fb.RAM}/R3.BIN restore; log restore R3.BIN $?") < last_set
    assert [s for s in sh if s.startswith("UserData Set")][-2:] == ["UserData Set StillSnapDistanceCm 250",
                                                                     "UserData Set StillFocus Select"]
    # every frame: the menu value, then the Focus switch, then the press
    for i, s in enumerate(sh):
        if s.startswith("UserData Set StillSnapDistanceCm") and i < len(sh) - 3 and sh[i + 1].endswith("Infinity"):
            assert sh[i + 2] == "UserData Set StillFocus Snap" and sh[i + 3] == "SW_sws 1"


def test_fit_line_flags_a_frame_off_the_line():
    ds = [3.8, 3.2, 2.6, 2.0, 1.4, 0.8, 0.2]
    pos = [17500 + 1200 * d for d in ds]
    a, b, res = fb.fit_line(ds, pos)
    assert b == pytest.approx(1200) and max(map(abs, res)) < 1e-6
    pos[3] += 400                                         # one frame 0.33 D away
    a, b, res = fb.fit_line(ds, pos)
    assert max(range(len(res)), key=lambda i: abs(res[i])) == 3
