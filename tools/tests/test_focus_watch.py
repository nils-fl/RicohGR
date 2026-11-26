"""Focus modes watcher (tools/focus/focus_watch.py): the shell's patch building, planner and meter against
the Python versions, and WATCH.SH run on the Mac against mocks of PEEK.BIN, webapid and the relay.
Needs the decoded 1.60 image."""
import math
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

import af_debug_card as card  # noqa: E402
import focus_bracket as fb  # noqa: E402
import focus_probe as fp  # noqa: E402
import focus_probe3 as p3  # noqa: E402
import focus_watch as fw  # noqa: E402
import recipe_card as rc  # noqa: E402

SCRIPT = fw.watch_sh()
FUNCS = SCRIPT.split("log watch start", 1)[0]
TEMPLATES = fw.templates()
OBJ, LA, LB, LC = 0x5571A000, 0x55800000, 0x55900000, 0x55A00000


def x(path, text):
    path.write_text(text)
    path.chmod(0o755)


def bindir(t):
    b = t / "bin"
    b.mkdir(exist_ok=True)
    x(b / "busybox", '#!/bin/sh\nc=$1; shift\ncase $c in usleep) exec sleep 0.02 ;; nc) exec "$T/bin/mocknc" "$@" ;; '
                     'dd) for a; do case $a in bs=*|count=*|seek=*|skip=*|if=*|of=*) ;; *) echo "dd: invalid argument $a" >&2; exit 1 ;; esac; done; exec dd "$@" ;; hexdump) [ "$1" = -v ] && exec hexdump "$@"; f=$T/hd.$$; cat ${2:-} >$f; [ -s $f ] && [ -z "$(od -An -v -tx1 $f | head -1 | tr -d " 0\\n")" ] && { echo "*"; exit 0; }; exec hexdump "$1" $f ;; *) exec "$c" "$@" ;; esac\n')
    return b


def shell(t, body, env=None):
    b = bindir(t)
    (t / "s.sh").write_text(body)
    e = dict(os.environ, T=str(t), PATH=f"{b}:{os.environ['PATH']}", **(env or {}))
    return subprocess.run(["sh", str(t / "s.sh")], env=e, capture_output=True, text=True, timeout=300)


def funcs_only(t):
    """The helper functions with SOLO.LIB replaced by stubs and W pointing at t/w."""
    w = t / "w"
    w.mkdir(exist_ok=True)
    for k, (elf, _) in TEMPLATES.items():
        (w / f"T{k}.BIN").write_bytes(elf)
        (w / f"T{k}.BIN").chmod(0o755)   # as the boot copy leaves them on the camera
    f = FUNCS.replace(f". {rc.WWW}/SOLO.LIB", f"w={t}\nlog() {{ :; }}\nb() {{ cat >/dev/null; }}")
    return f.replace(f"W={fw.RAM}", f"W={w}"), w


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


@pytest.mark.parametrize("mm,slot", [(261, "0 30"), (400, "0 30"), (550, "1 100"), (829, "1 100"), (1330, "2 150"), (1745, "3 200"),
                                     (2110, "3 200"), (2500, "4 250"), (3000, "5 350"), (4000, "5 350"),
                                     (5952, "6 500"), (12164, "6 500")])
def test_nearest_snap_setting_in_dioptres(tmp_path, mm, slot):
    assert " ".join(awk_out(tmp_path, f"nearslot {mm}")) == slot


@pytest.mark.parametrize("mms", [[99], [70000], [300, 50]])
def test_shell_refuses_out_of_range_millimetres(tmp_path, mms):
    f, _ = funcs_only(tmp_path)
    r = shell(tmp_path, f + f'mkround $W/R.BIN {len(mms)} "{" ".join(map(str, mms))}"; echo rc=$?\n')
    assert "rc=1" in r.stdout


def awk_out(t, call):
    f, _ = funcs_only(t)
    return shell(t, f + call + "\n").stdout.split()


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


@pytest.mark.parametrize("subject,f,snap", [(1330, 2.8, 1330), (20000, 8.0, round(1000 / fb.half_zone(8.0))),
                                            (150, 5.6, 200)])
def test_meter_is_the_subject_or_the_hyperfocal_distance(tmp_path, subject, f, snap):
    assert abs(int(awk_out(tmp_path, f"meter {subject} {f}")[0]) - snap) <= 1


# ---- the whole watcher against mocks -----------------------------------------------------------
LBL_TAB = 0x5407E140 + 2 * 0xC48


def lbl_ent(i):
    return 0x53B70000 + 16 * i


def lbl_str(i):
    return 0x53B80000 + 16 * i


def run_watch(t, rows, cpy_first=0, attach=1, limit=None, lens_zero=0, tag_file=None, fn=3, setting=None, lang=None):
    """rows: (props call k, U mode, av, last photo number, lens mm, focus code, uptime s); the state for props
    call k is the last row with row k <= k. PEEK and /v1/photos answer from the current state.
    lens_zero: the first that many lens reads give 0. tag_file: a WATCH/TAG the boot copied to RAM.
    The focus code is the mode in use (+0xc6) and, unless setting is given, the setting (+0x11c).
    fn: the Fn button setting. lang: the label chain answers (table index 0, whatever the language)."""
    (t / "k").write_text("0\n")
    (t / "lz").write_text(f"{lens_zero}\n")
    (t / "table").write_text("\n".join(" ".join(map(str, r)) for r in rows) + "\n")
    x(t / "state", "#!/bin/sh\nk=$(cat $T/k)\nawk -v k=$k -v f=$1 '$1 <= k { v = $f } END { print v }' $T/table\n")
    b = bindir(t)
    x(b / "mocknc", """#!/bin/sh
read req path _
case $path in
/v1/props) echo $(( $(cat $T/k) + 1 )) >$T/k
 printf 'HTTP/1.0 200 OK\\r\\n\\r\\n{"ssid":"S","exposureMode":"%s","av":"%s","avList":["2.8"]}\\n' $($T/state 2) $($T/state 3) ;;
/v1/photos) s=$($T/state 4); printf 'HTTP/1.0 200 OK\\r\\n\\r\\n{"dirs":[{"name":"100RICOH","files":["R%07d.JPG","R%07d.DNG","R%07d.JPG"]}]}\\n' $((s - 1)) $s $s ;;
esac
""")
    w = t / "w"
    w.mkdir()
    for k, (elf, _) in TEMPLATES.items():
        (w / f"T{k}.BIN").write_bytes(elf)
        (w / f"T{k}.BIN").chmod(0o755)   # as the boot copy leaves them on the camera
    for n, (elf, _) in fw.label_templates().items():
        (w / f"LB{n}.BIN").write_bytes(elf)
        (w / f"LB{n}.BIN").chmod(0o755)
    lbl = ""
    if lang is not None:
        lbl = (f"{fw.LANG_TAB}) v={LBL_TAB} ;;\n"
               + "".join(f"{LBL_TAB + 4 * i}) v={lbl_ent(i)} ;; {lbl_ent(i)}) v=261 ;; {lbl_ent(i) + 4}) v={lbl_str(i)} ;;\n"
                         for ids in fw.LABEL_IDS.values() for i in ids))
    for name in ("CPTAG", "CPEMPTY"):
        x(w / f"{name}.BIN", "#!/bin/sh\n")
    if tag_file is not None:
        (w / "TAG").write_text(tag_file)
    x(w / "PEEK.BIN", f"""#!/bin/sh
a=$((0x$1))
# The settings block for the probe: zero words, +0xa40 from table column 8 (a toggle state).
if [ $a = {OBJ} ]; then f=$($T/state 8); python3 -c "import sys, struct; w = [0] * $2; w[{0xA40 // 4}] = int('${{f:-0}}'); sys.stdout.buffer.write(struct.pack('<%dI' % $2, *w))"; exit 0; fi
case $a in
{p3.SETTINGS_PTR}) v={OBJ} ;; {OBJ + p3.OFF['focus']}) v={'$($T/state 6)' if setting is None else setting} ;; {OBJ + p3.OFF['snap']}) v=250 ;; {OBJ + fw.OFF_FN - 2}) v=$(({fn} << 16)) ;; {OBJ + fw.OFF_CUR - 2}) v=$(($($T/state 6) << 16)) ;;
{OBJ + p3.OFF_COPYRIGHT - 1}) v={cpy_first << 8} ;; {OBJ + p3.OFF_ATTACH - 3}) v={attach << 24} ;;
{p3.LENS_ROOT}) v={LA} ;; {LA + p3.LENS_A}) v={LB} ;; {LB}) v={LC} ;;
{lbl}
{LC + p3.LENS_MM}) z=$(cat $T/lz); if [ $z -gt 0 ]; then echo $((z - 1)) >$T/lz; v=0; else v=$($T/state 5); fi ;;
*) exit 9 ;;
esac
[ $v -lt 0 ] && v=$((v + 4294967296))
python3 -c "import sys, struct; sys.stdout.buffer.write(struct.pack('<I', $v) * $2)"
""")
    lib = (f"w={t}\nclobber() {{ t=9; k=9; q=9; o=9; l=9; v=9; a=9; }}\n"
           f"log() {{ echo \"$*\" >>{t}/log.txt; clobber; }}\n"
           f"b() {{ cat >>{t}/relay.txt; echo ---- >>{t}/relay.txt; clobber; }}\n")
    s = SCRIPT.replace(f". {rc.WWW}/SOLO.LIB", f". {t}/lib.sh").replace(f"W={fw.RAM}", f"W={w}")
    s = s.replace('pt() { "$@"; }', 'pt() { [ -x "$1" ] || { echo "$(basename $1) not executable" >>$T/patch.txt; return 126; }; echo "$(basename $1) $2" >>$T/patch.txt; }')
    s = s.replace("zup() { read zq0 _ </proc/uptime; echo ${zq0%.*}; }", "zup() { $T/state 7; }")
    s = s.replace("sleep 2 )", ":; )")
    s = s.replace("poll; zpc=4; fi", "poll; zpc=2; fi")                # each table row: two lens reads
    s = s.replace("while :; do", f"while [ $(cat $T/k) -lt {limit or max(r[0] for r in rows) + 1} ]; do")
    (t / "lib.sh").write_text(lib)
    (t / "watch.sh").write_text(s)
    env = dict(os.environ, T=str(t), PATH=f"{b}:{os.environ['PATH']}")
    subprocess.run(["sh", str(t / "watch.sh")], env=env, check=True, timeout=600)
    read = lambda n: (t / n).read_text() if (t / n).exists() else ""   # noqa: E731
    return read("log.txt"), read("relay.txt"), read("patch.txt")


def frames(relay):
    return [int(m) for m in re.findall(r"StillSnapDistanceCm (\d+)'\nmpause \d+\nsendln 'UserData Set StillFocus "
                                       r"Infinity'\nmpause \d+\nsendln 'UserData Set StillFocus Snap'\nmpause \d+\n"
                                       r"sendln 'SW_sws 1'", relay)]


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


def test_meter_sets_snap_after_the_lens_settles_and_restores_on_leaving(tmp_path):
    # 5 m: below the hyperfocal distance at f/2.8, beyond it at f/8, so the new aperture moves the snap.
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 5000, 1, 101),
            (3, "U2", "2.8", 7000, 5000, 1, 102), (4, "U2", "8", 7000, 5000, 10, 103),
            (5, "P", "8", 7000, 5000, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject 5000 av 2.8 snap 5000 slot 500 patch 0" in log
    assert "meter av 8" in log and "meter subject 5000 av 8 snap " in log
    assert "meter subject 5000 av 8 snap 5000 " not in log
    assert patch.splitlines() == ["M.BIN ", "CPTAG.BIN ", "CPTAG.BIN restore", "M.BIN restore", "M.BIN ",
                                  "CPTAG.BIN ", "CPTAG.BIN restore", "M.BIN restore", "CPEMPTY.BIN ", "CPEMPTY.BIN restore"]
    assert relay.count("UserData Set StillSnapDistanceCm 500'") == 2   # both near the 5 m label
    # As Fn (AF/Snap toggle) does it: AF mode into the back-up slot, focus Snap. The new aperture comes
    # in Snap: only the distance, no beep.
    assert relay.count("StillBackUpFocus Select'") == 1 and relay.count("StillFocus Snap'") == 1
    assert relay.count("AUD_beep'") == 1 and "SW_fn" not in relay and "CommonTempFocus" not in relay
    assert "meter focus 10" in log
    assert "StillFullPressSnap" not in relay and "StillFocus Select" not in relay   # leaving: P recalls its own
    assert "SW_sws" not in relay                                           # the meter never presses
    assert "meter fn" not in log                                           # Fn = AF/Snap toggle in U2: quiet


def test_a_new_aperture_in_af_does_not_switch_to_snap(tmp_path):
    """After Fn back to AF, P's aperture changes with the light: no Snap until a half-press."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 5000, 1, 101),
            (3, "U2", "2.8", 7000, 5000, 1, 102), (4, "U2", "2.8", 7000, 5000, 10, 103),
            (5, "U2", "2.8", 7000, 5000, 1, 104), (6, "U2", "8", 7000, 5000, 1, 105),
            (7, "U2", "8", 7000, 5000, 1, 106)]
    log, relay, patch = run_watch(tmp_path, rows, limit=8)
    assert "meter focus 1\n" in log.split("meter focus 10")[1] and "meter av" not in log
    assert relay.count("StillFocus Snap'") == 1


def test_a_half_press_right_after_the_dial_counts(tmp_path):
    """The lens reading from before the turn is the base: AF already done when U2 is seen still meters."""
    rows = [(1, "P", "2.8", 7000, 2500, 1, 100), (2, "P", "2.8", 7000, 2500, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 1, 103),
            (5, "U2", "2.8", 7000, 1330, 1, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject 1330" in log


def test_meter_ignores_snap_and_mf_and_an_unmoved_lens(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 10, 100), (2, "U2", "2.8", 7000, 1330, 10, 101),
            (3, "U2", "2.8", 7000, 1330, 9, 102), (4, "U2", "2.8", 7000, 2500, 1, 103),
            (5, "U2", "2.8", 7000, 2500, 1, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=6)
    assert "meter subject" not in log and patch == ""


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


def test_meter_batch_fits_the_relay():
    meter = re.search(r'printf "([^"]*StillFocus Snap[^"]*)" \$3 ', SCRIPT).group(1).replace("\\n", "\n").replace("%s", "ZoneSelect")
    assert meter.index("AUD_beep") < meter.index("StillBackUpFocus") < meter.index("StillFocus Snap")
    tag = re.search(r'printf "([^"]*SetupCopyrightAttach On[^"]*)" GRSNAP', SCRIPT).group(1).replace("\\n", "\n")
    tag = tag.replace("%s", "GRSNAP:65535:999999")
    pre = "sendln 'UserData Set StillSnapDistanceCm 500'\nmpause 300\n"
    assert len(meter) + len(tag) + 120 <= rc.RELAY_SIZE - 24, len(meter) + len(tag)
    assert len(pre) + len(meter) + 120 <= rc.RELAY_SIZE - 24          # the redraw line: the tag on its own


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


def test_meter_measures_again_on_every_new_distance_but_not_at_the_snap_distance(tmp_path):
    h = 1000 / fb.half_zone(2.8)                                           # hyperfocal mm at f/2.8
    far = 30000
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, far, 1, 103),
            (5, "U2", "2.8", 7000, far, 1, 104), (6, "U2", "2.8", 7001, round(h) + 50, 1, 105),
            (7, "U2", "2.8", 7001, round(h) + 50, 1, 106)]
    log, relay, patch = run_watch(tmp_path, rows, limit=8)
    assert "meter subject 1330 av 2.8 snap 1330" in log
    assert f"meter subject {far} av 2.8 snap {round(h)}" in log or f"meter subject {far} av 2.8 snap {round(h) + 1}" in log
    assert "StillSnapDistanceCm 150'" in relay and "StillSnapDistanceCm 500'" in relay


def test_fn_back_to_af_measures_again_even_at_the_same_distance(tmp_path):
    """Snap set; Fn (AF/Snap toggle) gives AF with the lens still at 1330; AF hunts and lands at 1330 again."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 103),
            (6, "U2", "2.8", 7001, 1330, 10, 105), (7, "U2", "2.8", 7001, 1330, 1, 106),
            (9, "U2", "2.8", 7001, 900, 1, 108), (10, "U2", "2.8", 7001, 1330, 1, 109)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    lines = [l for l in log.splitlines() if l.startswith("meter ")]
    assert [l.split(" av")[0] for l in lines if "subject" in l or l == "meter focus 1"] == [
        "meter focus 1", "meter subject 1330", "meter focus 1", "meter subject 1330"]
    assert relay.count("StillFocus Snap'") == 2 and relay.count("StillBackUpFocus Select'") == 2


def test_back_from_a_hyperfocal_snap_waits_for_the_half_press(tmp_path):
    """The lens rests at the hyperfocal snap distance, not at the last subject: not a subject itself."""
    h = round(1000 / fb.half_zone(2.8))
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 30000, 1, 101),
            (3, "U2", "2.8", 7000, 30000, 1, 102), (4, "U2", "2.8", 7000, h, 10, 103),
            (6, "U2", "2.8", 7000, h, 1, 105), (9, "U2", "2.8", 7000, 2000, 1, 108)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    assert log.count("meter subject") == 2 and "meter subject 2000" in log
    assert f"meter subject {h}" not in log


def test_in_snap_a_moving_lens_is_not_measured(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 103),
            (6, "U2", "2.8", 7000, 4000, 10, 105), (9, "U2", "2.8", 7000, 600, 10, 108)]
    log, relay, patch = run_watch(tmp_path, rows, limit=12)
    assert log.count("meter subject") == 1


def test_snap_dropped_by_the_camera_is_sent_again_twice_at_most(tmp_path):
    """Seen on the camera: three batches acked, focus stayed AF. Retries after 5 s, then gives up."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 1, 104),
            (5, "U2", "2.8", 7000, 1330, 1, 108), (6, "U2", "2.8", 7000, 1330, 1, 110),
            (7, "U2", "2.8", 7000, 1330, 1, 114), (8, "U2", "2.8", 7000, 1330, 1, 120),
            (9, "U2", "2.8", 7000, 1330, 1, 126)]
    log, relay, patch = run_watch(tmp_path, rows, limit=10)
    assert "meter snap retry 1" in log and "meter snap retry 2" in log and log.count("meter snap failed") == 1
    assert relay.count("StillFocus Snap'") == 3 and relay.count("AUD_beep'") == 1
    assert relay.count("StillBackUpFocus Select'") == 3


def test_snap_that_came_is_not_sent_again(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101),
            (3, "U2", "2.8", 7000, 1330, 1, 102), (4, "U2", "2.8", 7000, 1330, 10, 104),
            (5, "U2", "2.8", 7000, 1330, 1, 110), (7, "U2", "2.8", 7000, 1330, 1, 120)]
    log, relay, patch = run_watch(tmp_path, rows, limit=9)
    assert "retry" not in log and relay.count("StillFocus Snap'") == 1


def test_snap_photos_carry_the_exact_distance_and_af_photos_do_not(tmp_path):
    """Copyright empty: GRSNAP:<snap>:<subject> with the Snap batch; Fn back to AF clears it in one batch."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 479, 1, 101),
            (3, "U2", "2.8", 7000, 479, 1, 102), (4, "U2", "2.8", 7000, 479, 10, 104),
            (5, "U2", "2.8", 7000, 479, 1, 106), (6, "U2", "2.8", 7000, 479, 1, 107)]
    log, relay, patch = run_watch(tmp_path, rows, limit=7)
    batches = relay.split("----\n")
    snap = next(b for b in batches if "StillFocus Snap'" in b)
    assert "SetupCopyright GRSNAP:479:479'" in snap and "SetupCopyrightAttach On'" in snap
    assert snap.index("StillFocus Snap") < snap.index("GRSNAP")          # Snap first, the tag after
    off = next(b for b in batches if "SetupCopyright x'" in b)
    assert "SetupCopyrightAttach Off'" in off                            # attach byte 1: it was Off
    assert patch.splitlines()[:5] == ["M.BIN ", "CPTAG.BIN ", "CPTAG.BIN restore", "CPEMPTY.BIN ", "CPEMPTY.BIN restore"]
    assert "meter tag GRSNAP:479:479" in log and "tag cleared attach Off" in log
    # The card marker: put with the tag (attach byte 1), deleted with the clear (PULL.SH does both).
    assert (tmp_path / "w" / "tag.put").read_text() == "1\n" and (tmp_path / "w" / "tag.del").exists()


def test_snap_meter_leaves_a_set_copyright_alone(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 479, 1, 101),
            (3, "U2", "2.8", 7000, 479, 1, 102), (4, "U2", "2.8", 7000, 479, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5, cpy_first=0x41)
    assert "SetupCopyright" not in relay and "CPTAG" not in patch and "meter subject 479" in log


def label_sites(elf):
    """The (address, stock, new) entries of a filled label patch."""
    for k, (tpl, off) in fw.label_templates().items():
        if len(tpl) == len(elf):
            return [struct.unpack_from("<3I", elf, off + 12 * i) for i in range(k)]
    raise AssertionError("no label template of that size")


def meter_rows(mm):
    return [(1, "U2", "2.8", 7000, 1000 if mm != 1000 else 2000, 1, 100), (2, "U2", "2.8", 7000, mm, 1, 101),
            (3, "U2", "2.8", 7000, mm, 1, 102), (4, "U2", "2.8", 7000, mm, 10, 104), (5, "P", "2.8", 7000, mm, 1, 105)]


def test_the_label_shows_the_exact_distance(tmp_path):
    """0.479 m: the 0.3 m setting, both its labels (0.3m, 0.4m) read 48cm; restored on leaving."""
    log, relay, patch = run_watch(tmp_path, meter_rows(479), limit=6, lang=1)
    a, b = fw.LABEL_IDS[30]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [
        (lbl_str(a), 0x002E0030, 0x00380034), (lbl_str(a) + 4, 0x006D0033, 0x006D0063),
        (lbl_str(b), 0x002E0030, 0x00380034), (lbl_str(b) + 4, 0x006D0034, 0x006D0063)]
    assert "meter subject 479 av 2.8 snap 479 slot 30 patch 0 label 48cm" in log
    assert "StillSnapDistanceCm 30'" in relay and "StillSnapDistanceCm 500'" not in relay   # 250 in use
    lines = patch.splitlines()
    assert lines.index("L.BIN ") < lines.index("M.BIN ") and "L.BIN restore" in lines


def test_the_label_writes_only_the_words_that_change(tmp_path):
    """2.95 m on 3.5 m: "3." stays, one word."""
    log, relay, patch = run_watch(tmp_path, meter_rows(2950), limit=6, lang=1)
    i = fw.LABEL_IDS[350][0]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [(lbl_str(i) + 4, 0x006D0035, 0x006D0030)]
    assert "slot 350 patch 0 label 3.0m" in log


def test_a_three_character_label_gets_its_length(tmp_path):
    """Beyond the hyperfocal distance at f/2.8 (12.2 m): 12m, the length word 5 -> 4."""
    log, relay, patch = run_watch(tmp_path, meter_rows(30000), limit=6, lang=1)
    i = fw.LABEL_IDS[350][0]
    assert label_sites((tmp_path / "w" / "L.BIN").read_bytes()) == [
        (lbl_ent(i), 0x105, 0x104), (lbl_str(i), 0x002E0033, 0x00320031), (lbl_str(i) + 4, 0x006D0035, 0x0000006D)]
    assert "snap 12164 slot 350 patch 0 label 12m" in log


def test_a_stock_text_needs_no_patch(tmp_path):
    log, relay, patch = run_watch(tmp_path, meter_rows(1500), limit=6, lang=1)
    assert "slot 150 patch 0 label 1.5m" in log and "L.BIN" not in patch


def test_the_label_on_the_setting_in_use_is_redrawn(tmp_path):
    """2.4 m: 2.5 m, already the setting (250): 5 m first, so the widget sees a new id."""
    log, relay, patch = run_watch(tmp_path, meter_rows(2400), limit=5, lang=1)
    snap = next(b for b in relay.split("----\n") if "StillFocus Snap'" in b)
    assert snap.index("StillSnapDistanceCm 500'") < snap.index("StillSnapDistanceCm 250'") and "label 2.4m" in log
    assert "GRSNAP" not in snap and "SetupCopyright GRSNAP:2400:2400'" in relay


def test_without_the_label_chain_the_nearest_setting(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 1000, 1, 100), (2, "U2", "2.8", 7000, 2400, 1, 101),
            (3, "U2", "2.8", 7000, 2400, 1, 102), (4, "U2", "2.8", 7000, 2400, 10, 104)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "meter label off" in log and "slot 250 patch 0 label none" in log and "L.BIN" not in patch


@pytest.mark.parametrize("mm,text", [(479, "48cm"), (200, "20cm"), (994, "99cm"), (995, "1.0m"), (3540, "3.5m"),
                                     (9949, "9.9m"), (9950, "10m"), (12164, "12m"), (65535, "66m")])
def test_label_text_is_at_most_four_characters(mm, text):
    fn = re.search(r"^lbltxt\(\) \{.*\}$", SCRIPT, re.M).group(0)
    out = subprocess.run(["sh", "-c", fn + f"\nlbltxt {mm}"], capture_output=True, text=True).stdout
    assert out == text and len(out) <= 4


def test_meter_warns_when_fn_is_not_the_af_snap_toggle(tmp_path):
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 2500, 1, 101)]
    log, relay, patch = run_watch(tmp_path, rows, limit=3, fn=0)
    assert "meter fn 0 not af snap toggle" in log


def test_meter_ignores_a_garbage_lens_reading(tmp_path):
    """Seen on the camera right after turning to U2: 536870893 mm."""
    rows = [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 536870893, 1, 101),
            (3, "U2", "2.8", 7000, 536870893, 1, 102), (4, "U2", "2.8", 7000, 536870893, 1, 103)]
    log, relay, patch = run_watch(tmp_path, rows, limit=5)
    assert "meter subject" not in log and patch == ""


def test_a_tag_left_by_a_power_off_is_cleared_at_start(tmp_path):
    rows = [(1, "P", "2.8", 7000, -1, 1, 100)]
    log, relay, patch = run_watch(tmp_path, rows, cpy_first=0, attach=0, limit=2, tag_file="1\n")
    lines = log.splitlines()
    assert lines[1:4] == ["tag leftover attach 1", "tag cleared attach Off", "tag on attach 0"]
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
