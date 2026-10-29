#!/usr/bin/env python3
"""Write the SD-card files that turn on the GR IIIx's built-in AF debug log.

Usage:  python3 tools/af_debug_card.py [--out DIR] [--flags C]
Writes three card roots into DIR (default work/af-debug-card/), never to a
card directly. Copy the contents of one folder to the card root:

  enable/   turns SD scripts on  (once; hold MENU while switching on)
  log/      SCRIPT/STARTUP.TTL: at each power-on, sends
            "EnableAfDebugDataSave 1 <flags>" to the engineering shell,
            then re-arms "CAF_Debug SaveDebugFile on" every second
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
# SCRIPT/AFDBG/MEM.BIN every 10 s for 10 minutes, and MEM.SH copies each
# snapshot to Mnnn.BIN (cp works on the card, mv does not). It holds the AF debug object
# 0x5506b56c (vtable 0x53c15308, flags +0x27..+0x2a, log slots +0x112c), its
# guard 0x5506b564, the CSV counter 0x5506b568 and the busy bytes 0x5506b56a,
# 0x5506c765. tools/af_debug_card.py --read-mem decodes it.
PROBE_PAGE, PROBE_PAGES = 0x5506b000, 14
# 14 pages also reach the static contrast AF object (0x5506c938) up to +0xc6c7:
# the inputs of the fast-drive selector 0x533f70a4 (+0xc44e, +0xc484, +0xc488,
# vetoes +0x43c0, +0xfec/+0x1004) and the search path +0xc4ca.
# Re-arm every second (bright-edge session: presses about 3 s apart lost runs
# with a 3 s re-arm); snapshot memory every 10th tick. 600 ticks = 10 minutes.
TICK_MS, TICKS, PROBE_EVERY = 1000, 600, 10
# The contrast CSV needs a second command (check 3, 2026-10-29): the logger
# 0x53407c5c is called from 0x53408870 only while CAF byte +0x16d8c is set,
# and only "CAF_Debug SaveDebugFile on [N] [name]" sets it (0x5341716c ->
# 0x53408b5c). After one dump 0x53408870 clears it again (0x534089f0), so the
# script re-arms it at every poll. SaveDebugFile writes through the contrast AF
# object [CafFactory 0x5506c870 + 4] (getter 0x533f1d94), which is 0 until the
# factory builds it, so the script arms only after MEM.SH has seen the pointer
# set (CAF.OK, written by MEMRD.BIN). Checks 3 and 4 saw 0x5506c938 there.
ARM_CSV = "sendln 'CAF_Debug SaveDebugFile on'"
CAF_POINTER = 0x5506c874


def startup_ttl(flags):
    # The 3 s wait comes first: GRKit found that the RTOS builds its host
    # command tables after the script starts, and lcmd_resp faults until they
    # exist (crates/grkit-core/src/runtime/loader.rs). GRKit waits 2 s.
    lines = [
        "mpause 3000",
        "sendln 'lcmd_resp on'",
        f"sendln 'EnableAfDebugDataSave 1 {flags}'",
        "filedelete 'C:\\SCRIPT\\AFDBG\\CAF.OK'",
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
        "sendln 'lcmd sh /media/storage1/SCRIPT/AFDBG/PATCH.SH'",
        "ticks = 0",
        "countdown = 0",
        ":tick",
        "if countdown > 0 goto arm",
        "sendln 'lcmd sh /media/storage1/SCRIPT/AFDBG/MEM.SH'",
        f"countdown = {PROBE_EVERY}",
        ":arm",
        "countdown = countdown - 1",
        "filesearch 'C:\\SCRIPT\\AFDBG\\CAF.OK'",
        "if result = 0 goto sleep",
        ARM_CSV,
        ":sleep",
        f"mpause {TICK_MS}",
        "ticks = ticks + 1",
        f"if ticks < {TICKS} goto tick",
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
    # dd cannot read this range: read() on /dev/mem fails with "Bad address"
    # (check 2, 2026-10-29). MEMRD.BIN maps it instead, as GRKit's dump does.
    return (
        "d=/media/storage1/SCRIPT/AFDBG\n"
        # Errors of this script, for the next check (check 4: a dd/tr/wc
        # pointer test here never wrote CAF.OK; MEMRD.BIN now does it.
        # Check 5: mv on the card failed with EIO, so MEMRD.BIN writes
        # MEM.BIN directly).
        "exec 2>>$d/MEM.LOG\n"
        "$d/MEMRD.BIN\n"
        "s=$?\n"
        'echo "$s" >$d/MEM.ERR\n'
        # Keep every snapshot: M000.BIN, M001.BIN ... (counter in MEM.N).
        'n=$(cat $d/MEM.N 2>/dev/null || echo 0)\n'
        '[ "$s" = 0 ] && cp $d/MEM.BIN $d/M$(printf %03d "$n").BIN && echo $((n + 1)) >$d/MEM.N\n'
        "read t _ </proc/uptime\n"
        'echo "$t" >$d/MEM.TIME\n'
        "exit 0\n"
    ).encode("ascii")


# MEMRD.BIN: open /dev/mem with O_SYNC, map the probe pages read-only, write
# them to MEM.BIN, and create CAF.OK when the contrast AF object pointer is
# non-zero. Exit status 1 open, 2 mmap, 3 create, 4 write, 0 done.
# Only PC-relative references, so the assembled .text runs as is and needs no
# linker (Apple clang assembles ARM ELF objects but ships no ELF linker).
MEMRD_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    adr     r0, dev
    ldr     r1, =0x101000           @ O_RDONLY | O_SYNC
    mov     r7, #5                  @ open
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0                  @ fd of /dev/mem
    mov     r0, #0
    ldr     r1, ={size}
    mov     r2, #1                  @ PROT_READ
    mov     r3, #1                  @ MAP_SHARED
    ldr     r5, ={page:#x}            @ page number of 0x5506b000
    mov     r7, #192                @ mmap2
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    mov     r6, r0                  @ mapped pages
    adr     r0, out
    ldr     r1, =0x241              @ O_WRONLY | O_CREAT | O_TRUNC
    ldr     r2, =0x1a4              @ 0644
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #3
    bhi     quit
    mov     r8, r0
    mov     r1, r6
    ldr     r2, ={size}
    mov     r7, #4                  @ write
    svc     #0
    ldr     r1, ={size}
    cmp     r0, r1
    movne   r0, #4
    bne     quit
    mov     r0, r8
    mov     r7, #118                @ fsync
    svc     #0
    mov     r0, r8
    mov     r7, #6                  @ close
    svc     #0
    ldr     r1, ={caf_offset}
    ldr     r0, [r6, r1]            @ contrast AF object pointer
    cmp     r0, #0
    beq     done
    adr     r0, caf_ok
    ldr     r1, =0x241
    ldr     r2, =0x1a4
    mov     r7, #5                  @ create CAF.OK
    svc     #0
    cmn     r0, #4096
    bhi     done
    mov     r7, #6
    svc     #0
done:
    mov     r0, #0
quit:
    mov     r7, #1                  @ exit
    svc     #0
dev:
    .asciz  "/dev/mem"
out:
    .asciz  "/media/storage1/SCRIPT/AFDBG/MEM.BIN"
caf_ok:
    .asciz  "/media/storage1/SCRIPT/AFDBG/CAF.OK"
    .balign 4
    .ltorg
"""


def memrd_bin():
    return arm_elf(MEMRD_S.replace("{page:#x}", f"{PROBE_PAGE // 4096:#x}")
                   .replace("{size}", str(PROBE_PAGES * 4096))
                   .replace("{caf_offset}", f"{CAF_POINTER - PROBE_PAGE:#x}"))


def arm_elf(source):
    """Assemble ARM source with clang and wrap its .text in a static ELF."""
    import struct, subprocess, tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src, obj = Path(tmp, "memrd.S"), Path(tmp, "memrd.o")
        src.write_text(source)
        subprocess.run(["clang", "--target=armv7-linux-gnueabi", "-march=armv7-a", "-marm",
                        "-c", str(src), "-o", str(obj)], check=True)
        o = obj.read_bytes()
    shoff, = struct.unpack_from("<I", o, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", o, 0x2e)
    secs = [struct.unpack_from("<10I", o, shoff + i * shentsize) for i in range(shnum)]
    names = secs[shstrndx][4]
    name = lambda sec: o[names + sec[0]:].split(b"\0")[0]
    if any(name(sec).startswith(b".rel") for sec in secs):
        sys.exit("the object has relocations; it must use PC-relative references only")
    text = next(sec for sec in secs if name(sec) == b".text")
    code = o[text[4]:text[4] + text[5]]
    # One PT_LOAD segment (read + execute) at 0x10000; the code follows the headers.
    base, headers = 0x10000, 52 + 32
    size = headers + len(code)
    ehdr = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8) + struct.pack(
        "<HHIIIIIHHHHHH", 2, 40, 1, base + headers, 52, 0, 0x05000200, 52, 32, 1, 0, 0, 0)
    phdr = struct.pack("<8I", 1, 0, base, base, size, size, 5, 0x1000)
    return ehdr + phdr + code


# The AF-C fast-drive patch (prototype). In the selector 0x533f70a4 the AF-C
# branch ends "ldr r3,[r3,#0x484]; subs r3,r3,#2; movne r3,#1", so +0xc484 = 2
# gives 0 (live-view drive, 59 fps). Making the movne unconditional gives 1
# (the 200 fps CAF drive that AF-S uses); the +0xc488 test and both vetoes stay.
# Selector session (af-debug-log): AF-C alternated 200/59 fps through +0xc484.
RTOS_IDENTITY, GR3X_160 = 0x53002038, 0x110B3C01
AFC_SITE, AFC_STOCK, AFC_FAST = 0x533F7118, 0x13A03001, 0xE3A03001

# AFCFAST.BIN: check the build identity and the word at the site, write one
# word through /dev/mem (O_RDWR | O_SYNC, as GRKit's installer does), dsb,
# cacheflush (ARM private syscall 0xf0002), read back. No argument applies the
# patch, any argument restores the stock word. Exit status: 0 done or already
# in that state, 1 open, 2 map identity page, 3 not GR IIIx 1.60, 4 map site
# page, 5 unexpected word at the site (nothing written), 7 read-back differs.
PATCH_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    ldr     r9, [sp]                @ argc
    adr     r0, dev
    ldr     r1, =0x101002           @ O_RDWR | O_SYNC
    mov     r7, #5                  @ open
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0
    mov     r0, #0
    mov     r1, #4096
    mov     r2, #1                  @ PROT_READ
    mov     r3, #1                  @ MAP_SHARED
    ldr     r5, ={id_page}
    mov     r7, #192                @ mmap2
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    ldr     r1, ={id_offset}
    ldr     r0, [r0, r1]
    ldr     r1, ={identity}
    cmp     r0, r1
    movne   r0, #3
    bne     quit
    mov     r0, #0
    mov     r1, #4096
    mov     r2, #3                  @ PROT_READ | PROT_WRITE
    mov     r3, #1
    ldr     r5, ={site_page}
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #4
    bhi     quit
    ldr     r1, ={site_offset}
    add     r6, r0, r1              @ the site in the mapping
    cmp     r9, #1
    ldreq   r2, ={stock}            @ apply: expect stock, write fast
    ldreq   r3, ={fast}
    ldrne   r2, ={fast}             @ restore: expect fast, write stock
    ldrne   r3, ={stock}
    ldr     r0, [r6]
    cmp     r0, r3
    moveq   r0, #0                  @ already in that state
    beq     quit
    cmp     r0, r2
    movne   r0, #5
    bne     quit
    str     r3, [r6]
    dsb     sy
    mov     r0, r6
    add     r1, r6, #4
    mov     r2, #0
    ldr     r7, =0xf0002            @ cacheflush
    svc     #0
    ldr     r0, [r6]
    cmp     r0, r3
    movne   r0, #7
    bne     quit
    mov     r0, #0
quit:
    mov     r7, #1                  @ exit
    svc     #0
dev:
    .asciz  "/dev/mem"
    .balign 4
    .ltorg
"""


def afcfast_bin():
    return arm_elf(PATCH_S.replace("{id_page}", f"{RTOS_IDENTITY >> 12:#x}")
                   .replace("{id_offset}", f"{RTOS_IDENTITY & 0xfff:#x}")
                   .replace("{identity}", f"{GR3X_160:#x}")
                   .replace("{site_page}", f"{AFC_SITE >> 12:#x}")
                   .replace("{site_offset}", f"{AFC_SITE & 0xfff:#x}")
                   .replace("{stock}", f"{AFC_STOCK:#x}")
                   .replace("{fast}", f"{AFC_FAST:#x}"))


def patch_sh():
    # Applied once per power-on, only when AFCFAST.ON is on the card. The patch
    # lives in RAM; switching off (or starting without the file) gives stock.
    return (
        "d=/media/storage1/SCRIPT/AFDBG\n"
        "exec 2>>$d/MEM.LOG\n"
        'if [ -f $d/AFCFAST.ON ]; then $d/AFCFAST.BIN; echo "on $?" >$d/PATCH.ERR; else echo off >$d/PATCH.ERR; fi\n'
        "exit 0\n"
    ).encode("ascii")


def read_mem(path):
    """Decode a MEM.BIN that the probe wrote."""
    data = Path(path).read_bytes()
    if len(data) % 4096 or len(data) < 2 * 4096:
        sys.exit(f"{path}: {len(data)} bytes, expected a whole number of pages (at least 2)")
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
    caf = u32(CAF_POINTER)
    if caf and caf + 0xc4ca < PROBE_PAGE + len(data):
        # Inputs of the fast-drive selector 0x533f70a4 (Read): with +0xc44e = 0
        # the result is 1 unless CisDriveModeFix says LV; with +0xc44e = 1 it is
        # (+0xc488 == 0 and +0xc484 != 2). Vetoes: +0x43c0 set, or +0x1004 == 0
        # and +0xfec == 2.
        c44e, c484, c488 = u8(caf + 0xc44e), u32(caf + 0xc484), u8(caf + 0xc488)
        v43c0, fec, f1004 = u8(caf + 0x43c0), u32(caf + 0xfec), u8(caf + 0x1004)
        fast = (1 if c44e == 0 else int(c488 == 0 and c484 != 2))
        if v43c0 or (f1004 == 0 and fec == 2):
            fast = 0
        print(f"contrast AF object    {caf:#010x}")
        print(f"selector inputs       +c44e {c44e}  +c484 {c484}  +c488 {c488}  veto +43c0 {v43c0}  +fec {fec}  +1004 {f1004}")
        print(f"fast drive (last)     {fast}   search path +c4ca {u8(caf + 0xc4ca)}")


def card_roots(flags):
    return {
        "enable": {MODESET_FILE: SCRIPTS_ON, "DEVELOP.MOD": DEVELOP_MOD},
        "log": {
            "SCRIPT/STARTUP.TTL": startup_ttl(flags),
            "SCRIPT/AFDBG/MARK.SH": mark_sh(flags),
            "SCRIPT/AFDBG/MEM.SH": mem_sh(),
            "SCRIPT/AFDBG/MEMRD.BIN": memrd_bin(),
            "SCRIPT/AFDBG/PATCH.SH": patch_sh(),
            "SCRIPT/AFDBG/AFCFAST.BIN": afcfast_bin(),
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
