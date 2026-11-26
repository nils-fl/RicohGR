#!/usr/bin/env python3
"""Focus modes on the mode dial (docs/focus-bracketing.html §8): a watcher on the recipe card that runs
at every power-on and turns two U modes into a macro stack and a snap meter. No Mac, no phone.

    python3 tools/focus/focus_watch.py card [--macro U1] [--meter U2] [--out work/focus-watch]
        writes the card root (copy SCRIPT/ onto the card, keep its other files); "none" leaves a mode out
    python3 tools/focus/focus_watch.py log SCRIPT/RECIPE/RECIPE.LOG [--all]
        the watcher's lines of the last start, the tracer's records ("tr") decoded, by time

Macro stack (save the U mode with AF-S, Select or Pinpoint, Av at the aperture you want; tripod):
  1. Focus on the nearest point and take a photo. Wait for one blink.
  2. Focus on the farthest point and take a photo. Two blinks; the lamp comes on.
  3. The camera shoots the frames between them, spaced in dioptres for the aperture (at least 5, at most
     35), then puts focus mode and snap distance back; the lamp stays on for 3 s.
  No second photo within 60 s: the near point is forgotten (one long blink). Turning the dial away stops
  the bracket after the current round of up to seven frames.
Snap meter (save the U mode with AF-S and the Fn button on AF/Snap toggle, set while on that U mode):
  Half-press on the subject, wait for focus, release. SAMP.BIN watches the lens every 5 ms; once it has
  moved and stood still for STABLE_MS, the camera switches to Snap at the subject's distance, or the
  hyperfocal distance when the subject is beyond it: the snap setting with a four-character label nearest
  to it, whose distance and label are rewritten in RAM to the exact one, set the way Fn (AF/Snap toggle)
  does it (snap cm, StillBackUpFocus = the AF mode, then StillFocus Snap). Each batch is read back in RAM;
  what is missing goes again (three batches at most), and Snap the camera takes back by itself (back-up
  not 13, as Fn leaves it) is sent again. Shoot in Snap. To measure again press Fn (back to AF) and
  half-press on the new subject. A new aperture recomputes it while in Snap. The tracer (SAMP.BIN t)
  logs the lens, focus words and half-press switch as they change, for the camera runs.
  With the camera's copyright empty, Snap photos carry GRSNAP:<snap mm>:<subject mm> in EXIF Copyright
  (cleared on Fn back to AF and on leaving the mode).

What it reads (Read, GR IIIx 1.60; probe 3 run 2026-11-25): the U mode is /v1/props exposureMode (U1,
U2, U3 seen); the aperture /v1/props av; the lens distance in mm [[[[0x553458b0] + 0x518]] + 0x698],
which matched Snap within 1 % (995 / 2525 mm at 100 / 250 cm); the focus mode byte at settings +0x11c
(codes from the StillFocus matcher 0x532ddd7c). Focus goes through the ApplySnapDistance slots as in
focus_bracket.py: the shell writes the millimetres into multipatch templates T1..T7.BIN (placeholder
words at known offsets) and reads each word back before running it; the multipatch binary then checks
the build identity and the stock words as always. CommonFocusPosMm + Mf does not move the lens (probe 3).
Tag (only if the stored copyright reads empty; its text is never logged): EXIF Copyright
GRFS:<far photo number>:<frame>/<frames>:<mm> on the bracket frames, through the patched SetupCopyright
handler of focus_probe3.py; afterwards empty again and attach as before.
The watcher never writes the card: its log lines and the tag marker WATCH/TAG go through the recipe
card's PULL.SH. A bracket cut off by a power-off leaves the tag in the camera; the next start clears it.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
import af_debug_card as card  # noqa: E402
import focus_bracket as fb  # noqa: E402
import focus_probe as fp  # noqa: E402
import focus_probe3 as p3  # noqa: E402
import recipe_card as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WATCH_DIR = "SCRIPT/RECIPE/WATCH"
RAM = f"{rc.WWW}/watch"
U_MODES = ("U1", "U2", "U3", "U4", "U5", "none")
FOCUS_NAMES = {0: "AutoArea", 1: "Select", 4: "Pinpoint", 5: "Follow", 6: "Continuous", 9: "Mf", 10: "Snap",
               11: "Infinity", 14: "AutoAreaM", 15: "ZoneSelect"}          # StillFocus matcher 0x532ddd7c
SLOT_CM = [cm for cm, _ in fb.SLOTS]
MIN_FRAMES, MACRO_TIMEOUT_S = 5, 60
# The snap meter waits in SAMP.BIN (af_debug_card.SAMP_S, a sample every 5 ms): the subject is the lens once it
# has moved and stood still STABLE_MS; WAIT_MS per round when nothing happens.
STABLE_MS, WAIT_MS = 250, 1000
MSET_MS, MSNAP_MS = 150, 300   # relay pauses after the plain stores and after StillFocus Snap (read back anyway)
SNAP_LOST_S = 5    # Snap gone within this long after we set it, back-up not 13 (not Fn): the camera's doing, again
TRACE_MS, TRACE_RECORDS, TRACE_MAX = 120000, 200, 6   # the tracer for the camera runs: per tracer, per visit
PLACEHOLDER = 0x5A5A0000
OFF_CUR = 0xC6     # focus mode in use (setter 0x532b03b4; read by 0x5326c5f8 for ApplySnapDistance)
OFF_FN = 0x922     # CustomSwitchFn byte (setter 0x532435f8; "ToggleAfSnap" stores 3, handler 0x5332e6ec)
# The snap label on screen is text: label id (cm -> id 0x53291ec4) -> [[LANG_TAB + 4 * index] + 4 * id] =
# entry {u16 length with NUL, u16 1, ptr} -> UTF-16 string (draw 0x533970a4, size 0x5339666c). For these
# ids the widget forces language 0x14 (0x53396f80 -> 0x53396f4c), table index 0 (0x533963f8), whatever
# the language setting (camera run 12: the user's language table was patched, the screen kept the stock
# text). The four-character labels sit in 12 bytes: room for five characters ("0.48m", "12.2m") with the
# length 5 -> 6 (5-character strings have 6, e.g. id 0x10). Same words in every table checked.
LANG_TAB = 0x55002BAC
# The settings with four-character labels, and their label ids with the stock words of the text. 30 cm can
# show as 0.4 m (0x532af020): both. Camera run 13: "1.60m" read as "1.6", so at most four characters, the
# stock width: 48cm, 1.6m, 12m (the length word then 4).
LABEL_IDS = {30: (0x3E, 0x3F), 150: (0x41,), 250: (0x43,), 350: (0x44,)}
LABEL_STOCK = {0x3E: (0x002E0030, 0x006D0033), 0x3F: (0x002E0030, 0x006D0034), 0x41: (0x002E0031, 0x006D0035),
               0x43: (0x002E0032, 0x006D0035), 0x44: (0x002E0033, 0x006D0035)}
LABEL_LEN = 0x105
# The shutter half-press (S1): the SwitchMonitor at [S1_ROOT] (factory 0x5536c604, built by 0x536a4040)
# keeps the S1 CpuSwitch at +S1_OFF (0x536a4574); its word +8 is 1 while half-pressed (scan 0x536a5c38,
# active-low line). Checked before use: class pointer, event id 0x21600201, key code 0. Read once on
# entering the meter mode (held: the settled lens is a subject) and by the tracer.
S1_ROOT, S1_OFF, S1_CLASS, S1_EVENT = 0x5536C608, 0x5C, 0x53D5B9C0, 0x21600201
S2_OFF = 0x60      # the S2 (full press) CpuSwitch, event 0x2160021c, key code 1 (0x536a459c)
SUB_OFF = 0xD0     # the sub-CPU switch snapshot in the monitor (word +4, byte +8; copied by 0x536a5100)
# Camera run 16: S1 read 0 in almost every one-per-second read, so the meter does not wait on it; the tracer
# records it at 200 Hz for the next decision.
CAF_ACTION = 0xC44C  # the contrast AF object's action word (tracer only)
REDRAW_CM, REDRAW_ALT = 500, 350   # sent before the setting when it is already in use: the widget skips an unchanged id


def templates():
    """{k: (ELF bytes, [(offset, cond, slot index)])}: multipatch for the first k slots, new words left as
    placeholders for the shell to fill in; {"S<j>": ...} the same for slot j alone (its words take mm 1).
    Card names stay upper case: the camera's Linux sees the card that way (Ts0.BIN was not found)."""
    out = {}
    for k in range(1, len(fb.SLOTS) + 1):
        sites, table = [], []
        for j, (_, slot_sites) in enumerate(fb.SLOTS[:k]):
            for a, stock, cond in slot_sites:
                ph = PLACEHOLDER | len(sites)
                sites.append((a, stock, ph))
                table.append((ph, cond, j))
        elf = card.multipatch_bin(sites)
        rows = []
        for ph, cond, j in table:
            b = ph.to_bytes(4, "little")
            if elf.count(b) != 1:
                raise ValueError(f"placeholder {ph:#x} found {elf.count(b)} times")
            rows.append((elf.index(b), cond, j))
        out[k] = (elf, rows)
    for j, (_, slot_sites) in enumerate(fb.SLOTS):           # "S<j>": slot j alone, for the snap meter
        sites, table = [], []
        for a, stock, cond in slot_sites:
            ph = PLACEHOLDER | len(sites)
            sites.append((a, stock, ph))
            table.append((ph, cond, 0))
        elf = card.multipatch_bin(sites)
        out[f"S{j}"] = (elf, [(elf.index(ph.to_bytes(4, "little")), cond, 0) for ph, cond, _ in table])
    return out


def label_templates():
    """{n: (ELF, offset of the first site)}: a multipatch of n sites, every address, stock and new word a
    placeholder; the sites follow each other (address, stock, new), 12 bytes apart."""
    out = {}
    for n in range(1, 7):
        sites = [(PLACEHOLDER | (3 * i), PLACEHOLDER | (3 * i + 1), PLACEHOLDER | (3 * i + 2)) for i in range(n)]
        elf = card.multipatch_bin(sites)
        off = elf.index(PLACEHOLDER.to_bytes(4, "little"))
        for i, site in enumerate(sites):
            for k, ph in enumerate(site):
                if elf.count(ph.to_bytes(4, "little")) != 1 or elf.index(ph.to_bytes(4, "little")) != off + 12 * i + 4 * k:
                    raise ValueError(f"label template {n}: placeholder {ph:#x}")
        out[n] = (elf, off)
    return out


def fill(elf, rows, mms):
    """What the shell's mkround does, for the tests: the template with the movw words written in."""
    b = bytearray(elf)
    for off, cond, j in rows:
        b[off:off + 4] = fp.movw_r1(mms[j], cond).to_bytes(4, "little")
    return bytes(b)


def ttl(line, pause):
    return p3.ttl(line, pause)


SCRIPT = r"""# focus modes on the dial: generated by tools/focus/focus_watch.py
. @WWW@/SOLO.LIB
d=@D@
W=@RAM@                       # REMOTE.SH copied $d/WATCH here at boot
ZMACRO=@MACRO@; ZMETER=@METER@
pt() { "$@"; }
zsl() { busybox usleep $1 2>/dev/null || sleep 1; }
zup() { read zq0 _ </proc/uptime; echo ${zq0%.*}; }
get() { ( printf 'GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n' "$1"; sleep 2 ) | busybox nc 127.0.0.1 80 >${2:-$w/watch.txt} 2>/dev/null & zp=$!; zj=0; while kill -0 $zp 2>/dev/null && [ $zj -lt 30 ]; do zsl 100000; zj=$((zj + 1)); done; kill $zp 2>/dev/null; }
props() { busybox sed 's/[][{}]/,/g' $1 2>/dev/null | busybox awk -v RS=, '/"exposureMode"[ ]*:/ { sub(/.*:/, ""); gsub(/[^A-Za-z0-9]/, ""); m = $0 } /"av"[ ]*:/ { sub(/.*:/, ""); gsub(/[^0-9.]/, ""); a = $0 } END { print m, a }'; }
poll() { get /v1/props; set -- $(props $w/watch.txt); zu=${1:-none}; zav=$2; }
# On the meter mode /v1/props runs in the background (a request takes up to 2 s): pollbg starts one when
# none is running, pollck takes a finished one's answer (a failed one keeps the last mode).
pollck() { [ -n "$zpb" ] || return 1; kill -0 $zpb 2>/dev/null && return 1; wait $zpb 2>/dev/null; zpb=; set -- $(props $w/watch.pq); [ -n "$1" ] && { zu=$1; zav=$2; }; return 0; }
pollbg() { if [ -n "$zpb" ]; then pollck || return 0; fi; get /v1/props $w/watch.pq & zpb=$!; }
pbstop() { [ -n "$zpb" ] && { kill $zpb 2>/dev/null; wait $zpb 2>/dev/null; zpb=; }; }
stem() { get /v1/photos; busybox awk -v RS='"' '/^HTTP/ { ok = 1 } /^R[0-9]+\.(JPG|DNG)$/ { s = $0; gsub(/[^0-9]/, "", s); if (s + 0 > l) l = s + 0 } END { if (ok) print l + 0 }' $w/watch.txt 2>/dev/null; }
rd() { $W/PEEK.BIN $1 1 2>/dev/null | busybox hexdump -v -C | busybox awk 'NR == 1 { print $5 $4 $3 $2 }'; }
num() { zx=$(rd $1); [ -n "$zx" ] && echo $((0x$zx)); }
at() { num $(printf %x $(($1 + $2))); }
obj() { zo=$(num @SETTINGS@); [ -n "$zo" ]; }
lens() { za=$(num @LENSROOT@); [ -n "$za" ] || return; zb=$(at $za @LENSA@); [ -n "$zb" ] || return; zc=$(num $(printf %x $zb)); [ -n "$zc" ] || return; zm=$(at $zc @LENSMM@); [ -n "$zm" ] && [ "$zm" -lt 1000000 ] && echo $zm; }
lensr() { zj3=0; while [ $zj3 -lt 6 ]; do zl=$(lens); [ -n "$zl" ] && [ "$zl" -gt 0 ] && { echo $zl; return 0; }; zj3=$((zj3 + 1)); zsl 400000; done; return 1; }
lensdiag() { za=$(num @LENSROOT@); [ -n "$za" ] && [ "$za" != 0 ] || { echo root; return; }; zb=$(at $za @LENSA@); [ -n "$zb" ] && [ "$zb" != 0 ] || { echo a; return; }; zc=$(num $(printf %x $zb)); [ -n "$zc" ] && [ "$zc" != 0 ] || { echo b; return; }; echo mm $(at $zc @LENSMM@); }
focus() { obj && echo $(( $(at $zo @OFFFOCUS@) & 255 )); }
# The focus mode in use (+0xc6, what ApplySnapDistance tests): the AF/Snap toggle changes it, not the
# setting above. 13 means "computed": then the setting.
efocus() { obj || return; zx5=$(at $zo @OFFCURW@); [ -n "$zx5" ] || return; zx5=$(( (zx5 >> 16) & 255 )); [ $zx5 = 13 ] && { focus; return; }; echo $zx5; }
snapcm() { obj && at $zo @OFFSNAP@; }
fname() { case $1 in @FNAMES@ *) echo Select ;; esac; }
send() { printf "sendln '%s'\nmpause %s\n" "$1" "$2" | b; }
blinks() { zi2=0; while [ $zi2 -lt $1 ]; do printf "@BLINK@" | b; zi2=$((zi2 + 1)); done; }
tpl() { case $1 in @TPL@ esac; }
putw() { zb0=$(($3 & 255)); zb1=$((16 | (($3 >> 8) & 15))); zb2=$((($3 >> 12) & 15)); zb3=$(($4 * 16 + 3)); zsz0=$(busybox wc -c <$1); { busybox dd if=$1 bs=$2 count=1 2>/dev/null; printf "$(printf '\\%03o\\%03o\\%03o\\%03o' $zb0 $zb1 $zb2 $zb3)"; busybox dd if=$1 bs=$(($2 + 4)) skip=1 2>/dev/null; } >$1.t && busybox chmod 755 $1.t && busybox mv $1.t $1 || return 1; [ "$(busybox wc -c <$1)" = "$zsz0" ] && [ "$(busybox dd if=$1 bs=1 skip=$2 count=4 2>/dev/null | busybox hexdump -v -C | busybox awk 'NR == 1 { print $2 $3 $4 $5 }')" = "$(printf %02x%02x%02x%02x $zb0 $zb1 $zb2 $zb3)" ]; }
mkround() { cp $W/T$2.BIN $1 || return 1; for zm in $3; do [ $zm -ge @MINMM@ ] && [ $zm -le 65535 ] || return 1; done; tpl $2 | while read zof zcd zsi; do putw $1 $zof $(echo $3 | busybox cut -d' ' -f$((zsi + 1))) $zcd || echo bad; done | busybox grep -q bad && return 1; return 0; }
planmm() { busybox awk -v n="$1" -v f="$2" -v a="$3" 'BEGIN { if (a + 0 <= 0) a = 2.8; h = a * @COC@ / (@FOCAL@ * @FOCAL@); if (n < @NEARMM@) n = @NEARMM@; dn = 1000 / n; df = 0; if (f > 0 && f < 60000) df = 1000 / f; if (df > dn) { x = dn; dn = df; df = x } first = dn - h; last = df + h; if (last < h) last = h; k = 0; if (first > last) { s = 2 * h * @STEP@; k = int((first - last) / s - 0.000000001); if (k < (first - last) / s - 0.000000001) k = k + 1; k = k + 1 } if (k < @MINFRAMES@) { first = dn; last = df; k = @MINFRAMES@ } if (k > @MAXFRAMES@) k = @MAXFRAMES@; o = ""; for (i = 0; i < k; i++) { d = first - i * (first - last) / (k - 1); m = 65535; if (d > 0) m = int(1000 / d + 0.5); if (m > 65535) m = 65535; o = o " " m } print substr(o, 2) }'; }
meter() { busybox awk -v l="$1" -v a="$2" 'BEGIN { if (a + 0 <= 0) a = 2.8; h = a * @COC@ / (@FOCAL@ * @FOCAL@); s = l; if (1000 / l < h) s = 1000 / h; if (s < @NEARMM@) s = @NEARMM@; if (s > 65535) s = 65535; print int(s + 0.5) }'; }
nearslot() { zj4=0; zbj=; for zc4 in @SLOTS@; do ze4=$(( 1000000 / $1 - 100000 / zc4 )); [ $ze4 -lt 0 ] && ze4=$(( -ze4 )); if [ -z "$zbj" ] || [ $ze4 -lt $zbe ]; then zbj=$zj4; zbc=$zc4; zbe=$ze4; fi; zj4=$((zj4 + 1)); done; echo $zbj $zbc; }
# The exact distance as the snap label (LABEL_* above). lblent: the entry and string addresses of label id
# $1 in table index 0 (mres keeps them in zlbc, only entries whose length word is the stock 5).
# mklabel $1 setting cm $2 mm: the patch (the length and string words that change), every word checked by
# the patch tool before it writes.
lblent() { for zq6 in $zlbc; do case $zq6 in $1:*) zr6=${zq6#*:}; echo ${zr6%%:*} ${zr6#*:}; return 0 ;; esac; done; return 1; }
lbltxt() { if [ $1 -lt 995 ]; then printf '%dcm' $((($1 + 5) / 10)); elif [ $1 -lt 9950 ]; then zc7=$((($1 + 50) / 100)); printf '%d.%dm' $((zc7 / 10)) $((zc7 % 10)); else printf '%dm' $((($1 + 500) / 1000)); fi; }
u16c() { case $1 in .) echo 46 ;; m) echo 109 ;; c) echo 99 ;; *) echo $((48 + $1)) ;; esac; }
putraw() { zsz0=$(busybox wc -c <$1); { busybox dd if=$1 bs=$2 count=1 2>/dev/null; printf "$(printf '\\%03o\\%03o\\%03o\\%03o' $(($3 & 255)) $((($3 >> 8) & 255)) $((($3 >> 16) & 255)) $((($3 >> 24) & 255)))"; busybox dd if=$1 bs=$(($2 + 4)) skip=1 2>/dev/null; } >$1.t && busybox chmod 755 $1.t && busybox mv $1.t $1 || return 1; [ "$(busybox wc -c <$1)" = "$zsz0" ]; }
# The label setting nearest to $1 mm (in dioptres, as nearslot).
nearlbl() { zbc=; for zc4 in @LBLSLOTS@; do ze4=$(( 1000000 / $1 - 100000 / zc4 )); [ $ze4 -lt 0 ] && ze4=$(( -ze4 )); if [ -z "$zbc" ] || [ $ze4 -lt $zbe ]; then zbc=$zc4; zbe=$ze4; fi; done; echo $zbc; }
slotj() { zj9=0; for zc9 in @SLOTS@; do [ $zc9 = $1 ] && { echo $zj9; return; }; zj9=$((zj9 + 1)); done; }
mklabel() { # $1 setting cm, $2 mm -> $W/L.BIN, zlt the text; 2: the text is the stock one, nothing to write
 case $1 in @LBLCASE@ *) return 1 ;; esac
 zt8=$(lbltxt $2); zc8=; zr8=$zt8; while [ -n "$zr8" ]; do zh8=${zr8%"${zr8#?}"}; zc8="$zc8 $(u16c $zh8)"; zr8=${zr8#?}; done
 set -- $zc8; [ $# -ge 3 ] && [ $# -le 4 ] || return 1; zl8=$((257 + $#))
 zn0=$(($1 + 65536 * $2)); zn1=$(($3 + 65536 * ${4:-0}))
 zsites=
 for zq8 in $zids8; do set -- $(echo $zq8 | busybox tr : ' ')
  zx8=$(lblent $1) || return 1; ze8=${zx8% *}; zs8=${zx8#* }
  [ $zl8 = @LBLLEN@ ] || zsites="$zsites $ze8 @LBLLEN@ $zl8"
  [ $2 = $zn0 ] || zsites="$zsites $zs8 $2 $zn0"
  [ $3 = $zn1 ] || zsites="$zsites $((zs8 + 4)) $3 $zn1"
 done
 zlt=$zt8; [ -n "$zsites" ] || return 2
 set -- $zsites; zm8=$(($# / 3)); cp $W/LB$zm8.BIN $W/L.BIN || return 1
 case $zm8 in @LBLOFF@ *) return 1 ;; esac
 zi8=0; for zv8 in $zsites; do putraw $W/L.BIN $((zo8 + 4 * zi8)) $zv8 || return 1; zi8=$((zi8 + 1)); done; return 0; }
tagcheck() { ztag=; obj || { log tag off no settings; return; }; $W/PEEK.BIN $(printf %x $((zo + @CPYWORD@))) 1 >$w/watch.pk 2>/dev/null; zrc=$?; zsz=$(busybox wc -c <$w/watch.pk); zb=$(busybox hexdump -v -C $w/watch.pk | busybox awk 'NR == 1 { print $3 }'); za0=$(( ($(at $zo @ATTWORD@) >> 24) & 255 )); if [ "$zb" = 00 ]; then ztag=1; log tag on attach $za0; else zb2=set; [ -n "$zb" ] || zb2=unread; log tag off copyright $zb2 peek $zrc bytes $zsz attach $za0; fi; }
tag() { send "UserData Set SetupCopyright $1" 300; }
# The tag is in the camera from the first frame until untag. WATCH/TAG on the card (the original
# attach byte) lets the next start clean up after a power-off mid-bracket: the boot copies it to
# $W/TAG; the watcher asks for writes in RAM (tag.put, tag.del) and PULL.SH does them on the card.
untag() { pt $W/CPEMPTY.BIN && { send "UserData Set SetupCopyright x" 500; pt $W/CPEMPTY.BIN restore; }; zv=Off; [ "$za0" = 0 ] && zv=On; send "UserData Set SetupCopyrightAttach $zv" 500; rm -f $W/TAG; : >$W/tag.del; log tag cleared attach $zv; }
frame() { printf "@FRAME@" $1 | b; }
bracket() { # $1 near mm, $2 far mm
 zf0=$(focus); zc0=$(snapcm); zset=$zstem; zbt=$(zup); ztb=
 zl=$(planmm $1 $2 "$zav"); log bracket near $1 far $2 av ${zav:-none} frames $(echo $zl | busybox wc -w); set -- $zl; zn=$#
 [ $zn -gt 0 ] || return
 printf "@LAMPON@" | b
 if [ "$ztag" = 1 ]; then
  echo ${za0:-1} >$W/tag.put
  pt $W/CPTAG.BIN; zrc=$?; if [ $zrc = 0 ]; then ztb=1; else log tag patch $zrc; pt $W/CPTAG.BIN restore; fi
  send "UserData Set SetupCopyrightAttach On" 500
 fi
 zi=0; zr=0
 while [ $zi -lt $zn ]; do
  zk=$((zn - zi)); [ $zk -gt @ROUND@ ] && zk=@ROUND@; zr=$((zr + 1))
  zround=$(echo $zl | busybox cut -d' ' -f$((zi + 1))-$((zi + zk)))
  mkround $W/R.BIN $zk "$zround" || { log bracket round $zr build failed; break; }
  pt $W/R.BIN; zrc=$?; log patch round $zr $zrc
  if [ $zrc = 0 ]; then
   zj=0; for zm in $zround; do zi=$((zi + 1)); zj=$((zj + 1)); zs=$(echo @SLOTS@ | busybox cut -d' ' -f$zj)
    [ "$ztb" = 1 ] && tag GRFS:$zset:$zi/$zn:$zm; log frame $zi $zm; frame $zs; done
  fi
  pt $W/R.BIN restore; log restore round $zr $?
  [ $zrc = 0 ] || break
  [ $zi -lt $zn ] || break
  poll; [ "$zu" = "$ZMACRO" ] || { log bracket stopped dial $zu; break; }
 done
 [ "$ztb" = 1 ] && pt $W/CPTAG.BIN restore
 [ "$ztag" = 1 ] && untag
 # The dial recalls each mode's own settings: restore only while still on the macro mode.
 if [ "$zu" = "$ZMACRO" ]; then
  case $zc0 in 30|100|150|200|250|350|500) ;; *) zc0=Infinity ;; esac
  send "UserData Set StillSnapDistanceCm $zc0" @PAUSE@; send "UserData Set StillFocus $(fname $zf0)" @PAUSE@
 fi
 printf "@LAMPEND@" | b; log bracket done $zi of $zn in $(( $(zup) - zbt ))s
}
macro_step() {
 zt=$(stem); [ -n "$zt" ] || return
 if [ -z "$zstem" ]; then zstem=$zt; return; fi
 if [ "$zt" -gt "$zstem" ]; then
  zstem=$zt; zfc=$(focus); case $zfc in 9|10|11|"") log macro needs AF focus ${zfc:-none}; blinks 5; zstate=idle; return ;; esac; zl=$(lensr) || { log macro no lens distance $(lensdiag); blinks 5; zstate=idle; return; }
  if [ "$zstate" = near ]; then
   log macro far $zl; blinks 2; zstate=idle; zsl 2000000; bracket $znear $zl; zstem=
  else
   znear=$zl; zt0=$(zup); zstate=near; log macro near $zl; blinks 1
  fi
 elif [ "$zstate" = near ] && [ $(( $(zup) - zt0 )) -gt @TIMEOUT@ ]; then
  zstate=idle; log macro timeout; send Af_Led_On 1500; send Af_Led_Off 300
 fi
}
# SAMP.BIN (af_debug_card.SAMP_S): one process samples the RAM every 5 ms; samp prints its words in decimal.
# Without it (zsamp 0) the same calls go through PEEK.BIN, a sample about every 0.3 s.
samp() { $W/SAMP.BIN "$@" 2>/dev/null | busybox hexdump -v -e '1/4 "%u "'; }
hx() { printf %x $1; }
rdn() { if [ "$zsamp" = 1 ]; then set -- $(samp w 0 0 0 0 0 0 100 "$@"); [ $# -ge 7 ] || return 1; shift 6; echo "$@"; return 0; fi
 zr5=; for za5; do zv5=$(num $za5); [ -n "$zv5" ] || return 1; zr5="$zr5 $zv5"; done; echo $zr5; }
sampck() { zsamp=0; [ -x $W/SAMP.BIN ] && set -- $(samp w 0 0 0 0 0 0 100 @SETTINGS@) && [ $# = 7 ] && [ "$1" = 1 ] && [ "$7" = "$(num @SETTINGS@)" ] && { zsamp=1; return 0; }
 [ -n "$zsoff" ] || log meter samp off $#; zsoff=1; }
# The meter's addresses, resolved once per visit (hex): zfa the focus word (setting +0x11c byte 0, back-up
# +0x11e byte 2), zca snap cm, zua focus in use, zpa +0x108, zla the lens mm, zs1a the half-press word; the
# tracer's words (ztrw, names ztrk); zlbc the label entries.
mres() { zla=; zs1a=; ztrw=; ztrk=; zlbc=; zfa=
 set -- $(rdn @SETTINGS@ @LENSROOT@ @S1ROOT@); [ $# = 3 ] && [ "$1" != 0 ] || return 1
 zo=$1; za=$2; zp1=$3
 zfa=$(hx $((zo + @OFFFOCUS@))); zca=$(hx $((zo + @OFFSNAP@))); zua=$(hx $((zo + @OFFCURW@))); zpa=$(hx $((zo + @OFFPOS@)))
 if [ "$za" != 0 ]; then set -- $(rdn $(hx $((za + @LENSA@)))); [ "${1:-0}" != 0 ] && set -- $(rdn $(hx $1)) && [ "${1:-0}" != 0 ] && zla=$(hx $(($1 + @LENSMM@))); fi
 # The half-press switch (S1_* in focus_watch.py): checked class, event id and key code; S2 and the sub-CPU
 # snapshot it is copied from only for the tracer.
 if [ "$zp1" != 0 ]; then set -- $(rdn $(hx $((zp1 + @S1OFF@))) $(hx $((zp1 + @S2OFF@)))); zq1=${1:-0}; zq2=${2:-0}
  if [ "$zq1" != 0 ]; then set -- $(rdn $(hx $zq1) $(hx $((zq1 + 12))) $(hx $((zq1 + 16))))
   [ "$1" = @S1CLASS@ ] && [ "$2" = @S1EVENT@ ] && [ $(( ${3:-1} & 255 )) = 0 ] && { zs1a=$(hx $((zq1 + 8))); ztrw="$(hx $((zq1 + 4))) $zs1a"; ztrk="s1p s1"; }
  fi
  [ -n "$zs1a" ] && [ "$zq2" != 0 ] && { ztrw="$ztrw $(hx $((zq2 + 8)))"; ztrk="$ztrk s2"; }
  [ -n "$zs1a" ] && { ztrw="$ztrw $(hx $((zp1 + @SUBOFF@ + 4))) $(hx $((zp1 + @SUBOFF@ + 8)))"; ztrk="$ztrk sub4 sub8"; }
 fi
 ztrw="$ztrw $zfa $zca $zua $zpa @CAFW@"; ztrk="$ztrk focus cm cur pos caf"
 set -- $(rdn @LANGTABX@); zt6=${1:-0}
 [ "$zt6" != 0 ] && for zi6 in @LBLALL@; do set -- $(rdn $(hx $((zt6 + 4 * zi6)))); ze6=${1:-0}; [ "$ze6" != 0 ] || continue
  set -- $(rdn $(hx $ze6) $(hx $((ze6 + 4)))); [ "$1" = @LBLLEN@ ] && [ "${2:-0}" != 0 ] && zlbc="$zlbc $zi6:$ze6:$2"; done
 return 0; }
# swait TIMEOUT STABLE BASE MOVED EXPECT (ms and values decimal; TIMEOUT 0: one snapshot), the lens at zlw (0:
# not watched): until the lens left BASE (or MOVED) and stood still STABLE ms, the focus byte is not EXPECT
# (256: not watched), or the time out. -> zr result (0 settled, 1 time out, 2 focus), zl lens (4294967295: no
# valid reading), zm moved, zfw the focus word, zcw snap cm, zpw +0x108, zs1w the half-press (empty: unknown).
swait() { if [ "$zsamp" = 1 ]; then set -- $(samp w $(hx $1) $(hx $2) ${zlw:-0} $(hx ${3:-0}) ${4:-0} ${zfa:-0} $(hx $5) ${zca:-0} ${zpa:-0} ${zs1a:-${zca:-0}}); else set -- $(swaitsh "$@"); fi
 [ $# = 9 ] || return 1; zr=$1; zl=$4; zm=$5; zfw=$6; zcw=$7; zpw=$8; zs1w=$9; [ -n "$zs1a" ] || zs1w=; return 0; }
swaitsh() { zt5=0; zl5=4294967295; zm5=${4:-0}; zq5=0
 while :; do
  if [ "${zlw:-0}" != 0 ]; then zv5=$(num $zlw)
   if [ -z "$zv5" ] || [ "$zv5" = 0 ] || [ "$zv5" -ge 1000000 ]; then zl5=4294967295; zq5=$zt5
   else [ "$zv5" != "$3" ] && zm5=1; if [ "$zv5" != "$zl5" ]; then zl5=$zv5; zq5=$zt5; elif [ $zm5 = 1 ] && [ $((zt5 - zq5)) -ge $2 ]; then zr5=0; break; fi; fi
  fi
  zf5=$(num $zfa); [ $5 -lt 256 ] && [ $(( ${zf5:-0} & 255 )) != $5 ] && { zr5=2; break; }
  [ $zt5 -ge $1 ] && { zr5=1; break; }
  zsl 200000; zt5=$((zt5 + 300))
 done
 zc5=$(num $zca); zp5=$(num $zpa); echo $zr5 $zt5 1 $zl5 $zm5 ${zf5:-0} ${zc5:-0} ${zp5:-0} 0; }
snap1() { zlw=${zla:-0}; swait 0 0 0 0 256; }
# The tracer (SAMP.BIN t) while on the meter mode, for the camera runs: each change of the lens and the words
# in ztrk with its time, drained into the log while idle as "tr <s.ms> <index> <old> <new>" (index 0 the lens;
# focus_watch.py log decodes them). At most @TRMAX@ tracers per visit, @TRREC@ records each.
tron() { [ "$zsamp" = 1 ] && [ -n "$zla" ] && [ ${ztrn:-0} -lt @TRMAX@ ] || return 0; ztrn=$((ztrn + 1))
 [ $ztrn = 1 ] && log meter tron lens $ztrk
 $W/SAMP.BIN t @TRMS@ @TRRECX@ $w/tr.dat $zla $ztrw >/dev/null 2>&1 & ztrp=$!; }
troff() { [ -n "$ztrp" ] && kill $ztrp 2>/dev/null; ztrp=; }
trdrain() { if [ -f $w/tr.dat ]; then mv $w/tr.dat $w/tr.d; busybox hexdump -v -e '5/4 "%u " "\n"' $w/tr.d | while read za7 zb7 zc7 zd7 ze7; do [ -n "$ze7" ] && log tr $za7.$(printf %03d $((zb7 / 1000000))) $zc7 $zd7 $ze7; done; rm -f $w/tr.d; fi
 [ -n "$ztrp" ] && ! kill -0 $ztrp 2>/dev/null && { wait $ztrp 2>/dev/null; ztrp=; tron; }; return 0; }
meter_on() { # $1 subject mm. Switches to Snap at a snap setting rewritten to the exact distance: the
 # nearest one with a four-character label, which then shows the distance (LABEL_*); without the label,
 # the nearest setting, so the screen shows about the right one.
 zs=$(meter $1 "$zav"); zmav=$zav; zmd=$1; meter_off
 set -- $1 $(nearslot $zs); zlt=
 if [ "$zlbl" != off ]; then
  zc9=$(nearlbl $zs); mklabel $zc9 $zs; zrc9=$?
  case $zrc9 in
   0) pt $W/L.BIN; zrc9=$?
      if [ $zrc9 = 0 ]; then zlon=1; set -- $1 $(slotj $zc9) $zc9; else pt $W/L.BIN restore; log meter label patch $zrc9; zlbl=off; zlt=; fi ;;
   2) set -- $1 $(slotj $zc9) $zc9 ;;
   *) log meter label off; zlbl=off; zlt= ;;
  esac
 fi
 mkround $W/M.BIN S$2 "$zs" || { zx4=missing; [ -f $W/TS$2.BIN ] && zx4=there; log meter build failed $zs slot ${3:-none} TS${2:-none}.BIN $zx4; meter_off; return; }
 pt $W/M.BIN; zrc=$?; log meter subject $1 av ${zav:-none} snap $zs slot $3 patch $zrc label ${zlt:-none}
 zmon=1; [ $zrc = 0 ] || { meter_off; return; }
 zms=$zs; zslot=$3; zlost=0
 # The dial may have turned meanwhile (the background poll): nothing into another mode's settings.
 pollck; [ "$zu" = "$ZMETER" ] || { log meter dial $zu; return; }
 meter_send $3
}
# meter_send $1 setting cm: the state the AF/Snap toggle (Fn) itself makes, the plain stores first (snap cm,
# the AF mode into the back-up slot +0x11e), StillFocus Snap last; only what the camera does not have yet,
# read back after each batch (the camera dropped whole batches or the Snap line, runs 13 to 16), at most
# three. A label patched on the setting already in use: another setting first, so the widget draws it.
meter_send() { # $2 "again": Snap the camera took back, the label is drawn already
 zpre=; [ "$2" != again ] && [ -n "$zlt$zredraw" ] && snap1 && [ "$zcw" = $1 ] && zpre=1; zredraw=
 zpv=@REDRAW@; [ $1 = @REDRAW@ ] && zpv=@REDRAW2@
 zi4=0; zok=
 while :; do
  zlw=0; swait 0 0 0 0 256 || break
  zf4=$((zfw & 255)); zb4=$(( (zfw >> 16) & 255 ))
  [ $zi4 -gt 0 ] && log meter set $zi4 focus $zf4 backup $zb4 cm $zcw
  [ "$zpre" != 1 ] && [ $zf4 = 10 ] && [ $zb4 = $zaf ] && [ "$zcw" = $1 ] && { zok=1; break; }
  [ $zi4 -lt 3 ] || break
  zi4=$((zi4 + 1))
  { [ "$zpre" = 1 ] && printf "@MSETCM@" $zpv
    [ "$zpre" = 1 ] || [ "$zcw" != $1 ] && printf "@MSETCM@" $1
    [ $zb4 = $zaf ] || printf "@MSETBK@" $(fname $zaf)
    [ $zf4 = 10 ] || printf "@MSETSNAP@"; } | b
  zpre=
 done
 if [ "$zok" = 1 ]; then zsnap=$(zup); zoff=; meter_tag; else zsnap=; log meter snap failed; fi
}
# The exact distance into the photos' copyright (only while the camera's own is empty: tagcheck), its own
# batch after Snap, CPTAG.BIN while the RTOS runs it: GRSNAP:<snap mm>:<subject mm>.
meter_tag() { [ "$ztag" = 1 ] || return 0
 pt $W/CPTAG.BIN; zrc=$?; [ $zrc = 0 ] || { log tag patch $zrc; pt $W/CPTAG.BIN restore; return 0; }
 [ "$zmtag" = 1 ] || echo ${za0:-1} >$W/tag.put
 printf "@METERTAG@" GRSNAP:$zms:$zmd | b; pt $W/CPTAG.BIN restore; zmtag=1; zleft=; log meter tag GRSNAP:$zms:$zmd; }
# The tag off in one batch (Fn back to AF, or leaving the mode): empty copyright (CPEMPTY.BIN while the RTOS
# runs it), the attach setting as it was.
mtagoff() { [ "$zmtag" = 1 ] || return 0; zmtag=; zv=Off; [ "$za0" = 0 ] && zv=On
 if pt $W/CPEMPTY.BIN; then printf "@METERUNTAG@" $zv | b; pt $W/CPEMPTY.BIN restore; else pt $W/CPEMPTY.BIN restore; send "UserData Set SetupCopyrightAttach $zv" 500; fi
 rm -f $W/TAG; : >$W/tag.del; log tag cleared attach $zv; }
# Only the RAM patch: the settings belong to the U mode, and the dial recalls the next mode's own.
meter_off() { [ "$zlon" = 1 ] && { pt $W/L.BIN restore; zrc9=$?; [ $zrc9 = 0 ] || log meter label restore $zrc9; zlon=; }
 [ "$zmon" = 1 ] || return 0; pt $W/M.BIN restore; log meter restore $?; zmon=; zms=; }
meter_enter() { ztrn=0; zlb=$zlpre; zmv=0; zsnap=; zlost=0; zoff=; zidle=0; zredraw=; zfw=0
 sampck; mres || log meter resolve failed
 [ -n "$zla" ] || log meter lens off
 if [ -n "$zfa" ] && snap1; then zf=$((zfw & 255)); isaf $zf && zaf=$zf
  log meter entry lens $zl focus $zf backup $(( (zfw >> 16) & 255 )) cm $zcw pos $zpw s1 ${zs1w:-none} samp $zsamp
  # A half-press right after turning the dial (seen only at the next poll): the lens from before the turn
  # is the base. The switch still held: the lens where it settles is the subject.
  [ -n "$zlb" ] || zlb=$zl; [ "$zs1w" = 1 ] && zmv=1
 fi
 # Fn not on AF/Snap toggle in the meter mode: no way back to AF, say so once.
 obj && zx4=$(at $zo @OFFFNW@) && zmfn=$(( (zx4 >> 16) & 255 )) && [ $zmfn != 3 ] && log meter fn $zmfn not af snap toggle
 tron; }
meter_leave() { troff; trdrain; pbstop; }
# The AF focus modes (FOCUS_NAMES); anything else (Snap, MF, Infinity, 13 "computed") has no subject.
isaf() { case $1 in @AFCODES@) return 0 ;; esac; return 1; }
# One round on the meter mode: wait in SAMP.BIN for the lens (AF) or the focus mode (Snap, MF, Infinity).
meter_step() {
 [ -n "$zfa" ] || { zsl 1000000; mres; return; }
 zf=$((zfw & 255)); zaf=${zaf:-1}
 if isaf $zf; then
  if [ -z "$zlb" ] || [ "$zlb" = 4294967295 ]; then snap1 && zlb=$zl; zmv=0; [ "$zlb" = 4294967295 ] && zsl 300000; return; fi
  zlw=${zla:-0}; swait @WAITMS@ @STABLE@ $zlb $zmv $zf
 else zlw=0; swait @WAITMS@ 0 0 0 $zf; fi || { log meter samp failed; zsamp=0; zsl 300000; return; }
 case $zr in
  0) zidle=0; zlb=$zl; zmv=0; meter_on $zl ;;
  2) zidle=0; meter_focus ;;
  *) isaf $zf && zmv=$zm; meter_idle ;;
 esac
}
# The focus mode changed (zf the old one, zfw the new word). Into AF from Snap with the back-up 13: the
# AF/Snap toggle (Fn; 0x532b0b6c writes 13); the back-up still the AF mode soon after our Snap: the camera
# changed it back, Snap again (twice at most).
meter_focus() { zf2=$((zfw & 255)); zb2=$(( (zfw >> 16) & 255 )); log meter focus $zf2 backup $zb2
 isaf $zf2 || return 0
 if [ "$zf" = 10 ] && [ -n "$zsnap" ] && [ $zb2 != 13 ] && [ $(( $(zup) - zsnap )) -le @LOSTS@ ] && [ $zlost -lt 2 ]; then
  zlost=$((zlost + 1)); log meter snap lost $zlost; zaf=$zf2; meter_send $zslot again; return 0; fi
 [ "$zf" = 10 ] && zoff=1
 zsnap=; zlost=0; zaf=$zf2; zmv=0; snap1 && zlb=$zl; }
# Nothing happened for @WAITMS@ ms: the aperture, the tracer, then (quiet for two rounds) the tag off.
meter_idle() { zidle=$((zidle + 1))
 # A new aperture (P changes it with the light) only matters in our Snap, and only when it moves the snap
 # distance (subject beyond the hyperfocal distance). In AF it would switch to Snap.
 if [ "$zf" = 10 ] && [ "$zmon" = 1 ] && [ -n "$zsnap" ] && [ "$zav" != "$zmav" ]; then
  if [ "$(meter $zmd "$zav")" != "$zms" ]; then log meter av $zav; zredraw=1; meter_on $zmd; return; fi; zmav=$zav; fi
 [ "$zmv" = 1 ] && return 0
 trdrain
 [ $zidle -ge 2 ] || return 0
 if [ "$zleft" = 1 ] || { [ "$zoff" = 1 ] && [ "$zf" != 10 ]; }; then zoff=; zleft=; mtagoff; fi
}
log watch start macro $ZMACRO meter $ZMETER
# A tag left by a power-off (bracket or Snap): cleared at the first quiet moment, not before the loop.
if [ -f $W/TAG ]; then read za0 _ <$W/TAG; log tag leftover attach ${za0:-none}; ztag=1; zmtag=1; zleft=1; else tagcheck; fi
zmode=; zstate=idle
while :; do
 if [ "$zmode" = "$ZMETER" ]; then pollbg; else poll; fi
 if [ "$zu" != "$zmode" ]; then meter_off; [ "$zu" = "$ZMETER" ] || { mtagoff; zleft=; }; [ "$zmode" = "$ZMETER" ] && meter_leave
  log watch mode $zu; zmode=$zu; zstate=idle; zstem=; zmfn=
  [ "$zu" = "$ZMETER" ] && meter_enter; zlpre=
 fi
 case $zu in
  "$ZMACRO") macro_step; zl0=$(lens) && zlpre=$zl0 ;;
  "$ZMETER") meter_step ;;
  *) zl0=$(lens) && zlpre=$zl0; zsl 1000000 ;;
 esac
done
"""


def watch_sh(macro="U1", meter="U2"):
    t = templates()
    tpl = " ".join(f"{k}) printf '" + "".join(f"{off} {cond} {j}\\n" for off, cond, j in rows) + "' ;;"
                   for k, (_, rows) in t.items())
    frame = "".join(ttl(line.replace("{slot}", "%s"), p) for line, p in [
        ("UserData Set StillSnapDistanceCm {slot}", fb.SET_PAUSE), ("UserData Set StillFocus Infinity", fb.SWITCH_MS),
        ("UserData Set StillFocus Snap", fb.SWITCH_MS)] + [(c[1], c[2]) for c in fb.press()])
    # No beep (the user's choice after run 14): the label is the feedback.
    msetcm = ttl("UserData Set StillSnapDistanceCm %s", MSET_MS)
    msetbk = ttl("UserData Set StillBackUpFocus %s", MSET_MS)
    msetsnap = ttl("UserData Set StillFocus Snap", MSNAP_MS)
    metertag = ttl("UserData Set SetupCopyright %s", 300) + ttl("UserData Set SetupCopyrightAttach On", 300)
    meteruntag = ttl("UserData Set SetupCopyright x", 300) + ttl("UserData Set SetupCopyrightAttach %s", 300)
    subs = {
        "@WWW@": rc.WWW, "@D@": rc.D, "@RAM@": RAM, "@MACRO@": macro, "@METER@": meter,
        "@SETTINGS@": f"{p3.SETTINGS_PTR:x}", "@LENSROOT@": f"{p3.LENS_ROOT:x}", "@LENSA@": f"{p3.LENS_A:#x}",
        "@LENSMM@": f"{p3.LENS_MM:#x}", "@OFFFOCUS@": f"{p3.OFF['focus']:#x}", "@OFFSNAP@": f"{p3.OFF['snap']:#x}",
        "@OFFFNW@": f"{OFF_FN - 2:#x}",
        "@CPYWORD@": f"{p3.OFF_COPYRIGHT - 1:#x}", "@ATTWORD@": f"{p3.OFF_ATTACH - 3:#x}",
        "@FNAMES@": " ".join(f"{c}) echo {n} ;;" for c, n in FOCUS_NAMES.items()),
        "@BLINK@": ttl("Af_Led_On", 150) + ttl("Af_Led_Off", 350),
        "@LAMPON@": ttl("Af_Led_On", 300), "@LAMPEND@": ttl("Af_Led_On", 3000) + ttl("Af_Led_Off", 300),
        "@MSETCM@": msetcm, "@MSETBK@": msetbk, "@MSETSNAP@": msetsnap,
        "@METERTAG@": metertag, "@METERUNTAG@": meteruntag,
        "@LANGTABX@": f"{LANG_TAB:x}", "@S1ROOT@": f"{S1_ROOT:x}", "@S1OFF@": str(S1_OFF),
        "@S1CLASS@": str(S1_CLASS), "@S1EVENT@": str(S1_EVENT), "@S2OFF@": str(S2_OFF), "@SUBOFF@": str(SUB_OFF),
        "@REDRAW@": str(REDRAW_CM), "@REDRAW2@": str(REDRAW_ALT), "@LBLLEN@": str(LABEL_LEN),
        "@LBLALL@": " ".join(str(i) for ids in LABEL_IDS.values() for i in ids),
        "@OFFPOS@": f"{p3.OFF['pos']:#x}", "@CAFW@": f"{card.CAF_OBJECT + CAF_ACTION:x}",
        "@STABLE@": str(STABLE_MS), "@AFCODES@": "|".join(str(c) for c in sorted(FOCUS_NAMES) if c not in (9, 10, 11)), "@WAITMS@": str(WAIT_MS), "@LOSTS@": str(SNAP_LOST_S),
        "@TRMS@": f"{TRACE_MS:x}", "@TRRECX@": f"{TRACE_RECORDS:x}", "@TRREC@": str(TRACE_RECORDS), "@TRMAX@": str(TRACE_MAX),
        "@LBLSLOTS@": " ".join(map(str, LABEL_IDS)),
        "@LBLCASE@": " ".join(f"{cm}) zids8='" + " ".join(f"{i}:{LABEL_STOCK[i][0]}:{LABEL_STOCK[i][1]}" for i in ids) + "' ;;"
                              for cm, ids in LABEL_IDS.items()),
        "@LBLOFF@": " ".join(f"{n}) zo8={off} ;;" for n, (_, off) in label_templates().items()),
        "@SWITCH@": str(fb.SWITCH_MS),
        "@OFFCURW@": f"{OFF_CUR - 2:#x}", "@FRAME@": frame, "@TPL@": tpl,
        "@MINMM@": str(fp.MIN_MM), "@NEARMM@": str(int(fb.NEAR_MIN_M * 1000)),
        "@COC@": repr(fb.COC_M), "@FOCAL@": repr(fb.FOCAL_M), "@STEP@": repr(1 - 0.25),
        "@MINFRAMES@": str(MIN_FRAMES), "@MAXFRAMES@": str(fb.MAX_FRAMES), "@ROUND@": str(fb.ROUND),
        "@SLOTS@": " ".join(map(str, SLOT_CM)), "@PAUSE@": str(fb.SET_PAUSE), "@TIMEOUT@": str(MACRO_TIMEOUT_S),
    }
    s = SCRIPT
    for k, v in subs.items():
        s = s.replace(k, v)
    if "@" in s.replace("$@", ""):
        raise ValueError("unfilled placeholder")
    return s


def card_files(macro="U1", meter="U2", clear_tag=None):
    """clear_tag "On"/"Off": a WATCH/TAG as a bracket cut off by a power-off leaves it, so the first
    start clears the copyright and sets attach back to that."""
    files = dict(rc.card_files())
    if clear_tag:
        files[f"{WATCH_DIR}/TAG"] = b"0\n" if clear_tag == "On" else b"1\n"
    files[f"{rc.CARD_DIR}/WATCH.SH"] = watch_sh(macro, meter).encode("ascii")
    files[f"{WATCH_DIR}/PEEK.BIN"] = card.peek_bin()
    files[f"{WATCH_DIR}/SAMP.BIN"] = card.samp_bin()
    for k, (elf, _) in templates().items():
        files[f"{WATCH_DIR}/T{k}.BIN"] = elf                 # T1..T7.BIN, TS0..TS6.BIN
    for n, (elf, _) in label_templates().items():
        files[f"{WATCH_DIR}/LB{n}.BIN"] = elf                # LB1..LB6.BIN: the label patch, 1 to 6 words
    files[f"{WATCH_DIR}/CPTAG.BIN"] = card.multipatch_bin([p3.COPYRIGHT_LEN, p3.COPYRIGHT_END])
    files[f"{WATCH_DIR}/CPEMPTY.BIN"] = card.multipatch_bin([p3.COPYRIGHT_EMPTY])
    return files


def trace_word(name, v):
    """One traced word as text: the focus word holds the setting (byte 0) and the back-up (byte 2)."""
    if name == "focus":
        return f"focus {FOCUS_NAMES.get(v & 255, v & 255)} backup {FOCUS_NAMES.get((v >> 16) & 255, (v >> 16) & 255)}"
    if name == "cur":
        return f"cur {FOCUS_NAMES.get((v >> 16) & 255, (v >> 16) & 255)}"
    if name == "caf":
        return f"caf {v:#x}"
    return f"{name} {v}"


def decode_log(lines, every=False):
    """The watcher's lines of the last start (every: all starts) with the tracer records ("tr") decoded and
    merged by time: [(seconds, text)]. tr records carry CLOCK_MONOTONIC, the other lines /proc/uptime."""
    starts = [i for i, l in enumerate(lines) if l.startswith("solo watch start")]
    if starts and not every:
        lines = lines[starts[-1]:]
    names, out = [], []
    for l in lines:
        f = l.split()
        if len(f) < 3 or f[0] != "solo":
            continue
        try:
            up = float(f[-1])
        except ValueError:
            continue
        if f[1:3] == ["meter", "tron"]:
            names = f[3:-1]
        if f[1] == "tr" and len(f) == 7 and names:
            t, i, old, new = float(f[2]), int(f[3]), int(f[4]), int(f[5])
            name = names[i] if i < len(names) else f"w{i}"
            text = trace_word(name, new) if old == new else f"{trace_word(name, old)} -> {trace_word(name, new).split(' ', 1)[1]}"
            out.append((t, "  tr " + (text if old != new else text + " (start)")))
        else:
            out.append((up, " ".join(f[1:-1])))
    return sorted(out, key=lambda r: r[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("log", help="the watcher's lines from the card's RECIPE.LOG, tracer records decoded")
    lg.add_argument("file", type=Path)
    lg.add_argument("--all", action="store_true", help="every start, not only the last")
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=ROOT / "work" / "focus-watch")
    c.add_argument("--macro", choices=U_MODES, default="U1")
    c.add_argument("--meter", choices=U_MODES, default="U2")
    c.add_argument("--clear-tag", choices=("On", "Off"),
                   help="clear a copyright tag left by a cut-off bracket at the first start; attach set to this")
    a = ap.parse_args()
    if a.cmd == "log":
        for t, text in decode_log(a.file.read_text(errors="replace").splitlines(), a.all):
            print(f"{t:9.3f}  {text}")
        return
    if a.macro == a.meter != "none":
        sys.exit("--macro and --meter need different U modes")
    bad = fb.check() + p3.check()
    if bad:
        sys.exit("the patch sites do not match this image:\n  " + "\n  ".join(bad))
    files = card_files(a.macro, a.meter, a.clear_tag)
    if a.out.exists():
        shutil.rmtree(a.out)
    for rel, data in files.items():
        f = a.out / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    r = subprocess.run(["sh", "-n", str(a.out / rc.CARD_DIR / "WATCH.SH")], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stderr)
    print(f"WATCH: macro stack on {a.macro}, snap meter on {a.meter}; no SESSION.ON (runs at every power-on)")
    print(f"wrote {a.out}/SCRIPT (copy onto the card root)")


if __name__ == "__main__":
    main()
