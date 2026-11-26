#!/usr/bin/env python3
"""Focus bracketing, phase 0 (docs/focus-bracketing.html): a probe card for the stock camera. It
finds out whether snap distances outside the menu list move the lens, and what the card's Linux side
can read for an in-camera trigger.

    python3 tools/focus/focus_probe.py check
        checks the snap-distance matcher words in the decoded 1.60 image
    python3 tools/focus/focus_probe.py card [--out work/focus-probe] [--focus-after Select] [--snap-after 250]
        writes the card root (copy SCRIPT/ onto the card, keep its other files) and plan.json
    python3 tools/focus/focus_probe.py import CARD_DCIM_FOLDER --first R0001234 [--log CARD/SCRIPT/RECIPE/RECIPE.LOG]
        after the run: copies the photos to work/focus-tests/<first>/, estimates where each frame is
        focused from tile sharpness, and writes report.txt, sharpness maps and rows.json

Why a patch: the engineering shell's "UserData Set StillSnapDistanceCm" matcher (0x532de3dc) accepts
only 30, 100, 150, 200, 250, 350, 500 and Infinity, and each branch hands a plain cm value to the
setter (0x53242680, stored unchecked at +0x130). The probe rewrites the one instruction of the "30"
branch (0x532de4e4, mov r1,#30) to movw r1,#N in RAM, sends "... 30", shoots, and puts the stock word
back. Only the shell's UserData parser reaches that branch, so shooting code is unchanged.

The solo run SESSION (one power-on; SESSION.ON is deleted when it starts):
  start:     /v1/props and /v1/photos from webapid on 127.0.0.1 are logged as field names, with values
             only for focus, drive and mode fields (never Wi-Fi names, keys, serials or file names)
  settings:  Focus Snap, AF lamp on; then a 20 s slow blink: put the camera on a table or tripod,
             facing a scene with things from about 30 cm to far away (along a table to a window)
  frames:    snap 30 (stock), 45*, 70*, 100, 120*, 150, 250, 500 cm, Infinity, then 45* and 120* again with
             a Focus Infinity -> Snap switch after the setting, then 30 again (* = patched value)
  end:       Focus --focus-after, snap distance --snap-after; props and photos again; lamp on for 10 s
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
import recipe_card as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BASE, OFF = 0x53000000, 0x10870
DECODED = ROOT / "work" / "fwdc243b-v160.decoded.bin"

# ---- the snap-distance matcher (Read, GR IIIx 1.60) ------------------------------------
MATCHER = 0x532DE3DC          # UserData Set StillSnapDistanceCm <value>
SNAP_SITE = 0x532DE4E4        # the "30" branch: mov r1, #30, then bl the setter
SNAP_STOCK = 0xE3A0101E
SNAP_FACTS = (                 # (address, word): the branch around the site, as read
    (0x532DE3F4, 0xE34513B2),  # movt r1, #0x53b2 ('30')
    (0x532DE400, 0x0A000035),  # beq to the "30" branch
    (0x532DE4DC, 0xEBFCB229),  # bl 0x5320ad88
    (0x532DE4E0, 0xEBFCB38D),  # bl 0x5320b31c
    (SNAP_SITE, SNAP_STOCK),   # mov r1, #30
    (0x532DE4E8, 0xEBFD9064),  # bl 0x53242680 (the setter)
    (0x532AF4D0, 0xE354001E),  # ApplySnapDistance: cmp r4, #30
    (0x532AF4D4, 0x03A01F4B),  # moveq r1, #300
    (0x532AF4D8, 0x0AFFFFD0),  # beq 0x532af420 (send)
    (0x532AF4F8, 0xE3A01E19),  # mov r1, #400 (40 cm)
    (0x532AF4FC, 0xEAFFFFC7),  # b 0x532af420
    (0x532AF420, 0xE1A00005),  # mov r0, r5, then bl 0x532aeb68
)
MENU_CM = (30, 100, 150, 200, 250, 350, 500)   # the matcher's list, -1 = Infinity
FOCUS_VALUES = ("AutoArea", "AutoAreaM", "ZoneSelect", "Select", "Pinpoint", "Follow", "Continuous",
                "Mf", "Snap", "Infinity")      # StillFocus matcher 0x532ddd7c
MIN_CM, MAX_CM = 10, 10000


def movw_r1(n: int, cond: int = 0xE) -> int:
    """movw r1, #n with condition cond (0xE always, 0x0 eq)."""
    if not 0 <= n <= 0xFFFF:
        raise ValueError(n)
    return (cond << 28) | 0x03001000 | ((n >> 12) & 0xF) << 16 | (n & 0xFFF)


def snap_word(cm: int) -> int:
    """movw r1, #cm (condition always), the replacement for SNAP_STOCK."""
    if not MIN_CM <= cm <= MAX_CM:
        raise ValueError(f"snap distance {cm} cm outside {MIN_CM}..{MAX_CM}")
    return movw_r1(cm)


def snap_sites(cm: int):
    return [(SNAP_SITE, SNAP_STOCK, snap_word(cm))]


# ApplySnapDistance (0x532af3d8, Model/FocusModel.cpp; Read): when Snap is selected it maps the stored
# cm to mm in a switch and sends the mm to the lens task (0x532aeb68, message 0x4241080b). Any other cm
# value asserts and sends nothing. Probe 2 rewrites the mm of the 30 cm case (and of 40 cm, which the
# value becomes in one mode, 0x532af020) for the moment of a Focus Infinity -> Snap switch.
APPLY = 0x532AF3D8
APPLY_SITES = ((0x532AF4D4, 0x03A01F4B, 0x0),   # moveq r1, #300 (30 cm)
               (0x532AF4F8, 0xE3A01E19, 0xE))   # mov r1, #400 (40 cm)
MIN_MM, MAX_MM = 100, 0xFFFF


def apply_sites(mm: int):
    if not MIN_MM <= mm <= MAX_MM:
        raise ValueError(f"snap distance {mm} mm outside {MIN_MM}..{MAX_MM}")
    return [(a, stock, movw_r1(mm, cond)) for a, stock, cond in APPLY_SITES]


def check(image: Path = DECODED):
    """Problems with the matcher facts in the decoded image ([] when they all hold)."""
    data = image.read_bytes()
    bad = []
    for a, want in SNAP_FACTS:
        o = a - BASE + OFF
        got = int.from_bytes(data[o:o + 4], "little")
        if got != want:
            bad.append(f"{a:#010x}: {got:#010x}, expected {want:#010x}")
    return bad


# ---- the probe session ---------------------------------------------------------------------
FOCUS_DIR = "SCRIPT/RECIPE/FOCUS"
RAM = f"{rc.WWW}/focus"
COUNTDOWN_S = 20
SET_PAUSE, SETTLE_MS, SWITCH_MS = 300, 1000, 800
# (cm or None for Infinity, how, switch Focus Infinity -> Snap after the setting). how: "stock" sends the
# menu value; "shell" patches the shell matcher (probe 1); "apply" patches ApplySnapDistance's mm and
# sends 30 (probe 2, always with the switch, which is when ApplySnapDistance runs).
PROBES = {
    1: ((30, "stock", False), (45, "shell", False), (70, "shell", False), (100, "stock", False),
        (120, "shell", False), (150, "stock", False), (250, "stock", False), (500, "stock", False),
        (None, "stock", False), (45, "shell", True), (120, "shell", True), (30, "stock", False)),
    2: ((30, "stock", True), (45, "apply", True), (70, "apply", True), (100, "stock", True),
        (120, "apply", True), (150, "stock", True), (20, "apply", True), (None, "stock", True),
        (30, "stock", True)),
}
FRAMES = PROBES[1]

# Shell helpers (one line each: solo scripts take single "sh" lines). api PATH: one GET to webapid on
# 127.0.0.1:80 into $w/api.txt. The filter splits the JSON at commas, braces and brackets (so every name
# is judged on its own, also inside nested objects) and logs only names, plus
# the values of focus, drive and mode fields; names that may hold the Wi-Fi name or key, serials,
# addresses or file names are logged as "hidden". photos logs only how many JPGs the list holds.
# busybox awk reads "name (" as a call of a user function name (probe 1: "Call to undefined function"),
# so the program never puts a variable right before a parenthesis.
PRIVATE = "ssid|key|pass|serial|mac|addr|name|bt|wlan|owner|copyright|artist|token|uuid"
SHOW = "focus|snap|drive|shoot|mode|bracket|timer|exposure|capture|state|battery|version|errcode"
API_SH = ("api() { ( printf 'GET %s HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\n\\r\\n' \"$1\"; sleep 2 ) "
          "| busybox nc 127.0.0.1 80 >$w/api.txt 2>/dev/null & k=$!; sleep 4; kill $k 2>/dev/null; "
          "read s _ <$w/api.txt 2>/dev/null; log api $1 ${s:-none} $(busybox wc -c <$w/api.txt 2>/dev/null); }")
PROPS_SH = ("props() { api /v1/props; busybox sed 's/[][{}]/,/g' $w/api.txt 2>/dev/null "
            "| busybox awk -v RS=, -v t=\"$1\" '{ if (!match($0, /\"[A-Za-z0-9_]+\"[ ]*:/)) next; "
            "k = substr($0, RSTART + 1, RLENGTH - 1); sub(/\"[ ]*:$/, \"\", k); v = substr($0, RSTART + RLENGTH); "
            f"gsub(/[^A-Za-z0-9_.+-]/, \"\", v); l = tolower(k); if (l ~ /{PRIVATE}/) v = \"hidden\"; "
            f"else if (l !~ /{SHOW}/) v = \"\"; x = \"\"; if (v != \"\") x = \"=\" substr(v, 1, 24); o = o \" \" k x; "
            "if (length(o) > 160) { print \"props\", t, o; o = \"\" } } END { if (o != \"\") print \"props\", t, o }' "
            "| while read l; do log $l; done; }")
PHOTOS_SH = ("photos() { api /v1/photos; n=$(busybox awk '{ n += gsub(/\\.JPG/, \"\") } END { print n + 0 }' "
             "$w/api.txt 2>/dev/null); log photos $1 jpg ${n:-0}; }")


def label(cm):
    return "infinity" if cm is None else f"{cm}cm"


def bin_name(cm, how="shell"):
    return f"{'D' if how == 'shell' else 'A'}{cm}.BIN"


def patch_files(frames):
    """{bin name: sites} for the patched frames."""
    out = {}
    for cm, how, _ in frames:
        if how == "shell":
            out[bin_name(cm, how)] = snap_sites(cm)
        elif how == "apply":
            out[bin_name(cm, how)] = apply_sites(cm * 10)
    return out


def steps_for(focus_after="Select", snap_after=250, probe=1):
    if focus_after not in FOCUS_VALUES:
        raise SystemExit(f"--focus-after must be one of {', '.join(FOCUS_VALUES)}")
    if snap_after not in MENU_CM:
        raise SystemExit(f"--snap-after must be one of {', '.join(map(str, MENU_CM))}")
    frames = PROBES[probe]
    copy = " ".join(f"{rc.D}/FOCUS/{b}" for b in sorted(patch_files(frames)))
    steps = [("sh", f"busybox mkdir -p {RAM}; cp {copy} {RAM}/; busybox chmod 755 {RAM}/*.BIN; log focus copied $?"),
             ("sh", API_SH), ("sh", PROPS_SH), ("sh", PHOTOS_SH),
             ("sh", "props start; photos start"),
             ("log", "settings"), ("cmd", "Af_Led_On", 300),
             ("cmd", "UserData Set StillFocus Snap", SET_PAUSE),
             ("cmd", "UserData Set StillSnapDistanceCm 30", SETTLE_MS),
             ("cmd", "Af_Led_Off", 1000), ("log", "countdown")] + es.blink(COUNTDOWN_S)
    plan = []
    for n, (cm, how, switch) in enumerate(frames, 1):
        p = how != "stock"
        text = {"stock": "stock", "shell": "patched", "apply": "apply"}[how] + (" switch" if switch else "")
        steps.append(("log", f"frame {n} {label(cm)} {text}"))
        b = f"{RAM}/{bin_name(cm, how)}"
        if p:
            steps.append(("sh", f"{b}; log patch {bin_name(cm, how)} $?"))
        value = "Infinity" if cm is None else ("30" if p else str(cm))
        steps.append(("cmd", f"UserData Set StillSnapDistanceCm {value}", SETTLE_MS))
        if how == "shell":
            steps.append(("sh", f"{b} restore; log restore {bin_name(cm, how)} $?"))
        if switch:
            steps += [("cmd", "UserData Set StillFocus Infinity", SWITCH_MS),
                      ("cmd", "UserData Set StillFocus Snap", SWITCH_MS)]
        if how == "apply":   # ApplySnapDistance has run at the switch; stock words back before the shot
            steps.append(("sh", f"{b} restore; log restore {bin_name(cm, how)} $?"))
        if n == 2:
            steps.append(("sh", "props frame2"))
        steps += es.press()
        plan.append({"n": n, "cm": cm, "how": how, "patched": p, "switch": switch, "label": f"{label(cm)} {text}"})
    steps += [("log", "restore settings"),
              ("cmd", f"UserData Set StillSnapDistanceCm {snap_after}", SET_PAUSE),
              ("cmd", f"UserData Set StillFocus {focus_after}", SET_PAUSE),
              ("sh", "props end; photos end"),
              ("cmd", "Af_Led_On", es.END_LAMP_MS), ("cmd", "Af_Led_Off", 300)]
    return steps, plan


def card_files(focus_after="Select", snap_after=250, probe=1):
    steps, plan = steps_for(focus_after, snap_after, probe)
    files = dict(rc.card_files())
    files[f"{rc.CARD_DIR}/SESSION.SH"] = rc.solo_script("SESSION", steps)
    files[f"{rc.CARD_DIR}/SESSION.ON"] = b"armed\n"
    for name, sites in patch_files(PROBES[probe]).items():
        files[f"{FOCUS_DIR}/{name}"] = card.multipatch_bin(sites)
    return files, steps, plan


# ---- import: where is each frame focused? --------------------------------------------------
TILES = (24, 16)               # columns, rows


def dioptre(cm):
    return 0.0 if cm is None else 100.0 / cm


def tile_sharpness(jpg, tiles=TILES):
    """Mean absolute Laplacian of the luma per tile (half resolution), as an array [rows, cols]."""
    import numpy as np
    from PIL import Image
    im = Image.open(jpg).convert("L")
    im = im.resize((im.width // 2, im.height // 2))
    a = np.asarray(im, dtype=np.float64)
    lap = np.abs(4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])
    cols, rows = tiles
    h, w = lap.shape
    return np.array([[lap[r * h // rows:(r + 1) * h // rows, c * w // cols:(c + 1) * w // cols].mean()
                      for c in range(cols)] for r in range(rows)])


def estimate(sharp, stock_d):
    """Where each frame is focused, in dioptres (0 = infinity).

    sharp: [frames, tiles] sharpness; stock_d: {frame index: dioptre} for the frames whose focus is
    known (the stock menu values). Model (Inferred: defocus blur grows with the dioptre error, and fine
    detail falls off slowly, so blurred frames still carry information):
        sharp[f, t] = c_t * (b + 1 / (1 + ((D_f - d_t) / w) ** 2))
    fitted in log space for every tile depth d_t and contrast c_t, the unknown D_f, the width w and
    the floor b; the stock D_f stay fixed. Flat tiles (the lowest quarter by contrast) are left out.
    Returns ([dioptre per frame], {"w": w, "b": b, "rms": [log residual rms per frame]})."""
    import numpy as np
    from scipy.optimize import least_squares
    from scipy.sparse import lil_matrix
    s = np.maximum(np.asarray(sharp, dtype=np.float64), 1e-9)
    nf = s.shape[0]
    s = s[:, s.max(axis=0) >= np.percentile(s.max(axis=0), 25)]
    nt = s.shape[1]
    free = [f for f in range(nf) if f not in stock_d]
    rel = s / s.max(axis=0)
    known = sorted(stock_d)
    # start each unknown frame at the stock frame it looks most like, each tile at its sharpest frame
    d0 = np.zeros(nf)
    for f in range(nf):
        d0[f] = stock_d[f] if f in stock_d else stock_d[max(known, key=lambda k: np.corrcoef(rel[f], rel[k])[0, 1])]
    x0 = np.concatenate([d0[np.argmax(s, axis=0)], np.log(s.max(axis=0)), d0[free], [np.log(0.3), np.log(0.05)]])
    lo = np.concatenate([np.full(nt, -0.3), np.full(nt, -np.inf), np.full(len(free), -0.3), [np.log(0.02), np.log(1e-4)]])
    hi = np.concatenate([np.full(nt, 6.0), np.full(nt, np.inf), np.full(len(free), 6.0), [np.log(3.0), np.log(1.0)]])
    x0 = np.clip(x0, lo + 1e-9, hi - 1e-9)
    ls = np.log(s)

    def unpack(x):
        d = d0.copy()
        d[free] = x[2 * nt:2 * nt + len(free)]
        return x[:nt], x[nt:2 * nt], d, np.exp(x[-2]), np.exp(x[-1])

    def resid(x):
        dt, lc, df, w, b = unpack(x)
        model = lc[None, :] + np.log(b + 1 / (1 + ((df[:, None] - dt[None, :]) / w) ** 2))
        return (ls - model).ravel()

    jac = lil_matrix((nf * nt, x0.size), dtype=int)
    for f in range(nf):
        rows = f * nt + np.arange(nt)
        jac[rows, np.arange(nt)] = 1
        jac[rows, nt + np.arange(nt)] = 1
        if f in free:
            jac[rows, 2 * nt + free.index(f)] = 1
        jac[rows, x0.size - 2] = 1
        jac[rows, x0.size - 1] = 1
    fit = least_squares(resid, x0, bounds=(lo, hi), jac_sparsity=jac, x_scale="jac", max_nfev=200)
    _, _, df, w, b = unpack(fit.x)
    r = fit.fun.reshape(nf, nt)
    return [float(v) for v in df], {"w": float(w), "b": float(b), "rms": [float(v) for v in np.sqrt((r ** 2).mean(axis=1))]}


def similarity(sharp, a, b):
    import numpy as np
    s = np.asarray(sharp, dtype=np.float64)
    rel = s / np.maximum(s.max(axis=0), 1e-9)
    return float(np.corrcoef(rel[a], rel[b])[0, 1])


def import_run(a):
    import numpy as np
    from PIL import Image
    plan = json.loads(a.plan.read_text())
    stems = sorted({p.stem for p in a.dcim.glob("R*.JPG") if p.stem >= a.first})
    if len(stems) < len(plan):
        raise SystemExit(f"{len(stems)} photos from {a.first} on, the plan has {len(plan)}")
    out = a.out / a.first
    out.mkdir(parents=True, exist_ok=True)
    sharp = []
    for p, stem in zip(plan, stems):
        for f in (a.dcim / f"{stem}.JPG", a.dcim / f"{stem}.DNG"):
            if f.exists() and not (out / f.name).exists():
                shutil.copy2(f, out / f.name)
        m = tile_sharpness(out / f"{stem}.JPG")
        sharp.append(m.ravel())
        p["file"] = stem
    sharp = np.array(sharp)
    stock = {i: dioptre(p["cm"]) for i, p in enumerate(plan) if not p["patched"] and i < len(plan) - 1}
    est, fitinfo = estimate(sharp, stock)
    rel = sharp / np.maximum(sharp.max(axis=0), 1e-9)
    for i, p in enumerate(plan):   # sharpness maps: bright = this frame is the sharpest there
        im = Image.fromarray((rel[i].reshape(TILES[1], TILES[0]) ** 2 * 255).astype("uint8"))
        im.resize((TILES[0] * 20, TILES[1] * 20), Image.NEAREST).save(out / f"{p['n']:02d}-{label(p['cm'])}.png")
    first30 = next(i for i, p in enumerate(plan) if p["cm"] == 30)
    lines = [f"{'file':9s} {'frame':28s} {'asked':>8s} {'found':>8s}  same as 30 cm  fit rms"]
    for i, p in enumerate(plan):
        found = est[i]
        found_m = "inf" if found < 0.05 else f"{1 / found:.2f} m"
        asked = "inf" if p["cm"] is None else f"{p['cm'] / 100:.2f} m"
        lines.append(f"{p['file']:9s} {p['label']:28s} {asked:>8s} {found_m:>8s}  {similarity(sharp, i, first30):5.2f}         {fitinfo['rms'][i]:.2f}")
        p["found_dioptre"] = round(found, 3)
    lines += ["", f"fit: blur width {fitinfo['w']:.2f} dioptres, floor {fitinfo['b']:.3f} (stock frames held at their "
              "menu distances; found values of stock frames are those)",
              "", "Read: a patched frame that works sits between its stock neighbours and is not the same as "
              "the 30 cm frame (similarity well below 1). Frame 12 (30 cm after the restores) must match frame 1."]
    if a.log and a.log.exists():
        lines += ["", "RECIPE.LOG (solo):"] + [l for l in a.log.read_text().splitlines() if " solo " in f" {l} "]
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    (out / "rows.json").write_text(json.dumps(plan, indent=1))
    print("\n".join(lines))
    print(f"{len(plan)} frames into {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=ROOT / "work" / "focus-probe")
    c.add_argument("--probe", type=int, choices=sorted(PROBES), default=2,
                   help="1: patch the shell matcher (run 2026-11-25: rounded up to menu values); "
                        "2: patch ApplySnapDistance's mm and switch Focus Infinity -> Snap")
    c.add_argument("--focus-after", default="Select", help="Focus mode at the end (UserData value, e.g. "
                   "AutoArea, Select, Pinpoint, Snap)")
    c.add_argument("--snap-after", type=int, default=250, help="snap distance at the end, cm (a menu value)")
    i = sub.add_parser("import")
    i.add_argument("dcim", type=Path)
    i.add_argument("--first", required=True, help="file stem of the run's first photo, e.g. R0007301")
    i.add_argument("--plan", type=Path, default=ROOT / "work" / "focus-probe" / "plan.json")
    i.add_argument("--log", type=Path, help="the card's SCRIPT/RECIPE/RECIPE.LOG")
    i.add_argument("--out", type=Path, default=ROOT / "work" / "focus-tests")
    a = ap.parse_args()
    if a.cmd == "import":
        return import_run(a)
    bad = check()
    if bad:
        sys.exit("snap matcher does not match this image:\n  " + "\n  ".join(bad))
    if a.cmd == "check":
        print(f"ok: snap matcher at {MATCHER:#x}, patch site {SNAP_SITE:#x} = {SNAP_STOCK:#010x} (mov r1, #30)")
        return
    files, steps, plan = card_files(a.focus_after, a.snap_after, a.probe)
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
    print(f"SESSION (focus probe {a.probe}): {len(plan)} frames, about {es.seconds(steps) / 60:.1f} min; ends with "
          f"Focus {a.focus_after}, snap {a.snap_after} cm")
    for p in plan:
        print(f"  {p['n']:2d}  {p['label']}")
    print(f"wrote {a.out}/SCRIPT (copy onto the card root) and {a.out}/plan.json")


if __name__ == "__main__":
    main()
