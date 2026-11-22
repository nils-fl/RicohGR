#!/usr/bin/env python3
"""Build a GR IIIx 1.60 update file with the dim-light AF setting built in.

Usage:  python3 tools/af_firmware.py [--afc] [--out FILE]

Without --afc the output is the 19-word file flashed on 2026-11-20 (work/firmware-af/FWDC243B.BIN).
--afc adds the AF-C fast-drive word (AFCFAST, docs/af-debug-log.html#afc-patch): 0x533f7118 in the
drive selector 0x533f70a4, movne r3,#1 -> mov r3,#1, so AF-C searches always get the CAF drive
(200 fps in good light) instead of the 59 fps live-view drive. Default output with --afc:
work/firmware-af-afc/FWDC243B.BIN.

The setting is the one tested remotely as "X875+FALLS2+OPT+NO29" (docs/af-debug-log.html#opt-study):
margin exponent 0.875 (X875), margin base table x 0.32 and rise floor 0.014 (the study's OPT data
words), FALLS2 (a rising run of 2 instead of 3 arms the detector's return value; the name says
falls, after an early misreading), and 49 instead of 29 fps (NO29). The words come
from the same site lists the RAM patches use (tools/af_debug_card.py, tools/af_optimize.py), and
every stock word is checked against the official image before it is replaced.

The container is built by tools/gr_repack.py with the AF allow-list of tools/patch_guard.py
(--allow af): every change must lie in the contrast-AF code (CafProcess, CafDetection,
CafWobbling) and patched functions may only be called from there. Offline only: writing the file
installs nothing, and this tool never talks to a camera.
"""
import argparse
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import af_debug_card as card  # noqa: E402
import af_optimize as opt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OFFICIAL = ROOT / "firmware" / "fwdc243b-v160.bin"
DECODED = ROOT / "work" / "fwdc243b-v160.decoded.bin"
BASE, OFF = 0x53000000, 0x10870  # RTOS load address and payload offset (tools/rtos_analysis.py)

SETTING = dict(opt.FROZEN3, exponent="0.875", kb=0.32, falls=2, step=8, fps="auto")


AFC_SITES = ((card.AFC_SITE, card.AFC_STOCK, card.AFC_FAST),)


def sites(afc=False):
    out = list(card.X875_SITES) + list(card.FALLS2_SITES) + opt.opt_sites(SETTING) + list(card.NO29_SITES)
    if afc:
        out += list(AFC_SITES)
    addrs = [a for a, _, _ in out]
    if len(set(addrs)) != len(addrs):
        raise SystemExit("two site lists write the same address")
    return out


def patched_payload(original: bytes, afc=False) -> bytes:
    mod = bytearray(original)
    for a, old, new in sites(afc):
        o = a - BASE + OFF
        cur = struct.unpack_from("<I", original, o)[0]
        if cur != old:
            raise SystemExit(f"0x{a:08x}: image has 0x{cur:08x}, expected stock 0x{old:08x}; wrong firmware?")
        struct.pack_into("<I", mod, o, new)
    return bytes(mod)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--afc", action="store_true", help="also write the AF-C fast-drive word (AFCFAST)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    if args.out is None:
        args.out = ROOT / "work" / ("firmware-af-afc" if args.afc else "firmware-af") / "FWDC243B.BIN"
    if not DECODED.exists():
        raise SystemExit(f"{DECODED} missing: run python3 tools/gr_unpack.py {OFFICIAL}")
    s = sites(args.afc)
    print(f"{len(s)} words:")
    for a, old, new in s:
        print(f"  0x{a:08x}  0x{old:08x} -> 0x{new:08x}")
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload.bin"
        payload.write_bytes(patched_payload(DECODED.read_bytes(), args.afc))
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "gr_repack.py"), str(OFFICIAL), str(payload),
                            "--allow", "af", "--out", str(args.out)])
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
