"""Shared by test_focus_watch.py and test_snap_meter.py: WATCH.SH (tools/focus/focus_watch.py) run on the Mac
against mocks of PEEK.BIN, SAMP.BIN, webapid and the relay, and the shell's helper functions alone.
Needs the decoded 1.60 image."""
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
import snap_meter as sm  # noqa: E402
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


def awk_out(t, call):
    f, _ = funcs_only(t)
    return shell(t, f + call + "\n").stdout.split()


# ---- the whole watcher against mocks -----------------------------------------------------------
LBL_TAB = 0x5407E140 + 2 * 0xC48


def lbl_ent(i):
    return 0x53B70000 + 16 * i


def lbl_str(i):
    return 0x53B80000 + 16 * i


MOCK = r"""
import json, os, struct, sys
T = os.environ["T"]
C = json.load(open(f"{T}/cfg.json"))
rows = [list(map(lambda v: int(v) if v.lstrip("-").isdigit() else v, l.split())) for l in open(f"{T}/table")]


def rd(n, d="0"):
    try:
        return open(f"{T}/{n}").read().strip()
    except FileNotFoundError:
        return d


def wr(n, v):
    open(f"{T}/{n}", "w").write(f"{v}\n")


def row(k=None):
    k = int(rd("k")) if k is None else k
    r = rows[0]
    for x in rows:
        if x[0] <= k:
            r = x
    return r


def cam():
    f, b, c = map(int, rd("cam").split())
    return [f, b, c]


def lens():
    if rd("lensov", ""):
        return int(rd("lensov"))
    z = int(rd("lz"))
    if z > 0:
        wr("lz", z - 1)
        return 0
    return row()[4] & 0xFFFFFFFF


def value(a):
    f, b, c = cam()
    O = C["obj"]
    fixed = {C["settings"]: O, O + C["focus"]: f | (b << 16), O + C["snap"]: c, O + C["fn"]: C["fnv"] << 16,
             O + C["cur"]: f << 16, O + C["pos"]: C["posv"], O + C["cpy"]: C["cpyv"], O + C["att"]: C["attv"],
             C["lroot"]: C["la"], C["la"] + C["lensa"]: C["lb"], C["lb"]: C["lc"]}
    fixed.update({int(k): v for k, v in C["extra"].items()})
    if a == C["lc"] + C["lensmm"]:
        return lens()
    if a == C["caf"]:
        return 2 if rd("afrun") == "1" else 1
    if C["s1"] is not None and a == C["s1w"]:
        n = int(rd("s1"))
        if n > 0:
            wr("s1", n - 1)
            return 1
        return 0
    return fixed.get(a)


def ok(a):
    return a % 4 == 0 and 0x53000000 <= a < 0x5FF00000


mode = sys.argv[1]
if mode == "peek":
    v = value(int(sys.argv[2], 16))
    if v is None:
        sys.exit(9)
    sys.stdout.buffer.write(struct.pack("<I", v & 0xFFFFFFFF) * int(sys.argv[3], 16))
elif mode == "samp":
    args = sys.argv[2:]
    with open(f"{T}/samp.txt", "a") as fh:
        fh.write(" ".join(args) + "\n")
    if C["samp_broken"]:
        sys.exit(8)
    if args[0] == "t":
        addrs = [int(x, 16) for x in args[4:]]
        recs = b"".join(struct.pack("<5I", 100 + i, 5000000 * i, i, 7, 7) for i in range(2))
        open(args[3], "ab").write(recs)
        sys.exit(0)
    to, st, la, base, moved, fa, exp = [int(x, 16) for x in args[1:8]]
    extras = [int(x, 16) for x in args[8:]]
    if any(x and not ok(x) for x in [la, fa] + extras):
        sys.exit(8)
    f = value(fa) if fa else 0
    res, out = 1, 0xFFFFFFFF
    if fa == C["caf"] and to:                      # an AF run: started at a props call (afp), ends at the next wait
        if exp == 1 and to >= 8000 and C["press"]:  # the watch during a batch: wait for the press a while
            import time
            for _ in range(40):
                if rd("afp") == "1":
                    break
                time.sleep(0.05)
        if exp == 1 and rd("afp") == "1":
            wr("afp", 0)
            wr("afrun", 1)
            res, f = 2, 2
        elif exp != 1 and rd("afrun") == "1":
            wr("afrun", 0)
            res, f = 2, 1
    if la and base == 0 and moved == 1 and st:     # the lens after an AF run: where it is
        if C["af_slow"]:                           # its still check outlasts the batch (camera run 18)
            import time
            time.sleep(C["af_slow"])
        L = lens()
        if 0 < L < 1000000:
            res, out = 0, L
    elif la:
        L = lens()
        valid = 0 < L < 1000000
        if valid:
            out = L
            if L != base:
                moved = 1
        if to:
            prev = rd("prev", "")
            if valid and moved and str(L) == prev:
                res = 0
            wr("prev", L if valid else "")
    if res == 1 and to and fa and fa != C["caf"] and exp < 256 and (f & 255) != exp:
        res = 2
    vals = []
    for x in extras:
        v = value(x) if x else 0
        if v is None:
            sys.exit(9)
        vals.append(v & 0xFFFFFFFF)
    sys.stdout.buffer.write(struct.pack("<%dI" % (6 + len(vals)), res, to if res == 1 else 100, 1, out, moved, f & 0xFFFFFFFF, *vals))
elif mode == "state":
    print(row()[int(sys.argv[2]) - 1])
elif mode == "props":
    k = int(rd("k")) + 1
    wr("k", k)
    pf, nf = row(k - 1)[5], row(k)[5]
    f, b, c = cam()
    if nf != pf:                                   # the table's focus changed: the user (or the camera)
        if nf == 10:
            if f != 10:
                b, f = f, 10                       # Fn from AF: the AF mode into the back-up
        elif pf == 10 and nf not in (9, 11):
            if f == 10:
                f, b = nf, 13                      # Fn back to AF: 0x532b0b6c writes 13
        else:
            f = nf
    AF = (0, 1, 4, 5, 6, 14, 15)
    if cam()[0] in AF and f in AF and (row(k)[4] != row(k - 1)[4] or (len(row(k)) > 7 and row(k)[7] == "af")):
        wr("afp", 1)                               # an AF run: the lens moved in an AF mode, or the row says so
    if rd("revnext") == "1" and f == 10:
        f = b                                      # the camera drops Snap, back-up unchanged
        wr("revnext", 0)
    wr("cam", f"{f} {b} {c}")
    r = row(k)
    sys.stdout.write('HTTP/1.0 200 OK\r\n\r\n{"ssid":"S","exposureMode":"%s","av":"%s","avList":["2.8"]}\n' % (r[1], r[2]))
elif mode == "batch":
    text = open(sys.argv[2]).read()
    f, b, c = cam()
    if "StillFocus Snap'" in text and int(rd("dropall")) > 0:
        wr("dropall", int(rd("dropall")) - 1)
        sys.exit(0)
    for line in text.splitlines():
        if "StillSnapDistanceCm " in line:
            c = int(line.split("StillSnapDistanceCm ")[1].rstrip("'"))
        elif "StillBackUpFocus " in line:
            b = C["codes"][line.split("StillBackUpFocus ")[1].rstrip("'")]
        elif "StillFocus Snap'" in line:
            if int(rd("drop")) > 0:
                wr("drop", int(rd("drop")) - 1)
            else:
                f = 10
                if int(rd("revert")) > 0:
                    wr("revert", int(rd("revert")) - 1)
                    wr("revnext", 1)
    wr("cam", f"{f} {b} {c}")
    n = int(rd("press", "0"))
    if n > 0 and "StillFocus Snap'" in text:       # the user half-presses on another subject meanwhile
        wr("press", n - 1)
        wr("lensov", C["press_mm"])
        wr("afp", 1)
        import time
        time.sleep(1.0)
"""



def run_watch(t, rows, cpy_first=0, attach=1, limit=None, lens_zero=0, tag_file=None, fn=3, lang=None,
              samp=True, samp_broken=False, drop=0, drop_all=0, revert=0, pos=0, press=0, press_mm=700, af_slow=0):
    """rows: (props call k, U mode, av, last photo number, lens mm, focus code, uptime s); the state for props
    call k is the last row with row k <= k. Each round on the meter mode is one props call and one SAMP.BIN
    wait: the lens has settled when a wait sees the same moved lens as the wait before.
    The camera model ($T/cam: focus, back-up, snap cm) starts with the first row's focus, back-up 13, 250 cm;
    the relay batches change it as the camera would (drop: that many StillFocus Snap lines ignored; drop_all:
    that many Snap batches ignored; revert: Snap dropped again by the camera at the next round, back-up kept).
    A change of the table's focus is the user: to 10 Fn from AF, from 10 to AF Fn back (back-up 13). A lens
    change between rows in an AF mode is an AF run (the action word leaves 1), as is a row with an 8th
    column "af" (AF landing where the lens was).
    lens_zero: the first that many lens reads give 0. tag_file: a WATCH/TAG the boot copied to RAM.
    fn: the Fn button setting. lang: the label chain answers (table index 0, whatever the language).
    samp False: no SAMP.BIN (the PEEK path); samp_broken: it exits 8."""
    import json
    (t / "k").write_text("0\n")
    (t / "lz").write_text(f"{lens_zero}\n")
    for n, v in (("drop", drop), ("dropall", drop_all), ("revert", revert), ("press", press)):
        (t / n).write_text(f"{v}\n")
    (t / "cam").write_text(f"{rows[0][5]} 13 250\n")
    (t / "table").write_text("\n".join(" ".join(map(str, r)) for r in rows) + "\n")
    extra = {}
    if lang is not None:
        extra[sm.LANG_TAB] = LBL_TAB
        for ids in sm.LABEL_IDS.values():
            for i in ids:
                extra.update({LBL_TAB + 4 * i: lbl_ent(i), lbl_ent(i): 261, lbl_ent(i) + 4: lbl_str(i)})
    cfg = dict(obj=OBJ, settings=p3.SETTINGS_PTR, focus=p3.OFF["focus"], snap=p3.OFF["snap"], fn=sm.OFF_FN - 2,
               fnv=fn, cur=fw.OFF_CUR - 2, pos=p3.OFF["pos"], posv=pos, cpy=p3.OFF_COPYRIGHT - 1, cpyv=cpy_first << 8,
               att=p3.OFF_ATTACH - 3, attv=attach << 24, lroot=p3.LENS_ROOT, la=LA, lensa=p3.LENS_A, lb=LB, lc=LC,
               lensmm=p3.LENS_MM, extra={str(k): v for k, v in extra.items()}, s1=None, s1w=0,
               caf=card.CAF_OBJECT + sm.CAF_ACTION, press_mm=press_mm, press=press, af_slow=af_slow,
               samp_broken=samp_broken, codes={n: c for c, n in fw.FOCUS_NAMES.items()})
    (t / "cfg.json").write_text(json.dumps(cfg))
    (t / "mock.py").write_text(MOCK)
    b = bindir(t)
    x(b / "mocknc", """#!/bin/sh
read req path _
case $path in
/v1/props) python3 $T/mock.py props ;;
/v1/photos) s=$(python3 $T/mock.py state 4); printf 'HTTP/1.0 200 OK\\r\\n\\r\\n{"dirs":[{"name":"100RICOH","files":["R%07d.JPG","R%07d.DNG","R%07d.JPG"]}]}\\n' $((s - 1)) $s $s ;;
esac
""")
    w = t / "w"
    w.mkdir()
    for k, (elf, _) in TEMPLATES.items():
        (w / f"T{k}.BIN").write_bytes(elf)
        (w / f"T{k}.BIN").chmod(0o755)   # as the boot copy leaves them on the camera
    for n, (elf, _) in sm.label_templates().items():
        (w / f"LB{n}.BIN").write_bytes(elf)
        (w / f"LB{n}.BIN").chmod(0o755)
    for name in ("CPTAG", "CPEMPTY"):
        x(w / f"{name}.BIN", "#!/bin/sh\n")
    if tag_file is not None:
        (w / "TAG").write_text(tag_file)
    x(w / "PEEK.BIN", '#!/bin/sh\nexec python3 $T/mock.py peek "$@"\n')
    if samp:
        x(w / "SAMP.BIN", '#!/bin/sh\nexec python3 $T/mock.py samp "$@"\n')
    lib = (f"w={t}\nclobber() {{ t=9; k=9; q=9; o=9; l=9; v=9; a=9; }}\n"
           f"log() {{ echo \"$*\" >>{t}/log.txt; clobber; }}\n"
           f"b() {{ cat >{t}/batch.txt; cat {t}/batch.txt >>{t}/relay.txt; echo ---- >>{t}/relay.txt; "
           f"python3 $T/mock.py batch {t}/batch.txt; clobber; }}\n")
    s = SCRIPT.replace(f". {rc.WWW}/SOLO.LIB", f". {t}/lib.sh").replace(f"W={fw.RAM}", f"W={w}")
    s = s.replace('pt() { "$@"; }', 'pt() { [ -x "$1" ] || { echo "$(basename $1) not executable" >>$T/patch.txt; return 126; }; echo "$(basename $1) $2" >>$T/patch.txt; }')
    s = s.replace("zup() { read zq0 _ </proc/uptime; echo ${zq0%.*}; }", "zup() { python3 $T/mock.py state 7; }")
    s = s.replace("sleep 2 )", ":; )")
    s = s.replace("then pollbg; else poll; fi", "then poll; else poll; fi")          # each round: one props call
    s = s.replace("while :; do\n if [ \"$zmode\"", f"while [ $(cat $T/k) -lt {limit or max(r[0] for r in rows) + 1} ]; do\n if [ \"$zmode\"")
    assert "while [ $(cat $T/k)" in s
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


def batches(relay):
    return [b for b in relay.split("----\n") if b]


def snap_rows(n=6):
    return [(1, "U2", "2.8", 7000, 2500, 1, 100), (2, "U2", "2.8", 7000, 1330, 1, 101)] + [
        (k, "U2", "2.8", 7000, 1330, 1, 100 + k) for k in range(3, n + 1)]


def label_sites(elf):
    """The (address, stock, new) entries of a filled label patch."""
    for k, (tpl, off) in sm.label_templates().items():
        if len(tpl) == len(elf):
            return [struct.unpack_from("<3I", elf, off + 12 * i) for i in range(k)]
    raise AssertionError("no label template of that size")


def meter_rows(mm):
    return [(1, "U2", "2.8", 7000, 1000 if mm != 1000 else 2000, 1, 100), (2, "U2", "2.8", 7000, mm, 1, 101),
            (3, "U2", "2.8", 7000, mm, 1, 102), (4, "U2", "2.8", 7000, mm, 10, 104), (5, "P", "2.8", 7000, mm, 1, 105)]


def still_rows(mm, n=4):
    return [(k, "U2", "2.8", 7000, mm, 1, 100 + k) for k in range(1, n + 1)]
