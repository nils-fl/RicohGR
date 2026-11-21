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
import argparse, re, sys
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


def ttl_check(lines):
    """A counter must not share its name with a label (smoke checks 1-4)."""
    labels = {l[1:] for l in lines if l.startswith(":")}
    assigned = {m.group(1) for l in lines for m in [re.match(r"(\w+) = ", l)] if m}
    clash = labels & assigned
    if clash:
        sys.exit(f"script assigns to label name(s): {', '.join(sorted(clash))}")
    return lines


def startup_ttl(flags, auto=None):
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
    ]
    if auto:
        lines += auto_ttl_lines(auto)
        ttl_check(lines)
        return ("\r\n".join(lines) + "\r\n").encode("ascii")
    lines += [
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


# Automated runs (--auto): the script presses the shutter itself, so the user
# only changes the light. Engineering-shell commands in the 1.60 image:
#   SW_sws 0|1  (0x5303bda8) "emulate SwS switch" = half-press. It calls
#               0x536a7474(key 0, state), which stores the state in the
#               per-key override table (+0xac) that the normal key poll reads,
#               so the press takes the same UI path as the real button.
#   SW_swr 0|1  (0x5303be6c) the same for SwR, the full press (key 1).
#   camctl_set_still_focus_mode autoarea|zone|select|pin|follow|cont|mf|snap|inf
#               (0x53010470): used to park the lens at infinity (or the snap
#               distance) before every run, like the hand protocol's "point
#               at the far target first".
#   Af_Led_On / Af_Led_Off (0x53045a80 / 0x53045aa8, no arguments): the AF
#               lamp blinks while the user should change the light.
# Per run: defocus, focus mode back, ARM.SH (applies or restores the patch for
# this run and logs it to AUTO.LOG), re-arm the CSV, half-press for hold_ms,
# release, wait gap_ms. Light blocks of `per_block` runs with a blinking pause
# between them. A/B order: AUTO.SEQ, one arm per line, balanced in blocks of 4.
AUTO_DIR = "/media/storage1/SCRIPT/AFDBG"
AUTO_TARGET_MODES = ("autoarea", "zone", "select", "pin", "follow", "cont")
AUTO_DEFOCUS_MODES = ("none", "inf", "snap")
# Smoke checks 1-4 (2026-11-17, work/af-logs/auto-smoke1..4): every script
# stopped silently at "run = 0" or "bare = 0", each a counter with the same
# name as a label (":run", ":bare"); loops whose counter and label differ
# (tries/:ready) ran. As in Tera Term's macro language, a label seems to
# occupy the name. The trigger commands after those lines were never sent, so
# SW_sws, camctl_set_still_focus_mode and camctl_af are all still untested.
# ttl_check() refuses a script that assigns to a label name.
AUTO_TRIGGERS = ("sw", "camctl_af")
AUTO_DEFAULTS = dict(mode="select", defocus="inf", trigger="sw", blocks=3, per_block=24,
                     pause_s=30, hold_ms=2500, gap_ms=1500, full=False, patch=None, seed=1, arms=None)
AUTO_SMOKE = dict(AUTO_DEFAULTS, blocks=1, per_block=5, steps=True, bare=3, probe_get=True)
# USB / HTTP probe: PROBE.SH stages after CAF.OK, then 3 normal presses.
AUTO_PROBE = dict(AUTO_DEFAULTS, blocks=1, per_block=3, probe_sh=True)
# Remote mode: no scripted presses. After CAF.OK the card starts a small web
# endpoint (busybox httpd + CGI) for the Mac (tools/af_remote.py); the Mac
# presses through the camera's own API (POST /v1/lens/focus, the same AF as a
# half-press, usb-probe4) over the camera's Wi-Fi.
AUTO_REMOTE = dict(AUTO_DEFAULTS, blocks=1, per_block=1, remote=True, remote_min=45)
REMOTE_PORT = 8080
# The smoke card (1) presses `bare` times with the trigger alone before any
# focus-mode command and (2) logs a step after each command
# (STEP_<label>.SH: uptime and label to STEP.LOG, then sync, so a forced
# power-off keeps what was written).
AUTO_DEFOCUS_MS, AUTO_SETTLE_MS = 800, 700
# After ARM.SH, a fixed wait instead of a handshake file: Linux cannot
# recreate a file the RTOS deleted (A/B session 1, work/af-logs/auto-ab1:
# "can't create ARM.OK: Input/output error" on every run but the first, like
# mv). ARM.SH logs its arm before that line and did so 1.3 s after the call.
AUTO_ARM_WAIT_MS = 2500       # ARM.SH: 1.3 s with one patch; up to 7 restores now
AUTO_READY_WAIT_MS = 10000


def auto_arm_list(auto):
    """The session's arms: --auto-arms, or STOCK and the --auto-patch patch,
    or ['-'] (no patch, smoke check)."""
    if auto.get("arms"):
        return list(auto["arms"])
    if auto.get("patch"):
        return [auto["patch"], "STOCK"]
    return ["-"]


def auto_patch_names(auto):
    """Every patch any arm of the session uses, in a fixed order."""
    names = []
    for arm in auto_arm_list(auto):
        for p in arm.split("+"):
            if p not in ("STOCK", "-") and p not in names:
                names.append(p)
    return names


def auto_seq(auto):
    """Arm per run. Each group holds every arm once in shuffled order, so the
    arms alternate closely and share the light of a block evenly."""
    import random
    arms = auto_arm_list(auto)
    n = auto["blocks"] * auto["per_block"]
    if arms == ["-"]:
        return ["-"] * n
    rng, seq = random.Random(auto["seed"]), []
    while len(seq) < n:
        group = list(arms)
        rng.shuffle(group)
        seq += group
    return seq[:n]


def auto_session_s(auto):
    """Upper estimate of the session length from power-on, for the tracer."""
    if auto.get("remote"):
        return 60 + auto["remote_min"] * 60
    per_run = (AUTO_DEFOCUS_MS + AUTO_SETTLE_MS + AUTO_ARM_WAIT_MS + auto["hold_ms"] + auto["gap_ms"]
               + (1000 if auto["full"] else 0)) / 1000
    runs = auto["blocks"] * auto["per_block"] + 1
    return 60 + (sum(w for _, w in PROBE_STAGES) / 1000 if auto.get("probe_sh") else 0) + AUTO_READY_WAIT_MS / 1000 + runs * per_run + (auto["blocks"] - 1) * auto["pause_s"] + 30


# Step labels: STEP_<label>.SH logs "<uptime> <label>" after that command.
AUTO_STEP_LABELS = ("START", "READY", "WAITED", "GET", "AFON", "AFOFF", "DEFOCUS", "MODE", "ARMOK", "END")


def auto_ttl_lines(auto):
    d = "C:\\SCRIPT\\AFDBG"

    def mark(label):
        if not auto.get("steps"):
            return []
        assert label in AUTO_STEP_LABELS
        return [f"sendln 'lcmd sh {AUTO_DIR}/STEP_{label}.SH'", "mpause 300"]

    on, off = (("camctl_af cam start", "camctl_af cam stop") if auto["trigger"] == "camctl_af"
               else ("SW_sws 1", "SW_sws 0"))
    press = [
        f"sendln '{on}'",
        *mark("AFON"),
        f"mpause {auto['hold_ms']}",
    ]
    if auto["full"]:
        press += ["sendln 'SW_swr 1'", "mpause 300", "sendln 'SW_swr 0'"]
    press += [f"sendln '{off}'", *mark("AFOFF"),
              f"mpause {auto['gap_ms'] + (700 if auto['full'] else 0)}"]
    defocus = [] if auto["defocus"] == "none" else [
        f"sendln 'camctl_set_still_focus_mode {auto['defocus']}'",
        *mark("DEFOCUS"),
        f"mpause {AUTO_DEFOCUS_MS}",
        f"sendln 'camctl_set_still_focus_mode {auto['mode']}'",
        *mark("MODE"),
        f"mpause {AUTO_SETTLE_MS}",
    ]
    # Wait for CAF.OK (MEM.SH every 2 s, at most 60 s), then
    # AUTO_READY_WAIT_MS more, before the first press: the hand protocol
    # always waited 15 s, and the CSV can only be armed after CAF.OK.
    ready = [
        "tries = 0",
        ":ready",
        f"filesearch '{d}\\CAF.OK'",
        "if result != 0 goto isready",
        f"sendln 'lcmd sh {AUTO_DIR}/MEM.SH'",
        "mpause 2000",
        "tries = tries + 1",
        "if tries < 30 goto ready",
        ":isready",
        *mark("READY"),
        f"mpause {AUTO_READY_WAIT_MS}",
        *mark("WAITED"),
    ]
    if auto.get("probe_get"):
        # A read-only camctl command first: does any camctl call return?
        ready += ["sendln 'camctl_get_focus_state'", *mark("GET")]
    if auto.get("probe_sh"):
        for stage, wait in PROBE_STAGES:
            ready += [f"sendln 'lcmd sh {AUTO_DIR}/PROBE.SH {stage}'", f"mpause {wait}"]
    bare = []
    if auto.get("bare"):
        # Half-presses with no defocus, no arm and no CSV: does SW_sws alone
        # start AF?
        bare = [*mark("START"), "nbare = 0", ":bare", *press,
                "nbare = nbare + 1", f"if nbare < {auto['bare']} goto bare"]
    if auto.get("remote"):
        # The endpoint daemonises itself; if it is not running 4 s later
        # (HTTPD.DEAD, which REMOTE.SH removes at start), start it in the
        # foreground instead. Then two lamp flashes: ready for the Mac.
        return [
            *ready,
            f"sendln 'lcmd sh {AUTO_DIR}/REMOTE.SH start'",
            "mpause 4000",
            f"sendln 'lcmd sh {AUTO_DIR}/REMOTE.SH check'",
            "mpause 2000",
            f"filesearch '{d}\\HTTPD.DEAD'",
            "if result = 0 goto served",
            f"sendln 'lcmd sh {AUTO_DIR}/REMOTE.SH fg'",
            "mpause 2000",
            ":served",
            "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'", "mpause 500",
            "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'",
            # Shell relay: the Mac's shell CGI writes RELAY.TTL (a sequence
            # number, engineering-shell lines from an allowlist, an ack through
            # RACK.SH); this loop includes it, and the sequence guard inside
            # skips a file it has already run. Every 50 rounds (about 20 s) a
            # heartbeat through RACK.SH hb shows that the loop still runs
            # (status relay_hb), to tell a dead loop from a file it misses.
            "rdone = 0",
            "rn = 0",
            ":relay",
            f"filesearch '{d}\\RELAY.TTL'",
            "if result = 0 goto rwait",
            f"include '{d}\\RELAY.TTL'",
            ":rwait",
            "mpause 400",
            "rn = rn + 1",
            "if rn < 50 goto relay",
            "rn = 0",
            f"sendln 'lcmd sh {AUTO_DIR}/RACK.SH hb'",
            "goto relay",
        ]
    lines = [
        *ready,
        *bare,
        "nrun = 0",
        "inblock = 0",
        "block = 0",
        ":run",
        *defocus,
        # ARM.SH sets the arm of this run; wait so the patch state never
        # changes during an AF run.
        f"sendln 'lcmd sh {AUTO_DIR}/ARM.SH'",
        f"mpause {AUTO_ARM_WAIT_MS}",
        *mark("ARMOK"),
        # Until MEMRD.BIN has seen the contrast AF object (CAF.OK) the CSV
        # cannot be armed; take a snapshot instead, which writes CAF.OK.
        f"filesearch '{d}\\CAF.OK'",
        "if result != 0 goto csv",
        f"sendln 'lcmd sh {AUTO_DIR}/MEM.SH'",
        "goto press",
        ":csv",
        ARM_CSV,
        ":press",
        *press,
        "nrun = nrun + 1",
        "inblock = inblock + 1",
        f"if inblock < {auto['per_block']} goto run",
        "inblock = 0",
        "block = block + 1",
        f"if block >= {auto['blocks']} goto finish",
        # Blink the AF lamp for pause_s: change the light now.
        "blinks = 0",
        ":blink",
        "sendln 'Af_Led_On'",
        "mpause 500",
        "sendln 'Af_Led_Off'",
        "mpause 500",
        "blinks = blinks + 1",
        f"if blinks < {auto['pause_s']} goto blink",
        "goto run",
        ":finish",
        # One more AF start writes the last run's CSV; the lamp stays on 10 s
        # to say the session is over.
        ARM_CSV,
        *press,
        "sendln 'Af_Led_On'",
        *mark("END"),
        "mpause 10000",
        "sendln 'Af_Led_Off'",
        ":done",
        "end",
    ]
    return lines


def arm_sh(auto):
    """ARM.SH, once per run: next run number from RUN.N, its arm from
    AUTO.SEQ. First every patch of the session is restored (exit 0 restored,
    6 already stock, 5 = another variant holds the shared margin words, which
    its own restore then clears; anything else is logged as R<patch>:<code>),
    then the arm's patches (names joined by '+') are applied. Logs
    'run arm statuses t_start t_end block' to AUTO.LOG (statuses: the apply
    exits joined by ',', '-' for none). PATCH.SH resets RUN.N at power-on
    (from Linux: Linux cannot recreate a file the RTOS deleted); run 1 moves
    an older AUTO.LOG to the end of AUTO.PREV."""
    patches = " ".join(auto_patch_names(auto))
    return (
        f"d={AUTO_DIR}\n"
        "exec 2>>$d/MEM.LOG\n"
        "read t0 _ </proc/uptime\n"
        "n=$(cat $d/RUN.N 2>/dev/null); n=$((${n:-0} + 1)); echo $n >$d/RUN.N\n"
        'if [ $n = 1 ] && [ -f $d/AUTO.LOG ]; then cat $d/AUTO.LOG >>$d/AUTO.PREV; rm -f $d/AUTO.LOG; fi\n'
        "set -- $(cat $d/AUTO.SEQ)\n"
        'if [ $n -le $# ]; then shift $((n - 1)); a=$1; else a=-; fi\n'
        "s=\n"
        + (f"for p in {patches}; do $d/$p.BIN restore; r=$?; "
           'case $r in 0|5|6) ;; *) s="${s}R$p:$r," ;; esac; done\n' if patches else "")
        + 'if [ "$a" != STOCK ] && [ "$a" != - ]; then\n'
        " o=$IFS; IFS=+; for p in $a; do IFS=$o; $d/$p.BIN; s=\"$s$?,\"; IFS=+; done; IFS=$o\n"
        "fi\n"
        's=${s%,}; s=${s:--}\n'
        "read t _ </proc/uptime\n"
        f'echo "$n $a $s $t0 $t $(( (n - 1) / {auto["per_block"]} ))" >>$d/AUTO.LOG\n'
        "exit 0\n"
    ).encode("ascii")


def step_sh(label):
    return (
        f"d={AUTO_DIR}\n"
        "read t _ </proc/uptime\n"
        f'echo "$t {label}" >>$d/STEP.LOG\n'
        "sync\n"
        "exit 0\n"
    ).encode("ascii")


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


# PEEK.BIN ADDR COUNT (both hex, no prefix): open /dev/mem read-only and write
# COUNT 32-bit words from ADDR to stdout, raw. Only word-aligned addresses in
# 0x53000000..0x5fefffff (RTOS image and heap) and at most 0x800 words, so a
# typo cannot read device registers. Exit status 0 done, 1 open, 2 mmap,
# 4 short write, 8 bad arguments, 9 out of range. For the remote endpoint.
PEEK_S = r"""    .syntax unified
    .arm
    .text
    .global _start
_start:
    ldr     r0, [sp]                @ argc
    cmp     r0, #3
    movne   r0, #8
    bne     quit
    ldr     r0, [sp, #8]            @ argv[1]
    adr     lr, got1                @ no bl: it leaves a relocation
    b       hex
got1:
    cmp     r1, #0
    moveq   r0, #8
    beq     quit
    mov     r6, r0                  @ address
    ldr     r0, [sp, #12]           @ argv[2]
    adr     lr, got2
    b       hex
got2:
    cmp     r1, #0
    moveq   r0, #8
    beq     quit
    mov     r8, r0                  @ word count
    cmp     r8, #0
    moveq   r0, #9
    beq     quit
    cmp     r8, #0x800
    movhi   r0, #9
    bhi     quit
    tst     r6, #3
    movne   r0, #9
    bne     quit
    ldr     r1, =0x53000000
    cmp     r6, r1
    movlo   r0, #9
    blo     quit
    ldr     r1, =0x5ff00000
    cmp     r6, r1
    movhs   r0, #9
    bhs     quit
    adr     r0, dev
    ldr     r1, =0x101000           @ O_RDONLY | O_SYNC
    mov     r7, #5                  @ open
    svc     #0
    cmn     r0, #4096
    movhi   r0, #1
    bhi     quit
    mov     r4, r0
    mov     r0, #0
    mov     r1, #16384              @ offset < 4096 plus at most 8192 bytes
    mov     r2, #1                  @ PROT_READ
    mov     r3, #1                  @ MAP_SHARED
    lsr     r5, r6, #12
    mov     r7, #192                @ mmap2
    svc     #0
    cmn     r0, #4096
    movhi   r0, #2
    bhi     quit
    ldr     r1, =0xfff
    and     r1, r6, r1
    add     r1, r0, r1
    mov     r0, #1                  @ stdout
    lsl     r2, r8, #2
    mov     r9, r2
    mov     r7, #4                  @ write
    svc     #0
    cmp     r0, r9
    movne   r0, #4
    moveq   r0, #0
quit:
    mov     r7, #1                  @ exit
    svc     #0
hex:                                @ r0 string -> r0 value, r1 = digits (0 = bad)
    mov     r2, #0
    mov     r1, #0
1:
    ldrb    r3, [r0], #1
    cmp     r3, #0
    beq     3f
    sub     r12, r3, #48            @ '0'
    cmp     r12, #10
    blo     2f
    orr     r12, r3, #0x20
    sub     r12, r12, #97           @ 'a'
    cmp     r12, #6
    bhs     4f
    add     r12, r12, #10
2:
    add     r1, r1, #1
    cmp     r1, #8
    bhi     4f
    orr     r2, r12, r2, lsl #4
    b       1b
3:
    mov     r0, r2
    bx      lr
4:
    mov     r1, #0
    bx      lr
dev:
    .asciz  "/dev/mem"
    .balign 4
    .ltorg
"""


def peek_bin():
    return arm_elf(PEEK_S)


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

# Dim-light peak margin. The detectors accept a peak only when the contrast
# rises and then falls by at least T = +0x28 + +0x4c x +0x80 (normalised
# units). +0x80 comes from 0x5340c590: with y = brightness per pixel and a
# per-mode base b (table 0x53c16bb0, about 570-600), it returns sqrt(max(b/y,
# 0.25)) when y >= b, but b/y itself when y < b, so in dim light the margin
# grows with 1/brightness instead of 1/sqrt(brightness). Traces show the
# jitter of the normalised series stays at 0.001-0.002 while T reaches 20-35
# times that, and clear peaks are rejected because their fall stays below T.
# The patch removes the early return (bhi to the end) so dim light also takes
# the square root. It changes all three callers of 0x5340c590 (the per-frame
# detector update 0x5340caec, 0x5340c7c8, and the threshold helper 0x5340c620).
SQRTNS_SITES = (
    (0x5340C5F0, 0x8A000008, 0xE1A00000),   # bhi 0x5340c618 -> nop
)

# Milder variant: x^0.75 instead of x (x = b/y > 1), sqrt as stock above b.
# SQRTNS (x^0.5) locked 11 of 12 dim presses but some on wrong, end-of-scan
# peaks. Rewrites 0x5340c5f0..0x5340c614 with VFP vsqrt (the stock bright-light
# path called the double sqrt at 0x53153ba8; same result):
#   vsqrthi s14, s0; vsqrthi s13, s14; vmulhi s0, s14, s13; bhi 0x5340c618
#   vmov s15, #0.25; vcmpe s0, s15; vmrs; vmovmi s0, s15; vsqrt s0, s0; nop
# Scale 6.1 -> 3.9, 13 -> 6.8, 27 -> 11.8 (sqrt: 2.5, 3.6, 5.2). Not together
# with SQRTNS (the patcher then finds an unknown word and writes nothing).
SCALE75_SITES = tuple(zip(range(0x5340C5F0, 0x5340C618, 4), (
    0x8A000008, 0xE3063BD4, 0xE34533C1, 0xEDD37A00, 0xEEB40AE7,
    0xEEF1FA10, 0x5EF07A40, 0xEEB70AE7, 0xEBF51D64, 0xEEB70BC0), (
    0x8EB17AC0, 0x8EF16AC7, 0x8E270A26, 0x8A000005, 0xEEF57A00,
    0xEEB40AE7, 0xEEF1FA10, 0x4EB00A67, 0xEEB10AC0, 0xE1A00000)))

# X875: x^0.875 (x / x^0.125: three vsqrt and a vdiv), a milder step than
# SCALE75; same ten words, same bright-light branch:
#   vsqrthi s14, s0; vsqrthi s14, s14; vsqrthi s14, s14; vdivhi s0, s0, s14
#   bhi 0x5340c618; vmov s15, #0.25; vcmpe s0, s15; vmrs; vmovmi s0, s15
#   vsqrt s0, s0
# Scale 6 -> 4.8, 13 -> 9.4, 27 -> 17.9, 59 -> 35.
X875_SITES = tuple(zip(range(0x5340C5F0, 0x5340C618, 4), (w for _, w, _ in SCALE75_SITES), (
    0x8EB17AC0, 0x8EB17AC7, 0x8EB17AC7, 0x8E800A07, 0x8A000004,
    0xEEF57A00, 0xEEB40AE7, 0xEEF1FA10, 0x4EB00A67, 0xEEB10AC0)))

# HALFB (data only): the per-mode base b of 0x5340c590 (six floats at
# 0x53c16bb0: 569.22, 569.22, 574.5, 597.85, 563.3, 563.3) halved. Below b/2
# the scale is half of stock (6 -> 3, 13 -> 6.5, 27 -> 13.5, 59 -> 29.5);
# between b/2 and b it is sqrt(b/2y) (0.7..1) instead of b/y (1..2); above b
# it is 0.7 x stock, down to the same 0.5 floor. Against SCALE75 it eases mild
# dim light more and deep dim light less.
HALFB_SITES = (
    (0x53C16BB0, 0x440E4E14, 0x438E4E14),
    (0x53C16BB4, 0x440E4E14, 0x438E4E14),
    (0x53C16BB8, 0x440FA000, 0x438FA000),
    (0x53C16BBC, 0x44157666, 0x43957666),
    (0x53C16BC0, 0x440CD333, 0x438CD333),
    (0x53C16BC4, 0x440CD333, 0x438CD333),
)

# FALLS2 (data only): [0x53c16cc0] = 3 is read once, at 0x5340dcc0 in the
# detector update 0x5340dbfc, and compared with the RISE count +0xbc: a rising
# run of N samples whose rise exceeds +0x28 + T sets the detector's return
# value (first read as a fall count, hence the name; corrected 2026-11-21, see
# docs/af-subsystem.html#replay-rebuilt). Measured: 2 accepts about one frame
# earlier (34 ms at 29 fps).
FALLS2_SITES = (
    (0x53C16CC0, 0x00000003, 0x00000002),
)

# RISE10 / RISE05 (data only): the absolute floor of the detector threshold.
# 0x5340c3c4 sets +0x28 (and the copies +0x2c, +0x30) from the per-mode table
# 0x53c16d14 (8 bytes per mode; the first float unless +0x22 is set). AF-S
# runs in mode 0, where it is 0.02 (x 0.25 = 0.005 when +0xac is set). A peak
# counts when its rise exceeds +0x28 + T (T = +0x4c x +0x80), so at the edge
# of dim light, where T is only about 0.005, the floor decides: the first
# sweep starts about 4 samples before the peak and rises only 0.003-0.021,
# so it is rejected, and the camera turns and accepts on the way back
# (remote-nd2: 37 of 37 first sweeps). The table is read only there.
RISE10_SITES = (
    (0x53C16D14, 0x3CA3D70A, 0x3C23D70A),   # 0.02 -> 0.01
)
RISE05_SITES = (
    (0x53C16D14, 0x3CA3D70A, 0x3BA3D70A),   # 0.02 -> 0.005
)

# FORCEM0 (test harness, not a fix): the detector mode for every AF area
# comes from the bytes at CAF +0x43c1 + area, loaded in 0x533f893c for the
# horizontal (0x533f8a34) and vertical (0x533f8ad4) detector setups. In dim
# light the camera sets them to 0 (rise floor 0.02, its own margin factor and
# base); otherwise 1 (floor 0.001). The writer is not decoded. Loading 0
# instead reproduces the dim-light mode in any light, so the rise floor can
# be tested without chasing the light level.
FORCEM0_SITES = (
    (0x533F8A34, 0xE5D9A000, 0xE3A0A000),   # ldrb sl, [sb] -> mov sl, #0
    (0x533F8AD4, 0xE5D91000, 0xE3A01000),   # ldrb r1, [sb] -> mov r1, #0
)

# FORCEG0 (test harness, not a fix): 0x533f85dc(obj, grade) writes the phase
# decision grade of every AF area (CAF +0x43c1 + area, the [DicisionGrade] of
# the CSV). 0x533f88c4 gives 0 when the phase result is not usable or a level
# at +0x41e4/+0x41e8 is at most 0x20000, else 1 (2 on search path 2); the
# search start 0x53400e10 sets 1 in several branches; the turn in 0x53404b28
# (0x5340545c) sets 0 before the return pass. Grade 0 makes the contrast
# detectors use mode 0 (rise floor 0.02 instead of 0.001). Forcing the grade
# to 0 at the setter reproduces the dim-light case in any light, for testing
# RISE10/RISE05 against it. Unlike FORCEM0 it also changes everything else
# that reads the grade.
FORCEG0_SITES = (
    (0x533F85EC, 0xE1A05001, 0xE3A05000),   # mov r5, r1 -> mov r5, #0
)

# Phase-guided start of the first sweep (path 0). 0x534064e0 pre-moves the
# lens toward the phase-predicted peak but stops OFFSET pulses short of it:
# OFFSET = max(clamp(X, 2s, 4s), min(4s, 2 FULL)) plus a correction when the
# defocus is large (s = drive step from 0x533fc0c0; the firmware's own debug
# text calls these "OFFSET (FULL/AVE/LIMIT/CORRECTION)"). The window still
# ends at a fixed distance past the predicted peak (0x5340523c), so a larger
# OFFSET lengthens only the rising side of the first sweep. In dim light the
# first sweep starts about 4 samples before the peak, too few for the rise to
# clear the threshold.
OFFS4_SITES = (
    (0x534065D4, 0xE1A0108A, 0xE1A0110A),   # lower clamp 2s -> 4s
)
OFFS2_SITES = (
    (0x534065F8, 0xA1A03002, 0xA1A03082),   # OFFSET x2 (movge r3,r2 -> lslge r3,r2,#1)
    (0x534065FC, 0xB1A03000, 0xB1A03080),   # (movlt r3,r0 -> lsllt r3,r0,#1)
)

# Near-stop guard candidates. In a scene without a real peak, stock sometimes
# reports a lock at the near stop, on area 1 with no rise-then-fall in area 0.
# Two code paths can do that:
# NOFB: the second chance after a failed path-1 pass (0x53404878 re-runs the
#   pass decision as path 2, then calls 0x53400c50). 0x53400c50 picks the
#   first area, starting at area 1, whose state byte +0x14bb8+area is 3 and
#   sets it to 4 (lens placed by 0x5340d838, not by the peak fit). With the
#   patch it always returns 0: that pass ends 0x40 -> 0x80 (fail) instead.
#   Also used at the end of the path-2 return pass (0x53405120).
# NOFO: the fall-only accept 0x5340afc0 (via 0x5340ced4 in the detector
#   wrapper 0x5340e0b8) returns 1 with no rise test (a fall of floor + 2T
#   10-20 samples after the maximum). With the patch it never accepts. Meant
#   for subjects at the near limit, probably: those would fail instead.
NOFB_SITES = (
    (0x53400D74, 0xE0872004, 0xEAFFFFFC),   # add r2,r7,r4 -> b 0x53400d6c (return 0)
)
NOFO_SITES = (
    (0x5340B15C, 0xE3A00001, 0xE3A00000),   # mov r0,#1 -> mov r0,#0
)

# Patches a session can switch per run (--auto-arms). The margin variants
# share the words at 0x5340c5f0, the rise floors the word at 0x53c16d14, so
# only one of each group can be on at a time.
AUTO_PATCHES = {"SCALE75": None, "SQRTNS": None, "X875": None, "HALFB": None, "FALLS2": None,
                "FORCE49": None, "FORCE100": None, "RISE10": None, "RISE05": None,
                "FORCEM0": None, "FORCEG0": None, "OFFS4": None, "OFFS2": None, "NOFB": None, "NOFO": None,
                "NO29": None}
AUTO_EXCLUSIVE = ("SCALE75", "SQRTNS", "X875")
AUTO_EXCLUSIVE_GROUPS = (AUTO_EXCLUSIVE, ("RISE10", "RISE05"))


def arm_conflict(parts):
    """The first exclusive group an arm uses twice, or None."""
    for group in AUTO_EXCLUSIVE_GROUPS:
        if sum(p in group for p in parts) > 1:
            return group
    return None

# NO29: 0x533f4a70 picks the drive level from three values at [CAF+0xfcc]
# (each above 0x7ffff lowers it; in dim light all three sit at about 0x80400,
# the gain cap). Its level-3 store "movhi r3, #3" becomes #2, so light that
# would give 29 fps gives 49 fps; brighter light (levels 0-2) is unchanged.
# Live 2026-11-19: level stays 1 (100 fps) in a bright scene. Unlike FORCE49
# the level byte itself changes, so the trace shows the rate.
# 2026-11-19: with the ND filter in it gives 49 fps; with kb 0.32 it cut a
# press from 575 to 440 ms (docs af-debug-log.html#opt-no29).
NO29_SITES = ((0x533F4B10, 0x83A03003, 0x83A03002),)

# The same five sites forced to drive level 2 (49 fps) instead of 1 (100 fps):
# for testing the faster sampling with the milder margin (SCALE75). Like
# FORCE100 it also forces the level in bright light, where stock uses 200 fps.
FORCE49_SITES = tuple((a, old, new + 1) for a, old, new in FORCE100_SITES)

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
# written). Record: tv_sec, tv_nsec (CLOCK_MONOTONIC), then one word per offset,
# then the four detector arrays. Exit status (TRACE.ERR): 0 done, 1 open
# /dev/mem, 2 map, 3 create TRACE.DAT, 6 map the arrays, 7 unexpected array
# pointers.
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
    # Horizontal rise/fall detectors, one per AF area: +0x102c + 0xc4 x area
    # (vertical ones at +0x2724). Update 0x5340bf78: b = noise-normalised
    # contrast of the last three frames (0x5340b3ac); a rise when
    # (b4 + 3 b8)/4 > (b0 + 2 b4 + b8)/4, otherwise a fall.
    0x10D0,   # area 0: raw contrast, newest frame (+0xa4)
    0x10B8,   # area 0: brightness, newest frame (+0x8c)
    0x10DC,   # area 0: b0 (float, +0xb0)
    0x10E0,   # area 0: b4 (float, +0xb4)
    0x10E4,   # area 0: b8 (float, +0xb8)
    0x1078,   # area 0: peak margin factor (float, +0x4c)
    0x10AC,   # area 0: peak margin scale (float, +0x80)
    0x1194,   # area 1: raw contrast (+0xa4)
    0x117C,   # area 1: brightness (+0x8c)
    0x11A0,   # area 1: b0
    0x11A4,   # area 1: b4
    0x11A8,   # area 1: b8
    0x11AC,   # area 1: rising / falling counts
    # Detector headers: +0xc samples in the pass, +0x18 peak sample, +0x1c..+0x27
    # flags (+0x24 and +0x26 are read by 0x5340bbd8 / 0x5340bbd0).
    0x1038, 0x1044, 0x1048, 0x104C, 0x1050,     # area 0
    0x10FC, 0x1108, 0x110C, 0x1110, 0x1114,     # area 1
    # Per-area results after a pass (written at 0x53404658): count, flags.
    0x0F3C, 0x0F40, 0x0F5C, 0x4074, 0x4078, 0x40EC, 0x40F0,
    0x1100, 0x1104,                             # area 1: max / min index (+0x10/+0x14)
)
# Then, per record, the first TRACE_ARRAY_WORDS entries of four per-sample
# arrays on the heap, through the pointers at detector +0 and +4: raw
# contrast (u32) and the smoothed noise-normalised value (float) that the
# peak helpers 0x5340bbe0 / 0x5340bc18 / 0x5340bc2c read; area 0, then area 1.
TRACE_ARRAY_WORDS = 40
# Earlier layouts: (fields, array words) per record.
TRACE_LAYOUTS = ((None, TRACE_ARRAY_WORDS), (29, 0), (16, 0))
TRACE_PERIOD_NS, TRACE_RECORDS, TRACE_CHUNK = 4_000_000, 15_000, 250
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
    ldr     r3, =0x102c             @ area 0 detector
    add     r3, r6, r3
    ldr     r0, [r3]                @ p0: area 0 raw contrast array
    ldr     r1, [r3, #4]            @ p1: area 0 smoothed array
    ldr     r2, [r3, #0xc4]         @ p2: area 1 raw contrast array
    ldr     r3, [r3, #0xc8]         @ p3: area 1 smoothed array
    cmp     r0, #0                  @ expect p0 < p1 < p2 < p3, within 256 KB
    beq     badptr
    cmp     r1, r0
    bls     badptr
    cmp     r2, r1
    bls     badptr
    cmp     r3, r2
    bls     badptr
    sub     ip, r3, r0
    cmp     ip, #0x40000
    bhs     badptr
    push    {r0, r1, r2, r3}        @ [sp] = p0..p3
    lsr     r5, r0, #12             @ map from p0's page to past p3's array
    sub     r1, r3, r5, lsl #12
    add     r1, r1, #{array_bytes}
    mov     r0, #0
    mov     r2, #1
    mov     r3, #1
    mov     r7, #192
    svc     #0
    cmn     r0, #4096
    movhi   r0, #6
    bhi     quit
    sub     r0, r0, r5, lsl #12     @ pointer -> mapping
    pop     {r1, r2, r3, ip}
    add     r1, r1, r0
    add     r2, r2, r0
    add     r3, r3, r0
    add     ip, ip, r0
    push    {r1, r2, r3, ip}
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
    mov     r3, #0                  @ the four arrays
array:
    ldr     r1, [sp, r3, lsl #2]
    mov     r2, #{array_words}
copy:
    ldr     r0, [r1], #4
    str     r0, [r9], #4
    subs    r2, r2, #1
    bne     copy
    add     r3, r3, #1
    cmp     r3, #4
    bne     array
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
badptr:
    mov     r0, #7
    b       quit
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


def trace_bin(records=TRACE_RECORDS):
    offsets = "\n".join(f"    .word   {o:#x}" for o in TRACE_OFFSETS)
    rec = 8 + 4 * len(TRACE_OFFSETS) + 16 * TRACE_ARRAY_WORDS
    return arm_elf(TRACE_S.replace("{map_len}", f"{TRACE_MAP:#x}")
                   .replace("{array_bytes}", f"{4 * TRACE_ARRAY_WORDS:#x}")
                   .replace("{array_words}", str(TRACE_ARRAY_WORDS))
                   .replace("{map_page}", f"{(CAF_OBJECT & ~0xfff) >> 12:#x}")
                   .replace("{obj_offset}", f"{CAF_OBJECT & 0xfff:#x}")
                   .replace("{chunk_bytes}", f"{rec * TRACE_CHUNK:#x}")
                   .replace("{records}", str(records))
                   .replace("{nfields}", str(len(TRACE_OFFSETS)))
                   .replace("{chunk}", str(TRACE_CHUNK))
                   .replace("{period_ns}", str(TRACE_PERIOD_NS))
                   .replace("{offsets}", offsets))


def trace_records(path):
    """The records of a TRACE.DAT as (seconds, words): the fields, then any
    array words. Older traces have fewer fields (TRACE_LAYOUTS); the layout
    is picked by the plausible steps between the first timestamps."""
    import struct
    data = Path(path).read_bytes()
    for n, arrays in TRACE_LAYOUTS:
        n = (n or len(TRACE_OFFSETS)) + 4 * arrays
        rec = 8 + 4 * n
        if len(data) % rec or len(data) < 3 * rec:
            continue
        ts = [struct.unpack_from("<II", data, k * rec) for k in range(3)]
        steps = [(b[0] - a[0]) * 1e9 + b[1] - a[1] for a, b in zip(ts, ts[1:])]
        if all(nsec < 1_000_000_000 for _, nsec in ts) and all(0 < d < 100_000_000 for d in steps):
            return [(sec + nsec / 1e9, w) for sec, nsec, *w in
                    (struct.unpack_from(f"<II{n}I", data, k * rec) for k in range(len(data) // rec))]
    sys.exit(f"{path}: not a TRACE.DAT of a known layout")


def read_trace(path):
    """Print the changes in a TRACE.DAT: one line whenever a traced state field
    changes (the contrast fields change every frame; see --trace-contrast)."""
    prev, t0 = None, None
    for t, w in trace_records(path):
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


def trace_contrast(path):
    """Print one line per contrast frame of each AF area (a new raw contrast or
    b8 value) during searches (action 4): the raw contrast, brightness, the
    normalised value b8, the detector's step (b4 + 3 b8)/4 - (b0 + 2 b4 + b8)/4
    that it compares with 0, and its rise/fall counts."""
    import struct
    records = trace_records(path)
    if len(records[0][1]) < 29:
        sys.exit(f"{path}: recorded without the contrast fields")
    f32 = lambda x: struct.unpack("<f", struct.pack("<I", x))[0]
    t0, prev = records[0][0], {}
    for t, w in records:
        if w[0] & 0xffff != 4:
            prev.clear()
            continue
        path_ = (w[1] >> 16) & 0xff
        for area, (raw, y, b0, b4, b8, counts) in enumerate(((w[16], w[17], w[18], w[19], w[20], w[12]),
                                                              (w[23], w[24], w[25], w[26], w[27], w[28]))):
            key = (raw, b8)
            if prev.get(area) == key:
                continue
            prev[area] = key
            b0, b4, b8 = f32(b0), f32(b4), f32(b8)
            step = (b4 + 3 * b8) / 4 - (b0 + 2 * b4 + b8) / 4
            print(f"{t - t0:9.3f} path {path_} area {area}  raw {raw:9d}  Y {y:8d}  b8 {b8:10.4f}  "
                  f"step {step:+9.4f}  rise/fall {counts & 0xffff}/{counts >> 16}"
                  + (f"  margin {f32(w[21]):.4g} x {f32(w[22]):.4g}" if area == 0 else ""))


def trace_passes(path):
    """For each pass (action 4 to anything else) in a v3 trace, print the
    detector state of the last record of the pass for areas 0 and 1: sample
    count, max/min/peak indices (+0x10/+0x14/+0x18), flags (+0x1c..+0x27),
    and the per-sample raw contrast and smoothed value; then the outcome
    (action, area index, per-area results)."""
    import struct
    records = trace_records(path)
    n = len(TRACE_OFFSETS)
    if len(records[0][1]) < n + 4 * TRACE_ARRAY_WORDS:
        sys.exit(f"{path}: recorded without the detector arrays")
    f32 = lambda x: struct.unpack("<f", struct.pack("<I", x))[0]
    signed = lambda x: x - (1 << 32) if x >> 31 else x
    t0, prev = records[0][0], None
    for t, w in records:
        act = w[0] & 0xffff
        if prev is not None and prev[1][0] & 0xffff == 4 and act != 4:
            pt, pw = prev
            print(f"{pt - t0:8.3f} path {(pw[1] >> 16) & 0xff} -> action {act}, area {signed(w[5])}; "
                  f"results {w[39]:#010x} {w[40]:#010x} {w[41]:#010x} {[signed(x) for x in w[42:46]]}")
            for area in (0, 1):
                hdr = pw[29 + 5 * area: 34 + 5 * area]
                count = min(hdr[0], TRACE_ARRAY_WORDS)
                raw = pw[n + 2 * area * TRACE_ARRAY_WORDS:][:count]
                val = [f32(x) for x in pw[n + (2 * area + 1) * TRACE_ARRAY_WORDS:][:count]]
                max_i, min_i = (pw[13], pw[14]) if area == 0 else (pw[46], pw[47])
                print(f"    area {area}: n {hdr[0]} max@{max_i} min@{min_i} peak@{signed(hdr[1])} "
                      f"flags {hdr[2]:#010x} {hdr[3]:#010x} {hdr[4]:#010x}")
                print("      raw " + " ".join(str(x) for x in raw))
                print("      val " + " ".join(f"{x:.4f}" for x in val))
        prev = (t, w)


RISE_FLOOR = 0.02   # +0x28 in AF-S (mode 0), table 0x53c16d14


def pass_features(path):
    """One dict per pass (action 4 to anything else) of a v3 trace, area 0:
    path, next action and area index, sample count, peak index, the rise of
    the rising run that ends at the peak, the fall after it, samples after
    the peak, the margin T = +0x4c x +0x80, and the drive level."""
    import struct
    records = trace_records(path)
    n = len(TRACE_OFFSETS)
    if len(records[0][1]) < n + 4 * TRACE_ARRAY_WORDS:
        sys.exit(f"{path}: recorded without the detector arrays")
    f32 = lambda x: struct.unpack("<f", struct.pack("<I", x))[0]
    signed = lambda x: x - (1 << 32) if x >> 31 else x
    out, prev = [], None
    for t, w in records:
        act = w[0] & 0xffff
        if prev is not None and prev[1][0] & 0xffff == 4 and act != 4:
            pt, pw = prev
            count = min(pw[29], TRACE_ARRAY_WORDS)
            val = [f32(x) for x in pw[n + TRACE_ARRAY_WORDS:][:count]]
            if count > 2:
                k = max(range(count), key=lambda i: val[i])
                j = k
                while j > 0 and val[j - 1] < val[j]:
                    j -= 1
                out.append(dict(t=pt, path=(pw[1] >> 16) & 0xff, act=act, area=signed(w[5]), n=pw[29],
                                peak_i=k, run=k - j, rise=val[k] - val[j], fall=val[k] - min(val[k:]),
                                after=count - 1 - k, T=f32(pw[21]) * f32(pw[22]),
                                level=(pw[10] >> 16) & 0xff))
        prev = (t, w)
    return out


def pass_table(path, floor=RISE_FLOOR):
    """Print pass_features with the rise against floor + T (the rule that
    decides at the edge of dim light) and whether the pass was accepted."""
    t0 = None
    for p in pass_features(path):
        t0 = p["t"] if t0 is None else t0
        acc = p["act"] in (8, 128)
        need = floor + p["T"]
        print(f"{p['t'] - t0:8.3f} path {p['path']} {'ACCEPT' if acc else 'reject'} (action {p['act']:3d}) "
              f"n {p['n']:3d} peak@{p['peak_i']:2d} run {p['run']:2d} rise {p['rise']:.4f} "
              f"{'>' if p['rise'] > need else '<'} {need:.4f} (floor+T)  fall {p['fall']:.4f}  "
              f"T {p['T']:.4f}  after {p['after']:2d}  {AF_FPS.get(p['level'], '?')} fps")


def trace_presses(path):
    """One dict per AF run (half-press) in a TRACE.DAT: t (start, s), ms (half-press
    to done, None if the trace ends first), pre (action 2 to the first action 4,
    s), passes, outcome (lock = action 8, fail = 64, abort = released before a
    result), final (lens position +0xc434 at action 128; about -15,900 is the
    near stop), scale (+0x80 per sampling record) and y (area-0 brightness per
    sampling record; both empty without contrast fields)."""
    import struct
    f32 = lambda x: struct.unpack("<f", struct.pack("<I", x))[0]
    signed = lambda x: x - (1 << 32) if x >> 31 else x
    records = trace_records(path)
    has_contrast = len(records[0][1]) >= 29
    runs, cur, last = [], None, None
    for t, w in records:
        act = w[0] & 0xffff
        # A run starts with action 2 (pre-move), or straight with 4 (sampling)
        # when the lens needs no pre-move (smoke check 5, run 4); a 4 at the
        # first record is a run already under way when the trace began.
        if (act == 2 and last in (1, None)) or (act == 4 and last == 1):
            cur = {"t": t, "acts": [], "final": None, "scale": [], "y": [], "levels": [],
                   "acc0": w[3] & 0xff, "c434": None, "modes": []}
            runs.append(cur)
        if cur is not None and "end" not in cur:
            if not cur["acts"] or cur["acts"][-1] != act:
                cur["acts"].append(act)
                if act == 4 and "pre" not in cur:
                    cur["pre"] = t - cur["t"]
            if act == 128 and signed(w[6]):
                cur["final"] = signed(w[6])
            if act == 4 and signed(w[6]):
                cur["c434"] = signed(w[6])
            if act == 4:
                cur["levels"].append((w[10] >> 16) & 0xff)
            if act == 4 and has_contrast:
                cur["scale"].append(f32(w[22]))
                cur["y"].append(w[17])
            # Area-0 detector mode (+0x102c +0x20; 0 in dim light, then the
            # rise floor +0x28 is 0.02, else 0.001). v3 traces only.
            if act == 4 and len(w) > 32:
                cur["modes"].append(w[32] & 0xff)
            if act == 1 and len(cur["acts"]) > 1:
                cur["end"] = t
                cur["acc_end"], cur["area_end"] = w[3] & 0xff, signed(w[5])
        last = act
    for r in runs:
        r["outcome"] = "lock" if 8 in r["acts"] else "fail" if 64 in r["acts"] else \
            "done" if 128 in r["acts"] else "abort"
        # Locks whose actions 8 and 128 fell between two trace samples (auto-ab2
        # runs 25, 26, 29: 4, then straight to 1): the accepted flag +0xc470
        # went from 0 to 1 during the run and an area was chosen. The final
        # position is then the last sweep position +0xc434 seen.
        if (r["outcome"] == "abort" and r.get("acc_end") == 1 and r["acc0"] == 0
                and r.get("area_end", -1) >= 0):
            r["outcome"], r["inferred"] = "lock", True
            r["final"] = r["final"] if r["final"] is not None else r["c434"]
        # Drive level during sampling (0..3 = 200/100/49/29 fps): the AF
        # signal Y is per frame, so it only compares at the same level.
        r["level"] = max(set(r["levels"]), key=r["levels"].count) if r["levels"] else None
        r["ms"] = 1000 * (r["end"] - r["t"]) if "end" in r else None
        r["passes"] = r["acts"].count(4)
        r["mode"] = "".join(str(m) for i, m in enumerate(r["modes"]) if i == 0 or m != r["modes"][i - 1]) or None
    return records[0][0], runs


AF_FPS = {0: 200, 1: 100, 2: 49, 3: 29}    # drive level +0x1022 -> contrast-AF frame rate


def read_auto_log(path):
    """AUTO.LOG of an automated session: one dict per run (n, arm, status,
    t = uptime when ARM.SH started, t_end = when it was done, block). Reads
    both the 5-field A/B format and the 6-field multi-arm one."""
    out = []
    for line in Path(path).read_text().split("\n"):
        f = line.split()
        if len(f) == 5:
            out.append({"n": int(f[0]), "arm": f[1], "status": f[2], "t": float(f[3]),
                        "t_end": float(f[3]), "block": int(f[4])})
        elif len(f) == 6:
            out.append({"n": int(f[0]), "arm": f[1], "status": f[2], "t": float(f[3]),
                        "t_end": float(f[4]), "block": int(f[5])})
    return out


def join_auto_log(runs, log, window=4.0):
    """Give each AF run the AUTO.LOG entry of its press: the last entry set at
    most 0.5 s after the run started and at most `window` s before (TRACE.DAT uses
    CLOCK_MONOTONIC, AUTO.LOG /proc/uptime; both count from boot). A second
    run on the same entry is marked extra (an AF start the script did not
    press). Returns the entries no run was found for."""
    used = set()
    for r in runs:
        entry = None
        for e in log:
            if e["t"] <= r["t"] + 0.5:
                entry = e
        if entry is not None and r["t"] - entry["t"] < window:
            r["auto"] = entry
            r["extra"] = entry["n"] in used
            used.add(entry["n"])
    return [e for e in log if e["n"] not in used]


def trace_summary(path, auto_log=None, window=4.0, wrong_pulses=150):
    """Print trace_presses() one line per AF run (start, time, pre-move,
    passes, outcome, final lens position, scale max and
    median area-0 brightness, a light level that does not depend on a patch),
    then totals. With an AUTO.LOG: the run number, arm and light block of
    each run, and totals per block and arm."""
    import statistics
    t0, runs = trace_presses(path)
    missed = join_auto_log(runs, read_auto_log(auto_log), window) if auto_log else []
    # A fixed target gives one reference lens position: the median final
    # position of all locks. A lock more than wrong_pulses from it is a wrong
    # lock. Spread of good locks: 16 pulses on the tripod in bright light
    # (auto-ab2), up to about 180 in the handheld dim sessions; the SQRTNS
    # false locks sat at the near stop, about 400 pulses off.
    finals = [r["final"] for r in runs if r["outcome"] in ("lock", "done") and r["final"] is not None]
    ref = statistics.median(finals) if finals else None
    for r in runs:
        r["wrong"] = (ref is not None and r["outcome"] in ("lock", "done") and r["final"] is not None
                      and abs(r["final"] - ref) > wrong_pulses)
    if auto_log:
        late = [e for e in read_auto_log(auto_log)
                if any(r.get("auto") is e for r in runs) and
                any(r.get("auto") is e and r["t"] < e["t_end"] for r in runs)]
        if late:
            print("warning: AF started before ARM.SH finished in runs "
                  + " ".join(str(e["n"]) for e in late))
        bad = [e for e in read_auto_log(auto_log)
               if e["status"] != "-" and any(c not in ("0", "6") for c in e["status"].split(","))]
        if bad:
            print("warning: patch status not 0/6 in runs "
                  + " ".join(f"{e['n']}({e['status']})" for e in bad))
    for r in runs:
        ms = 1000 * (r.get("end", r["t"]) - r["t"])
        extra = (f"  scale max {max(r['scale']):5.2f}  Y {statistics.median(r['y']):8.0f}"
                 if r["scale"] else "")
        tag = ""
        if auto_log:
            e = r.get("auto")
            tag = (f"run {e['n']:3d} {e['arm']} blk {e['block']}{' extra' if r['extra'] else ''}  "
                   if e else "run   ? ? blk ?  ")
        print(f"{tag}{r['t'] - t0:7.2f} s {ms:5.0f} ms  pre-move {1000 * r.get('pre', 0):4.0f} ms  "
              f"{AF_FPS.get(r['level'], '?'):>3} fps  "
              f"passes {r['passes']}  {r['outcome']:5s}{'*' if r.get('inferred') else ' '}{' WRONG' if r['wrong'] else '      '}  "
              f"final {r['final']!s:>6s}{extra}")
    if runs:
        print(f"\nreference lens position (median of all locks): {ref}; wrong lock = more than "
              f"{wrong_pulses} pulses from it; * = lock seen only by the accepted flag (codes 8/128 "
              "fell between two trace samples). Y is the AF signal per frame: it only compares at "
              "the same fps, and auto exposure holds it near about 500,000 until the gain is at "
              "its ceiling; dim light = 29 fps and Y far below that (earlier dim sessions 85,000-132,000).")
        groups = {"all": runs}
        if auto_log:
            keyed = [r for r in runs if r.get("auto") and not r["extra"]]
            groups = {}
            for r in sorted(keyed, key=lambda r: (r["auto"]["block"], r["auto"]["arm"])):
                groups.setdefault(f"block {r['auto']['block']} arm {r['auto']['arm']}", []).append(r)
            for arm in sorted({r["auto"]["arm"] for r in keyed}):
                groups[f"all blocks arm {arm}"] = [r for r in keyed if r["auto"]["arm"] == arm]
        for name, rs in groups.items():
            print(f"{name}: " + summary_line(rs))
        if auto_log:
            print(f"\n{len(missed)} press(es) without an AF run in the trace"
                  + (": runs " + " ".join(str(e["n"]) for e in missed) if missed else "")
                  + f"; {sum(1 for r in runs if not r.get('auto'))} run(s) outside any press, "
                  f"{sum(1 for r in runs if r.get('extra'))} extra")


def summary_line(runs):
    import statistics
    ok = [r for r in runs if r["outcome"] in ("lock", "done") and not r.get("wrong")]
    wrong = [r for r in runs if r.get("wrong")]
    scales = [max(r["scale"]) for r in runs if r["scale"]]
    near = [r for r in ok if r["final"] is not None and r["final"] > -16000]
    times = [r["ms"] for r in runs if r["ms"] is not None]
    ys = [statistics.median(r["y"]) for r in runs if r["y"]]
    return (f"{len(runs)} runs: {len(ok)} locked ({len(near)} of them at the near stop, final > -16000), "
            f"{len(wrong)} wrong, "
            f"{sum(r['outcome'] == 'fail' for r in runs)} failed, {sum(r['outcome'] == 'abort' for r in runs)} aborted"
            + (f"; time median {statistics.median(times):.0f} ms" if times else "")
            + (f"; Y median {statistics.median(ys):.0f}" if ys else "")
            + (f"; scale max median {statistics.median(scales):.2f}" if scales else "")
            + "; fps " + "/".join(f"{AF_FPS[l]}:{sum(r['level'] == l for r in runs)}"
                                  for l in sorted({r['level'] for r in runs if r['level'] is not None})))


def trace_runs(path):
    """Summarise a TRACE.DAT per AF run (action 2 ... back to 1): duration,
    action codes, accepted flag, and per pass (path 0/1/2) the area-0
    detector's rising/falling counts as they change."""
    runs, cur, t0 = [], None, None
    for t, w in trace_records(path):
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
        # A/B mode (AB.ON holds a patch name, e.g. SCALE75): count power-ons,
        # save the previous power-on's trace to S<n-1>/, and apply the patch on
        # odd power-ons only. AB.LOG says which power-on had it. (The camera's
        # CSVs from an earlier power-on read back empty from Linux, so they
        # are not copied; they stay in the card root.)
        "if [ -f $d/AB.ON ]; then\n"
        " n=$(cat $d/BOOT.N 2>/dev/null); n=$((${n:-0} + 1)); echo $n >$d/BOOT.N\n"
        " s=$d/S$((n - 1)); mkdir -p $s\n"
        " for f in $d/TRACE.DAT $d/TRACE.ERR; do [ -f \"$f\" ] && cat \"$f\" >\"$s/${f##*/}\"; done\n"
        " p=$(cat $d/AB.ON)\n"
        " if [ $((n % 2)) = 1 ]; then $d/$p.BIN; echo \"$n $p on $?\" >>$d/AB.LOG; else echo \"$n $p off\" >>$d/AB.LOG; fi\n"
        "fi\n"
        'if [ -f $d/AFCFAST.ON ]; then $d/AFCFAST.BIN; echo "on $?" >$d/PATCH.ERR; else echo off >$d/PATCH.ERR; fi\n'
        'if [ -f $d/FORCE100.ON ]; then $d/FORCE100.BIN; echo "on $?" >$d/FORCE.ERR; else echo off >$d/FORCE.ERR; fi\n'
        'if [ -f $d/SQRTNS.ON ]; then $d/SQRTNS.BIN; echo "on $?" >$d/SQRTNS.ERR; else echo off >$d/SQRTNS.ERR; fi\n'
        'if [ -f $d/SCALE75.ON ]; then $d/SCALE75.BIN; echo "on $?" >$d/SCALE75.ERR; else echo off >$d/SCALE75.ERR; fi\n'
        'if [ -f $d/FORCE49.ON ]; then $d/FORCE49.BIN; echo "on $?" >$d/FORCE49.ERR; else echo off >$d/FORCE49.ERR; fi\n'
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


def card_roots(flags, auto=None):
    roots = {
        "enable": {MODESET_FILE: SCRIPTS_ON, "DEVELOP.MOD": DEVELOP_MOD},
        "log": {
            "SCRIPT/STARTUP.TTL": startup_ttl(flags, auto),
            "SCRIPT/AFDBG/MARK.SH": mark_sh(flags),
            "SCRIPT/AFDBG/MEM.SH": mem_sh(),
            "SCRIPT/AFDBG/MEMRD.BIN": memrd_bin(),
            "SCRIPT/AFDBG/PTRRD.BIN": ptrrd_bin(),
            "SCRIPT/AFDBG/PATCH.SH": patch_sh(),
            "SCRIPT/AFDBG/AFCFAST.BIN": afcfast_bin(),
            "SCRIPT/AFDBG/GAINCAP.BIN": gaincap_bin(),
            "SCRIPT/AFDBG/FORCE100.BIN": multipatch_bin(FORCE100_SITES),
            "SCRIPT/AFDBG/SQRTNS.BIN": multipatch_bin(SQRTNS_SITES),
            "SCRIPT/AFDBG/SCALE75.BIN": multipatch_bin(SCALE75_SITES),
            "SCRIPT/AFDBG/FORCE49.BIN": multipatch_bin(FORCE49_SITES),
            "SCRIPT/AFDBG/X875.BIN": multipatch_bin(X875_SITES),
            "SCRIPT/AFDBG/HALFB.BIN": multipatch_bin(HALFB_SITES),
            "SCRIPT/AFDBG/FALLS2.BIN": multipatch_bin(FALLS2_SITES),
            "SCRIPT/AFDBG/RISE10.BIN": multipatch_bin(RISE10_SITES),
            "SCRIPT/AFDBG/RISE05.BIN": multipatch_bin(RISE05_SITES),
            "SCRIPT/AFDBG/PEEK.BIN": peek_bin(),
            "SCRIPT/AFDBG/FORCEM0.BIN": multipatch_bin(FORCEM0_SITES),
            "SCRIPT/AFDBG/FORCEG0.BIN": multipatch_bin(FORCEG0_SITES),
            "SCRIPT/AFDBG/NO29.BIN": multipatch_bin(NO29_SITES),
            "SCRIPT/AFDBG/OFFS4.BIN": multipatch_bin(OFFS4_SITES),
            "SCRIPT/AFDBG/OFFS2.BIN": multipatch_bin(OFFS2_SITES),
            "SCRIPT/AFDBG/NOFB.BIN": multipatch_bin(NOFB_SITES),
            "SCRIPT/AFDBG/NOFO.BIN": multipatch_bin(NOFO_SITES),
            "SCRIPT/AFDBG/TRACE.BIN": trace_bin(),
        },
        "disable": {MODESET_FILE: SCRIPTS_OFF, "DEVELOP.MOD": DEVELOP_MOD},
    }
    if auto:
        # The run outcomes come from the tracer, so it is switched on and made
        # long enough for the whole session.
        log = roots["log"]
        log["SCRIPT/AFDBG/TRACE.BIN"] = trace_bin(auto_trace_records(auto))
        log["SCRIPT/AFDBG/TRACE.ON"] = b"on\n"
        log["SCRIPT/AFDBG/ARM.SH"] = arm_sh(auto)
        # New session at every power-on: ARM.SH counts runs from 1 again. Reset
        # from Linux (overwrite, no delete): Linux cannot recreate a file the
        # RTOS deleted, so the script must not delete RUN.N.
        log["SCRIPT/AFDBG/PATCH.SH"] = patch_sh().replace(
            b"exec 2>>$d/MEM.LOG\n", b"exec 2>>$d/MEM.LOG\necho 0 >$d/RUN.N\n", 1)
        log["SCRIPT/AFDBG/AUTO.SEQ"] = ("\n".join(auto_seq(auto)) + "\n").encode("ascii")
        if auto.get("steps"):
            for label in AUTO_STEP_LABELS:
                log[f"SCRIPT/AFDBG/STEP_{label}.SH"] = step_sh(label)
        if auto.get("probe_sh"):
            log["SCRIPT/AFDBG/PROBE.SH"] = probe_sh()
        if auto.get("remote"):
            log.update(remote_files())
    return roots


# Engineering-shell commands the relay passes on (names from the shell's text
# table in the 1.60 image; usage strings read from each handler). Arguments are
# limited to [A-Za-z0-9 ._-].
SHELL_ALLOW = (
    "ND_open", "ND_close",                     # ND filter in / out ("ND-OPEN.", "ND-CLOSE.")
    "SW_md",                                   # mode dial 0 P, 1 Av, 2 Tv, 3 M, 4-6 U3-U1
    "SW_cd",                                   # control dial, -4..4 clicks
    "SW_sws", "SW_swr",                        # half / full press 0|1
    "camctl_set_expmode",                      # p / tv / av / m
    "camctl_set_aperture", "camctl_set_iso", "camctl_set_shutter",   # APEX values
    "camctl_set_exp_comp", "camctl_set_iso_auto",
    "camctl_set_still_focus_mode", "camctl_set_still_metering_mode",
    "camctl_af",                               # AF with setting, ctrl, pos x, y
    "AF_hp", "AF_drive",                       # focus motor: home, dir + pulses
    "Af_Led_On", "Af_Led_Off",
)


def remote_files():
    """REMOTE.SH (start / check / fg the endpoint) and the CGI scripts under
    /tmp/afwww/cgi-bin, served by busybox httpd on REMOTE_PORT:
      uptime        /proc/uptime (the trace's clock)
      arm?A+B       restore every patch (card and uploaded), apply the arm's
                    patches (an uploaded one wins over the card's); logs to REMOTE.LOG
      tail?N        the last N trace records (raw TRACE.DAT bytes)
      ls            the AFDBG folder and the card root
      file?NAME     a file from AFDBG or the card root ([A-Za-z0-9._] names)
      put?NAME[+tool]  POST body -> RAM (/tmp/afwww/up/patch or tool/NAME.BIN,
                    at most 256 KB); a patch is usable in arm, a tool in run
      rm?NAME       restore and delete an uploaded patch, delete an uploaded tool
      run?NAME+ARGS an uploaded tool with ARGS (split on +), 30 s limit;
                    "exit N", then its output
      peek?ADDR+N   N words at ADDR (hex) via PEEK.BIN, raw; header X-Exit
      status        uptime, current arm, trace size, card and memory free, uploads
      log?N         the last N lines of REMOTE.LOG
    Uploads live in RAM and are gone at power-off. Anyone on the camera's
    Wi-Fi (WPA2, the user's key) can use them, so the endpoint only runs from
    the test card. busybox applets are called as 'busybox x': several have no
    command link."""
    d = AUTO_DIR
    rec = 8 + 4 * len(TRACE_OFFSETS) + 16 * TRACE_ARRAY_WORDS
    head = "#!/bin/sh\n" f"d={d}\n" "exec 2>>$d/MEM.LOG\n"
    text = "printf 'Content-Type: text/plain\\r\\n\\r\\n'\n"
    binary = "printf 'Content-Type: application/octet-stream\\r\\n\\r\\n'\n"
    patches = " ".join(AUTO_PATCHES)
    www = "/tmp/afwww"
    cgi = {
        "uptime": head + text + "cat /proc/uptime\n",
        "arm": head + (
            'a=$QUERY_STRING\n'
            'case "$a" in ""|*[!A-Z0-9+]*) ' + text.strip() + '; echo "bad arm"; exit 0 ;; esac\n'
            f"pf() {{ if [ -f {www}/up/patch/$1.BIN ]; then echo {www}/up/patch/$1.BIN; else echo $d/$1.BIN; fi; }}\n"
            f"ups=; for f in {www}/up/patch/*.BIN; do [ -f \"$f\" ] && {{ f=${{f##*/}}; ups=\"$ups ${{f%.BIN}}\"; }}; done\n"
            "read t0 _ </proc/uptime; s=\n"
            f"for p in {patches}; do $d/$p.BIN restore; r=$?; "
            'case $r in 0|5|6) ;; *) s="${s}R$p:$r," ;; esac; done\n'
            f"for p in $ups; do {www}/up/patch/$p.BIN restore; r=$?; "
            'case $r in 0|5|6) ;; *) s="${s}R$p:$r," ;; esac; done\n'
            'if [ "$a" != STOCK ]; then\n'
            " o=$IFS; IFS=+; for p in $a; do IFS=$o; f=$(pf $p)\n"
            '  if [ -f $f ]; then $f; s="$s$?,"; else s="${s}missing:$p,"; fi\n'
            "  IFS=+; done; IFS=$o\n"
            "fi\n"
            "s=${s%,}; s=${s:--}; read t _ </proc/uptime\n"
            'echo "arm $a $s $t0 $t" >>$d/REMOTE.LOG\n'
            f'echo "$a $s $t" >{www}/ARM.NOW\n'
            + text + 'echo "$a $s $t0 $t"\n'),
        "tail": head + (
            "n=${QUERY_STRING:-750}; case $n in *[!0-9]*) n=750 ;; esac\n"
            "f=$d/TRACE.DAT; sz=$(ls -ln $f 2>/dev/null | awk '{print $5}')\n"
            f"r=$((${{sz:-0}} / {rec})); s=$((r - n)); [ $s -lt 0 ] && s=0\n"
            + binary +
            f"busybox dd if=$f bs={rec} skip=$s count=$n 2>/dev/null\n"),
        "ls": head + text + "ls -ln $d; echo; ls -ln /media/storage1\n",
        "file": head + (
            "f=$QUERY_STRING\n"
            'case "$f" in ""|.*|*[!A-Za-z0-9._]*) ' + text.strip() + '; echo "bad name"; exit 0 ;; esac\n'
            "[ -f $d/$f ] || f=../../$f\n"
            + binary + "cat $d/$f\n"),
        "put": head + (
            "q=$QUERY_STRING; n=${q%%+*}; k=patch; [ \"$q\" = \"$n+tool\" ] && k=tool\n"
            "no() { " + text.strip() + "; echo \"$*\"; exit 0; }\n"
            'case "$n" in ""|*[!A-Z0-9]*) no bad name ;; esac; [ ${#n} -le 12 ] || no name too long\n'
            '[ "$q" = "$n" ] || [ "$k" = tool ] || no bad kind\n'
            '[ "$REQUEST_METHOD" = POST ] || no POST only\n'
            "l=${CONTENT_LENGTH:-0}; case $l in *[!0-9]*) l=0 ;; esac\n"
            "[ $l -gt 0 ] && [ $l -le 262144 ] || no bad length $l\n"
            f"busybox mkdir -p {www}/up/$k; f={www}/up/$k/$n.BIN\n"
            # A new version of an uploaded patch: put the old one's words back first.
            '[ $k = patch ] && [ -f $f ] && $f restore\n'
            "busybox dd of=$f bs=1 count=$l 2>/dev/null; busybox chmod 755 $f\n"
            "z=$(ls -ln $f | awk '{print $5}'); m=$(busybox md5sum $f | awk '{print $1}')\n"
            '[ "$z" = "$l" ] || { rm -f $f; no short $z of $l; }\n'
            "read t _ </proc/uptime\n"
            'echo "put $k $n $z $m $t" >>$d/REMOTE.LOG\n'
            + text + 'echo "$k $n $z $m"\n'),
        "rm": head + (
            "n=$QUERY_STRING\n"
            'case "$n" in ""|*[!A-Z0-9]*) ' + text.strip() + '; echo "bad name"; exit 0 ;; esac\n'
            "r=-\n"
            f"if [ -f {www}/up/patch/$n.BIN ]; then {www}/up/patch/$n.BIN restore; r=$?; rm -f {www}/up/patch/$n.BIN; fi\n"
            f"rm -f {www}/up/tool/$n.BIN\n"
            f"[ \"$r\" = - ] || echo \"unknown: $n restored and removed\" >{www}/ARM.NOW\n"
            "read t _ </proc/uptime\n"
            'echo "rm $n $r $t" >>$d/REMOTE.LOG\n'
            + text + 'echo "rm $n restore $r"\n'),
        "run": head + (
            "q=$QUERY_STRING; n=${q%%+*}; a=; [ \"$q\" = \"$n\" ] || a=${q#*+}\n"
            "no() { " + text.strip() + "; echo \"$*\"; exit 0; }\n"
            'case "$n" in ""|*[!A-Z0-9]*) no bad name ;; esac\n'
            'case "$a" in *[!A-Za-z0-9+._-]*) no bad arguments ;; esac\n'
            f"f={www}/up/tool/$n.BIN; [ -f $f ] || no no uploaded tool $n\n"
            f"o={www}/run.out; read t0 _ </proc/uptime\n"
            "x=$IFS; IFS=+; set -- $a; IFS=$x\n"
            "$f \"$@\" >$o 2>&1 & k=$!\n"
            "i=0; while kill -0 $k 2>/dev/null; do i=$((i + 1)); "
            "[ $i -gt 150 ] && { kill $k; break; }; busybox usleep 200000; done\n"
            "wait $k; r=$?; read t _ </proc/uptime\n"
            'echo "run $n $a $r $t0 $t" >>$d/REMOTE.LOG\n'
            + text + 'echo "exit $r"; cat $o\n'),
        "peek": head + (
            "q=$QUERY_STRING; ad=${q%%+*}; c=${q#*+}\n"
            'case "$q" in *+*) ;; *) c=1 ;; esac\n'
            'case "$ad$c" in ""|*[!0-9A-Fa-f]*) ' + text.strip() + '; echo "bad address"; exit 0 ;; esac\n'
            f"$d/PEEK.BIN $ad $c >{www}/peek.out; r=$?\n"
            "printf 'Content-Type: application/octet-stream\\r\\nX-Exit: %d\\r\\n\\r\\n' $r\n"
            f"cat {www}/peek.out\n"),
        "status": head + text + (
            "read u _ </proc/uptime; echo \"uptime $u\"\n"
            f"echo \"arm $(cat {www}/ARM.NOW 2>/dev/null)\"\n"
            "echo \"trace_bytes $(ls -ln $d/TRACE.DAT 2>/dev/null | awk '{print $5}')\"\n"
            "busybox df -k /media/storage1 | awk 'NR > 1 {print \"card_free_kb\", $4}'\n"
            "busybox free | awk '/Mem/ {print \"mem_free_kb\", $4}'\n"
            f"echo \"relay_seq $(cat {www}/relay.seq 2>/dev/null)\"\n"
            f"echo \"relay_ack $(cat {www}/relay.ack 2>/dev/null)\"\n"
            f"echo \"relay_hb $(cat {www}/relay.hb 2>/dev/null)\"\n"
            "for k in patch tool; do for f in " f"{www}" "/up/$k/*.BIN; do [ -f \"$f\" ] || continue\n"
            " echo \"up $k ${f##*/} $(ls -ln $f | awk '{print $5}') $(busybox md5sum $f | awk '{print $1}')\"\n"
            "done; done\n"),
        "shell": head + (
            "no() { " + text.strip() + "; echo \"$*\"; exit 0; }\n"
            '[ "$REQUEST_METHOD" = POST ] || no POST only\n'
            "l=${CONTENT_LENGTH:-0}; case $l in *[!0-9]*) l=0 ;; esac\n"
            "[ $l -gt 0 ] && [ $l -le 2048 ] || no bad length $l\n"
            f"b={www}/shell.in; busybox dd of=$b bs=1 count=$l 2>/dev/null\n"
            f"q=$(cat {www}/relay.seq 2>/dev/null); q=$((${{q:-0}} + 1))\n"
            f"o={www}/relay.tmp; echo \"rseq = $q\" >$o; echo \"if rseq = rdone goto rskip$q\" >>$o\n"
            "n=0; while read -r c x || [ -n \"$c\" ]; do [ -n \"$c\" ] || continue; n=$((n + 1))\n"
            ' case " ' + " ".join(SHELL_ALLOW) + ' " in *" $c "*) ;; *) no not allowed: $c ;; esac\n'
            ' case "$x" in *[!A-Za-z0-9\\ ._-]*) no bad arguments: $x ;; esac\n'
            " echo \"sendln '$c $x'\" >>$o; echo 'mpause 300' >>$o\n"
            "done <$b\n"
            "[ $n -gt 0 ] && [ $n -le 8 ] || no 1 to 8 commands, got $n\n"
            "echo 'rdone = rseq' >>$o\n"
            f"echo \"sendln 'lcmd sh {d}/RACK.SH $q'\" >>$o; echo \":rskip$q\" >>$o\n"
            # In place, one sector: replacing the file (mv) let the RTOS keep
            # reading the released clusters (stall 5, 2026-11-19).
            f"{RELAY_PAD_SH}"
            f"echo $q >{www}/relay.seq; cat $o 1<>$d/RELAY.TTL; sync\n"
            "read t _ </proc/uptime\n"
            'echo "shell $q $(busybox tr \"\\n\" \";\" <$b) $t" >>$d/REMOTE.LOG\n'
            + text + 'echo "queued $q"\n'),
        "log": head + (
            "n=${QUERY_STRING:-20}; case $n in *[!0-9]*) n=20 ;; esac\n"
            + text + "busybox tail -n $n $d/REMOTE.LOG\n"),
    }
    # REMOTE.SH writes the CGI scripts to the RAM disk from here-documents: the
    # camera's Linux side sees card names in capitals and httpd only runs scripts
    # under a lowercase /cgi-bin/; copying them off the card matched nothing
    # (remote-try2, 2026-11-19). diag.txt and selftest.txt are served as plain
    # files, so a broken endpoint can still be read over Wi-Fi.
    here = "".join(f"cat >{www}/cgi-bin/{k} <<'END_{k.upper()}'\n{v}END_{k.upper()}\n" for k, v in cgi.items())
    files = {}
    files["SCRIPT/AFDBG/RACK.SH"] = (
        "read t _ </proc/uptime\n"
        f"if [ \"$1\" = hb ]; then echo \"$t\" >{www}/relay.hb; exit 0; fi\n"
        f"echo \"$1 $t\" >{www}/relay.ack\n"
        f"echo \"ack $1 $t\" >>{d}/REMOTE.LOG\n").encode()
    files["SCRIPT/AFDBG/REMOTE.SH"] = (
        f"d={d}\n"
        "exec 2>>$d/MEM.LOG\n"
        "read u _ </proc/uptime\n"
        "setup() {\n"
        f"busybox mkdir -p {www}/cgi-bin\n"
        # RELAY.TTL is created once and then only overwritten in place.
        f"o={www}/relay.idle; echo 'rseq = 0' >$o\n"
        f"{RELAY_PAD_SH}"
        f"cat $o 1<>$d/RELAY.TTL; sync\n"
        + here +
        f"busybox chmod 755 {www}/cgi-bin/*\n"
        f"echo 'AF remote endpoint: /cgi-bin/uptime arm tail ls file put rm run peek status log shell; /diag.txt /selftest.txt' >{www}/index.html\n"
        f"{{ date; ls -lR {www}; echo; ls -l $d $d/WWW; echo; ps; }} >{www}/diag.txt 2>&1\n"
        f"{{ for c in uptime ls status; do echo \"== $c\"; {www}/cgi-bin/$c; done; "
        f"QUERY_STRING=5 {www}/cgi-bin/tail | busybox wc -c; "
        f"echo '== peek identity'; QUERY_STRING=53002038+1 {www}/cgi-bin/peek | busybox hexdump -C; }} >{www}/selftest.txt 2>&1\n"
        f"cat {www}/diag.txt {www}/selftest.txt >>$d/REMOTE.LOG\n"
        "}\n"
        "case $1 in\n"
        f"start) rm -f $d/HTTPD.DEAD; setup; busybox httpd -p {REMOTE_PORT} -h {www}; echo \"$u start $?\" >>$d/REMOTE.LOG ;;\n"
        # By port, not by name: the daemon may run as "busybox".
        f"check) if netstat -ltn | grep -q ':{REMOTE_PORT} '; then echo \"$u up\" >>$d/REMOTE.LOG; "
        "else echo dead >$d/HTTPD.DEAD; echo \"$u dead\" >>$d/REMOTE.LOG; fi\n"
        "  netstat -ltn >>$d/REMOTE.LOG ;;\n"
        f"fg) echo \"$u fg\" >>$d/REMOTE.LOG; sync; busybox httpd -f -p {REMOTE_PORT} -h {www} ;;\n"
        "esac\n"
        "sync\n"
    ).encode("ascii")
    return files


# The shell relay's RELAY.TTL has a fixed size and is written in place with
# "cat $o 1<>file" (read-write, no truncation): the camera's BusyBox dd has
# no conv=notrunc, and "mv" over the file failed with EIO on 2026-11-19 at
# the relay stalls (MEM.LOG).
# RELAY_PAD_SH pads the file $o to RELAY_SIZE bytes with assignment lines
# (the TTL interpreter is only known to accept statements, not blank lines or
# comments): "rpad = 0" lines, then one "rq...q = 0" line of the exact rest.
RELAY_SIZE = 512
RELAY_PAD_SH = (
    "l=$(busybox wc -c <$o); [ $l -le $((RELAY_SIZE - 7)) ] || no relay file too long: $l\n"
    "while [ $((RELAY_SIZE - l)) -gt 16 ]; do echo 'rpad = 0' >>$o; l=$((l + 9)); done\n"
    "v=r; k=$((RELAY_SIZE - l - 6)); while [ $k -gt 0 ]; do v=${v}q; k=$((k - 1)); done; echo \"$v = 0\" >>$o\n"
).replace("RELAY_SIZE", str(RELAY_SIZE))


PROBE_STAGES = ((1, 6000), (2, 6000), (3, 16000), (4, 16000))   # stage, TTL wait (ms)


def probe_sh():
    """PROBE.SH <stage>: what the camera's Linux side offers for a link to
    the Mac. Run as short separate lcmd calls (the 2026-11-19 version that
    forked itself into the background and wrote into a new folder left no
    trace at all). Every stage first appends a line to PROBE.LOG, as STEP.SH
    does, and writes P<boot>_<stage>.TXT into the existing AFDBG folder.
    1 inventory, 2 gadget functions (in a throw-away gadget that is never
    bound to the USB controller), 3 which port answers HTTP, 4 the phone-app
    API on localhost: props and two AF calls."""
    return (
        f"d={AUTO_DIR}\n"
        "read u _ </proc/uptime; echo \"$u stage $1 start\" >>$d/PROBE.LOG; sync\n"
        'if [ "$1" = 1 ]; then b=$(cat $d/PBOOT.N 2>/dev/null); echo $((${b:-0} + 1)) >$d/PBOOT.N; fi\n'
        "b=$(cat $d/PBOOT.N 2>/dev/null); o=$d/P${b:-0}_$1.TXT\n"
        "t() { read u _ </proc/uptime; echo \"== $u $*\"; }\n"
        "req() {  # port method path body\n"
        ' { printf "%s %s HTTP/1.0\\r\\nHost: 127.0.0.1\\r\\nContent-Type: application/x-www-form-urlencoded\\r\\n'
        'Content-Length: %d\\r\\n\\r\\n%s" "$2" "$3" ${#4} "$4"; sleep 3; } | busybox nc 127.0.0.1 $1 &\n'
        " k=$!; sleep 4; kill $k 2>/dev/null; echo\n"   # not $p: the caller loops over p
        "}\n"
        "{\n"
        "t stage $1; cat /tmp/startup_mode; cat /sys/class/udc/*/state\n"
        "case $1 in\n"
        "1)\n"
        " for c in 'uname -a' 'cat /proc/cmdline' 'ps' 'lsmod' 'netstat -ltnu' 'ifconfig -a' 'ls /dev' "
        "'cat /proc/filesystems' 'ls -R /lib/modules' 'busybox' 'ls -l /sys/class/udc' "
        "'cat /sys/class/udc/*/current_speed' 'mount' 'ls -lR /sys/kernel/config/usb_gadget' "
        "'cat /sys/kernel/config/usb_gadget/*/UDC'; do t \"$c\"; sh -c \"$c\"; done\n"
        # nc, sort and tr are busybox applets without a command link on the
        # camera (2026-11-19): list every applet that has none.
        " t unlinked applets; for a in $(busybox --list); do busybox which $a >/dev/null || printf '%s ' $a; done; echo\n"
        " t config.gz; [ -f /proc/config.gz ] && zcat /proc/config.gz | grep -E 'USB|GADGET|CONFIGFS|_FS|NET|ETHER|IKCONFIG' | grep -v '^#'\n"
        " ;;\n"
        "2)\n"
        # 2026-11-19: a second gadget fails (ENOMEM) and the kernel has only the
        # MTP function (CONFIG_USB_F_MTP). Keep the whole config instead, for
        # the own-kernel-module route.
        " zcat /proc/config.gz >$d/P${b:-0}_CFG.TXT\n"
        " grep -E 'MODULE|KALLSYMS|DEVMEM|USB_F_|USB_CONFIGFS|FUNCTIONFS' $d/P${b:-0}_CFG.TXT\n"
        " cat /proc/sys/kernel/modules_disabled /proc/sys/kernel/kptr_restrict\n"
        " ;;\n"
        "3)\n"
        " rm -f $d/PAPI.N\n"
        # sort, tr and head -3 are not linked or not supported on the camera: awk only.
        " ports=$(netstat -ltn 2>/dev/null | awk 'NR>2 {sub(/.*:/, \"\", $4); print $4}')\n"
        ' echo "ports: $ports"\n'
        " for p in $(echo 80 $ports | awk '{for (i = 1; i <= NF; i++) if (!s[$i]++ && ++n <= 3) print $i}'); do\n"
        '  t "GET /v1/ping :$p"; r=$(req $p GET /v1/ping \'\'); echo "$r"\n'
        '  case "$r" in HTTP*) echo $p >$d/PAPI.N; break ;; esac\n'
        " done\n"
        " ;;\n"
        "4)\n"
        " api=$(cat $d/PAPI.N 2>/dev/null); echo \"api port: ${api:-none}\"\n"
        ' if [ -n "$api" ]; then\n'
        "  t GET /v1/props; req $api GET /v1/props ''\n"
        "  t POST /v1/lens/focus; req $api POST /v1/lens/focus ''\n"
        "  t POST /v1/lens/focus pos; req $api POST /v1/lens/focus 'pos=50,50'\n"
        "  t done\n"
        " fi\n"
        " ;;\n"
        "esac\n"
        "t end\n"
        "} >$o 2>&1\n"
        "read u _ </proc/uptime; echo \"$u stage $1 end\" >>$d/PROBE.LOG; sync\n"
    ).encode("ascii")


def auto_trace_records(auto):
    return int(auto_session_s(auto) * 1e9 / TRACE_PERIOD_NS)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path("work/af-debug-card"))
    parser.add_argument("--flags", default="C", help="subset of PCZO (default C: contrast AF CSV only)")
    parser.add_argument("--read-mem", metavar="MEM.BIN", help="decode a probe dump instead of writing files")
    parser.add_argument("--read-tab", metavar="TAB.BIN", help="decode a pointer dump instead of writing files")
    parser.add_argument("--read-trace", metavar="TRACE.DAT", help="decode an AF state trace instead of writing files")
    parser.add_argument("--trace-runs", metavar="TRACE.DAT", help="summarise an AF state trace per AF run")
    parser.add_argument("--trace-summary", metavar="TRACE.DAT", help="one line per AF run with outcome and light level")
    parser.add_argument("--trace-passes", metavar="TRACE.DAT", help="print the detector arrays at the end of each pass")
    parser.add_argument("--pass-table", metavar="TRACE.DAT", help="one line per pass: rise against the floor + T, accepted or not")
    parser.add_argument("--rise-floor", type=float, default=RISE_FLOOR, help="floor for --pass-table (stock 0.02; RISE10 0.01, RISE05 0.005)")
    parser.add_argument("--trace-contrast", metavar="TRACE.DAT", help="print the per-frame contrast of an AF state trace")
    parser.add_argument("--auto-log", metavar="AUTO.LOG", help="with --trace-summary: arm and light block per run, totals per block and arm")
    parser.add_argument("--wrong-pulses", type=int, default=150,
                        help="with --trace-summary: a lock this far from the median lock position is wrong (default %(default)s)")
    parser.add_argument("--join-window", type=float, default=4.0,
                        help="with --auto-log: longest time from the arm log entry to the AF start (default %(default)s s)")
    auto = parser.add_argument_group("automated runs (the script presses the shutter; see docs/af-debug-log.html)")
    auto.add_argument("--auto", action="store_true", help="light blocks of automated half-presses")
    auto.add_argument("--auto-smoke", action="store_true", help="smoke check: 5 automated half-presses, no patch")
    auto.add_argument("--remote", action="store_true",
                      help="remote mode: no scripted presses; after CAF.OK a web endpoint on port "
                           f"{REMOTE_PORT} for tools/af_remote.py (camera Wi-Fi), trace on")
    auto.add_argument("--remote-min", type=int, default=45, metavar="MIN",
                      help="with --remote: trace length in minutes (default %(default)s)")
    auto.add_argument("--probe", action="store_true",
                      help="USB/HTTP probe: PROBE.SH lists what Linux offers (gadget functions, ports, "
                           "localhost API with two AF calls), then 3 automated presses; no patch")
    auto.add_argument("--auto-arms", metavar="A,B,...",
                      help="arms to alternate per run: STOCK or patch names joined by '+', e.g. "
                           "STOCK,SCALE75,X875,HALFB,FALLS2,SCALE75+FALLS2 (patches: "
                           + ", ".join(AUTO_PATCHES) + ")")
    auto.add_argument("--auto-patch", metavar="NAME", choices=tuple(AUTO_PATCHES),
                      help="A/B per run: A = NAME.BIN applied, B = stock (default: no patch)")
    auto.add_argument("--auto-mode", choices=AUTO_TARGET_MODES, default=AUTO_DEFAULTS["mode"],
                      help="focus mode of the runs, restored after each defocus (default %(default)s; set the camera to it)")
    auto.add_argument("--auto-defocus", choices=AUTO_DEFOCUS_MODES, default=AUTO_DEFAULTS["defocus"],
                      help="focus mode that parks the lens before each run (default %(default)s)")
    auto.add_argument("--auto-trigger", choices=AUTO_TRIGGERS, default=AUTO_DEFAULTS["trigger"],
                      help="sw = SW_sws 1/0, the half-press (default); camctl_af = camctl_af cam start/stop")
    auto.add_argument("--blocks", type=int, help=f"light blocks (default {AUTO_DEFAULTS['blocks']})")
    auto.add_argument("--per-block", type=int, help=f"runs per block (default {AUTO_DEFAULTS['per_block']})")
    auto.add_argument("--light-pause-s", type=int, default=AUTO_DEFAULTS["pause_s"],
                      help="lamp-blinking pause between blocks (default %(default)s s)")
    auto.add_argument("--hold-ms", type=int, default=AUTO_DEFAULTS["hold_ms"], help="half-press time (default %(default)s)")
    auto.add_argument("--gap-ms", type=int, default=AUTO_DEFAULTS["gap_ms"], help="wait after release (default %(default)s)")
    auto.add_argument("--full-press", action="store_true", help="take a photo at the end of each half-press")
    auto.add_argument("--seed", type=int, default=AUTO_DEFAULTS["seed"], help="A/B order seed (default %(default)s)")
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
    if args.trace_summary:
        trace_summary(args.trace_summary, args.auto_log, args.join_window, args.wrong_pulses)
        return
    if args.pass_table:
        pass_table(args.pass_table, args.rise_floor)
        return
    if args.trace_passes:
        trace_passes(args.trace_passes)
        return
    if args.trace_contrast:
        trace_contrast(args.trace_contrast)
        return
    flags = args.flags.upper()
    if not flags or any(c not in FLAGS for c in flags) or len(set(flags)) != len(flags):
        sys.exit(f"--flags must be letters from {FLAGS}, each at most once")
    run = None
    if args.auto + args.auto_smoke + args.probe + args.remote > 1:
        sys.exit("--auto, --auto-smoke, --probe and --remote exclude each other")
    if args.auto or args.auto_smoke or args.probe or args.remote:
        run = dict(AUTO_SMOKE if args.auto_smoke else AUTO_PROBE if args.probe
                   else dict(AUTO_REMOTE, remote_min=args.remote_min) if args.remote else AUTO_DEFAULTS)
        run.update(mode=args.auto_mode, defocus=args.auto_defocus, trigger=args.auto_trigger,
                   pause_s=args.light_pause_s,
                   hold_ms=args.hold_ms, gap_ms=args.gap_ms, full=args.full_press,
                   patch=None if args.auto_smoke or args.probe or args.remote else args.auto_patch, seed=args.seed,
                   arms=None if args.auto_smoke or args.probe or args.remote or not args.auto_arms
                   else args.auto_arms.split(","))
        if run["arms"] and run["patch"]:
            sys.exit("--auto-arms and --auto-patch exclude each other")
        for arm in auto_arm_list(run):
            parts = [] if arm in ("STOCK", "-") else arm.split("+")
            bad = [p for p in parts if p not in AUTO_PATCHES]
            if bad:
                sys.exit(f"unknown patch in arm {arm}: {', '.join(bad)}")
            group = arm_conflict(parts)
            if group:
                sys.exit(f"arm {arm}: {', '.join(group)} share the same words, use one at a time")
        if len(set(auto_arm_list(run))) != len(auto_arm_list(run)):
            sys.exit("an arm is listed twice")
        if args.blocks is not None:
            run["blocks"] = args.blocks
        if args.per_block is not None:
            run["per_block"] = args.per_block
        if run["blocks"] < 1 or run["per_block"] < 1:
            sys.exit("--blocks and --per-block must be at least 1")
        if run["full"] and run["trigger"] != "sw":
            sys.exit("--full-press needs --auto-trigger sw")
        k = len(auto_arm_list(run))
        if run["per_block"] % k:
            sys.exit(f"--per-block must be a multiple of the number of arms ({k})")
    elif args.auto_patch or args.auto_arms or args.full_press:
        sys.exit("--auto-patch, --auto-arms and --full-press need --auto")
    if args.out.exists():
        sys.exit(f"{args.out} exists; remove it or pick another --out (this tool never overwrites)")
    for root, files in card_roots(flags, run).items():
        for name, data in files.items():
            path = args.out / root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path} ({len(data)} bytes)")
    if run:
        rec = 8 + 4 * len(TRACE_OFFSETS) + 16 * TRACE_ARRAY_WORDS
        n = run["blocks"] * run["per_block"]
        seq = auto_seq(run)
        print(f"\nautomated: {run['blocks']} block(s) x {run['per_block']} runs = {n}"
              + (" (" + ", ".join(f"{seq.count(a)} {a}" for a in auto_arm_list(run)) + ")"
                 if auto_arm_list(run) != ["-"] else ", no patch")
              + f", trigger {run['trigger']}, focus mode {run['mode']}, defocus {run['defocus']}; about {auto_session_s(run) / 60:.0f} min; "
              f"TRACE.DAT up to {auto_trace_records(run) * rec / 1e6:.0f} MB")
    print(f"\nNext: follow docs/af-debug-log.html. Copy the contents of {args.out}/enable to the card root first.")


if __name__ == "__main__":
    main()
