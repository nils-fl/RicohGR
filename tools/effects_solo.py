#!/usr/bin/env python3
"""Automated grain test (arm B, docs/image-effects.html §5a): the camera shoots stock and patched
grain by itself after power-on, using the recipe card's solo runs (tools/recipe_card.py).

    python3 tools/effects_solo.py card [--out work/effects-solo] [--grain LOW,MID,HIGH]
        writes the card root (copy SCRIPT/ onto the card, keep its other files) and plan.json
    python3 tools/effects_solo.py import CARD_DCIM_FOLDER --first R0001234 [--log CARD/SCRIPT/RECIPE/RECIPE.LOG]
        after the run: copies the photos to work/effects-tests/<run>/, checks image control and
        EV against the plan, measures the grain of a centre crop and writes crops and report.txt

The solo run SESSION (one power-on; SESSION.ON is deleted when it starts):
  settings:  the AF lamp is on steadily for about 12 s while Custom 1 is set up
  countdown: then it blinks slowly for 30 s (point the camera at a plain, evenly lit,
             mid-grey surface, slightly out of focus is fine, and leave it); the first
             shutter release follows the last blink, then one shot about every 10 s
  stock:     Monotone grain Off, Low, Mid, High; Negative Film grain Off, High
  B on:      the grain-scale words in RAM (B.BIN), then Monotone Low, Mid, High; Negative Film High
  B off:     B.BIN restore (stock words back), Monotone High again (must match the stock High)
  A on:      the capability-table words (A.BIN) stay in RAM until power-off, for the RAW
             development check by hand (Positive Film: is there a Grain row?)
  end:       Custom 1 gets the user's recipe back, Highlight Correction Auto, EV 0, ISO Auto; the
             AF lamp stays on for 10 s
Every shot uses Custom 1, ISO 200, EV 0, Auto WB, every other parameter at 0, Highlight and
Shadow Correction off. Patched Low (0x100) is the stock High scale, so those two shots should
measure the same: a check on the measurement itself.

The patch writers are the multipatch binaries of tools/effects_card.py: they check the build and
every stock word before writing, and log their exit code ("solo patch B 0" in RECIPE.LOG; 0
written, 6 already, 3 other build, 5 unexpected word). They live in RAM; a power-off undoes them.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import af_debug_card as card  # noqa: E402
import effects_card as ec  # noqa: E402
import recipe_card as rc  # noqa: E402
import recipes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SLOT = 1
ISO_APEX = "6"                                 # ISO 200
UD_PAUSE, ACTIVATE_PAUSE, EV_PAUSE = 300, 300, 500
HALF_MS, FULL_MS, SAVE_MS = 1500, 300, 3500
COUNTDOWN_S, END_LAMP_MS = 30, 10000
SHOOTING = {"wb": "auto", "wb_shift_ab": "0", "wb_shift_gm": "0",
            "highlight_correction": "off", "shadow_correction": "off", "peripheral_correction": "on"}
USER_RECIPE = (ROOT.parent / "GRLab" / "RECIPES.TXT", "Nils' every day")
FX = "SCRIPT/RECIPE/FX"
RAM = f"{rc.WWW}/fx"
CROP = 1024                                    # centre crop for the grain measure, pixels


def at_zero(control):
    """[control] with every parameter the slot takes at its starting value (as grcal/session.py)."""
    body = {"control": control}
    for key in sorted(recipes.SUPPORTED[control]):
        if key in recipes.USERDATA_SKIP:
            continue
        if key == "toning":
            body[key] = "0" if control == "retro" else recipes.TONING[control][0]
        elif key == "hdr_strength":
            body[key] = "2"
        else:
            body[key] = "0"
    return body


GRAIN_NAMES = ("off", "low", "mid", "high")


def shots():
    """[(phase, control, grain index)] in shooting order; phases: stock, B, check."""
    out = [("stock", "monotone", g) for g in (0, 1, 2, 3)] + [("stock", "negative_film", g) for g in (0, 3)]
    out += [("B", "monotone", g) for g in (1, 2, 3)] + [("B", "negative_film", 3)]
    return out + [("check", "monotone", 3)]


def press():
    return [("cmd", "SW_sws 1", HALF_MS), ("cmd", "SW_swr 1", FULL_MS), ("cmd", "SW_swr 0", 300),
            ("cmd", "SW_sws 0", SAVE_MS)]


def blink(seconds, on=300, off=1700):
    return [s for _ in range(seconds * 1000 // (on + off))
            for s in (("cmd", "Af_Led_On", on), ("cmd", "Af_Led_Off", off))]


DEVELOP_WAIT_S = 6   # the last shot may still be developing (grain is applied then) when its batch is acked


def patch(arm, restore=False):
    word = "restore" if restore else "patch"
    return ("sh", f"sleep {DEVELOP_WAIT_S}; {RAM}/{arm}.BIN{' restore' if restore else ''}; log {word} {arm} $?")


def restore_user(user_recipe):
    lines = []
    if user_recipe and Path(user_recipe[0]).exists():
        _, body = rc.recipe_from(Path(user_recipe[0]), user_recipe[1])
        lines = recipes.userdata(body, SLOT)["lines"]
    return ([("log", "restore settings")] + [("cmd", ln, UD_PAUSE) for ln in lines]
            + [("cmd", "UserData Set StillHighlightCorrect Auto", UD_PAUSE),
               ("cmd", f"UserData Set StillActiveImageControlSet No{SLOT}", ACTIVATE_PAUSE),
               ("cmd", "camctl_set_exp_comp 0.0", EV_PAUSE), ("cmd", "camctl_set_iso_auto on", EV_PAUSE),
               ("cmd", "Af_Led_On", END_LAMP_MS), ("cmd", "Af_Led_Off", 300)])


def steps_for(user_recipe=USER_RECIPE):
    # Settings first, with the AF lamp on steadily (they take about 12 s), then the countdown, so
    # the first shutter release follows the last blink at once (a first run was switched off in
    # the silent gap between the countdown and shot 1).
    steps = [("sh", f"busybox mkdir -p {RAM}; cp {rc.D}/FX/A.BIN {rc.D}/FX/B.BIN {RAM}/; "
                    f"busybox chmod 755 {RAM}/A.BIN {RAM}/B.BIN; log fx copied $?"),
             ("log", "settings"), ("cmd", "Af_Led_On", 300),
             ("cmd", "camctl_set_iso_auto off", 500), ("cmd", f"camctl_set_iso {ISO_APEX}", 500),
             ("cmd", "camctl_set_exp_comp 0.0", EV_PAUSE)]
    sent, plan, phase = {}, [], None
    ph0, control0, g0 = shots()[0]
    for ln in recipes.userdata({**at_zero(control0), "grain": str(g0), **SHOOTING}, SLOT)["lines"]:
        steps.append(("cmd", ln, UD_PAUSE))
        sent[ln.split()[2]] = ln.split()[3:]
    steps += [("cmd", f"UserData Set StillActiveImageControlSet No{SLOT}", ACTIVATE_PAUSE),
              ("cmd", "Af_Led_Off", 1000), ("log", "countdown")] + blink(COUNTDOWN_S)
    for n, (ph, control, g) in enumerate(shots(), 1):
        if ph != phase:
            if ph == "B":
                steps.append(patch("B"))
            elif ph == "check":
                steps.append(patch("B", restore=True))
            phase = ph
        body = {**at_zero(control), "grain": str(g), **SHOOTING}
        bad = recipes.check_recipe(f"shot {n}", body)
        if bad:
            raise SystemExit("; ".join(bad))
        label = f"{ph} {control} grain {GRAIN_NAMES[g]}"
        steps.append(("log", f"shot {n} {label}"))
        for ln in recipes.userdata(body, SLOT)["lines"]:
            name, value = ln.split()[2], ln.split()[3:]
            if sent.get(name) != value:
                steps.append(("cmd", ln, UD_PAUSE))
                sent[name] = value
        steps.append(("cmd", f"UserData Set StillActiveImageControlSet No{SLOT}", ACTIVATE_PAUSE))
        steps += press()
        plan.append({"n": n, "phase": ph, "control": control, "grain": g, "label": label, "recipe": body})
    steps.append(patch("A"))
    return steps + restore_user(user_recipe), plan


def seconds(steps):
    cmds = [s for s in steps if s[0] == "cmd"]
    return sum(s[2] for s in cmds) / 1000 + 0.6 * (len(cmds) / 5 + sum(s[0] != "cmd" for s in steps))


def card_files(grain=ec.DEFAULT_GRAIN, user_recipe=USER_RECIPE):
    sites = ec.arm_sites(grain)
    if not sites["B"]:
        raise SystemExit("stock grain scales: nothing to test")
    steps, plan = steps_for(user_recipe)
    files = dict(rc.card_files())
    files[f"{rc.CARD_DIR}/SESSION.SH"] = rc.solo_script("SESSION", steps)
    files[f"{rc.CARD_DIR}/SESSION.ON"] = b"armed\n"
    files[f"{FX}/A.BIN"] = card.multipatch_bin(sites["A"])
    files[f"{FX}/B.BIN"] = card.multipatch_bin(sites["B"])
    return files, steps, plan


# ---- import --------------------------------------------------------------------------

def grain_measure(jpg):
    """Std of the fine detail (luma minus a 4 px Gaussian blur) in the centre crop, and the crop."""
    import numpy as np
    from PIL import Image, ImageFilter
    im = Image.open(jpg).convert("L")
    w, h = im.size
    crop = im.crop(((w - CROP) // 2, (h - CROP) // 2, (w + CROP) // 2, (h + CROP) // 2))
    a = np.asarray(crop, dtype=np.float64)
    hp = a - np.asarray(crop.filter(ImageFilter.GaussianBlur(4)), dtype=np.float64)
    return {"grain_std": round(float(hp[8:-8, 8:-8].std()), 3), "mean": round(float(a.mean()), 1)}, crop


def maker_slots(jpg):
    blob = subprocess.run(["exiftool", "-u", "-b", "-Pentax:Pentax_0x0247", str(jpg)],
                          capture_output=True, check=True).stdout
    import struct
    return list(struct.unpack("<22h", blob[:44]))


def import_run(a):
    plan = json.loads(a.plan.read_text())
    stems = sorted({p.stem for p in a.dcim.glob("R*.JPG") if p.stem >= a.first})
    if len(stems) < len(plan):
        raise SystemExit(f"{len(stems)} photos from {a.first} on, the plan has {len(plan)}")
    out = a.out / a.first
    out.mkdir(parents=True, exist_ok=True)
    rows, bad = [], 0
    for p, stem in zip(plan, stems):
        jpg = a.dcim / f"{stem}.JPG"
        for f in (jpg, a.dcim / f"{stem}.DNG"):
            if f.exists() and not (out / f.name).exists():
                shutil.copy2(f, out / f.name)
        got = rc.maker_settings(out / jpg.name)
        m, crop = grain_measure(out / jpg.name)
        crop.save(out / f"{p['n']:02d}-{p['phase']}-{p['control']}-{GRAIN_NAMES[p['grain']]}.png")
        flag = "" if got["control"] == p["control"] else f"  MISMATCH control {got['control']}"
        bad += bool(flag)
        rows.append({**p, "file": stem, **m, "slots": maker_slots(out / jpg.name)})
        print(f"{stem}  {p['label']:32s} grain_std {m['grain_std']:7.3f}  mean {m['mean']:6.1f}{flag}")
    # which maker-note slot of tag 0x0247 records grain: the one that follows the plan on stock shots
    stock = [r for r in rows if r["phase"] == "stock" and r["control"] == "monotone"]
    cand = [i for i in range(22) if len({r["slots"][i] for r in stock}) == 4]
    lines = [f"{r['file']}  {r['label']:32s} grain_std {r['grain_std']:7.3f}  mean {r['mean']:6.1f}" for r in rows]
    by = {(r["phase"], r["control"], r["grain"]): r["grain_std"] for r in rows}
    lines += ["", "checks:",
              f"  patched Low vs stock High (same scale 0x100): {by.get(('B', 'monotone', 1))} vs {by.get(('stock', 'monotone', 3))}",
              f"  restore: check High vs stock High: {by.get(('check', 'monotone', 3))} vs {by.get(('stock', 'monotone', 3))}",
              f"  maker-note 0x0247 slot(s) that follow the grain setting: {cand}"]
    if a.log and a.log.exists():
        lines += ["", "RECIPE.LOG (solo):"] + [l for l in a.log.read_text().splitlines() if " solo " in f" {l} "]
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    (out / "rows.json").write_text(json.dumps(rows, indent=1))
    print("\n".join(lines[len(rows):]))
    print(f"{len(rows)} shots into {out}; {bad} mismatches")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=ROOT / "work" / "effects-solo")
    c.add_argument("--grain", type=ec.parse_grain, default=ec.DEFAULT_GRAIN)
    i = sub.add_parser("import")
    i.add_argument("dcim", type=Path)
    i.add_argument("--first", required=True, help="file stem of the run's first photo, e.g. R0007001")
    i.add_argument("--plan", type=Path, default=ROOT / "work" / "effects-solo" / "plan.json")
    i.add_argument("--log", type=Path, help="the card's SCRIPT/RECIPE/RECIPE.LOG")
    i.add_argument("--out", type=Path, default=ROOT / "work" / "effects-tests")
    a = ap.parse_args()
    if a.cmd == "import":
        return import_run(a)
    bad = ec.em.check()
    if bad:
        sys.exit("effects_map.py does not match this image:\n  " + "\n  ".join(bad))
    files, steps, plan = card_files(a.grain)
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
    print(f"SESSION: {len(plan)} shots, about {seconds(steps) / 60:.1f} min; grain scales "
          + ", ".join(f"{v:#x}" for v in a.grain))
    for p in plan:
        print(f"  {p['n']:2d}  {p['label']}")
    print(f"wrote {a.out}/SCRIPT (copy onto the card root) and {a.out}/plan.json")


if __name__ == "__main__":
    main()
