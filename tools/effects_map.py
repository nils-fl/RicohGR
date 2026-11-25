#!/usr/bin/env python3
"""Offline map of the GR IIIx 1.60 grain (Particle) path and the candidate effect patch sites.

Usage:  python3 tools/effects_map.py            # check every fact against the decoded image, print the map
        python3 tools/effects_map.py --sites    # also list the candidate words (stock -> new)

Read-only: needs work/fwdc243b-v160.decoded.bin (tools/gr_unpack.py) and capstone. It writes
nothing and never talks to a camera. Findings and labels: docs/image-effects.html.

The path, image control to pixels (Read unless noted):
  settings / RAW-development slot 15 -> CommonProperty byte +0x795 (property id 0xc3)
  -> DigitalFilterProcess::SetEffectParameter 0x53503054 (shading 0x793, clarity 0x794,
     grain 0x795, HDR strength 0x796 -> DigitalFilterConfig +0/+2/+4/+6)
  -> effect routine 0x53680dbc -> grain 0x5367ad3c(level): scale High 0x100, Mid 0x80, Low 0x40
     blends two tone curves (0x538f19d4 up, 0x538f1bd4 down) composited through a noise mask
     with the S-curve 0x538f1dd4.
Gates that keep grain off the colour controls:
  RAW development: capability table 0x53b5a750 (13 rows x 17 slots), column 15;
  shooting: ImageControlModel::SetParticle 0x532c1784 stores only for the four monotone controls
  and Negative Film, and the still planner 0x535131c0 runs the digital-filter stage for the colour
  controls only when shading or clarity is set.
"""
import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rtos_analysis as ra  # noqa: E402

# ---- capability table (RAW development) -------------------------------------------------------
CAP_TABLE = 0x53B5A750
CAP_ROWS, CAP_COLS = 13, 17
SLOT_SHADING, SLOT_CLARITY, SLOT_GRAIN, SLOT_HDR = 13, 14, 15, 16
# model rows (ConvertToRawDevelopmentModelImageControl 0x53288d64; docs/firmware-mod-feasibility.html §4.4)
ROWS = {1: "standard", 2: "vivid", 3: "monotone", 4: "soft_monotone", 5: "hard_monotone",
        6: "hi_contrast_bw", 7: "negative_film", 8: "positive_film", 9: "bleach_bypass",
        10: "retro", 11: "hdr_tone", 12: "cross_process"}
STOCK_GRAIN_ROWS = {3, 4, 5, 6, 7}
# colour controls that get grain; HDR Tone is left out (its own tone path, no shading or clarity)
UNLOCK_GRAIN_ROWS = (1, 2, 8, 9, 10, 12)

# ---- grain strength (shared code: RAW development and shooting) --------------------------------
# 0x5367ad3c: r5 = level from DigitalFilterConfig +4 (1 High, 2 Mid, 3 Low; the settings codes)
GRAIN_SCALE_SITES = {  # level -> (address, stock word, condition, stock scale)
    "mid": (0x5367AE2C, 0x03A06080, 0x0, 0x80),    # moveq r6, #0x80
    "low": (0x5367AE38, 0x03A06040, 0x0, 0x40),    # moveq r6, #0x40
    "high": (0x5367AE3C, 0x13A06C01, 0x1, 0x100),  # movne r6, #0x100
}
GRAIN_CURVES = {"up": 0x538F19D4, "down": 0x538F1BD4, "compose": 0x538F1DD4}

# ---- still planner: does a capture run the digital-filter stage? -------------------------------
PLANNER = 0x535131C0  # reads the image-tone byte +0x77c of the capture's CommonProperty
PLANNER_SHADING_CLARITY = 0x53513218  # "shading != 0 or clarity != 0"
PLANNER_WITH_GRAIN = 0x53513240       # "shading, clarity or grain" (monotone, Negative Film)
PLANNER_SITES = (  # (address, stock word, tones it decides)
    (0x535131CC, 0x9A000011, "0x0b-0x0c"),  # bls -> shading/clarity
    (0x535131F4, 0x9A000007, "0x11-0x13"),  # bls -> shading/clarity
    (0x53513204, 0x0A000003, "0x15"),       # beq -> shading/clarity
)

# ---- facts the map rests on: (address, expected instruction text) -----------------------------
FACTS = [
    (0x53288390 + 0x48, "ldrb r3, [r1, #0xf]"),       # RAW-dev grain setter reads cap[row][15]
    (0x53288424, "b #0x535597bc"),                    # ... then the CommonProperty grain setter
    (0x53559724, "strb r1, [r3, #0xfb5]"),            # 0x5355970c stores the grain byte
    (0x53559734, "strb r2, [r3, #0x795]"),
    (0x535031b0, "ldrb r3, [r5, #0x795]"),            # SetEffectParameter reads grain
    (0x53503094, "ldrb r3, [r5, #0x793]"),            # ... shading
    (0x535030e4, "ldrb r3, [r5, #0x794]"),            # ... clarity
    (0x53503188, "ldrb r3, [r5, #0x796]"),            # ... HDR strength
    (0x53680e78, "ldrsh r3, [r2, #4]"),               # effect routine: grain level != 0 ->
    (0x53680f2c, "bl #0x5367ad3c"),                   # ... grain
    (0x5367ae28, "cmp r5, #2"),
    (0x5367ae48, "movt r1, #0x538f"),                 # curve copies
    (0x5354c58c, "mov r2, #0xc3"),                    # property id of grain
    (0x535131c0, "ldrb r3, [r1, #0x77c]"),            # planner reads the image tone
    (0x53513248, "ldrb r1, [r1, #0x795]"),            # ... and grain only on its mono/NF path
]


def word(a):
    return ra.u32(a)


def insn_text(a):
    i = ra.dis(a, a + 4)[0]
    return f"{i.mnemonic} {i.op_str}"


def enc_b(cond, at, target):
    off = (target - (at + 8)) >> 2
    if not -(1 << 23) <= off < (1 << 23):
        raise ValueError("branch out of range")
    return (cond << 28) | (0b101 << 25) | (off & 0xFFFFFF)


def enc_mov_imm(cond, rd, value):
    for rot in range(16):
        v = ((value << (2 * rot)) | (value >> (32 - 2 * rot))) & 0xFFFFFFFF if rot else value
        if v < 0x100:
            return (cond << 28) | 0x03A00000 | (rd << 12) | (rot << 8) | v
    raise ValueError(f"0x{value:x} is not an ARM immediate")


def cap_row(r):
    o = ra.fo(CAP_TABLE) + r * CAP_COLS
    return bytes(ra.d[o:o + CAP_COLS])


def cap_sites(rows=UNLOCK_GRAIN_ROWS, slot=SLOT_GRAIN):
    """Word-sized writes that set cap[row][slot] = 1 (the RAM writer and the repacker take words)."""
    words = {}
    for r in rows:
        a = CAP_TABLE + r * CAP_COLS + slot
        wa = a & ~3
        cur = words.get(wa, word(wa))
        shift = 8 * (a - wa)
        words[wa] = (cur & ~(0xFF << shift)) | (1 << shift)
    return [(wa, word(wa), new) for wa, new in sorted(words.items()) if new != word(wa)]


def grain_scale_sites(low=0x40, mid=0x80, high=0x100):
    out = []
    for name, value in (("low", low), ("mid", mid), ("high", high)):
        a, stock, cond, stock_scale = GRAIN_SCALE_SITES[name]
        if value != stock_scale:
            out.append((a, stock, enc_mov_imm(cond, 6, value)))
    return out


def planner_sites():
    return [(a, stock, enc_b(stock >> 28, a, PLANNER_WITH_GRAIN)) for a, stock, _ in PLANNER_SITES]


def check():
    """Every fact and stock word; returns a list of problems (empty on the official 1.60 image)."""
    bad = []
    for a, text in FACTS:
        got = insn_text(a)
        if got != text:
            bad.append(f"0x{a:08x}: '{got}', expected '{text}'")
    for r in range(1, CAP_ROWS):
        has = cap_row(r)[SLOT_GRAIN] == 1
        if has != (r in STOCK_GRAIN_ROWS):
            bad.append(f"cap row {r} ({ROWS[r]}): grain {has}, expected {r in STOCK_GRAIN_ROWS}")
    for name, (a, stock, _, scale) in GRAIN_SCALE_SITES.items():
        if word(a) != stock:
            bad.append(f"grain {name} 0x{a:08x}: 0x{word(a):08x}, expected 0x{stock:08x}")
        elif enc_mov_imm(stock >> 28, 6, scale) != stock:
            bad.append(f"grain {name}: encoder does not reproduce the stock word")
    for a, stock, _ in PLANNER_SITES:
        if word(a) != stock:
            bad.append(f"planner 0x{a:08x}: 0x{word(a):08x}, expected 0x{stock:08x}")
        elif enc_b(stock >> 28, a, PLANNER_SHADING_CLARITY) != stock:
            bad.append(f"planner 0x{a:08x}: encoder does not reproduce the stock word")
    up, down = (struct.unpack_from("<256h", ra.d, ra.fo(GRAIN_CURVES[k])) for k in ("up", "down"))
    if not (max(v - i for i, v in enumerate(up)) == 33 and min(v - i for i, v in enumerate(down)) == -33):
        bad.append("grain curves differ from the 1.60 values (±33 at most)")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sites", action="store_true", help="list the candidate words")
    args = ap.parse_args()
    bad = check()
    print(f"{len(FACTS)} instruction facts, capability table, grain scales, planner: "
          + ("all match" if not bad else f"{len(bad)} mismatches"))
    for b in bad:
        print("  " + b)
    print("\nGrain column of the capability table (RAW development):")
    for r in range(1, CAP_ROWS):
        print(f"  {r:2d} {ROWS[r]:15s} {cap_row(r)[SLOT_GRAIN]}")
    if args.sites:
        groups = [("A  grain on colour controls, RAW development (data)", cap_sites()),
                  ("B  grain scale example: Low 0x100, Mid 0x200, High 0x300", grain_scale_sites(0x100, 0x200, 0x300)),
                  ("C1 planner: grain triggers the filter stage for colour controls", planner_sites())]
        for title, sites in groups:
            print(f"\n{title}:")
            for a, old, new in sites:
                print(f"  0x{a:08x}  0x{old:08x} -> 0x{new:08x}  {insn_text(a) if a < 0x53b00000 else 'data'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
