#!/usr/bin/env python3
"""Write the SD-card files that turn on the GR IIIx's built-in AF debug log.

Usage:  python3 tools/af_debug_card.py [--out DIR] [--flags C]
Writes three card roots into DIR (default work/af-debug-card/), never to a
card directly. Copy the contents of one folder to the card root:

  enable/   turns SD scripts on  (once; hold MENU while switching on)
  log/      SCRIPT/STARTUP.TTL: at each power-on, sends
            "EnableAfDebugDataSave 1 <flags>" to the engineering shell
  disable/  turns SD scripts off again (hold MENU while switching on)

The procedure, what the camera writes and how to recover are in
docs/af-debug-log.html. GR IIIx 1.60 only. Nothing here is firmware: the
camera writes nothing to flash, and the log setting is lost at power-off.
"""
import argparse, sys
from pathlib import Path

# ModeSet file (ModeSetOperation.cpp): the camera reads C:\%08ld.%03ld, the
# numbers returned by 0x5381fe9c (0x1329a) and 0x5381fe4c (0x261) on GR IIIx
# 1.60. The GR III uses 00078350.588. Commands as in the firmware strings
# "SCRIPT_EN_MODE EN" / "SCRIPT_EN_MODE DIS" (0x538f2e14, 0x538f2e34).
MODESET_FILE = "00078490.609"
SCRIPTS_ON = b"[SCRIPT_EN_MODE EN]\r\n"
SCRIPTS_OFF = b"[SCRIPT_EN_MODE DIS]\r\n"
# Companion file the ModeSet command needs. Contents from GRKit
# (crates/grkit-core/src/runtime/files.rs, MIT), tested there on cameras;
# not decoded in this repository.
DEVELOP_MOD = bytes([7, 1, 44, 31, 16, 3, 30, 22, 5, 45])

# SHELLCMD "EnableAfDebugDataSave" (0x53017778): P, C, Z, O select flags
# +0x27..+0x2a of the AF debug object. C = contrast AF CSV, Z = phase AF log.
FLAGS = "PCZO"


def startup_ttl(flags):
    # The 3 s wait comes first: GRKit found that the RTOS builds its host
    # command tables after the script starts, and lcmd_resp faults until they
    # exist (crates/grkit-core/src/runtime/loader.rs). GRKit waits 2 s.
    lines = [
        "mpause 3000",
        "sendln 'lcmd_resp on'",
        f"sendln 'EnableAfDebugDataSave 1 {flags}'",
        # Proof that the script ran this far: Linux appends the uptime to
        # SCRIPT/AFDBG/MARK.OK. lcmd returns NG until Linux is up, so retry.
        "attempts = 0",
        ":mark",
        "sendln 'lcmd sh /media/storage1/SCRIPT/AFDBG/MARK.SH'",
        "filesearch 'C:\\SCRIPT\\AFDBG\\MARK.OK'",
        "if result != 0 goto done",
        "mpause 1000",
        "attempts = attempts + 1",
        "if attempts < 60 goto mark",
        ":done",
        "end",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def mark_sh(flags):
    return (
        "read t _ </proc/uptime\n"
        f'echo "$t EnableAfDebugDataSave 1 {flags} sent" >>/media/storage1/SCRIPT/AFDBG/MARK.OK\n'
        "exit 0\n"
    ).encode("ascii")


def card_roots(flags):
    return {
        "enable": {MODESET_FILE: SCRIPTS_ON, "DEVELOP.MOD": DEVELOP_MOD},
        "log": {
            "SCRIPT/STARTUP.TTL": startup_ttl(flags),
            "SCRIPT/AFDBG/MARK.SH": mark_sh(flags),
        },
        "disable": {MODESET_FILE: SCRIPTS_OFF, "DEVELOP.MOD": DEVELOP_MOD},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("work/af-debug-card"))
    parser.add_argument("--flags", default="C", help="subset of PCZO (default C: contrast AF CSV only)")
    args = parser.parse_args()
    flags = args.flags.upper()
    if not flags or any(c not in FLAGS for c in flags) or len(set(flags)) != len(flags):
        sys.exit(f"--flags must be letters from {FLAGS}, each at most once")
    if args.out.exists():
        sys.exit(f"{args.out} exists; remove it or pick another --out (this tool never overwrites)")
    for root, files in card_roots(flags).items():
        for name, data in files.items():
            path = args.out / root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path} ({len(data)} bytes)")
    print(f"\nNext: follow docs/af-debug-log.html. Copy the contents of {args.out}/enable to the card root first.")


if __name__ == "__main__":
    main()
