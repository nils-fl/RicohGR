"""Focus probe card (tools/focus/focus_probe.py); the card tests need the decoded 1.60 image."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

import focus_probe as fp  # noqa: E402
import recipe_card as rc  # noqa: E402


def test_snap_word_is_movw_r1():
    assert fp.snap_word(30) == 0xE300101E
    assert fp.snap_word(45) == 0xE300102D
    assert fp.snap_word(4096) == 0xE3011000
    assert fp.snap_word(10000) == 0xE3021710
    for bad in (0, 9, 10001, -1):
        with pytest.raises(ValueError):
            fp.snap_word(bad)


def test_matcher_facts_hold_in_the_image():
    assert fp.check() == []


def test_apply_sites_are_movw_r1_with_the_stock_conditions():
    assert fp.apply_sites(450) == [(0x532AF4D4, 0x03A01F4B, 0x030011C2), (0x532AF4F8, 0xE3A01E19, 0xE30011C2)]
    for bad in (99, 0x10000):
        with pytest.raises(ValueError):
            fp.apply_sites(bad)


@pytest.mark.parametrize("probe", sorted(fp.PROBES))
def test_card_patches_and_restores_around_every_patched_frame(tmp_path, probe):
    frames = fp.PROBES[probe]
    files, steps, plan = fp.card_files(probe=probe)
    want = {f"{fp.FOCUS_DIR}/{fp.bin_name(cm, how)}" for cm, how, _ in frames if how != "stock"}
    assert {k for k in files if k.startswith(fp.FOCUS_DIR)} == want
    script = files[f"{rc.CARD_DIR}/SESSION.SH"].decode()
    (tmp_path / "s.sh").write_text(script)
    assert subprocess.run(["sh", "-n", str(tmp_path / "s.sh")]).returncode == 0
    sh = [s[1] for s in steps if s[0] in ("sh", "cmd")]
    # every patch: "... 30", (for apply: the Focus switch), the restore, and only then the shutter press
    patches = [i for i, s in enumerate(sh) if s.startswith(fp.RAM) and " restore" not in s.split(";")[0]]
    assert len(patches) == sum(how != "stock" for _, how, _ in frames)
    for i in patches:
        name = sh[i].split(";")[0]
        assert sh[i + 1] == "UserData Set StillSnapDistanceCm 30"
        r = next(j for j in range(i, len(sh)) if sh[j].startswith(f"{name} restore"))
        press = next(j for j in range(i, len(sh)) if sh[j] == "SW_swr 1")
        assert r < press
        if "/A" in name:
            assert sh[r - 1] == "UserData Set StillFocus Snap" and sh[r - 2] == "UserData Set StillFocus Infinity"
    tail = [s for s in sh if s.startswith("UserData Set")][-2:]
    assert tail == ["UserData Set StillSnapDistanceCm 250", "UserData Set StillFocus Select"]
    for s in sh:
        m = re.fullmatch(r"UserData Set StillSnapDistanceCm (\S+)", s)
        if m:
            assert m.group(1) == "Infinity" or int(m.group(1)) in fp.MENU_CM
    assert len(plan) == len(frames) == sum(s == ("cmd", "SW_swr 1", 300) for s in steps)


def test_awk_programs_never_put_a_name_before_a_parenthesis():
    """busybox awk reads "k (" as a call of a user function k (probe 1 lost its props this way)."""
    builtins = {"if", "match", "substr", "sub", "gsub", "tolower", "length", "print"}
    for prog in (fp.PROPS_SH, fp.PHOTOS_SH):
        body = prog[prog.index("awk"):]
        for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body):
            assert name in builtins, name


def test_card_rejects_unknown_restore_values():
    with pytest.raises(SystemExit):
        fp.steps_for(focus_after="Spot")
    with pytest.raises(SystemExit):
        fp.steps_for(snap_after=45)


def test_props_filter_never_logs_private_values(tmp_path):
    api = tmp_path / "api.txt"
    api.write_text('HTTP/1.0 200 OK\r\n\r\n{"errCode":200,"ssid":"SECRET_A","key":"SECRET_B","serialNo":"SECRET_C",'
                   '"focusSetting":"snap","captureInfo":{"ssid":"SECRET_D","state":"idle"},'
                   '"storages":[{"name":"SECRET_E","remain":12}],"macAddress":"SECRET_F"}\n')
    body = fp.PROPS_SH.replace("busybox ", "")
    sh = (f"w={tmp_path}\nlog() {{ echo \"$*\"; }}\napi() {{ :; }}\n{body}\nprops t\n")
    out = subprocess.run(["sh", "-c", sh], capture_output=True, text=True, check=True).stdout
    assert "SECRET" not in out
    assert "focusSetting=snap" in out and "ssid=hidden" in out and "state=idle" in out


ASKED = [100 / 30, 100 / 45, 100 / 70, 1.0, 100 / 120, 100 / 150, 0.4, 0.2, 0.0]
STOCK = {0: ASKED[0], 3: ASKED[3], 5: ASKED[5], 6: ASKED[6], 7: ASKED[7], 8: ASKED[8]}


def scene(focus, seed=1):
    """Synthetic frames: 400 tiles at depths 0..3.6 dioptres; sharpness falls off slowly with the
    dioptre error (the model's form), times 10 % noise."""
    import numpy as np
    rng = np.random.default_rng(seed)
    depth = rng.uniform(0.0, 3.6, 400)
    contrast = rng.uniform(0.5, 2.0, 400)
    return np.array([contrast * (0.03 + 1 / (1 + ((depth - d) / 0.25) ** 2)) * rng.lognormal(0, 0.1, 400)
                     for d in focus])


def test_estimate_finds_the_focus_of_frames_between_stock_ones():
    est, info = fp.estimate(scene(ASKED), STOCK)
    for want, got in zip(ASKED, est):
        assert abs(got - want) < 0.2, (want, got)
    assert 0.15 < info["w"] < 0.4


def test_estimate_shows_a_patch_that_does_nothing():
    """If the camera ignored the patched values, frames 1, 2 and 4 are really at 30 cm."""
    focus = list(ASKED)
    focus[1] = focus[2] = focus[4] = ASKED[0]
    est, _ = fp.estimate(scene(focus, seed=2), STOCK)
    for f in (1, 2, 4):
        assert abs(est[f] - ASKED[0]) < 0.2, (f, est[f])
    sharp = scene(focus, seed=2)
    assert fp.similarity(sharp, 1, 0) > 0.9 > fp.similarity(scene(ASKED, seed=2), 1, 0)
