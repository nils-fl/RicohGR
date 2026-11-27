#!/usr/bin/env python3
"""Snap meter (docs/snap-meter.html): one of the focus modes on the dial. The watcher that runs it,
follows the dial and owns the card is tools/focus/focus_watch.py; this module holds the meter's part of
its WATCH.SH, the constants and RAM patches that part uses, and the tracer decoding.

    python3 tools/focus/focus_watch.py card --meter U2      # the card, with the macro stack on U1
    python3 tools/focus/focus_watch.py log SCRIPT/RECIPE/RECIPE.LOG

Save the U mode with AF-S and the Fn button on AF/Snap toggle (set while on that U mode, then save it
again: each U mode recalls its own button setup). Half-press on the subject, wait for focus, release.
SAMP.BIN samples RAM every 5 ms; an AF run shows in the contrast AF object's action word (1 idle, then
2, 4, 8 or 0x40, 0x80, back to 1), and the lens AF_STILL_MS after the word returns to 1 is the subject.
The camera then switches to Snap at the subject's distance, or the hyperfocal distance when the subject
is beyond it: the snap setting with a four-character label nearest to it, whose distance and label are
rewritten in RAM to the exact one, set the way Fn (AF/Snap toggle) does it (snap cm, StillBackUpFocus =
the AF mode, then StillFocus Snap). Each batch is read back in RAM; what is missing goes again (three
batches at most), and Snap the camera takes back by itself (back-up not 13, as Fn leaves it) is sent
again. A half-press while a batch runs is watched in the background and measured right after it. Shoot
in Snap. To measure again press Fn (back to AF) and half-press on the new subject. A new aperture
recomputes it while in Snap. With the camera's copyright empty, Snap photos carry
GRSNAP:<snap mm>:<subject mm> in EXIF Copyright (cleared on Fn back to AF and on leaving the mode).
The tracer (SAMP.BIN t) logs the lens and the focus words as they change, for the camera runs; without
SAMP.BIN the meter falls back to PEEK.BIN and the lens alone.
"""
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("af", "focus")]
import af_debug_card as card  # noqa: E402
import focus_probe3 as p3  # noqa: E402

FOCUS_NAMES = p3.FOCUS_NAMES
# The meter waits in SAMP.BIN (af_debug_card.SAMP_S, a sample every 5 ms). Without the action word (PEEK.BIN
# path) the subject is the lens once it has moved and stood still STABLE_MS; WAIT_MS per round when nothing
# happens.
STABLE_MS, WAIT_MS = 250, 1000
MSET_MS, MSNAP_MS = 150, 300   # relay pauses after the plain stores and after StillFocus Snap (read back anyway)
SNAP_LOST_S = 5    # Snap gone within this long after we set it, back-up not 13 (not Fn): the camera's doing, again
TRACE_MS, TRACE_RECORDS, TRACE_MAX = 120000, 200, 6   # the tracer for the camera runs: per tracer, per visit
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
# The half-press switch words (SwitchMonitor [0x5536c608] +0x5c S1, +0x60 S2, +0xd0 the sub-CPU snapshot;
# docs §8) never changed in /dev/mem during camera run 17's presses, traced at 200 Hz: not used. The AF
# runs show in the contrast AF object's action word instead (CAF_OBJECT + CAF_ACTION): 1 idle, 2 and 4 the
# scan, 8 or 0x40, 0x80 at the end, every press.
CAF_ACTION = 0xC44C
AF_MS, AF_STILL_MS = 1500, 120   # an AF run's step at most; the lens still after it
AF_BG_MS = 3000    # after a batch, how long an AF run the background watch saw start may still take
BOOT_S = 40        # entering the meter mode this soon after power-on with a valid lens: AF ran before the watcher
REDRAW_CM, REDRAW_ALT = 500, 350   # sent before the setting when it is already in use: the widget skips an unchanged id
LABEL_PH = 0x5A5A0000   # the label template words the shell fills in (LB<n>.BIN only)


def label_templates():
    """{n: (ELF, offset of the first site)}: a multipatch of n sites, every address, stock and new word a
    placeholder; the sites follow each other (address, stock, new), 12 bytes apart."""
    out = {}
    for n in range(1, 7):
        sites = [(LABEL_PH | (3 * i), LABEL_PH | (3 * i + 1), LABEL_PH | (3 * i + 2)) for i in range(n)]
        elf = card.multipatch_bin(sites)
        off = elf.index(LABEL_PH.to_bytes(4, "little"))
        for i, site in enumerate(sites):
            for k, ph in enumerate(site):
                if elf.count(ph.to_bytes(4, "little")) != 1 or elf.index(ph.to_bytes(4, "little")) != off + 12 * i + 4 * k:
                    raise ValueError(f"label template {n}: placeholder {ph:#x}")
        out[n] = (elf, off)
    return out


# Helpers among the watcher's (focus_watch.SCRIPT): the snap distance, the nearest setting, the label patch.
SH_HELPERS = r"""meter() { busybox awk -v l="$1" -v a="$2" 'BEGIN { if (a + 0 <= 0) a = 2.8; h = a * @COC@ / (@FOCAL@ * @FOCAL@); s = l; if (1000 / l < h) s = 1000 / h; if (s < @NEARMM@) s = @NEARMM@; if (s > 65535) s = 65535; print int(s + 0.5) }'; }
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
"""

# The meter mode: SAMP.BIN, the AF run, Snap, the tag, the tracer; called from the watcher's loop as
# meter_enter, meter_step, meter_off, meter_leave and mtagoff.
SH_MODE = r"""# SAMP.BIN (af_debug_card.SAMP_S): one process samples the RAM every 5 ms; samp prints its words in decimal.
# Without it (zsamp 0) the same calls go through PEEK.BIN, a sample about every 0.3 s.
samp() { $W/SAMP.BIN "$@" 2>/dev/null | busybox hexdump -v -e '1/4 "%u "'; }
hx() { printf %x $1; }
rdn() { if [ "$zsamp" = 1 ]; then set -- $(samp w 0 0 0 0 0 0 100 "$@"); [ $# -ge 7 ] || return 1; shift 6; echo "$@"; return 0; fi
 zr5=; for za5; do zv5=$(num $za5); [ -n "$zv5" ] || return 1; zr5="$zr5 $zv5"; done; echo $zr5; }
sampck() { zsamp=0; [ -x $W/SAMP.BIN ] && set -- $(samp w 0 0 0 0 0 0 100 @SETTINGS@) && [ $# = 7 ] && [ "$1" = 1 ] && [ "$7" = "$(num @SETTINGS@)" ] && { zsamp=1; return 0; }
 [ -n "$zsoff" ] || log meter samp off $#; zsoff=1; }
# The meter's addresses, resolved once per visit (hex): zfa the focus word (setting +0x11c byte 0, back-up
# +0x11e byte 2), zca snap cm, zua focus in use, zpa +0x108, zla the lens mm, zcafa the contrast AF action
# word (static); the tracer's words (ztrw, names ztrk); zlbc the label entries.
mres() { zla=; ztrw=; ztrk=; zlbc=; zfa=; zcafa=@CAFW@
 set -- $(rdn @SETTINGS@ @LENSROOT@); [ $# = 2 ] && [ "$1" != 0 ] || return 1
 zo=$1; za=$2
 zfa=$(hx $((zo + @OFFFOCUS@))); zca=$(hx $((zo + @OFFSNAP@))); zua=$(hx $((zo + @OFFCURW@))); zpa=$(hx $((zo + @OFFPOS@)))
 if [ "$za" != 0 ]; then set -- $(rdn $(hx $((za + @LENSA@)))); [ "${1:-0}" != 0 ] && set -- $(rdn $(hx $1)) && [ "${1:-0}" != 0 ] && zla=$(hx $(($1 + @LENSMM@))); fi
 ztrw="$zfa $zca $zua $zpa $zcafa"; ztrk="focus cm cur pos caf"
 set -- $(rdn @LANGTABX@); zt6=${1:-0}
 [ "$zt6" != 0 ] && for zi6 in @LBLALL@; do set -- $(rdn $(hx $((zt6 + 4 * zi6)))); ze6=${1:-0}; [ "$ze6" != 0 ] || continue
  set -- $(rdn $(hx $ze6) $(hx $((ze6 + 4)))); [ "$1" = @LBLLEN@ ] && [ "${2:-0}" != 0 ] && zlbc="$zlbc $zi6:$ze6:$2"; done
 return 0; }
# swait TIMEOUT STABLE BASE MOVED EXPECT (ms and values decimal; TIMEOUT 0: one snapshot), the lens at zlw (0:
# not watched), the word at zwa (0: none): until the lens left BASE (or MOVED) and stood still STABLE ms, the
# low byte of the word is not EXPECT (256: not watched), or the time out. -> zr result (0 settled, 1 time out,
# 2 the word changed), zl lens (4294967295: no valid reading), zm moved, zww the word, zcw snap cm, zpw +0x108,
# zfw the focus word.
swait() { if [ "$zsamp" = 1 ]; then set -- $(samp w $(hx $1) $(hx $2) ${zlw:-0} $(hx ${3:-0}) ${4:-0} ${zwa:-0} $(hx $5) ${zca:-0} ${zpa:-0} ${zfa:-0}); else set -- $(swaitsh "$@"); fi
 [ $# = 9 ] || return 1; zr=$1; zl=$4; zm=$5; zww=$6; zcw=$7; zpw=$8; zfw=$9; return 0; }
swaitsh() { zt5=0; zl5=4294967295; zm5=${4:-0}; zq5=0; zw5=0
 while :; do
  if [ "${zlw:-0}" != 0 ]; then zv5=$(num $zlw)
   if [ -z "$zv5" ] || [ "$zv5" = 0 ] || [ "$zv5" -ge 1000000 ]; then zl5=4294967295; zq5=$zt5
   else [ "$zv5" != "$3" ] && zm5=1; if [ "$zv5" != "$zl5" ]; then zl5=$zv5; zq5=$zt5; elif [ $zm5 = 1 ] && [ $((zt5 - zq5)) -ge $2 ]; then zr5=0; break; fi; fi
  fi
  [ "${zwa:-0}" != 0 ] && zw5=$(num $zwa); [ $5 -lt 256 ] && [ $(( ${zw5:-0} & 255 )) != $5 ] && { zr5=2; break; }
  [ $zt5 -ge $1 ] && { zr5=1; break; }
  zsl 200000; zt5=$((zt5 + 300))
 done
 zc5=$(num $zca); zp5=$(num $zpa); zf5=$(num $zfa); echo $zr5 $zt5 1 $zl5 $zm5 ${zw5:-0} ${zc5:-0} ${zp5:-0} ${zf5:-0}; }
snap1() { zlw=${zla:-0}; zwa=${zcafa:-0}; swait 0 0 0 0 256; }
# afdone: the AF run under way ends (the contrast AF action word CAF_ACTION: 1 idle, 2 and 4 the scan, 8 or 0x40,
# then 0x80 at the end; camera run 17), then the lens holds @AFSTILL@ ms (it reached its end within 50 ms of the
# word going back to 1). -> zl the subject, zafp the words seen; 2: the word never went back to 1.
afdone() { zafp=; zj8=0; zlw=0; zwa=$zcafa; swait 0 0 0 0 256 || return 1
 while [ $(( zww & 255 )) != 1 ]; do zafp="$zafp$(hx $((zww & 255)))."; zj8=$((zj8 + 1)); [ $zj8 -le 8 ] || return 2
  swait @AFMS@ 0 0 0 $((zww & 255)) || return 1; done
 zlw=${zla:-0}; zwa=0; swait 500 @AFSTILL@ 0 1 256 && [ "$zr" = 0 ]; }
# A half-press while a batch runs (run 17, 55.5 s: AF on a new subject, then our Snap of the one before moved
# the lens away) is watched in the background; the newer subject (zpq) is measured right after the batch.
# An AF run already under way when the batch ends (af.run) is waited for, up to @AFBGN@ x 50 ms: in camera run
# 18 two presses (Fn, then a half-press during the tag batch) ended just as the batch did, and the watch was
# killed inside its @AFSTILL@ ms lens check.
afbg() { [ "$zsamp" = 1 ] && [ -n "$zcafa" ] || return 0; rm -f $w/af.new $w/af.run; ( zwa=$zcafa; zlw=0; swait 8000 0 0 0 1 && [ "$zr" = 2 ] && : >$w/af.run && afdone && echo $zl >$w/af.new; rm -f $w/af.run ) >/dev/null 2>&1 & zafb=$!; }
afbgend() { [ -n "$zafb" ] || return 0; zj9=0
 kill -0 $zafb 2>/dev/null && zsl 30000
 while kill -0 $zafb 2>/dev/null && [ -f $w/af.run ] && [ $zj9 -lt @AFBGN@ ]; do zsl 50000; zj9=$((zj9 + 1)); done
 kill -0 $zafb 2>/dev/null && kill $zafb 2>/dev/null; wait $zafb 2>/dev/null; zafb=
 [ -s $w/af.new ] && read zl9 _ <$w/af.new && [ -n "$zl9" ] && { zpq=$zl9; log meter pressed during batch $zl9 waited $zj9; }; rm -f $w/af.new $w/af.run; return 0; }
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
  afbg
  { [ "$zpre" = 1 ] && printf "@MSETCM@" $zpv
    [ "$zpre" = 1 ] || [ "$zcw" != $1 ] && printf "@MSETCM@" $1
    [ $zb4 = $zaf ] || printf "@MSETBK@" $(fname $zaf)
    [ $zf4 = 10 ] || printf "@MSETSNAP@"; } | b
  afbgend; zpre=
  [ -n "$zpq" ] && { zsnap=; return 0; }        # pressed again meanwhile: that one next
 done
 if [ "$zok" = 1 ]; then zsnap=$(zup); zoff=; meter_tag; else zsnap=; log meter snap failed; fi
}
# The exact distance into the photos' copyright (only while the camera's own is empty: tagcheck), its own
# batch after Snap, CPTAG.BIN while the RTOS runs it: GRSNAP:<snap mm>:<subject mm>.
meter_tag() { [ "$ztag" = 1 ] || return 0
 pt $W/CPTAG.BIN; zrc=$?; [ $zrc = 0 ] || { log tag patch $zrc; pt $W/CPTAG.BIN restore; return 0; }
 [ "$zmtag" = 1 ] || echo ${za0:-1} >$W/tag.put
 afbg; printf "@METERTAG@" GRSNAP:$zms:$zmd | b; afbgend; pt $W/CPTAG.BIN restore; zmtag=1; zleft=; log meter tag GRSNAP:$zms:$zmd; }
# The tag off in one batch (Fn back to AF, or leaving the mode): empty copyright (CPEMPTY.BIN while the RTOS
# runs it), the attach setting as it was.
mtagoff() { [ "$zmtag" = 1 ] || return 0; zmtag=; zv=Off; [ "$za0" = 0 ] && zv=On
 if pt $W/CPEMPTY.BIN; then afbg; printf "@METERUNTAG@" $zv | b; afbgend; pt $W/CPEMPTY.BIN restore; else pt $W/CPEMPTY.BIN restore; send "UserData Set SetupCopyrightAttach $zv" 500; fi
 rm -f $W/TAG; : >$W/tag.del; log tag cleared attach $zv; }
# Only the RAM patch: the settings belong to the U mode, and the dial recalls the next mode's own.
meter_off() { [ "$zlon" = 1 ] && { pt $W/L.BIN restore; zrc9=$?; [ $zrc9 = 0 ] || log meter label restore $zrc9; zlon=; }
 [ "$zmon" = 1 ] || return 0; pt $W/M.BIN restore; log meter restore $?; zmon=; zms=; }
meter_enter() { ztrn=0; zlb=$zlpre; zmv=0; zsnap=; zlost=0; zoff=; zidle=0; zredraw=; zfw=0; zpq=
 sampck; mres || log meter resolve failed
 [ -n "$zla" ] || log meter lens off
 if [ -n "$zfa" ] && snap1; then zf=$((zfw & 255)); isaf $zf && zaf=$zf
  log meter entry lens $zl focus $zf backup $(( (zfw >> 16) & 255 )) cm $zcw pos $zpw caf $(hx $((zww & 255))) samp $zsamp
  [ -n "$zlb" ] || zlb=$zl
  # AF done before the watcher looked: right after turning the dial (the lens moved from where it was before
  # the turn), or right after power-on (the lens reads 1000000 until the first AF; camera run 17).
  if [ "$zsamp" = 1 ] && isaf $zf && [ "$zl" != 4294967295 ]; then
   if [ -n "$zlpre" ]; then [ "$zl" != "$zlpre" ] && { zpq=$zl; log meter subject before the dial $zl; }
   elif [ "$(zup)" -lt @BOOTS@ ]; then zpq=$zl; log meter subject before the start $zl; fi
  fi
 fi
 # Fn not on AF/Snap toggle in the meter mode: no way back to AF, say so once.
 obj && zx4=$(at $zo @OFFFNW@) && zmfn=$(( (zx4 >> 16) & 255 )) && [ $zmfn != 3 ] && log meter fn $zmfn not af snap toggle
 tron; }
meter_leave() { troff; trdrain; pbstop; }
# The AF focus modes (FOCUS_NAMES); anything else (Snap, MF, Infinity, 13 "computed") has no subject.
isaf() { case $1 in @AFCODES@) return 0 ;; esac; return 1; }
sampbad() { log meter samp failed; zsamp=0; zsl 300000; }
# One round on the meter mode: wait in SAMP.BIN for an AF run (AF modes) or the focus mode (Snap, MF, Infinity).
meter_step() {
 [ -n "$zfa" ] || { zsl 1000000; mres; return; }
 if [ -n "$zpq" ]; then zl=$zpq; zpq=; zidle=0; meter_on $zl; return; fi
 zf=$((zfw & 255)); zaf=${zaf:-1}
 if ! isaf $zf; then zwa=$zfa; zlw=0; swait @WAITMS@ 0 0 0 $zf || { sampbad; return; }
  case $zr in 2) zidle=0; meter_focus ;; *) meter_idle ;; esac; return
 fi
 if [ "$zsamp" = 1 ] && [ -n "$zcafa" ]; then
  # Every AF run takes the action word away from 1; the lens where it ends is the subject, also at the
  # distance it was at before (the lens alone did not show that).
  zwa=$zcafa; zlw=0; swait @WAITMS@ 0 0 0 1 || { sampbad; return; }
  [ $((zfw & 255)) = $zf ] || { zidle=0; meter_focus; return; }
  [ "$zr" = 2 ] || { meter_idle; return; }
  zidle=0; afdone; zrc8=$?
  case $zrc8 in
   0) log meter af $zafp; meter_on $zl ;;
   2) log meter caf off $zafp; zcafa= ;;
   *) log meter af $zafp no lens ;;
  esac; return
 fi
 # Without SAMP.BIN or the action word: the lens moved away from the base and stood still.
 if [ -z "$zlb" ] || [ "$zlb" = 4294967295 ]; then snap1 && zlb=$zl; zmv=0; [ "$zlb" = 4294967295 ] && zsl 300000; return; fi
 zwa=$zfa; zlw=${zla:-0}; swait @WAITMS@ @STABLE@ $zlb $zmv $zf || { sampbad; return; }
 case $zr in
  0) zidle=0; zlb=$zl; zmv=0; meter_on $zl ;;
  2) zidle=0; meter_focus ;;
  *) zmv=$zm; meter_idle ;;
 esac
}
# The focus mode changed (zf the old one, zfw the new word). Into AF from Snap with the back-up 13: the
# AF/Snap toggle (Fn); it writes the focus mode first and the back-up 13 about 10 ms later (0x532b0b6c;
# camera run 17), so a back-up that is not 13 is read again. Still the AF mode soon after our Snap: the
# camera changed it back, Snap again (twice at most).
meter_focus() { zf2=$((zfw & 255)); zb2=$(( (zfw >> 16) & 255 ))
 if [ "$zf" = 10 ] && isaf $zf2 && [ $zb2 != 13 ]; then zsl 100000; snap1; zf2=$((zfw & 255)); zb2=$(( (zfw >> 16) & 255 )); fi
 log meter focus $zf2 backup $zb2
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
"""


def subs():
    """The meter's placeholders in WATCH.SH (the watcher fills its own and the shared ones)."""
    ttl = p3.ttl
    # No beep (the user's choice after run 14): the label is the feedback.
    msetcm = ttl("UserData Set StillSnapDistanceCm %s", MSET_MS)
    msetbk = ttl("UserData Set StillBackUpFocus %s", MSET_MS)
    msetsnap = ttl("UserData Set StillFocus Snap", MSNAP_MS)
    metertag = ttl("UserData Set SetupCopyright %s", 300) + ttl("UserData Set SetupCopyrightAttach On", 300)
    meteruntag = ttl("UserData Set SetupCopyright x", 300) + ttl("UserData Set SetupCopyrightAttach %s", 300)
    return {
        "@OFFFNW@": f"{OFF_FN - 2:#x}",
        "@MSETCM@": msetcm, "@MSETBK@": msetbk, "@MSETSNAP@": msetsnap,
        "@METERTAG@": metertag, "@METERUNTAG@": meteruntag,
        "@LANGTABX@": f"{LANG_TAB:x}", "@AFMS@": str(AF_MS), "@AFSTILL@": str(AF_STILL_MS),
        "@AFBGN@": str(AF_BG_MS // 50), "@BOOTS@": str(BOOT_S),
        "@REDRAW@": str(REDRAW_CM), "@REDRAW2@": str(REDRAW_ALT), "@LBLLEN@": str(LABEL_LEN),
        "@LBLALL@": " ".join(str(i) for ids in LABEL_IDS.values() for i in ids),
        "@OFFPOS@": f"{p3.OFF['pos']:#x}", "@CAFW@": f"{card.CAF_OBJECT + CAF_ACTION:x}",
        "@STABLE@": str(STABLE_MS), "@WAITMS@": str(WAIT_MS), "@LOSTS@": str(SNAP_LOST_S),
        "@AFCODES@": "|".join(str(c) for c in sorted(FOCUS_NAMES) if c not in (9, 10, 11)),
        "@TRMS@": f"{TRACE_MS:x}", "@TRRECX@": f"{TRACE_RECORDS:x}", "@TRREC@": str(TRACE_RECORDS),
        "@TRMAX@": str(TRACE_MAX),
        "@LBLSLOTS@": " ".join(map(str, LABEL_IDS)),
        "@LBLCASE@": " ".join(f"{cm}) zids8='" + " ".join(f"{i}:{LABEL_STOCK[i][0]}:{LABEL_STOCK[i][1]}" for i in ids) + "' ;;"
                              for cm, ids in LABEL_IDS.items()),
        "@LBLOFF@": " ".join(f"{n}) zo8={off} ;;" for n, (_, off) in label_templates().items()),
    }


def card_files(watch_dir):
    """The meter's files in the card's WATCH folder (its single-slot templates TS0..TS6.BIN come with the
    watcher's T<k>.BIN)."""
    files = {f"{watch_dir}/SAMP.BIN": card.samp_bin()}
    for n, (elf, _) in label_templates().items():
        files[f"{watch_dir}/LB{n}.BIN"] = elf                # LB1..LB6.BIN: the label patch, 1 to 6 words
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

