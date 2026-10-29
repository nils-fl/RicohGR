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
# SCRIPT/AFDBG/MEM.BIN every 2 s for 10 minutes, and MEM.SH copies each
# snapshot to Mnnn.BIN (cp works on the card, mv does not). It holds the AF debug object
# 0x5506b56c (vtable 0x53c15308, flags +0x27..+0x2a, log slots +0x112c), its
# guard 0x5506b564, the CSV counter 0x5506b568 and the busy bytes 0x5506b56a,
# 0x5506c765. tools/af_debug_card.py --read-mem decodes it.
PROBE_PAGE, PROBE_PAGES = 0x5506b000, 23
# 23 pages reach the static contrast AF object (0x5506c938) up to +0x156c7:
# the inputs of the fast-drive selector 0x533f70a4 (+0xc44e, +0xc484, +0xc488,
# vetoes +0x43c0, +0xfec/+0x1004), the search path +0xc4ca, the scan window
# +0xc438/+0xc43c, and the two flags that skip the peak check at 0x53404f50
# (+0xc53d, printed as EyeDetectStatus; +0x150c9, the state of an outside
# position-list request, set by 0x53406d10).
# Re-arm every second (bright-edge session: presses about 3 s apart lost runs
# with a 3 s re-arm); snapshot memory every 10th tick. 600 ticks = 10 minutes.
TICK_MS, TICKS, PROBE_EVERY = 1000, 600, 2
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
        "$d/PTRRD.BIN; echo $? >$d/TAB.ERR\n"
        # The gain-cap patch is idempotent, so it runs with every snapshot: the
        # drive table may not exist yet at boot, or may be rebuilt later.
        # GAIN.LOG: uptime and exit per run (0 written, 6 already patched), to
        # see whether the camera puts the table back between runs.
        'if [ -f $d/GAINCAP.ON ]; then $d/GAINCAP.BIN; g=$?; echo "on $g" >$d/GAIN.ERR; '
        'read t _ </proc/uptime; echo "$t $g" >>$d/GAIN.LOG; else echo off >$d/GAIN.ERR; fi\n'
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


# PTRRD.BIN: follow two pointers of the static contrast AF object and dump
# what they point to, to TAB.BIN: for each pointer its value (4 bytes) and, when
# non-zero, 8 KB from the start of its page (mapped read-only via /dev/mem like
# MEMRD.BIN; RTOS heap addresses are assumed to be physical as well). Pointers:
# +0xfcc, the per-level CAF drive table that SwitchLiveView (0x53436184) reads
# for the levels 0..3 at +0x1022 (200/100/49/29 fps), and +0xfe8, the AE
# exposure info (integration, gain, float) that 0x533fd410 copies each frame.
# Exit: 0 done, 1 open, 2 map object page, 3 create, 5 map target, 4 write.
PTR_PAGE = 0x5506D000
PTR_OFFSETS = (0x5506C938 + 0xFCC - PTR_PAGE, 0x5506C938 + 0xFE8 - PTR_PAGE)
PTRRD_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    adr     r0, dev
    ldr     r1, =0x101000           @ O_RDONLY | O_SYNC
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0                  @ /dev/mem
    mov     r0, #0
    mov     r1, #4096
    mov     r2, #1
    mov     r3, #1
    ldr     r5, ={obj_page}
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    mov     r6, r0                  @ object page
    adr     r0, out
    ldr     r1, =0x241
    ldr     r2, =0x1a4
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #3
    bhi     quit
    mov     r8, r0                  @ TAB.BIN
    adr     r10, offsets
    mov     r11, #2
next:
    ldr     r1, [r10], #4
    ldr     r9, [r6, r1]            @ the pointer
    sub     sp, sp, #8
    str     r9, [sp]
    mov     r0, r8
    mov     r1, sp
    mov     r2, #4
    mov     r7, #4
    svc     #0
    add     sp, sp, #8
    cmp     r0, #4
    movne   r0, #4
    bne     quit
    cmp     r9, #0
    beq     skip
    mov     r0, #0
    mov     r1, #8192
    mov     r2, #1
    mov     r3, #1
    lsr     r5, r9, #12
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #5
    bhi     quit
    mov     r1, r0
    mov     r0, r8
    mov     r2, #8192
    mov     r7, #4
    svc     #0
    cmp     r0, #8192
    movne   r0, #4
    bne     quit
skip:
    subs    r11, r11, #1
    bne     next
    mov     r0, r8
    mov     r7, #118                @ fsync
    svc     #0
    mov     r0, r8
    mov     r7, #6
    svc     #0
    mov     r0, #0
quit:
    mov     r7, #1
    svc     #0
dev:
    .asciz  "/dev/mem"
out:
    .asciz  "/media/storage1/SCRIPT/AFDBG/TAB.BIN"
    .balign 4
offsets:
    .word   {off0}
    .word   {off1}
    .ltorg
"""


def ptrrd_bin():
    return arm_elf(PTRRD_S.replace("{obj_page}", f"{PTR_PAGE >> 12:#x}")
                   .replace("{off0}", f"{PTR_OFFSETS[0]:#x}")
                   .replace("{off1}", f"{PTR_OFFSETS[1]:#x}"))


def read_tab(path):
    """Decode a TAB.BIN that PTRRD.BIN wrote."""
    import struct
    data, at = Path(path).read_bytes(), 0
    for name in ("drive table (+0xfcc)", "AE exposure info (+0xfe8)"):
        ptr = struct.unpack_from("<I", data, at)[0]; at += 4
        print(f"{name}: pointer {ptr:#010x}")
        if not ptr:
            continue
        # The 8 KB start at the page of the pointer (mmap is page-aligned).
        block = data[at:at + 8192]; at += 8192
        words = struct.unpack_from("<32I", block, ptr & 0xFFF)
        for row in range(0, 32, 4):
            print(f"  +{row * 4:#04x}: " + " ".join(f"{w:10d}" for w in words[row:row + 4]))


# Dim-light gain-cap patch (prototype). The drive table behind +0xfcc holds per
# level (200/100/49/29 fps) the maximum AF gain at +0x00 (about 8x: 0x80400,
# 0x80400, 0x80200, 0x80000) and the integration time in us at +0x10 (4986,
# 9970, 19967, 29964); measured with PTRRD.BIN (session drive-table). Contrast AF
# drops to a slower level when its gain reaches that cap (all sessions), so a
# higher cap should keep dim scenes at a faster level, with a noisier signal.
# 24x lets level 1 (100 fps) reach the exposure that level 3 reaches today.
GAIN_STOCK = (0x80400, 0x80400, 0x80200, 0x80000)
GAIN_FAST = 0x180000                    # 24x if 0x10000 is 1x
INTEG_ROW = (4986, 9970, 19967, 29964)

# GAINCAP.BIN: check the build identity, follow +0xfcc, check the integration
# row, then write the gain row (no argument: GAIN_FAST in all four levels; any
# argument: the stock row) through /dev/mem with dsb + cacheflush and read
# back. Exit: 0 written now, 6 already in the wanted state, 1 open, 2 map,
# 3 other build or no table, 5 unexpected table (nothing written), 7 read-back
# differs.
GAINCAP_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    ldr     r9, [sp]                @ argc
    adr     r0, dev
    ldr     r1, =0x101002           @ O_RDWR | O_SYNC
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0
    mov     r0, #0                  @ identity page
    mov     r1, #4096
    mov     r2, #1
    mov     r3, #1
    ldr     r5, ={id_page}
    mov     r7, #192
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
    mov     r0, #0                  @ contrast AF object page
    mov     r1, #4096
    mov     r2, #1
    mov     r3, #1
    ldr     r5, ={obj_page}
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    ldr     r1, ={ptr_offset}
    ldr     r10, [r0, r1]           @ table address
    cmp     r10, #0
    moveq   r0, #3
    beq     quit
    mov     r0, #0                  @ table pages, read-write
    mov     r1, #8192
    mov     r2, #3
    mov     r3, #1
    lsr     r5, r10, #12
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    ldr     r1, =0xfff
    and     r1, r10, r1
    add     r6, r0, r1              @ the table in the mapping
    adr     r8, integ               @ check the integration row
    mov     r3, #0
check:
    add     r2, r6, #0x10
    ldr     r0, [r2, r3, lsl #2]
    ldr     r1, [r8, r3, lsl #2]
    cmp     r0, r1
    movne   r0, #5
    bne     quit
    add     r3, r3, #1
    cmp     r3, #4
    bne     check
    adr     r8, stock               @ r8 = expected now, r10 = to write
    adr     r10, fast
    cmp     r9, #1
    movne   r1, r8
    movne   r8, r10
    movne   r10, r1
    mov     r3, #0                  @ already in the wanted state?
same:
    ldr     r0, [r6, r3, lsl #2]
    ldr     r1, [r10, r3, lsl #2]
    cmp     r0, r1
    bne     expect
    add     r3, r3, #1
    cmp     r3, #4
    bne     same
    mov     r0, #6                  @ already in the wanted state
    b       quit
expect:
    mov     r3, #0
expect1:
    ldr     r0, [r6, r3, lsl #2]
    ldr     r1, [r8, r3, lsl #2]
    cmp     r0, r1
    movne   r0, #5
    bne     quit
    add     r3, r3, #1
    cmp     r3, #4
    bne     expect1
    mov     r3, #0
write:
    ldr     r1, [r10, r3, lsl #2]
    str     r1, [r6, r3, lsl #2]
    add     r3, r3, #1
    cmp     r3, #4
    bne     write
    dsb     sy
    mov     r0, r6
    add     r1, r6, #16
    mov     r2, #0
    ldr     r7, =0xf0002            @ cacheflush
    svc     #0
    mov     r3, #0
verify:
    ldr     r0, [r6, r3, lsl #2]
    ldr     r1, [r10, r3, lsl #2]
    cmp     r0, r1
    movne   r0, #7
    bne     quit
    add     r3, r3, #1
    cmp     r3, #4
    bne     verify
    mov     r0, #0
quit:
    mov     r7, #1
    svc     #0
dev:
    .asciz  "/dev/mem"
    .balign 4
integ:
    .word   {i0}, {i1}, {i2}, {i3}
stock:
    .word   {s0}, {s1}, {s2}, {s3}
fast:
    .word   {f}, {f}, {f}, {f}
    .ltorg
"""


def gaincap_bin():
    src = (GAINCAP_S.replace("{id_page}", f"{RTOS_IDENTITY >> 12:#x}")
           .replace("{id_offset}", f"{RTOS_IDENTITY & 0xfff:#x}")
           .replace("{identity}", f"{GR3X_160:#x}")
           .replace("{obj_page}", f"{PTR_PAGE >> 12:#x}")
           .replace("{ptr_offset}", f"{PTR_OFFSETS[0]:#x}")
           .replace("{f}", f"{GAIN_FAST:#x}"))
    for k in range(4):
        src = src.replace(f"{{i{k}}}", str(INTEG_ROW[k])).replace(f"{{s{k}}}", f"{GAIN_STOCK[k]:#x}")
    return arm_elf(src)


# Dim-light frame-rate test (prototype): force contrast AF to drive level 1
# (100 fps). The level byte at CAF +0x1022 (0..3 = 200/100/49/29 fps) is read
# by these five instructions (all "ldrb rX, [obj+0x1000, #0x22]"); each becomes
# "mov rX, #1". In dim light the AF frames then get a third of the light
# (integration 10 ms at the same gain ceiling), which is the question: does
# contrast AF still find focus with that, and how fast? Test only: it also
# slows bright light from 200 to 100 fps.
FORCE100_SITES = (
    (0x533F2604, 0xE5D03022, 0xE3A03001),   # level -> byte table 0x53c16548
    (0x533F728C, 0xE5D52022, 0xE3A02001),   # message 0x67c40808 to the live view
    (0x533F8DC0, 0xE5D33022, 0xE3A03001),   # table[level] in 0x533f8d88
    (0x533F8F14, 0xE5D33022, 0xE3A03001),   # 0x533f8e94
    (0x534057B8, 0xE5D33022, 0xE3A03001),   # search 0x53404b24
)

# MULTIPATCH: check the build identity, map every site read-write, require all
# sites stock (apply) or all patched (exit 6, nothing to do); a mixture or an
# unknown word exits 5 before anything is written. Then write each site, dsb,
# cacheflush, read back. Any argument restores the stock words instead.
# Exit: 0 written, 6 already, 1 open, 2 map identity, 3 other build, 4 map site,
# 5 unexpected or mixed (nothing written), 7 read-back differs.
MULTIPATCH_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    ldr     r9, [sp]                @ argc
    sub     sp, sp, #64             @ site pointers in the mappings
    adr     r0, dev
    ldr     r1, =0x101002
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0
    mov     r0, #0
    mov     r1, #4096
    mov     r2, #1
    mov     r3, #1
    ldr     r5, ={id_page}
    mov     r7, #192
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
    mov     r11, #0                 @ pass 1: map and check
    mov     r8, #0                  @ sites already in the wanted state
check:
    adr     r10, sites
    add     r1, r11, r11, lsl #1
    add     r10, r10, r1, lsl #2    @ this entry: address, stock, new
    ldr     r6, [r10]
    mov     r0, #0
    mov     r1, #4096
    mov     r2, #3
    mov     r3, #1
    lsr     r5, r6, #12
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #4
    bhi     quit
    ldr     r1, =0xfff
    and     r1, r6, r1
    add     r0, r0, r1
    str     r0, [sp, r11, lsl #2]
    ldr     r2, [r10, #4]           @ expected (stock)
    ldr     r3, [r10, #8]           @ wanted (new)
    cmp     r9, #1
    movne   r1, r2
    movne   r2, r3
    movne   r3, r1
    ldr     r0, [r0]
    cmp     r0, r3
    addeq   r8, r8, #1
    beq     checked
    cmp     r0, r2
    movne   r0, #5
    bne     quit
checked:
    add     r11, r11, #1
    cmp     r11, #{count}
    bne     check
    cmp     r8, #{count}
    moveq   r0, #6
    beq     quit
    cmp     r8, #0
    movne   r0, #5                  @ mixed state: refuse
    bne     quit
    mov     r11, #0                 @ pass 2: write
write:
    adr     r10, sites
    add     r1, r11, r11, lsl #1
    add     r10, r10, r1, lsl #2
    ldr     r3, [r10, #8]
    cmp     r9, #1
    ldrne   r3, [r10, #4]
    ldr     r6, [sp, r11, lsl #2]
    str     r3, [r6]
    dsb     sy
    mov     r0, r6
    add     r1, r6, #4
    mov     r2, #0
    ldr     r7, =0xf0002
    svc     #0
    ldr     r0, [r6]
    adr     r10, sites
    add     r1, r11, r11, lsl #1
    add     r10, r10, r1, lsl #2
    ldr     r3, [r10, #8]
    cmp     r9, #1
    ldrne   r3, [r10, #4]
    cmp     r0, r3
    movne   r0, #7
    bne     quit
    add     r11, r11, #1
    cmp     r11, #{count}
    bne     write
    mov     r0, #0
quit:
    mov     r7, #1
    svc     #0
dev:
    .asciz  "/dev/mem"
    .balign 4
sites:
{entries}
    .ltorg
"""


def multipatch_bin(sites):
    entries = "\n".join(f"    .word   {a:#x}, {o:#x}, {n:#x}" for a, o, n in sites)
    return arm_elf(MULTIPATCH_S.replace("{id_page}", f"{RTOS_IDENTITY >> 12:#x}")
                   .replace("{id_offset}", f"{RTOS_IDENTITY & 0xfff:#x}")
                   .replace("{identity}", f"{GR3X_160:#x}")
                   .replace("{count}", str(len(sites)))
                   .replace("{entries}", entries))


# AF state tracer. TRACE.BIN maps the static contrast AF object read-only and
# every TRACE_PERIOD_NS records a timestamp plus the 32-bit words at
# TRACE_OFFSETS (object-relative), TRACE_RECORDS times, appending to TRACE.BIN's
# output TRACE.DAT every TRACE_CHUNK records (so a power-off keeps what was
# written). Record: tv_sec, tv_nsec (CLOCK_MONOTONIC), then one word per offset.
CAF_OBJECT = 0x5506C938
TRACE_OFFSETS = (
    0xC44C,   # action code (u16) | +0xc44e AF-C search flag
    0xC4C8,   # +0xc4ca search path
    0xC4DC,   # +0xc4df pass countdown
    0xC470,   # +0xc470 result-accepted flag
    0x14EA4,  # +0x14ea5 peak-check gate | +0x14ea6 success flag
    0x0F7C,   # current area index (-2 = travel exceeded)
    0xC434,   # sweep start pulse
    0xC438,   # scan window end
    0xC43C,   # in-focus pulse
    0x14BD4,  # +0x14bd6 reversal count
    0x1020,   # +0x1022 drive level
    0x0FEC,   # drive-change state
    0x10E8,   # area 0 detector: rising / falling counts (+0x102c +0xbc/+0xbe)
    0x103C,   # area 0 detector: rising-side index (+0x102c +0x10)
    0x1040,   # area 0 detector: falling-side index (+0x102c +0x14)
    0xC450,   # FarEndPointState
)
TRACE_PERIOD_NS, TRACE_RECORDS, TRACE_CHUNK = 2_000_000, 30_000, 500
TRACE_MAP = 0x16000                      # 0x5506c000..0x55081fff covers all offsets

TRACE_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    adr     r0, dev
    ldr     r1, =0x101000           @ O_RDONLY | O_SYNC
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0
    mov     r0, #0                  @ map the object pages
    ldr     r1, ={map_len}
    mov     r2, #1
    mov     r3, #1
    ldr     r5, ={map_page}
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    ldr     r1, ={obj_offset}
    add     r6, r0, r1              @ r6 = the object in the mapping
    mov     r0, #0                  @ anonymous buffer for one chunk
    ldr     r1, ={chunk_bytes}
    mov     r2, #3
    mov     r3, #0x22               @ MAP_PRIVATE | MAP_ANONYMOUS
    mvn     r4, #0
    mov     r5, #0
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    mov     r10, r0                 @ r10 = buffer
    adr     r0, out
    ldr     r1, =0x441              @ O_WRONLY | O_CREAT | O_APPEND
    ldr     r2, =0x1a4
    mov     r7, #5
    svc     #0
    cmn     r0, #4096
    movhi   r0, #3
    bhi     quit
    mov     r8, r0                  @ r8 = TRACE.DAT
    ldr     r11, ={records}         @ records left
chunk:
    mov     r9, r10                 @ write pointer
    mov     r12, #0                 @ records in this chunk
record:
    mov     r0, #1                  @ CLOCK_MONOTONIC
    mov     r1, r9
    ldr     r7, =263                @ clock_gettime
    svc     #0
    add     r9, r9, #8
    adr     r2, offsets
    mov     r3, #0
field:
    ldr     r1, [r2, r3, lsl #2]
    ldr     r0, [r6, r1]
    str     r0, [r9], #4
    add     r3, r3, #1
    cmp     r3, #{nfields}
    bne     field
    adr     r0, period              @ nanosleep(&period, NULL)
    mov     r1, #0
    mov     r7, #162
    svc     #0
    add     r12, r12, #1
    subs    r11, r11, #1
    beq     flush
    cmp     r12, #{chunk}
    bne     record
flush:
    mov     r0, r8
    mov     r1, r10
    sub     r2, r9, r10
    mov     r7, #4                  @ write
    svc     #0
    mov     r0, r8
    mov     r7, #118                @ fsync
    svc     #0
    cmp     r11, #0
    bne     chunk
    mov     r0, r8
    mov     r7, #6
    svc     #0
    mov     r0, #0
quit:
    mov     r7, #1
    svc     #0
dev:
    .asciz  "/dev/mem"
out:
    .asciz  "/media/storage1/SCRIPT/AFDBG/TRACE.DAT"
    .balign 4
period:
    .word   0, {period_ns}
offsets:
{offsets}
    .ltorg
"""


def trace_bin():
    offsets = "\n".join(f"    .word   {o:#x}" for o in TRACE_OFFSETS)
    rec = 8 + 4 * len(TRACE_OFFSETS)
    return arm_elf(TRACE_S.replace("{map_len}", f"{TRACE_MAP:#x}")
                   .replace("{map_page}", f"{(CAF_OBJECT & ~0xfff) >> 12:#x}")
                   .replace("{obj_offset}", f"{CAF_OBJECT & 0xfff:#x}")
                   .replace("{chunk_bytes}", f"{rec * TRACE_CHUNK:#x}")
                   .replace("{records}", str(TRACE_RECORDS))
                   .replace("{nfields}", str(len(TRACE_OFFSETS)))
                   .replace("{chunk}", str(TRACE_CHUNK))
                   .replace("{period_ns}", str(TRACE_PERIOD_NS))
                   .replace("{offsets}", offsets))


def read_trace(path):
    """Print the changes in a TRACE.DAT: one line whenever a traced field changes."""
    import struct
    data = Path(path).read_bytes()
    rec = 8 + 4 * len(TRACE_OFFSETS)
    prev, t0 = None, None
    for k in range(len(data) // rec):
        sec, nsec, *w = struct.unpack_from(f"<II{len(TRACE_OFFSETS)}I", data, k * rec)
        t = sec + nsec / 1e9
        t0 = t if t0 is None else t0
        v = (w[0] & 0xffff, (w[0] >> 16) & 0xff, (w[1] >> 16) & 0xff, w[2] >> 24, w[3] & 0xff,
             (w[4] >> 8) & 0xff, (w[4] >> 16) & 0xff, w[5] - (1 << 32) if w[5] >> 31 else w[5],
             w[6], w[7], w[8], (w[9] >> 16) & 0xffff, (w[10] >> 16) & 0xff, w[11],
             w[12] & 0xffff, w[12] >> 16, w[13], w[14], w[15] & 0xffff)
        if v != prev:
            labels = ("act", "afc", "path", "count", "acc", "gate", "ok", "area", "start", "wend",
                      "infoc", "rev", "level", "drive", "rising", "falling", "rise_i", "fall_i", "farend")
            print(f"{t - t0:9.3f} " + " ".join(f"{a}={b}" for a, b in zip(labels, v)))
            prev = v


def trace_runs(path):
    """Summarise a TRACE.DAT per AF run (action 2 ... back to 1): duration,
    action codes, accepted flag, and per pass (path 0/1/2) the area-0
    detector's rising/falling counts as they change."""
    import struct
    data = Path(path).read_bytes()
    rec = 8 + 4 * len(TRACE_OFFSETS)
    runs, cur, t0 = [], None, None
    for k in range(len(data) // rec):
        sec, nsec, *w = struct.unpack_from(f"<II{len(TRACE_OFFSETS)}I", data, k * rec)
        t = sec + nsec / 1e9
        t0 = t if t0 is None else t0
        act, path, counts = w[0] & 0xffff, (w[1] >> 16) & 0xff, (w[12] & 0xffff, w[12] >> 16)
        if act == 2 and (cur is None or "end" in cur):
            cur = {"t": t, "acts": [], "passes": []}
            runs.append(cur)
        if cur is None or "end" in cur:
            continue
        if not cur["passes"] or cur["passes"][-1][0] != path:
            cur["passes"].append((path, t, []))
        seq = cur["passes"][-1][2]
        if not seq or seq[-1] != counts:
            seq.append(counts)
        if not cur["acts"] or cur["acts"][-1] != act:
            cur["acts"].append(act)
        if act == 1 and len(cur["acts"]) > 1:
            cur["end"], cur["acc"] = t, w[3] & 0xff
    for r in runs:
        print(f"{r['t'] - t0:7.2f} s  {1000 * (r.get('end', r['t']) - r['t']):5.0f} ms  "
              f"actions {r['acts']}  accepted {r.get('acc')}")
        for path, t, seq in r["passes"]:
            # The first entry is often left over from the previous run.
            print(f"    path {path} from {1000 * (t - r['t']):5.0f} ms, rise/fall: "
                  + " ".join(f"{a}/{b}" for a, b in seq[1:]))


def patch_sh():
    # Applied once per power-on, only when AFCFAST.ON is on the card. The patch
    # lives in RAM; switching off (or starting without the file) gives stock.
    return (
        "d=/media/storage1/SCRIPT/AFDBG\n"
        "exec 2>>$d/MEM.LOG\n"
        'if [ -f $d/AFCFAST.ON ]; then $d/AFCFAST.BIN; echo "on $?" >$d/PATCH.ERR; else echo off >$d/PATCH.ERR; fi\n'
        'if [ -f $d/FORCE100.ON ]; then $d/FORCE100.BIN; echo "on $?" >$d/FORCE.ERR; else echo off >$d/FORCE.ERR; fi\n'
        # The tracer runs in the background for TRACE_RECORDS x TRACE_PERIOD_NS.
        'if [ -f $d/TRACE.ON ]; then rm -f $d/TRACE.DAT; ($d/TRACE.BIN; echo "$?" >$d/TRACE.ERR) </dev/null >/dev/null 2>&1 & fi\n'
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
        if caf + 0x150c9 < PROBE_PAGE + len(data):
            print(f"peak-check skips      +c53d (EyeDetectStatus) {u8(caf + 0xc53d)}  +150c9 (position request) {u8(caf + 0x150c9)}")
            print(f"scan window           end +c438 {u32(caf + 0xc438)}  in focus +c43c {u32(caf + 0xc43c)}  start +c434 {u32(caf + 0xc434)}")


def card_roots(flags):
    return {
        "enable": {MODESET_FILE: SCRIPTS_ON, "DEVELOP.MOD": DEVELOP_MOD},
        "log": {
            "SCRIPT/STARTUP.TTL": startup_ttl(flags),
            "SCRIPT/AFDBG/MARK.SH": mark_sh(flags),
            "SCRIPT/AFDBG/MEM.SH": mem_sh(),
            "SCRIPT/AFDBG/MEMRD.BIN": memrd_bin(),
            "SCRIPT/AFDBG/PTRRD.BIN": ptrrd_bin(),
            "SCRIPT/AFDBG/PATCH.SH": patch_sh(),
            "SCRIPT/AFDBG/AFCFAST.BIN": afcfast_bin(),
            "SCRIPT/AFDBG/GAINCAP.BIN": gaincap_bin(),
            "SCRIPT/AFDBG/FORCE100.BIN": multipatch_bin(FORCE100_SITES),
            "SCRIPT/AFDBG/TRACE.BIN": trace_bin(),
        },
        "disable": {MODESET_FILE: SCRIPTS_OFF, "DEVELOP.MOD": DEVELOP_MOD},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("work/af-debug-card"))
    parser.add_argument("--flags", default="C", help="subset of PCZO (default C: contrast AF CSV only)")
    parser.add_argument("--read-mem", metavar="MEM.BIN", help="decode a probe dump instead of writing files")
    parser.add_argument("--read-tab", metavar="TAB.BIN", help="decode a pointer dump instead of writing files")
    parser.add_argument("--read-trace", metavar="TRACE.DAT", help="decode an AF state trace instead of writing files")
    parser.add_argument("--trace-runs", metavar="TRACE.DAT", help="summarise an AF state trace per AF run")
    args = parser.parse_args()
    if args.read_mem:
        read_mem(args.read_mem)
        return
    if args.read_tab:
        read_tab(args.read_tab)
        return
    if args.read_trace:
        read_trace(args.read_trace)
        return
    if args.trace_runs:
        trace_runs(args.trace_runs)
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
