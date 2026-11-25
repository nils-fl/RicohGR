#!/usr/bin/env python3
"""RAM-only test card for the grain changes A and B (docs/image-effects.html §4).

Usage:  python3 tools/effects/effects_card.py [--out DIR] [--grain LOW,MID,HIGH]

Writes a card root into DIR (default work/effects-card/), never to a card directly. Copy its
contents to the root of a card on which SD scripts are already on (tools/af/af_debug_card.py,
enable/). At each power-on SCRIPT/STARTUP.TTL waits for Linux, then APPLY.SH writes the words
of every arm whose .ON file is on the card, through /dev/mem, and logs the result:

  A  grain on the colour controls but HDR Tone in RAW development: 6 data words of the
     capability table 0x53b5a750 (the setter's check) and 26 of the row lists 0x53b3476c
     (the Grain row the screen shows; Negative Film gets it too), and 5 code words in
     ImageTuning 0x536db7dc that stop the colour tones writing grain back as 0, and 3 in the
     still planner 0x535131c0 so that grain alone runs the filter stage for them (C1). Both
     are shared with shooting: with A in RAM a colour shot may pick up the grain byte left
     from an earlier monotone or Negative Film setting
  B  grain strength: scales for Low, Mid and High (stock 0x40, 0x80, 0x100; default here
     0x100, 0x200, 0x300). Shared code: it also changes grain in shots taken with Monotone,
     the other monotone controls or Negative Film, and in RAW development.

Delete A.ON or B.ON from the card to leave an arm out. The AF lamp flashes twice when every
armed patch is in place, and stays on for 3 s when one failed (SCRIPT/EFFECTS/EFFECTS.LOG
says which, with the writer's exit code). The writer refuses another build, and refuses to
write when a site holds neither the stock nor the patched word.

Nothing is flashed. The words live in RAM: a power-off, or starting without the card, gives
the camera as before. Safe with the AF build flashed: no site is one of its words
(tools/tests/test_effects.py), and the card does not use the Wi-Fi bench. Linux writes the
card only once per power-on (one log line, then sync), after the script has stopped polling.
"""
import argparse
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_debug_card as card  # noqa: E402
import effects_map as em  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DIR = "SCRIPT/EFFECTS"
D = f"/media/storage1/{DIR}"
TTL_D = "C:\\SCRIPT\\EFFECTS"
DEFAULT_GRAIN = (0x100, 0x200, 0x300)
STOCK_GRAIN = (0x40, 0x80, 0x100)


def arm_sites(grain=DEFAULT_GRAIN):
    return {"A": em.cap_sites() + em.row_sites() + em.tuning_sites() + em.planner_sites(), "B": em.grain_scale_sites(*grain)}


def apply_sh(arms):
    # multipatch exit codes (af_debug_card.MULTIPATCH_S): 0 written, 6 already in place,
    # 1 open, 2 map identity, 3 other build, 4 map site, 5 unexpected word, 7 read-back differs
    lines = [f"d={D}", "exec 2>>$d/ERR.LOG", "read t _ </proc/uptime", "r=", "ok=1"]
    for a in arms:
        lines += [f"if [ -f $d/{a}.ON ]; then $d/{a}.BIN; x=$?; r=\"$r {a}=$x\"; "
                  f"[ $x = 0 ] || [ $x = 6 ] || ok=0; else r=\"$r {a}=off\"; fi"]
    lines += ['echo "$t$r" >>$d/EFFECTS.LOG',
              '[ $ok = 1 ] && echo "$t" >$d/APPLIED.OK',
              "sync", "exit 0"]
    return ("\n".join(lines) + "\n").encode("ascii")


def startup_ttl():
    T = TTL_D
    lines = [
        "mpause 3000",
        "sendln 'lcmd_resp on'",
        f"filedelete '{T}\\BOOT.OK'",
        f"filedelete '{T}\\APPLIED.OK'",
        "attempts = 0",
        ":mark",
        f"sendln 'lcmd sh {D}/MARK.SH'",
        f"filesearch '{T}\\BOOT.OK'",
        "if result != 0 goto linux",
        "mpause 1000",
        "attempts = attempts + 1",
        "if attempts < 60 goto mark",
        "goto lamp3",
        ":linux",
        f"sendln 'lcmd sh {D}/APPLY.SH'",
        "polls = 0",
        ":poll",
        "mpause 500",
        f"filesearch '{T}\\APPLIED.OK'",
        "if result != 0 goto lamp2",
        "polls = polls + 1",
        "if polls < 10 goto poll",
        ":lamp3",
        "sendln 'Af_Led_On'", "mpause 3000", "sendln 'Af_Led_Off'",
        "goto finish",
        ":lamp2",
        "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'", "mpause 500",
        "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'",
        ":finish",
        "end",
    ]
    card.ttl_check(lines)
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def card_files(grain=DEFAULT_GRAIN):
    sites = arm_sites(grain)
    files = {
        "SCRIPT/STARTUP.TTL": startup_ttl(),
        f"{DIR}/MARK.SH": (f"d={D}\nread t _ </proc/uptime\necho \"$t\" >$d/BOOT.OK\nexit 0\n").encode("ascii"),
        f"{DIR}/APPLY.SH": apply_sh([a for a in sites if sites[a]]),
    }
    for a, s in sites.items():
        if s:
            files[f"{DIR}/{a}.BIN"] = card.multipatch_bin(s)
            files[f"{DIR}/{a}.ON"] = b"on\n"
    return files


def parse_grain(text):
    vals = tuple(int(v, 0) for v in text.split(","))
    if len(vals) != 3:
        raise argparse.ArgumentTypeError("three values: LOW,MID,HIGH")
    for v in vals:
        if not 0 < v <= 0x400:
            raise argparse.ArgumentTypeError(f"{v:#x} outside 1..0x400")
        em.enc_mov_imm(0xE, 6, v)  # raises for a value that is not an ARM immediate
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "work" / "effects-card")
    ap.add_argument("--grain", type=parse_grain, default=DEFAULT_GRAIN,
                    help="grain scales LOW,MID,HIGH (stock 0x40,0x80,0x100; default 0x100,0x200,0x300)")
    args = ap.parse_args()
    bad = em.check()
    if bad:
        sys.exit("effects_map.py does not match this image:\n  " + "\n  ".join(bad))
    files = card_files(args.grain)
    for rel, data in files.items():
        p = args.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for a, s in arm_sites(args.grain).items():
        print(f"arm {a}: {len(s)} words")
        for addr, old, new in s:
            print(f"  0x{addr:08x}  0x{old:08x} -> 0x{new:08x}")
    print(f"grain scales Low/Mid/High: {', '.join(f'{v:#x}' for v in args.grain)} "
          f"(stock {', '.join(f'{v:#x}' for v in STOCK_GRAIN)})")
    print(f"wrote {len(files)} files to {args.out}; copy its contents to the card root")


if __name__ == "__main__":
    main()
