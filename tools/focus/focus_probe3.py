#!/usr/bin/env python3
"""Focus probe 3 (docs/focus-bracketing.html §8): what the dial focus modes need, in one session.

    python3 tools/focus/focus_probe3.py card [--out work/focus-probe3]
    python3 tools/focus/focus_probe3.py import /Volumes/…/DCIM/100RICOH --first R000… --log …/RECIPE.LOG
                                               --near-m 0.5 --far-m 1.5

The session (about 9 min, camera on a tripod, dial at P, AF-S Select with the AF point in the centre):
  A  lamp on steadily, about 85 s: turn the dial P -> U1 -> U2 -> U3 -> P, about 15 s each.
     Logs the webapid fields that change (mode names only, private fields left out) and the settings.
  B  any distance through MF, no RAM patch: CommonFocusPosMm 300, 612, 1000, 3000, then Focus Mf;
     one frame with the value changed while already in Mf; one with short pauses. Then three Snap frames
     at the menu values 30, 100, 250 cm, which calibrate the maker-note lens position in this session.
  C  lamp blinks fast for 12 s: aim the AF point at the near target (a measured 0.5 m from the sensor,
     about 2 cm in front of the screen). Three AF-S half presses, a shot, then Focus Mf; the card logs the
     lens distance in mm live and the stored CommonFocusPosMm. Then the same for the far target (1.5 m).
  D  only if the stored copyright is empty: copyright "GRFS:TEST:01/01:0612" with attach on, one shot,
     then the copyright empty again and attach as before. With a copyright set the part is skipped and
     its text is never read beyond its first byte.
  E  focus mode, snap distance and CommonFocusPosMm as before (focus mode: --focus-after).

What is read (Read, GR IIIx 1.60): settings values behind the pointer at 0x55063f34: CommonFocusPosMm
+0x108 (setter 0x53242608), focus +0x11c, snap cm +0x130, self-timer +0x484, copyright +0x945 (32 bytes,
0x5326cbc4), copyright attach +0x967 (0x532586b8; "On" stores 0, "Off" 1). The lens distance in mm
that 0x532af360 stores into +0x108: [[[[0x553458b0] + 0x518]] + 0x698] (0x53525d5c, 0x53549250,
0x5355128c). In Mf (focus code 9) 0x532af508 sends a non-zero +0x108 to the lens command 0x532aeb68.
The shell's SetupCopyright handler 0x53330cd8 copies only 2 characters (strncpy 3, then buf[2] = 0):
CPTAG.BIN makes it 32 (mov r2, #0x21; strb at buf[32]), CPEMPTY.BIN stores an empty string (strb at
buf[0]); both are restored right after their one command.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
import af_debug_card as card  # noqa: E402
import effects_solo as es  # noqa: E402
import focus_bracket as fb  # noqa: E402
import focus_probe as fp  # noqa: E402
import recipe_card as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIR = "SCRIPT/RECIPE/PROBE3"
RAM = f"{rc.WWW}/probe3"

SETTINGS_PTR = 0x55063F34
OFF = {"pos": 0x108, "focus": 0x11C, "snap": 0x130, "timer": 0x484}
# The focus mode byte at OFF["focus"] (codes from the StillFocus matcher 0x532ddd7c).
FOCUS_NAMES = {0: "AutoArea", 1: "Select", 4: "Pinpoint", 5: "Follow", 6: "Continuous", 9: "Mf", 10: "Snap",
               11: "Infinity", 14: "AutoAreaM", 15: "ZoneSelect"}
OFF_COPYRIGHT, OFF_ATTACH = 0x945, 0x967
LENS_ROOT, LENS_A, LENS_MM = 0x553458B0, 0x518, 0x698

# (address, stock word, new word); checked against the image by check()
COPYRIGHT_LEN = (0x53330CDC, 0xE3A02003, 0xE3A02021)      # mov r2, #3 -> mov r2, #0x21
COPYRIGHT_END = (0x53330D0C, 0xE54B503A, 0xE54B501C)      # strb r5, [fp, #-0x3a] -> [fp, #-0x1c]
COPYRIGHT_EMPTY = (0x53330D0C, 0xE54B503A, 0xE54B503C)    # -> strb r5, [fp, #-0x3c] (buf[0])
TAG = "GRFS:TEST:01/01:0612"

MF_MM = (300, 612, 1000, 3000)
MF_STILL_MM = 1500                 # changed while already in Mf
FAST_MM = 800                      # short pauses
CAL_CM = (30, 100, 250)
AF_REPS = 3
DIAL_LOOPS = 24                    # about 3.5 s each
AIM_S = 12
PAUSE = 300
MF_SETTLE = 1200

PRIVATE = fp.PRIVATE


def check():
    words = {a: w for a, w, _ in (COPYRIGHT_LEN, COPYRIGHT_END)}
    data = fp.DECODED.read_bytes()
    bad = []
    for a, w in words.items():
        o = 0x10870 + a - 0x53000000
        got = int.from_bytes(data[o:o + 4], "little")
        if got != w:
            bad.append(f"{a:#x}: {got:#010x}, expected {w:#010x}")
    return bad


def ttl(line, pause):
    if "'" in line or "\n" in line:
        raise ValueError(line)
    return f"sendln '{line}'\\nmpause {int(pause)}\\n"


def relay(*cmds):
    """One sh line that sends cmds (line, pause) as one batch: for commands inside a shell if."""
    return 'printf "' + "".join(ttl(c, p) for c, p in cmds) + '" | b'


def press_sh():
    return relay(*[(c[1], c[2]) for c in fb.press()])


# Helpers, one sh line each. Variables start with y (SOLO.LIB's log and b set w t k q o l v a).
HELPERS = [
    f"W={RAM}",
    "rd() { $W/PEEK.BIN $1 1 2>/dev/null | busybox hexdump -v -C | busybox awk 'NR == 1 { print $5 $4 $3 $2 }'; }",
    "num() { yx=$(rd $1); [ -n \"$yx\" ] && echo $((0x$yx)); }",
    "at() { num $(printf %x $(($1 + $2))); }",
    f"obj() {{ yo=$(num {SETTINGS_PTR:x}); [ -n \"$yo\" ]; }}",
    f"lens() {{ ya=$(num {LENS_ROOT:x}); [ -n \"$ya\" ] || return; yb=$(at $ya {LENS_A:#x}); [ -n \"$yb\" ] || return; "
    f"yc=$(num $(printf %x $yb)); [ -n \"$yc\" ] || return; at $yc {LENS_MM:#x}; }}",
    # byte 1 of the word at +0x944 is the copyright's first character; byte 3 of +0x964 the attach flag
    f"cpy0() {{ obj || return; yw=$(at $yo {OFF_COPYRIGHT - 1:#x}); [ -n \"$yw\" ] && echo $(((yw >> 8) & 255)); }}",
    f"attach() {{ obj || return; yw=$(at $yo {OFF_ATTACH - 3:#x}); [ -n \"$yw\" ] && echo $(((yw >> 24) & 255)); }}",
    "st() { if obj; then "
    + "".join(f"y{k}=$(at $yo {v:#x}); " for k, v in OFF.items())
    + "yfocus=$((${yfocus:-0} & 255)); ytimer=$((${ytimer:-0} & 255)); "
    "log st $1 focus $yfocus snap ${ysnap:-none} pos ${ypos:-none} lens $(lens) attach $(attach) cpy0 $(cpy0); "
    "else log st $1 no settings object; fi; }",
    "get() { ( printf 'GET %s HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\n\\r\\n' \"$1\"; sleep 2 ) | busybox nc 127.0.0.1 80 "
    ">$w/probe3.txt 2>/dev/null & yp=$!; sleep 3; kill $yp 2>/dev/null; }",
    # field=value for mode-like fields, lists and private names left out; logged when it changes
    "fields() { get /v1/props; busybox sed 's/[][{}]/,/g' $w/probe3.txt 2>/dev/null | busybox awk -v RS=, "
    "'{ if (!match($0, /\"[A-Za-z0-9_]+\"[ ]*:/)) next; k = substr($0, RSTART + 1, RLENGTH - 1); "
    "sub(/\"[ ]*:$/, \"\", k); v = substr($0, RSTART + RLENGTH); gsub(/[^A-Za-z0-9_.+-]/, \"\", v); l = tolower(k); "
    f"if (l ~ /{PRIVATE}/ || l ~ /list$/) next; "
    "if (l ~ /mode|user|dial|exposure|focus|shoot|capture|^av$|^tv$/) o = o \" \" k \"=\" substr(v, 1, 12) } "
    "END { print o }'; }",
    "dial() { yd=$(fields); [ \"$yd\" = \"$ye\" ] || { log dial$yd; ye=$yd; }; "
    "ys=$(obj && echo $(at $yo 0x11c) $(at $yo 0x130) $(at $yo 0x108)); [ \"$ys\" = \"$yt\" ] || { log dialst $ys; yt=$ys; }; }",
    "focused() { log focused $(fields | busybox tr ' ' '\\n' | busybox grep '^focused=' ); }",
]


def steps_for(focus_after="Select", snap_after=250):
    if focus_after not in fp.FOCUS_VALUES:
        raise SystemExit(f"--focus-after must be one of {', '.join(fp.FOCUS_VALUES)}")
    if snap_after not in fp.MENU_CM:
        raise SystemExit(f"--snap-after must be one of {', '.join(map(str, fp.MENU_CM))}")
    s = [("sh", f"busybox mkdir -p {RAM}; cp {rc.D}/PROBE3/*.BIN {RAM}/; busybox chmod 755 {RAM}/*.BIN; log probe3 copied $?")]
    s += [("sh", h) for h in HELPERS]
    s += [("sh", "st start; y0=$ypos; ya0=$(attach)"),
          ("log", "part A dial"), ("cmd", "Af_Led_On", PAUSE),
          ("sh", f"yi=0; while [ $yi -lt {DIAL_LOOPS} ]; do dial; yi=$((yi + 1)); done"),
          ("cmd", "Af_Led_Off", 2000), ("sh", "st after-dial")]
    plan = []

    def frame(kind, value, label):
        plan.append({"n": len(plan) + 1, "kind": kind, "value": value, "label": label})

    s += [("log", "part B mf"), ("cmd", "UserData Set StillFocus Select", 800)]
    for mm in MF_MM:
        s += [("log", f"frame {len(plan) + 1} mf {mm}"),
              ("cmd", "UserData Set StillFocus Select", 800),
              ("cmd", f"UserData Set CommonFocusPosMm {mm}", PAUSE),
              ("cmd", "UserData Set StillFocus Mf", MF_SETTLE),
              ("sh", f"st mf{mm}")] + fb.press() + [("sh", f"st mf{mm}-after")]
        frame("mf", mm, f"Mf {mm} mm")
    s += [("log", f"frame {len(plan) + 1} mf-still {MF_STILL_MM}"),
          ("cmd", f"UserData Set CommonFocusPosMm {MF_STILL_MM}", MF_SETTLE),
          ("sh", f"st mfstill{MF_STILL_MM}")] + fb.press()
    frame("mf-still", MF_STILL_MM, f"Mf {MF_STILL_MM} mm, set while in Mf")
    s += [("log", f"frame {len(plan) + 1} mf-fast {FAST_MM}"),
          ("cmd", "UserData Set StillFocus Select", PAUSE),
          ("cmd", f"UserData Set CommonFocusPosMm {FAST_MM}", PAUSE),
          ("cmd", "UserData Set StillFocus Mf", PAUSE),
          ("sh", f"st mffast{FAST_MM}")] + fb.press()
    frame("mf-fast", FAST_MM, f"Mf {FAST_MM} mm, short pauses")
    for cm in CAL_CM:
        s += [("log", f"frame {len(plan) + 1} snap {cm}"),
              ("cmd", f"UserData Set StillSnapDistanceCm {cm}", PAUSE),
              ("cmd", "UserData Set StillFocus Infinity", fb.SWITCH_MS),
              ("cmd", "UserData Set StillFocus Snap", fb.SWITCH_MS),
              ("sh", f"st snap{cm}")] + fb.press()
        frame("snap", cm * 10, f"Snap {cm} cm (calibration)")

    for target in ("near", "far"):
        s += [("log", f"part C {target} aim")] + es.blink(AIM_S, on=150, off=350)
        s += [("cmd", "UserData Set StillFocus Select", 800)]
        for k in range(1, AF_REPS + 1):
            s += [("log", f"frame {len(plan) + 1} af {target} {k}"),
                  ("cmd", "SW_sws 1", 1500),
                  ("sh", f"st af-{target}{k}-half; focused"),
                  ("cmd", "SW_swr 1", fb.FULL_MS), ("cmd", "SW_swr 0", fb.RELEASE_MS),
                  ("cmd", "SW_sws 0", fb.SAVE_MS),
                  ("sh", f"st af-{target}{k}-after"),
                  ("cmd", "UserData Set StillFocus Mf", MF_SETTLE),
                  ("sh", f"st af-{target}{k}-mf"),
                  ("cmd", "UserData Set StillFocus Select", 800)]
            frame("af", target, f"AF-S {target} target, try {k}")

    # D: only with an empty copyright; every SetupCopyright is inside the if
    on_off = 'yv=Off; [ "$ya0" = 0 ] && yv=On'
    s += [("log", "part D tag"),
          ("sh", 'yc0=$(cpy0); if [ "$yc0" = 0 ]; then log tag go; else log tag skipped cpy0 ${yc0:-none}; fi'),
          ("sh", 'if [ "$yc0" = 0 ]; then $W/CPTAG.BIN; log patch cptag $?; '
                 + relay((f"UserData Set SetupCopyright {TAG}", 500)) + f'; $W/CPTAG.BIN restore; log restore cptag $?; '
                 + relay(("UserData Set SetupCopyrightAttach On", 500)) + '; st tag; '
                 f'log tagread $($W/PEEK.BIN $(printf %x $((yo + {OFF_COPYRIGHT - 1:#x}))) 9 | busybox dd bs=1 skip=1 count=32 '
                 "2>/dev/null | busybox tr -cd 'A-Za-z0-9:/_-'); fi"),
          ("sh", f'if [ "$yc0" = 0 ]; then log frame {len(plan) + 1} tag; ' + press_sh() + "; fi"),
          ("sh", 'if [ "$yc0" = 0 ]; then $W/CPEMPTY.BIN; log patch cpempty $?; '
                 + relay(("UserData Set SetupCopyright x", 500)) + '; $W/CPEMPTY.BIN restore; log restore cpempty $?; '
                 + on_off + '; printf "sendln \'UserData Set SetupCopyrightAttach %s\'\\nmpause 500\\n" $yv | b; '
                 "st tag-after; fi")]
    frame("tag", TAG, "copyright tag (only with an empty copyright)")

    s += [("log", "part E restore"),
          ("cmd", f"UserData Set StillSnapDistanceCm {snap_after}", PAUSE),
          ("sh", 'case "$y0" in ""|*[!0-9]*) log pos not restored ${y0:-none} ;; *) '
                 'printf "sendln \'UserData Set CommonFocusPosMm %s\'\\nmpause 300\\n" $y0 | b; log pos restored $y0 ;; esac'),
          ("cmd", f"UserData Set StillFocus {focus_after}", PAUSE),
          ("sh", "st end"),
          ("cmd", "Af_Led_On", es.END_LAMP_MS), ("cmd", "Af_Led_Off", 300)]
    return s, plan


def card_files(focus_after="Select", snap_after=250):
    steps, plan = steps_for(focus_after, snap_after)
    files = dict(rc.card_files())
    files[f"{rc.CARD_DIR}/SESSION.SH"] = rc.solo_script("SESSION", steps)
    files[f"{rc.CARD_DIR}/SESSION.ON"] = b"armed\n"
    files[f"{DIR}/PEEK.BIN"] = card.peek_bin()
    files[f"{DIR}/CPTAG.BIN"] = card.multipatch_bin([COPYRIGHT_LEN, COPYRIGHT_END])
    files[f"{DIR}/CPEMPTY.BIN"] = card.multipatch_bin([COPYRIGHT_EMPTY])
    return files, steps, plan


# ---- import ----------------------------------------------------------------------------------
def log_rows(path):
    """{tag: {field: value}} from the 'solo st TAG k v ...' lines, plus the other solo lines."""
    st, other = {}, []
    for line in path.read_text(errors="replace").splitlines():
        w = line.split()
        if "solo" not in w:
            continue
        w = w[w.index("solo") + 1:]
        if w[:1] == ["st"] and len(w) > 2:
            st[w[1]] = dict(zip(w[2:-1:2], w[3:-1:2]))
        else:
            other.append(" ".join(w))
    return st, other


def copyright(jpg):
    r = subprocess.run(["exiftool", "-s", "-s", "-s", "-Copyright", str(jpg)], capture_output=True, text=True)
    return r.stdout.strip()


def import_run(a):
    plan = json.loads(a.plan.read_text())
    stems = sorted({p.stem for p in a.dcim.glob("R*.JPG") if p.stem >= a.first})
    if len(stems) < len(plan) - 1:
        raise SystemExit(f"{len(stems)} photos from {a.first} on, the plan has {len(plan)}")
    out = a.out / a.first
    out.mkdir(parents=True, exist_ok=True)
    for stem in stems[:len(plan)]:
        for f in (a.dcim / f"{stem}.JPG", a.dcim / f"{stem}.DNG"):
            if f.exists() and not (out / f.name).exists():
                shutil.copy2(f, out / f.name)
    st, other = log_rows(a.log) if a.log else ({}, [])
    rows = []
    for p, stem in zip(plan, stems):
        jpg = out / f"{stem}.JPG"
        rows.append(dict(p, file=stem, position=fb.lens_position(jpg)))
    cal = [r for r in rows if r["kind"] == "snap"]
    intercept, slope, _ = fb.fit_line([1000 / r["value"] for r in cal], [r["position"] for r in cal])
    to_m = lambda pos: (slope / (pos - intercept)) if pos - intercept > 1 else float("inf")   # noqa: E731
    lines = [f"calibration (Snap frames): position = {intercept:.0f} + {slope:.0f} / distance in m", "",
             f"{'file':9s} {'frame':42s} {'position':>8s} {'from position':>13s}  logged lens mm / pos"]
    for r in rows:
        key = {"mf": "mf{}", "mf-still": "mfstill{}", "mf-fast": "mffast{}", "snap": "snap{}"}.get(r["kind"], "")
        key = key.format(r["value"] // 10 if r["kind"] == "snap" else r["value"])
        if r["kind"] == "af":
            k = sum(1 for x in rows[:rows.index(r) + 1] if x["kind"] == "af" and x["value"] == r["value"])
            key = f"af-{r['value']}{k}-mf"
        lg = st.get(key, {})
        extra = f"{lg.get('lens', '-')} / {lg.get('pos', '-')}"
        if r["kind"] == "tag":
            extra = f"EXIF Copyright: {copyright(out / (r['file'] + '.JPG')) == TAG and 'tag ok' or 'not the tag'}"
        lines.append(f"{r['file']:9s} {r['label']:42s} {r['position']:8d} {to_m(r['position']):11.2f} m  {extra}")
    lines += ["", "Gates:",
              "  A dial: the 'dial' lines below name the U mode (or the settings change with it)",
              "  B MF: Mf frames at their asked distance (from position) within about 10 %",
              f"  C AF: near and far frames at {a.near_m} m and {a.far_m} m; the logged lens mm agree",
              "  D tag: EXIF Copyright = the tag on the tag frame, and 'st tag-after' shows cpy0 0", ""]
    lines += [l for l in other if l.split()[:1] in (["dial"], ["dialst"], ["focused"], ["tag"], ["tagread"],
                                                     ["patch"], ["restore"], ["pos"])]
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=ROOT / "work" / "focus-probe3")
    c.add_argument("--focus-after", default="Select")
    c.add_argument("--snap-after", type=int, default=250)
    i = sub.add_parser("import")
    i.add_argument("dcim", type=Path)
    i.add_argument("--first", required=True)
    i.add_argument("--log", type=Path)
    i.add_argument("--plan", type=Path, default=ROOT / "work" / "focus-probe3" / "plan.json")
    i.add_argument("--out", type=Path, default=ROOT / "work" / "focus-tests")
    i.add_argument("--near-m", type=float, default=0.5)
    i.add_argument("--far-m", type=float, default=1.5)
    a = ap.parse_args()
    if a.cmd == "import":
        return import_run(a)
    bad = check()
    if bad:
        sys.exit("copyright handler does not match this image:\n  " + "\n  ".join(bad))
    files, steps, plan = card_files(a.focus_after, a.snap_after)
    if a.out.exists():
        shutil.rmtree(a.out)
    for rel, data in files.items():
        f = a.out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    (a.out / "plan.json").write_text(json.dumps(plan, indent=1))
    r = subprocess.run(["sh", "-n", str(a.out / rc.CARD_DIR / "SESSION.SH")], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    print(f"SESSION (focus probe 3): {len(plan)} frames, about {es.seconds(steps) / 60 + DIAL_LOOPS * 3.5 / 60:.1f} min")
    for p in plan:
        print(f"  {p['n']:2d}  {p['label']}")
    print(f"wrote {a.out}/SCRIPT (copy onto the card root) and {a.out}/plan.json")


if __name__ == "__main__":
    main()
