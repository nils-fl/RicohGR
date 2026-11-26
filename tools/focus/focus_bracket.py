#!/usr/bin/env python3
"""Focus bracketing on the stock GR IIIx 1.60 from an SD-card script (docs/focus-bracketing.html §6).

    python3 tools/focus/focus_bracket.py plan --near 0.25 --far inf --f 5.6
        prints the frames: focus distance and the depth-of-field zone of each
    python3 tools/focus/focus_bracket.py card --near 0.25 --far inf --f 5.6 [--out work/focus-bracket]
        writes the card root (copy SCRIPT/ onto the card, keep its other files) and plan.json
    python3 tools/focus/focus_bracket.py check CARD_DCIM_FOLDER --first R0001234 [--plan …/plan.json]
        after the run: reads each frame's lens position from the maker notes (Pentax tag 0x0240) and
        checks that it follows the plan (straight line in 1/distance, monotonic)

The aperture is not set by the card: shoot in Av (or M) at the f-number given with --f.

Planning (Inferred, thin-lens depth of field in dioptres; f = 26.1 mm, circle of confusion 0.02 mm):
a frame focused at D dioptres is sharp from D - h to D + h with h = N c / f^2 (f/5.6: h = 0.164,
hyperfocal 6.1 m). The first frame sits at near - h, the last at far + h (at the hyperfocal distance
when far is infinity), and the frames between are spaced evenly in dioptres so that neighbouring zones
overlap by --overlap of a zone.

How a frame is focused (Read, probe 2, 2026-11-25): ApplySnapDistance (0x532af3d8) turns each menu
distance into millimetres with one instruction per distance and sends them to the lens when Snap is
selected. The card rewrites the seven of them (30/40, 100, 150, 200, 250, 350, 500 cm) in RAM so that
each menu value stands for one frame of a round, then per frame: UserData Set StillSnapDistanceCm
<menu value>, Focus Infinity, Focus Snap (ApplySnapDistance runs), shutter. After each round of up to
seven frames the stock words go back. A power-off undoes everything in any case. Probe 2 ran only the
30/40 pair; the other six words are the same pattern, which the first bracket run checks through the
lens positions in the maker notes (check).

The solo run SESSION (one power-on; SESSION.ON is deleted when it starts):
  start:   props and photos logged as in the probe (no private values); Focus Snap, AF lamp on
  countdown: --countdown seconds of slow blinking (camera on a tripod; do not touch it)
  frames:  near to far; the AF lamp is off while shooting
  end:     Focus --focus-after, snap --snap-after, photos counted again, lamp on for 3 s
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
import af_debug_card as card  # noqa: E402
import effects_solo as es  # noqa: E402
import focus_probe as fp  # noqa: E402
import recipe_card as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

# ---- optics ------------------------------------------------------------------------------
FOCAL_M = 0.0261          # GR IIIx (EXIF LensType 26.1mm F2.8)
COC_M = 0.00002           # 0.02 mm, the usual APS-C value
F_NUMBERS = (2.8, 3.2, 3.5, 4.0, 4.5, 5.0, 5.6, 6.3, 7.1, 8.0, 9.0, 10.0, 11.0, 13.0, 14.0, 16.0)
NEAR_MIN_M = 0.2          # closest distance run on the camera (probe 2: 20 cm)
MAX_FRAMES = 35           # five rounds


def half_zone(f_number, coc=COC_M, focal=FOCAL_M):
    """h in dioptres: a frame focused at D is sharp from D - h to D + h."""
    return f_number * coc / focal ** 2


def dioptre(m):
    return 0.0 if m is None or math.isinf(m) else 1.0 / m


def plan(near_m, far_m, f_number, overlap=0.25):
    """Focus distances in dioptres, near to far, whose zones cover near..far."""
    if not NEAR_MIN_M <= near_m:
        raise ValueError(f"near {near_m} m is closer than {NEAR_MIN_M} m")
    if far_m is not None and not math.isinf(far_m) and far_m <= near_m:
        raise ValueError("far must be beyond near")
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in 0..1")
    h = half_zone(f_number)
    first = dioptre(near_m) - h
    last = max(dioptre(far_m) + h, h)      # at infinity: the hyperfocal distance
    if first <= last:
        return [(dioptre(near_m) + dioptre(far_m)) / 2]
    step = 2 * h * (1 - overlap)
    n = math.ceil((first - last) / step - 1e-9) + 1
    if n > MAX_FRAMES:
        raise ValueError(f"{n} frames; more than {MAX_FRAMES}: stop down (--f) or narrow the range")
    return [first - k * (first - last) / (n - 1) for k in range(n)]


def to_mm(d):
    return min(fp.MAX_MM, round(1000 / d))


def zones(ds, f_number):
    h = half_zone(f_number)
    return [(d + h, max(d - h, 0.0)) for d in ds]


def metres(d):
    return "inf" if d <= 1e-9 else f"{1 / d:.2f} m"


# ---- ApplySnapDistance slots (Read, GR IIIx 1.60) ----------------------------------------------
# menu value -> [(address, stock word, condition)]; 30 cm also covers its 40 cm form (0x532af020).
SLOTS = (
    (30, ((0x532AF4D4, 0x03A01F4B, 0x0), (0x532AF4F8, 0xE3A01E19, 0xE))),   # moveq #300, mov #400
    (100, ((0x532AF4C8, 0xE3A01FFA, 0xE),)),                               # mov r1, #1000
    (150, ((0x532AF4B4, 0x030015DC, 0x0),)),                               # movweq r1, #1500
    (200, ((0x532AF500, 0xE3A01E7D, 0xE),)),                               # mov r1, #2000
    (250, ((0x532AF4E4, 0x030019C4, 0x0),)),                               # movweq r1, #2500
    (350, ((0x532AF4F0, 0xE3001DAC, 0xE),)),                               # movw r1, #3500
    (500, ((0x532AF4C0, 0xE3011388, 0xE),)),                               # movw r1, #5000
)
ROUND = len(SLOTS)


def check(image=fp.DECODED):
    data = image.read_bytes()
    bad = fp.check(image)
    for _, sites in SLOTS:
        for a, want, _ in sites:
            o = a - fp.BASE + fp.OFF
            got = int.from_bytes(data[o:o + 4], "little")
            if got != want:
                bad.append(f"{a:#010x}: {got:#010x}, expected {want:#010x}")
    return bad


def round_sites(mms):
    """Patch words for one round: slot k sends mms[k] millimetres."""
    if not 1 <= len(mms) <= ROUND:
        raise ValueError(f"a round has 1..{ROUND} frames")
    out = []
    for (_, sites), mm in zip(SLOTS, mms):
        if not fp.MIN_MM <= mm <= fp.MAX_MM:
            raise ValueError(f"{mm} mm outside {fp.MIN_MM}..{fp.MAX_MM}")
        out += [(a, stock, fp.movw_r1(mm, cond)) for a, stock, cond in sites]
    return out


# ---- the card ----------------------------------------------------------------------------------
BRACKET_DIR = "SCRIPT/RECIPE/BRACKET"
RAM = f"{rc.WWW}/bracket"
SET_PAUSE, SWITCH_MS = 300, 800                  # probe 2 timing for the Focus switch
HALF_MS, FULL_MS, RELEASE_MS = 600, 300, 300      # Snap: the half-press runs no AF
SAVE_MS = 1500


def press(save_ms=SAVE_MS):
    return [("cmd", "SW_sws 1", HALF_MS), ("cmd", "SW_swr 1", FULL_MS), ("cmd", "SW_swr 0", RELEASE_MS),
            ("cmd", "SW_sws 0", save_ms)]


def rounds(mms):
    return [mms[i:i + ROUND] for i in range(0, len(mms), ROUND)]


def steps_for(ds, countdown=10, save_ms=SAVE_MS, focus_after="Select", snap_after=250):
    if focus_after not in fp.FOCUS_VALUES:
        raise SystemExit(f"--focus-after must be one of {', '.join(fp.FOCUS_VALUES)}")
    if snap_after not in fp.MENU_CM:
        raise SystemExit(f"--snap-after must be one of {', '.join(map(str, fp.MENU_CM))}")
    mms = [to_mm(d) for d in ds]
    rs = rounds(mms)
    names = [f"R{k + 1}.BIN" for k in range(len(rs))]
    steps = [("sh", f"busybox mkdir -p {RAM}; cp {' '.join(f'{rc.D}/BRACKET/{n}' for n in names)} {RAM}/; "
                    f"busybox chmod 755 {RAM}/*.BIN; log bracket copied $?"),
             ("sh", fp.API_SH), ("sh", fp.PROPS_SH), ("sh", fp.PHOTOS_SH),
             ("sh", "props start; photos start"),
             ("log", "settings"), ("cmd", "Af_Led_On", 300),
             ("cmd", "UserData Set StillFocus Snap", SET_PAUSE),
             ("cmd", "Af_Led_Off", 1000), ("log", "countdown")] + es.blink(countdown)
    plan_rows, n = [], 0
    for r, (name, mm_round) in enumerate(zip(names, rs), 1):
        steps.append(("sh", f"{RAM}/{name}; log patch {name} $?"))
        for (slot, _), mm in zip(SLOTS, mm_round):
            n += 1
            steps += [("log", f"frame {n} {mm}mm"),
                      ("cmd", f"UserData Set StillSnapDistanceCm {slot}", SET_PAUSE),
                      ("cmd", "UserData Set StillFocus Infinity", SWITCH_MS),
                      ("cmd", "UserData Set StillFocus Snap", SWITCH_MS)] + press(save_ms)
            plan_rows.append({"n": n, "round": r, "slot_cm": slot, "mm": mm, "dioptre": round(ds[n - 1], 4)})
        steps.append(("sh", f"{RAM}/{name} restore; log restore {name} $?"))
    steps += [("log", "restore settings"),
              ("cmd", f"UserData Set StillSnapDistanceCm {snap_after}", SET_PAUSE),
              ("cmd", f"UserData Set StillFocus {focus_after}", SET_PAUSE),
              ("sh", "photos end"),
              ("cmd", "Af_Led_On", 3000), ("cmd", "Af_Led_Off", 300)]
    return steps, plan_rows, dict(zip(names, rs))


def card_files(ds, **kw):
    steps, rows, round_mms = steps_for(ds, **kw)
    files = dict(rc.card_files())
    files[f"{rc.CARD_DIR}/SESSION.SH"] = rc.solo_script("SESSION", steps)
    files[f"{rc.CARD_DIR}/SESSION.ON"] = b"armed\n"
    for name, mms in round_mms.items():
        files[f"{BRACKET_DIR}/{name}"] = card.multipatch_bin(round_sites(mms))
    return files, steps, rows


# ---- check the photos ------------------------------------------------------------------------
def lens_position(jpg):
    """Pentax maker note 0x0240, 32-bit word 1 (Inferred: the lens position at capture)."""
    import struct
    blob = subprocess.run(["exiftool", "-u", "-b", "-Pentax:Pentax_0x0240", str(jpg)],
                          capture_output=True, check=True).stdout
    if len(blob) < 8:
        raise SystemExit(f"{jpg}: no maker note 0x0240")
    return struct.unpack_from("<I", blob, 4)[0]


def fit_line(ds, pos):
    """Least-squares pos = a + b * D. Returns a, b and the residuals."""
    n = len(ds)
    mx, my = sum(ds) / n, sum(pos) / n
    sxx = sum((x - mx) ** 2 for x in ds)
    b = sum((x - mx) * (y - my) for x, y in zip(ds, pos)) / sxx if sxx else 0.0
    a = my - b * mx
    return a, b, [y - (a + b * x) for x, y in zip(ds, pos)]


TOLERANCE = 0.08          # dioptres off the line before a frame is flagged


def check_photos(a):
    rows = json.loads(a.plan.read_text())
    stems = sorted({p.stem for p in a.dcim.glob("R*.JPG") if p.stem >= a.first})[:len(rows)]
    if len(stems) < len(rows):
        raise SystemExit(f"{len(stems)} photos from {a.first} on, the plan has {len(rows)}")
    pos = [lens_position(a.dcim / f"{s}.JPG") for s in stems]
    ds = [r["dioptre"] for r in rows]
    lines = []
    if len(rows) >= 2:
        a0, b, res = fit_line(ds, pos)
        lines.append(f"line: position = {a0:.0f} + {b:.0f} per dioptre (probe 2: about 1200)")
    else:
        b, res = 0.0, [0.0]
    bad = 0
    for r, s, p, e in zip(rows, stems, pos, res):
        off = e / b if b else 0.0
        flag = "" if abs(off) <= TOLERANCE else f"  OFF by {off:+.2f} D"
        bad += bool(flag)
        lines.append(f"{s}  frame {r['n']:2d}  {r['mm']:6d} mm  {metres(r['dioptre']):>8s}  position {p}{flag}")
    mono = all(p1 > p2 for p1, p2 in zip(pos, pos[1:]))
    lines.append(f"monotonic near to far: {'yes' if mono else 'NO'}; {bad} frame(s) off the line")
    print("\n".join(lines))
    return 0 if mono and not bad else 1


# ---- command line ------------------------------------------------------------------------------
def far_value(text):
    return math.inf if text.lower() in ("inf", "infinity") else float(text)


def table(ds, f_number):
    out = [f"{len(ds)} frames at f/{f_number:g}: zone of each frame (near .. far)"]
    for k, (d, (zn, zf)) in enumerate(zip(ds, zones(ds, f_number)), 1):
        out.append(f"  {k:2d}  focus {metres(d):>8s}  ({to_mm(d)} mm)   sharp {metres(zn):>8s} .. {metres(zf)}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "card"):
        p = sub.add_parser(name)
        p.add_argument("--near", type=float, required=True, help="nearest distance to be sharp, m")
        p.add_argument("--far", type=far_value, default=math.inf, help="farthest distance, m, or inf")
        p.add_argument("--f", type=float, default=5.6, choices=F_NUMBERS, help="the f-number you shoot at (Av)")
        p.add_argument("--overlap", type=float, default=0.25, help="overlap of neighbouring zones, 0..1")
        if name == "card":
            p.add_argument("--out", type=Path, default=ROOT / "work" / "focus-bracket")
            p.add_argument("--countdown", type=int, default=10, help="seconds of blinking before frame 1")
            p.add_argument("--save-ms", type=int, default=SAVE_MS, help="pause after each shot, ms")
            p.add_argument("--focus-after", default="Select")
            p.add_argument("--snap-after", type=int, default=250)
    c = sub.add_parser("check")
    c.add_argument("dcim", type=Path)
    c.add_argument("--first", required=True)
    c.add_argument("--plan", type=Path, default=ROOT / "work" / "focus-bracket" / "plan.json")
    a = ap.parse_args()
    if a.cmd == "check":
        sys.exit(check_photos(a))
    try:
        ds = plan(a.near, a.far, a.f, a.overlap)
    except ValueError as e:
        sys.exit(str(e))
    print(table(ds, a.f))
    if a.cmd == "plan":
        return
    bad = check()
    if bad:
        sys.exit("ApplySnapDistance does not match this image:\n  " + "\n  ".join(bad))
    files, steps, rows = card_files(ds, countdown=a.countdown, save_ms=a.save_ms,
                                    focus_after=a.focus_after, snap_after=a.snap_after)
    if a.out.exists():
        shutil.rmtree(a.out)
    for rel, data in files.items():
        f = a.out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    (a.out / "plan.json").write_text(json.dumps(rows, indent=1))
    r = subprocess.run(["sh", "-n", str(a.out / rc.CARD_DIR / "SESSION.SH")], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    print(f"SESSION (bracket): {len(rows)} frames in {len(rounds(rows))} round(s), about "
          f"{es.seconds(steps) / 60:.1f} min with a {a.countdown} s countdown; ends with Focus {a.focus_after}, "
          f"snap {a.snap_after} cm. Shoot in Av at f/{a.f:g}.")
    print(f"wrote {a.out}/SCRIPT (copy onto the card root) and {a.out}/plan.json")


if __name__ == "__main__":
    main()
