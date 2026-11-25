#!/usr/bin/env python3
"""Build a GR IIIx 1.60 update file with the AF setting and the grain effects built in.

Usage:  python3 tools/effects_firmware.py [--grain LOW,MID,HIGH] [--out FILE]

The output is the flashed AF build (tools/af_firmware.py, 19 words) plus the grain changes
tested in RAM (docs/image-effects.html, tools/effects_card.py):

  A  grain on the colour controls but HDR Tone in RAW development: capability table and row
     lists (data used only by the RAW-development files), ImageTuning (5 words) and the still
     planner (3 words), 40 words
  B  grain strength Low, Mid, High; default 0x100, 0x200, 0x300 (1x, 2x, 3x the stock High),
     the scales of the automated run R0007217. Also applies to shots with the monotone
     controls and Negative Film.

Default output: work/firmware-af-effects/FWDC243B.BIN. Every stock word is checked against the
official image before it is replaced. tools/gr_repack.py re-runs the updater's container checks
and tools/patch_guard.py with --allow af --allow effects: the AF words must lie in the
contrast-AF units, and every other changed word must be one of the tested effects words
(effects_map.effect_word_kind). The protected ranges (entry, updater, start-up control) stay
untouched. Offline only: writing the file installs nothing, and this tool never talks to a
camera.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import af_firmware as af  # noqa: E402
import effects_card as ec  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ALLOW = ("af", "effects")


def sites(grain=ec.DEFAULT_GRAIN):
    arms = ec.arm_sites(grain)
    out = af.sites() + arms["A"] + arms["B"]
    addrs = [a for a, _, _ in out]
    if len(set(addrs)) != len(addrs):
        raise SystemExit("two site lists write the same address")
    return out, arms


def patched_payload(original: bytes, grain=ec.DEFAULT_GRAIN) -> bytes:
    mod = bytearray(af.patched_payload(original))
    _, arms = sites(grain)
    for a, old, new in arms["A"] + arms["B"]:
        o = a - af.BASE + af.OFF
        cur = int.from_bytes(original[o:o + 4], "little")
        if cur != old:
            raise SystemExit(f"0x{a:08x}: image has 0x{cur:08x}, expected stock 0x{old:08x}; wrong firmware?")
        mod[o:o + 4] = new.to_bytes(4, "little")
    return bytes(mod)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grain", type=ec.parse_grain, default=ec.DEFAULT_GRAIN,
                    help="grain scales LOW,MID,HIGH (stock 0x40,0x80,0x100; default 0x100,0x200,0x300)")
    ap.add_argument("--out", type=Path, default=ROOT / "work" / "firmware-af-effects" / "FWDC243B.BIN")
    args = ap.parse_args()
    if not af.DECODED.exists():
        raise SystemExit(f"{af.DECODED} missing: run python3 tools/gr_unpack.py {af.OFFICIAL}")
    bad = ec.em.check()
    if bad:
        raise SystemExit("effects_map.py does not match this image:\n  " + "\n  ".join(bad))
    allsites, arms = sites(args.grain)
    print(f"{len(allsites)} words: AF {len(af.sites())}, A {len(arms['A'])}, B {len(arms['B'])} "
          f"(grain {', '.join(f'{v:#x}' for v in args.grain)})")
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload.bin"
        payload.write_bytes(patched_payload(af.DECODED.read_bytes(), args.grain))
        cmd = [sys.executable, str(ROOT / "tools" / "gr_repack.py"), str(af.OFFICIAL), str(payload),
               "--out", str(args.out)]
        for name in ALLOW:
            cmd += ["--allow", name]
        r = subprocess.run(cmd)
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
