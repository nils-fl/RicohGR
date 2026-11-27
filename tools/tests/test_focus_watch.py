"""Focus modes watcher (tools/focus/focus_watch.py): the shell's patch building and planner against the
Python versions, the macro stack, the card, the tag clean-up and BusyBox limits. The snap meter's tests are in
test_snap_meter.py; the mocks in watch_harness.py. Needs the decoded 1.60 image."""
import math
import os
import re
import subprocess
from pathlib import Path

import pytest

if not (Path(__file__).resolve().parents[2] / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

from watch_harness import *  # noqa: E402,F401,F403


@pytest.mark.parametrize("mms", [[261], [261, 278, 299], [300, 612, 1000, 3000, 4095, 4096, 65535],
                                 [200, 201, 202, 203, 204, 205, 206]])
def test_shell_fills_the_templates_like_the_bracket_files(tmp_path, mms):
    f, w = funcs_only(tmp_path)
    r = shell(tmp_path, f + f'mkround $W/R.BIN {len(mms)} "{" ".join(map(str, mms))}"; echo rc=$?\n')
    assert "rc=0" in r.stdout, r.stderr
    assert (w / "R.BIN").read_bytes() == card.multipatch_bin(fb.round_sites(mms))
    assert os.access(w / "R.BIN", os.X_OK)   # the camera refused a rebuilt file without it (exit 126)
    elf, rows = TEMPLATES[len(mms)]
    assert fw.fill(elf, rows, mms) == card.multipatch_bin(fb.round_sites(mms))


@pytest.mark.parametrize("j,mm", [(0, 829), (0, 261), (2, 1745), (6, 12164)])
def test_shell_fills_a_single_slot_template(tmp_path, j, mm):
    """The snap meter rewrites one slot alone (both sites of the 30 cm slot)."""
    f, w = funcs_only(tmp_path)
    r = shell(tmp_path, f + f'mkround $W/M.BIN S{j} "{mm}"; echo rc=$?\n')
    assert "rc=0" in r.stdout, r.stderr
    want = card.multipatch_bin([(a, stock, fp.movw_r1(mm, cond)) for a, stock, cond in fb.SLOTS[j][1]])
    assert (w / "M.BIN").read_bytes() == want and os.access(w / "M.BIN", os.X_OK)


@pytest.mark.parametrize("mms", [[99], [70000], [300, 50]])
def test_shell_refuses_out_of_range_millimetres(tmp_path, mms):
    f, _ = funcs_only(tmp_path)
    r = shell(tmp_path, f + f'mkround $W/R.BIN {len(mms)} "{" ".join(map(str, mms))}"; echo rc=$?\n')
    assert "rc=1" in r.stdout


@pytest.mark.parametrize("near,far,f", [(300, 3000, 2.8), (261, 6082, 5.6), (430, 1400, 2.8), (200, 500, 8.0),
                                        (250, 60000, 5.6)])
def test_planner_matches_focus_bracket(tmp_path, near, far, f):
    got = list(map(int, awk_out(tmp_path, f"planmm {near} {far} {f}")))
    want = [fb.to_mm(d) for d in fb.plan(near / 1000, math.inf if far >= 60000 else far / 1000, f)]
    if len(want) >= fw.MIN_FRAMES:
        assert len(got) == len(want) and all(abs(a - b) <= 1 for a, b in zip(got, want))
    else:
        assert len(got) == fw.MIN_FRAMES and got[0] == near
    assert got == sorted(got)


def test_planner_order_cap_and_defaults(tmp_path):
    assert awk_out(tmp_path, "planmm 1400 430 2.8") == awk_out(tmp_path, "planmm 430 1400 2.8")   # far first
    assert len(awk_out(tmp_path, "planmm 200 60000 2.8")) == fb.MAX_FRAMES                        # capped
    assert awk_out(tmp_path, "planmm 430 1400 ''") == awk_out(tmp_path, "planmm 430 1400 2.8")    # no av


def test_macro_two_photos_then_the_bracket_between_them(tmp_path):
    rows = [(1, "U1", "5.6", 7000, -1, 1, 100), (2, "U1", "5.6", 7001, 430, 1, 105),
            (3, "U1", "5.6", 7002, 1400, 1, 110)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    want = list(map(int, awk_out(tmp_path, "planmm 430 1400 5.6")))
    assert "macro near 430" in log and "macro far 1400" in log
    assert f"bracket near 430 far 1400 av 5.6 frames {len(want)}" in log
    assert [int(m) for m in re.findall(r"frame \d+ (\d+)", log)] == want
    assert frames(relay) == (fw.SLOT_CM * 5)[:len(want)]                      # each round uses the slots in order
    rounds = (len(want) + fb.ROUND - 1) // fb.ROUND
    assert patch.count("R.BIN \n") == rounds == patch.count("R.BIN restore")
    tail = relay[relay.rindex("SW_sws 0"):]
    assert "StillSnapDistanceCm 250" in tail and "StillFocus Select" in tail
    # tag on (empty copyright): every frame tagged, then cleared and attach back to Off
    assert relay.count("SetupCopyright GRFS:7002:") == len(want)
    assert f"SetupCopyright GRFS:7002:1/{len(want)}:{want[0]}'" in relay
    assert "SetupCopyright x'" in tail and "SetupCopyrightAttach Off" in tail
    assert patch.count("CPTAG.BIN \n") == 1 == patch.count("CPTAG.BIN restore")        # once per bracket
    assert patch.index("CPTAG.BIN restore") < patch.index("CPEMPTY.BIN \n")          # both patch 0x53330d0c
    assert relay.index("SetupCopyrightAttach On") < relay.index("SetupCopyright GRFS")
    # the marker for a power-off mid-bracket: asked for in RAM (PULL.SH writes the card), then removed
    w = tmp_path / "w"
    assert (w / "tag.put").read_text() == "1\n" and (w / "tag.del").exists() and not (w / "TAG").exists()
    assert re.search(rf"bracket done {len(want)} of {len(want)} in \d+s", log) and log.count("bracket near") == 1


def test_macro_stops_when_the_dial_turns(tmp_path):
    rows = [(1, "U1", "2.8", 7000, -1, 1, 100), (2, "U1", "2.8", 7001, 300, 1, 105),
            (3, "U1", "2.8", 7002, 3000, 1, 110), (4, "P", "2.8", 7009, 3000, 1, 130)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "bracket stopped dial P" in log
    assert len(frames(relay)) == fb.ROUND
    assert patch.count("R.BIN \n") == 1 == patch.count("R.BIN restore")
    tail = relay[relay.rindex("SW_sws 0"):]
    # P recalls its own settings: none are sent into it; the tag (a setup setting) is still cleared
    assert "StillFocus" not in tail and "StillSnapDistanceCm" not in tail
    assert "SetupCopyright x'" in tail and "CPTAG.BIN restore" in patch


def test_macro_forgets_the_near_point_after_the_timeout(tmp_path):
    rows = [(1, "U1", "5.6", 7000, -1, 1, 100), (2, "U1", "5.6", 7001, 430, 1, 105),
            (3, "U1", "5.6", 7001, 430, 1, 200), (4, "U1", "5.6", 7002, 1400, 1, 210)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "macro timeout" in log and "macro far" not in log
    assert log.count("macro near") == 2 and "bracket" not in log


def test_a_set_copyright_means_no_tag(tmp_path):
    rows = [(1, "U1", "8", 7000, -1, 1, 100), (2, "U1", "8", 7001, 500, 1, 105), (3, "U1", "8", 7002, 900, 1, 110)]
    log, relay, patch = run_watch(tmp_path, rows, cpy_first=ord("N"), limit=5)
    assert "tag off copyright set peek 0 bytes 4" in log
    assert "SetupCopyright" not in relay and "CPTAG" not in patch and "CPEMPTY" not in patch
    assert "bracket done" in log


def test_card_and_script():
    files = fw.card_files()
    # The camera's Linux saw Ts0.BIN under another name (meter build failed): upper-case names only.
    assert all(n == n.upper() for n in files if n.startswith(fw.WATCH_DIR)), [n for n in files if n != n.upper()]
    assert {f"{fw.WATCH_DIR}/TS{j}.BIN" for j in range(7)} <= set(files)
    assert f"{rc.CARD_DIR}/SESSION.ON" not in files
    assert {f"{fw.WATCH_DIR}/T{k}.BIN" for k in range(1, 8)} <= set(files)
    r = subprocess.run(["sh", "-n"], input=files[f"{rc.CARD_DIR}/WATCH.SH"], capture_output=True)
    assert r.returncode == 0, r.stderr
    remote = files[f"{rc.CARD_DIR}/REMOTE.SH"].decode()
    assert "WATCH.SH" in remote and f"cp $d/WATCH/* {rc.WWW}/watch/" in remote
    assert fw.watch_sh("U3", "none").count("ZMACRO=U3; ZMETER=none") == 1


def test_frame_batch_fits_the_relay():
    frame = re.search(r'frame\(\) \{ printf "(.*?)" \$1', SCRIPT).group(1).replace("\\n", "\n").replace("%s", "500")
    assert len(frame) + 120 <= rc.RELAY_SIZE - 24


def test_variables_do_not_clash_with_solo_lib():
    lib = set(re.findall(r"\b([a-z]\w*)=", rc.solo_lib()))
    body = re.sub(r"-v \w+=", "", SCRIPT.split(f"W={fw.RAM}", 1)[1])
    ours = set(re.findall(r"(?<![\w$-])([a-z]\w*)=", body)) - {"bs", "seek", "skip", "count", "conv", "of", "if"}
    assert ours and not (ours & lib), ours & lib


def test_awk_programs_have_no_name_before_a_parenthesis():
    builtins = {"if", "match", "substr", "sub", "gsub", "tolower", "length", "print", "exit", "int", "for", "printf", "split", "while"}
    for prog in re.findall(r"busybox awk [^']*'([^']*)'", SCRIPT):
        for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", prog):
            assert name in builtins, (name, prog)


LINKS = ROOT / "work" / "initramfs" / "etc" / "busybox.links.nosuid"


@pytest.mark.skipif(not LINKS.exists(), reason="camera initramfs not extracted")
def test_every_busybox_applet_exists_on_the_camera():
    """The camera runs BusyBox 1.24.1 with its own applet list; the Mac's tools hid a missing one before."""
    have = {Path(l).name for l in LINKS.read_text().split()}
    used = set(re.findall(r"busybox (\w+)", fw.watch_sh("U1", "U2")))
    assert used and used <= have, used - have


def test_a_zero_word_reads_as_zero_despite_the_hexdump_bug(tmp_path):
    """BusyBox 1.24's hexdump -C prints only "*" for an all-zero first line (mocked); rd must use -v."""
    f, w = funcs_only(tmp_path)
    (w / "PEEK.BIN").write_text("#!/bin/sh\nprintf '\\000\\000\\000\\000'\n")
    (w / "PEEK.BIN").chmod(0o755)
    r = shell(tmp_path, f + "\nnum 55000000\nprintf '\\000\\000\\000\\000' | busybox hexdump -C\n")
    assert r.stdout.split() == ["0", "*"], r.stdout + r.stderr


def test_macro_refuses_snap_focus(tmp_path):
    """U1 still saved with Snap: the lens does not follow the subject, so no bracket (seen on the camera)."""
    rows = [(1, "U1", "2.8", 7000, -1, 10, 100), (2, "U1", "2.8", 7001, 304, 10, 105),
            (3, "U1", "2.8", 7002, 281, 10, 110)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "macro needs AF focus 10" in log and "bracket" not in log and "macro near" not in log


def test_a_tag_left_by_a_power_off_is_cleared_at_start(tmp_path):
    rows = [(1, "P", "2.8", 7000, -1, 1, 100)]
    log, relay, patch = run_watch(tmp_path, rows, cpy_first=0, attach=0, limit=2, tag_file="1\n")
    lines = log.splitlines()
    assert lines[1:4] == ["tag leftover attach 1", "tag cleared attach Off", "watch mode P"]
    assert "SetupCopyright x'" in relay and "SetupCopyrightAttach Off'" in relay
    w = tmp_path / "w"
    assert not (w / "TAG").exists() and (w / "tag.del").exists()


def test_the_first_lens_reads_may_be_zero(tmp_path):
    rows = [(1, "U1", "5.6", 7000, -1, 1, 100), (2, "U1", "5.6", 7001, 430, 1, 105)]
    log, relay, patch = run_watch(tmp_path, rows, limit=3, lens_zero=2)
    assert "macro near 430" in log and "no lens distance" not in log


def test_card_clear_tag_writes_the_marker():
    assert fw.card_files(clear_tag="Off")[f"{fw.WATCH_DIR}/TAG"] == b"1\n"
    assert fw.card_files(clear_tag="On")[f"{fw.WATCH_DIR}/TAG"] == b"0\n"
    assert f"{fw.WATCH_DIR}/TAG" not in fw.card_files()


def test_pull_sh_writes_and_removes_the_marker(tmp_path):
    """PULL.SH (the only Linux writer of the card) mirrors the watcher's RAM requests onto WATCH/TAG."""
    import af_debug_card as adc
    www, d = tmp_path / "www", tmp_path / "card"
    (www / "watch").mkdir(parents=True)
    (d / "WATCH").mkdir(parents=True)
    text = rc.card_files()
    body = [v for k, v in text.items() if k.endswith("REMOTE.SH")][0].decode()
    pull = body[body.index("END_PULL'\n") + 9:body.rindex("END_PULL\n")]
    pull = pull.replace(rc.WWW, str(www)).replace(f"d={rc.D}", f"d={d}")
    (www / "watch" / "tag.put").write_text("1\n")
    subprocess.run(["sh", "-c", pull], check=True)
    assert (d / "WATCH" / "TAG").read_text() == "1\n" and not (www / "watch" / "tag.put").exists()
    (www / "watch" / "tag.del").write_text("")
    subprocess.run(["sh", "-c", pull], check=True)
    assert not (d / "WATCH" / "TAG").exists() and not (www / "watch" / "tag.del").exists()
