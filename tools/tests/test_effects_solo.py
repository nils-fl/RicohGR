"""Automated grain session (tools/effects_solo.py); needs the decoded 1.60 image."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/gr_unpack.py)", allow_module_level=True)

import effects_card as ec  # noqa: E402
import effects_solo as es  # noqa: E402
import recipe_card as rc  # noqa: E402


@pytest.fixture(scope="module")
def built():
    files, steps, plan = es.card_files(user_recipe=None)
    return files, files[f"{rc.CARD_DIR}/SESSION.SH"].decode(), plan


def test_card_is_the_recipe_card_plus_one_armed_session(built):
    files, _, _ = built
    assert set(files) - set(rc.card_files()) == {f"{rc.CARD_DIR}/SESSION.SH", f"{rc.CARD_DIR}/SESSION.ON",
                                                 f"{es.FX}/A.BIN", f"{es.FX}/B.BIN"}
    assert files[f"{es.FX}/B.BIN"] == ec.card.multipatch_bin(ec.arm_sites()["B"])


def test_patches_sit_between_the_right_shots(built):
    _, script, plan = built
    order = re.findall(r"^(log shot \d+ \w+|sleep .*BIN(?: restore)?;)", script, re.M)
    marks = [o for o in order if "BIN" in o]
    assert [m.split("/")[-1] for m in marks] == ["B.BIN;", "B.BIN restore;", "A.BIN;"]
    phases = [p["phase"] for p in plan]
    i_b, i_r, i_a = (order.index(m) for m in marks)
    assert all(o.endswith("stock") for o in order[:i_b] if o.startswith("log"))
    assert all(o.endswith(" B") for o in order[i_b + 1:i_r])
    assert order[i_r + 1].endswith("check") and i_a == len(order) - 1
    assert phases == ["stock"] * 6 + ["B"] * 4 + ["check"]
    assert all(f"sleep {es.DEVELOP_WAIT_S};" in l for l in script.splitlines() if ".BIN" in l and "cp " not in l)


def test_each_shot_presses_once_and_sets_its_grain(built):
    _, script, plan = built
    assert script.count("sendln 'SW_swr 1'") == len(plan)
    assert "MonotoneGRParticle High" in script and "NegativeFilmParticle High" in script
    assert "camctl_set_iso 6" in script and script.rstrip().endswith("log done SESSION")


def test_script_parses(built):
    _, script, _ = built
    r = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
