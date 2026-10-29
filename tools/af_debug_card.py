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


# The probe copies RTOS RAM 0x5506b000-0x5506cfff (RTOS addresses are physical
# addresses; GRKit's dump tool maps /dev/mem the same way, read-only) to
# SCRIPT/AFDBG/MEM.BIN every 5 s for 10 minutes. It holds the AF debug object
# 0x5506b56c (vtable 0x53c15308, flags +0x27..+0x2a, log slots +0x112c), its
# guard 0x5506b564, the CSV counter 0x5506b568 and the busy bytes 0x5506b56a,
# 0x5506c765. tools/af_debug_card.py --read-mem decodes it.
PROBE_PAGE, PROBE_PAGES = 0x5506b000, 2
PROBE_POLL_MS, PROBE_POLLS = 5000, 120


def startup_ttl(flags):
    # The 3 s wait comes first: GRKit found that the RTOS builds its host
    # command tables after the script starts, and lcmd_resp faults until they
    # exist (crates/grkit-core/src/runtime/loader.rs). GRKit waits 2 s.
    lines = [
        "mpause 3000",
        "sendln 'lcmd_resp on'",
        f"sendln 'EnableAfDebugDataSave 1 {flags}'",
        # Proof that the script ran this far: Linux writes BOOT.OK and appends
        # the uptime to MARK.OK. lcmd returns NG until Linux is up, so retry.
        "filedelete 'C:\\SCRIPT\\AFDBG\\BOOT.OK'",
        "attempts = 0",
        ":mark",
        "sendln 'lcmd sh /media/storage1/SCRIPT/AFDBG/MARK.SH'",
        "filesearch 'C:\\SCRIPT\\AFDBG\\BOOT.OK'",
        "if result != 0 goto probe",
        "mpause 1000",
        "attempts = attempts + 1",
        "if attempts < 60 goto mark",
        "goto done",
        ":probe",
        "polls = 0",
        ":poll",
        "sendln 'lcmd sh /media/storage1/SCRIPT/AFDBG/MEM.SH'",
        f"mpause {PROBE_POLL_MS}",
        "polls = polls + 1",
        f"if polls < {PROBE_POLLS} goto poll",
        ":done",
        "end",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def mark_sh(flags):
    return (
        "d=/media/storage1/SCRIPT/AFDBG\n"
        "read t _ </proc/uptime\n"
        f'echo "$t EnableAfDebugDataSave 1 {flags} sent" >>$d/MARK.OK\n'
        'echo "$t" >$d/BOOT.OK\n'
        "exit 0\n"
    ).encode("ascii")


def mem_sh():
    return (
        "d=/media/storage1/SCRIPT/AFDBG\n"
        f"dd if=/dev/mem of=$d/MEM.TMP bs=4096 skip={PROBE_PAGE // 4096} count={PROBE_PAGES} 2>$d/MEM.ERR"
        " && mv $d/MEM.TMP $d/MEM.BIN\n"
        "read t _ </proc/uptime\n"
        'echo "$t" >$d/MEM.TIME\n'
        "exit 0\n"
    ).encode("ascii")


def read_mem(path):
    """Decode a MEM.BIN that the probe wrote."""
    data = Path(path).read_bytes()
    if len(data) != PROBE_PAGES * 4096:
        sys.exit(f"{path}: {len(data)} bytes, expected {PROBE_PAGES * 4096}")
    u8 = lambda a: data[a - PROBE_PAGE]
    u16 = lambda a: int.from_bytes(data[a - PROBE_PAGE:a - PROBE_PAGE + 2], "little")
    u32 = lambda a: int.from_bytes(data[a - PROBE_PAGE:a - PROBE_PAGE + 4], "little")
    obj = 0x5506b56c
    vtable = u32(obj)
    print(f"guard 0x5506b564      {u32(0x5506b564):#010x}  (bit 0 = object constructed)")
    print(f"vtable                {vtable:#010x}  ({'ok' if vtable == 0x53c15308 else 'expected 0x53c15308'})")
    print("flags P C Z O         " + " ".join(str(u8(obj + k)) for k in range(0x27, 0x2b)))
    print(f"CSV counter           {u16(0x5506b568)}  (files written since power-on)")
    print(f"writer busy 0x5506b56a {u8(0x5506b56a)}, open busy 0x5506c765 {u8(0x5506c765)}")
    for k in range(10):
        s = obj + 0x112c + k * 0x14
        if u8(s):
            # Slot layout from 0x533ef614 (open) and 0x533efbd0 (write): +0 in
            # use, +4 name (std::string), +8 buffer, +0xc bytes logged, +0x10 size.
            print(f"slot {k}: in use, buffer {u32(s + 8):#010x}, logged {u32(s + 0xc):#x}, size {u32(s + 0x10):#x}")
    print("slots in use          " + str(sum(1 for k in range(10) if u8(obj + 0x112c + k * 0x14))))


def card_roots(flags):
    return {
        "enable": {MODESET_FILE: SCRIPTS_ON, "DEVELOP.MOD": DEVELOP_MOD},
        "log": {
            "SCRIPT/STARTUP.TTL": startup_ttl(flags),
            "SCRIPT/AFDBG/MARK.SH": mark_sh(flags),
            "SCRIPT/AFDBG/MEM.SH": mem_sh(),
        },
        "disable": {MODESET_FILE: SCRIPTS_OFF, "DEVELOP.MOD": DEVELOP_MOD},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("work/af-debug-card"))
    parser.add_argument("--flags", default="C", help="subset of PCZO (default C: contrast AF CSV only)")
    parser.add_argument("--read-mem", metavar="MEM.BIN", help="decode a probe dump instead of writing files")
    args = parser.parse_args()
    if args.read_mem:
        read_mem(args.read_mem)
        return
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
