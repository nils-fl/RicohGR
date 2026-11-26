#!/usr/bin/env python3
"""Focus modes on the mode dial (docs/focus-bracketing.html §8): a watcher on the recipe card that runs
at every power-on and turns two U modes into a macro stack and a snap meter. No Mac, no phone.

    python3 tools/focus/focus_watch.py card [--macro U1] [--meter U2] [--out work/focus-watch]
        writes the card root (copy SCRIPT/ onto the card, keep its other files); "none" leaves a mode out

Macro stack (save the U mode with AF-S, Select or Pinpoint, Av at the aperture you want; tripod):
  1. Focus on the nearest point and take a photo. Wait for one blink.
  2. Focus on the farthest point and take a photo. Two blinks; the lamp comes on.
  3. The camera shoots the frames between them, spaced in dioptres for the aperture (at least 5, at most
     35), then puts focus mode and snap distance back; the lamp stays on for 3 s.
  No second photo within 60 s: the near point is forgotten (one long blink). Turning the dial away stops
  the bracket after the current round of up to seven frames.
Snap meter (save the U mode with AF-S and the Fn button on AF/Snap toggle, set while on that U mode):
  Half-press on the subject and hold about a second. When the lens has settled: a beep, and the
  camera switches to Snap at the subject's distance, or the hyperfocal distance when the subject is
  beyond it: the stock snap setting nearest to it (0.3 ... 5 m, what the screen shows), whose distance
  is rewritten in RAM to the exact one, set the way Fn (AF/Snap toggle) does it (StillBackUpFocus = the
  AF mode, StillFocus Snap; sent again if the camera dropped it). Shoot in Snap. To measure again press
  Fn (back to AF) and half-press on the new subject. A new aperture recomputes it while in Snap.
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
MIN_FRAMES, MACRO_TIMEOUT_S, SETTLE_US = 5, 60, 300000
POLL_EVERY = 4
SNAP_RETRY_S = 5    # the batch is acked about 2.3 s after the measurement, Snap seen about 1.5 s later     # meter mode: /v1/props (about 2 s) once per this many lens reads
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
REDRAW_CM = 500    # sent before the setting when it is already in use: the widget skips an unchanged id


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
get() { ( printf 'GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n' "$1"; sleep 2 ) | busybox nc 127.0.0.1 80 >$w/watch.txt 2>/dev/null & zp=$!; zj=0; while kill -0 $zp 2>/dev/null && [ $zj -lt 30 ]; do zsl 100000; zj=$((zj + 1)); done; kill $zp 2>/dev/null; }
poll() { get /v1/props; zpr=$(busybox sed 's/[][{}]/,/g' $w/watch.txt 2>/dev/null | busybox awk -v RS=, '/"exposureMode"[ ]*:/ { sub(/.*:/, ""); gsub(/[^A-Za-z0-9]/, ""); m = $0 } /"av"[ ]*:/ { sub(/.*:/, ""); gsub(/[^0-9.]/, ""); a = $0 } END { print m, a }'); set -- $zpr; zu=${1:-none}; zav=$2; }
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
# The exact distance as the snap label (LABEL_* above). lblent: the entry of label id $1 in table index 0,
# empty unless its length word is the stock 5. mklabel $1 file $2 slot cm $3 mm: the patch (length
# 6, the string words that change), every word checked by the patch tool before it writes.
lblent() { zt6=$(num $(printf %x @LANGTAB@)); [ -n "$zt6" ] && [ "$zt6" != 0 ] || return; ze6=$(at $zt6 $((4 * $1))); [ -n "$ze6" ] && [ "$ze6" != 0 ] || return; [ "$(num $(printf %x $ze6))" = @LBLLEN@ ] && echo $ze6; }
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
  ze8=$(lblent $1); [ -n "$ze8" ] || return 1; zs8=$(at $ze8 4); [ -n "$zs8" ] && [ "$zs8" != 0 ] || return 1
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
meter_on() { # $1 subject mm. Switches to Snap at a snap setting rewritten to the exact distance: the
 # nearest one with a four-character label, which then shows the distance (LABEL_*); without the label,
 # the nearest setting, so the screen shows about the right one.
 zs=$(meter $1 "$zav"); zmav=$zav; meter_off
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
 zms=$zs
 # One relay batch: the AF beep, the snap setting, then the state the AF/Snap toggle (Fn) itself makes:
 # the AF mode into the back-up slot (StillBackUpFocus, +0x11e), focus Snap. Fn swaps them back.
 # Already in Snap (a new aperture): only the snap setting, no beep.
 zf4=$(focus)
 # The exact distance into the photos' copyright (only while the camera's own is empty: tagcheck), in the
 # same batch, CPTAG.BIN while the RTOS runs it: GRSNAP:<snap mm>:<subject mm>.
 zmt=; if [ "$ztag" = 1 ]; then pt $W/CPTAG.BIN; zrc=$?; if [ $zrc = 0 ]; then zmt=1; else log tag patch $zrc; pt $W/CPTAG.BIN restore; fi; fi
 [ "$zmt" = 1 ] && [ "$zmtag" != 1 ] && echo ${za0:-1} >$W/tag.put
 zfn4=$(fname $zf4); [ "$zf4" = 10 ] || { zpend=$(zup); zsr=0; }
 # The label patched on the setting already in use: another one first, so the new text is drawn.
 zpre=; [ -n "$zlt" ] && [ "$(snapcm)" = "$3" ] && zpre=1
 { [ "$zpre" = 1 ] && printf "@METERPRE@" @REDRAW@
   if [ "$zf4" = 10 ]; then printf "@METERDIST@" $3; else printf "@METERSET@" $3 $zfn4; fi
   [ "$zmt" = 1 ] && [ "$zpre" != 1 ] && printf "@METERTAG@" GRSNAP:$zs:$1; } | b
 # With the redraw line the tag does not fit the batch (RELAY_SIZE): its own, about 2 s later.
 [ "$zmt" = 1 ] && [ "$zpre" = 1 ] && printf "@METERTAG@" GRSNAP:$zs:$1 | b
 [ "$zmt" = 1 ] && { pt $W/CPTAG.BIN restore; zmtag=1; log meter tag GRSNAP:$zs:$1; }
}
# The tag off in one batch (Fn back to AF, or leaving the mode): empty copyright (CPEMPTY.BIN while the RTOS
# runs it), the attach setting as it was.
mtagoff() { [ "$zmtag" = 1 ] || return; zmtag=; zv=Off; [ "$za0" = 0 ] && zv=On
 if pt $W/CPEMPTY.BIN; then printf "@METERUNTAG@" $zv | b; pt $W/CPEMPTY.BIN restore; else pt $W/CPEMPTY.BIN restore; send "UserData Set SetupCopyrightAttach $zv" 500; fi
 rm -f $W/TAG; : >$W/tag.del; log tag cleared attach $zv; }
# Only the RAM patch: the settings belong to the U mode, and the dial recalls the next mode's own.
meter_off() { [ "$zlon" = 1 ] && { pt $W/L.BIN restore; zrc9=$?; [ $zrc9 = 0 ] || log meter label restore $zrc9; zlon=; }
 [ "$zmon" = 1 ] || return; pt $W/M.BIN restore; log meter restore $?; zmon=; zms=; }
meter_step() {
 # A new aperture (P changes it with the light) only matters in Snap, and only when it moves the snap
 # distance (subject beyond the hyperfocal distance): then quietly. In AF it would switch to Snap.
 if [ "$zmon" = 1 ] && [ "$zav" != "$zmav" ]; then
  if [ "$(focus)" = 10 ] && [ "$(meter $zmd "$zav")" != "$zms" ]; then log meter av $zav; meter_on $zmd; else zmav=$zav; fi
 fi
 # In Snap the lens only goes to the snap distance: nothing to measure until Fn (AF/Snap toggle) gives AF.
 zfc=$(focus); [ "$zfc" = "$zfl" ] || { log meter focus ${zfc:-none}; zfl=$zfc; }
 case $zfc in 10) zsn=1; zpend=; return ;; 9|11|"") return ;; esac
 # Snap sent but still AF after @RETRYS@ s (the camera dropped the setting, seen while AF was busy): once
 # more, twice at most. Snap that came and went again was Fn.
 if [ -n "$zpend" ] && [ $(( $(zup) - zpend )) -ge @RETRYS@ ]; then
  if [ $zsr -lt 2 ]; then zsr=$((zsr + 1)); zpend=$(zup); log meter snap retry $zsr; printf "@METERSNAP@" $(fname $zfc) | b; else zpend=; log meter snap failed; fi
 fi
 zl=$(lens); [ -n "$zl" ] && [ "$zl" -gt 0 ] || return
 # First reading, or back from Snap with the lens still at the snap distance: the base, wait for a half-press.
 # Back from Snap: the base first, then the tag off (a batch, about 2 s): a half-press meanwhile still counts.
 if [ -z "$zlb" ] || [ "$zsn" = 1 ]; then zsn=; zmv=; zst=0; zlb=$zl; zlp=$zl; mtagoff; return; fi
 # The lens moved (AF) and holds for three readings (about 1 s): the subject, even if AF settled where
 # it started.
 [ "$zl" != "$zlb" ] && zmv=1
 if [ "$zl" = "$zlp" ]; then zst=$((zst + 1)); else zst=0; fi
 if [ "$zmv" = 1 ] && [ $zst -ge 2 ]; then zmv=; zst=0; zlb=$zl; zmd=$zl; meter_on $zl; fi
 zlp=$zl
}
log watch start macro $ZMACRO meter $ZMETER
if [ -f $W/TAG ]; then read za0 _ <$W/TAG; log tag leftover attach ${za0:-none}; untag; fi
tagcheck
zmode=; zstate=idle
while :; do
 # On the meter mode the lens is read every round, the dial and aperture (a web request) every @POLLN@th.
 if [ "$zmode" != "$ZMETER" ] || [ ${zpc:-0} -le 0 ]; then poll; zpc=@POLLN@; fi; zpc=$((zpc - 1))
 if [ "$zu" != "$zmode" ]; then meter_off; mtagoff; log watch mode $zu; zmode=$zu; zstate=idle; zstem=; zlb=; zlp=; zsn=; zmv=; zmfn=; zfl=
  # A half-press right after turning the dial (noticed only at the next poll) still counts: the lens
  # position from before the turn is the base.
  [ "$zu" = "$ZMETER" ] && [ -n "$zlpre" ] && { zlb=$zlpre; zlp=$zlpre; zst=0; }; zlpre=; zpend=
  # Fn not on AF/Snap toggle in the meter mode: no way back to AF, say so once.
  [ "$zu" = "$ZMETER" ] && obj && zx4=$(at $zo @OFFFNW@) && zmfn=$(( (zx4 >> 16) & 255 )) && [ $zmfn != 3 ] && log meter fn $zmfn not af snap toggle
 fi
 case $zu in
  "$ZMACRO") macro_step; zl0=$(lens) && zlpre=$zl0 ;;
  "$ZMETER") meter_step; zsl @SETTLE@ ;;
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
    meterdist = ttl("UserData Set StillSnapDistanceCm %s", fb.SET_PAUSE)
    metersnap = "".join(ttl(line, p) for line, p in [
        ("UserData Set StillBackUpFocus %s", fb.SET_PAUSE), ("UserData Set StillFocus Snap", fb.SWITCH_MS)])
    meterset = ttl("AUD_beep", 100) + meterdist + metersnap
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
        "@METERSET@": meterset, "@METERDIST@": meterdist, "@METERSNAP@": metersnap,
        "@METERTAG@": metertag, "@METERUNTAG@": meteruntag,
        "@LANGTAB@": str(LANG_TAB), "@REDRAW@": str(REDRAW_CM), "@METERPRE@": meterdist, "@LBLLEN@": str(LABEL_LEN),
        "@LBLSLOTS@": " ".join(map(str, LABEL_IDS)),
        "@LBLCASE@": " ".join(f"{cm}) zids8='" + " ".join(f"{i}:{LABEL_STOCK[i][0]}:{LABEL_STOCK[i][1]}" for i in ids) + "' ;;"
                              for cm, ids in LABEL_IDS.items()),
        "@LBLOFF@": " ".join(f"{n}) zo8={off} ;;" for n, (_, off) in label_templates().items()),
        "@RETRYS@": str(SNAP_RETRY_S), "@SWITCH@": str(fb.SWITCH_MS), "@POLLN@": str(POLL_EVERY),
        "@OFFCURW@": f"{OFF_CUR - 2:#x}", "@FRAME@": frame, "@TPL@": tpl,
        "@MINMM@": str(fp.MIN_MM), "@NEARMM@": str(int(fb.NEAR_MIN_M * 1000)),
        "@COC@": repr(fb.COC_M), "@FOCAL@": repr(fb.FOCAL_M), "@STEP@": repr(1 - 0.25),
        "@MINFRAMES@": str(MIN_FRAMES), "@MAXFRAMES@": str(fb.MAX_FRAMES), "@ROUND@": str(fb.ROUND),
        "@SLOTS@": " ".join(map(str, SLOT_CM)), "@PAUSE@": str(fb.SET_PAUSE), "@TIMEOUT@": str(MACRO_TIMEOUT_S),
        "@SETTLE@": str(SETTLE_US),
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
    for k, (elf, _) in templates().items():
        files[f"{WATCH_DIR}/T{k}.BIN"] = elf                 # T1..T7.BIN, TS0..TS6.BIN
    for n, (elf, _) in label_templates().items():
        files[f"{WATCH_DIR}/LB{n}.BIN"] = elf                # LB1..LB6.BIN: the label patch, 1 to 6 words
    files[f"{WATCH_DIR}/CPTAG.BIN"] = card.multipatch_bin([p3.COPYRIGHT_LEN, p3.COPYRIGHT_END])
    files[f"{WATCH_DIR}/CPEMPTY.BIN"] = card.multipatch_bin([p3.COPYRIGHT_EMPTY])
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=ROOT / "work" / "focus-watch")
    c.add_argument("--macro", choices=U_MODES, default="U1")
    c.add_argument("--meter", choices=U_MODES, default="U2")
    c.add_argument("--clear-tag", choices=("On", "Off"),
                   help="clear a copyright tag left by a cut-off bracket at the first start; attach set to this")
    a = ap.parse_args()
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
