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
Snap meter (save the U mode with AF-S and the Fn button on AF/Snap toggle): half-press on the subject;
the camera switches to Snap at its distance. Its part of WATCH.SH, its constants and RAM patches are in
snap_meter.py (docs/snap-meter.html).

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
import snap_meter as sm  # noqa: E402
import recipe_card as rc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WATCH_DIR = "SCRIPT/RECIPE/WATCH"
RAM = f"{rc.WWW}/watch"
U_MODES = ("U1", "U2", "U3", "U4", "U5", "none")
FOCUS_NAMES = p3.FOCUS_NAMES
SLOT_CM = [cm for cm, _ in fb.SLOTS]
MIN_FRAMES, MACRO_TIMEOUT_S = 5, 60
PLACEHOLDER = 0x5A5A0000
OFF_CUR = 0xC6     # focus mode in use (setter 0x532b03b4; read by 0x5326c5f8 for ApplySnapDistance)


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


def fill(elf, rows, mms):
    """What the shell's mkround does, for the tests: the template with the movw words written in."""
    b = bytearray(elf)
    for off, cond, j in rows:
        b[off:off + 4] = fp.movw_r1(mms[j], cond).to_bytes(4, "little")
    return bytes(b)


def ttl(line, pause):
    return p3.ttl(line, pause)


SCRIPT_HEAD = r"""# focus modes on the dial: generated by tools/focus/focus_watch.py
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
"""

# The watcher's tag functions, the bracket and the macro mode.
SCRIPT_MACRO = r"""tagcheck() { ztag=; obj || { log tag off no settings; return; }; $W/PEEK.BIN $(printf %x $((zo + @CPYWORD@))) 1 >$w/watch.pk 2>/dev/null; zrc=$?; zsz=$(busybox wc -c <$w/watch.pk); zb=$(busybox hexdump -v -C $w/watch.pk | busybox awk 'NR == 1 { print $3 }'); za0=$(( ($(at $zo @ATTWORD@) >> 24) & 255 )); if [ "$zb" = 00 ]; then ztag=1; log tag on attach $za0; else zb2=set; [ -n "$zb" ] || zb2=unread; log tag off copyright $zb2 peek $zrc bytes $zsz attach $za0; fi; }
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
"""

# The main loop: follows the dial, hands each round to its mode.
SCRIPT_MAIN = r"""log watch start macro $ZMACRO meter $ZMETER
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

SCRIPT = SCRIPT_HEAD + sm.SH_HELPERS + SCRIPT_MACRO + sm.SH_MODE + SCRIPT_MAIN




def watch_sh(macro="U1", meter="U2"):
    t = templates()
    tpl = " ".join(f"{k}) printf '" + "".join(f"{off} {cond} {j}\\n" for off, cond, j in rows) + "' ;;"
                   for k, (_, rows) in t.items())
    frame = "".join(ttl(line.replace("{slot}", "%s"), p) for line, p in [
        ("UserData Set StillSnapDistanceCm {slot}", fb.SET_PAUSE), ("UserData Set StillFocus Infinity", fb.SWITCH_MS),
        ("UserData Set StillFocus Snap", fb.SWITCH_MS)] + [(c[1], c[2]) for c in fb.press()])
    subs = {
        "@WWW@": rc.WWW, "@D@": rc.D, "@RAM@": RAM, "@MACRO@": macro, "@METER@": meter,
        "@SETTINGS@": f"{p3.SETTINGS_PTR:x}", "@LENSROOT@": f"{p3.LENS_ROOT:x}", "@LENSA@": f"{p3.LENS_A:#x}",
        "@LENSMM@": f"{p3.LENS_MM:#x}", "@OFFFOCUS@": f"{p3.OFF['focus']:#x}", "@OFFSNAP@": f"{p3.OFF['snap']:#x}",
        "@CPYWORD@": f"{p3.OFF_COPYRIGHT - 1:#x}", "@ATTWORD@": f"{p3.OFF_ATTACH - 3:#x}",
        "@FNAMES@": " ".join(f"{c}) echo {n} ;;" for c, n in FOCUS_NAMES.items()),
        "@BLINK@": ttl("Af_Led_On", 150) + ttl("Af_Led_Off", 350),
        "@LAMPON@": ttl("Af_Led_On", 300), "@LAMPEND@": ttl("Af_Led_On", 3000) + ttl("Af_Led_Off", 300),
        "@SWITCH@": str(fb.SWITCH_MS),
        "@OFFCURW@": f"{OFF_CUR - 2:#x}", "@FRAME@": frame, "@TPL@": tpl,
        "@MINMM@": str(fp.MIN_MM), "@NEARMM@": str(int(fb.NEAR_MIN_M * 1000)),
        "@COC@": repr(fb.COC_M), "@FOCAL@": repr(fb.FOCAL_M), "@STEP@": repr(1 - 0.25),
        "@MINFRAMES@": str(MIN_FRAMES), "@MAXFRAMES@": str(fb.MAX_FRAMES), "@ROUND@": str(fb.ROUND),
        "@SLOTS@": " ".join(map(str, SLOT_CM)), "@PAUSE@": str(fb.SET_PAUSE), "@TIMEOUT@": str(MACRO_TIMEOUT_S),
    }
    subs.update(sm.subs())
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
    files.update(sm.card_files(WATCH_DIR))           # SAMP.BIN, LB1..LB6.BIN
    files[f"{WATCH_DIR}/CPTAG.BIN"] = card.multipatch_bin([p3.COPYRIGHT_LEN, p3.COPYRIGHT_END])
    files[f"{WATCH_DIR}/CPEMPTY.BIN"] = card.multipatch_bin([p3.COPYRIGHT_EMPTY])
    return files


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
            text = sm.trace_word(name, new) if old == new else f"{sm.trace_word(name, old)} -> {sm.trace_word(name, new).split(' ', 1)[1]}"
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
