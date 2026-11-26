#!/usr/bin/env python3
"""Write recipes into the GR IIIx's Custom 1 / Custom 2 slots over Wi-Fi.

Step 1 of the on-camera recipe idea: the engineering shell's "UserData Set"
(docs/gr-mod-landscape.html §7) writes a slot's image control and parameters.
A card script relays shell lines from Wi-Fi to the RTOS, as the AF remote card
does (tools/af/af_debug_card.py --remote), but this card has nothing else:
no AF logging, no patch loader, no RAM uploads, and the relay accepts only
"UserData" and the AF lamp. Nothing is written to firmware or flash; take the
card out (or delete SCRIPT/STARTUP.TTL) and the camera boots as before.

UserData Set has never been run on this camera. What the slot does with the
values (and whether they survive a power cycle) is what this step tests.

Commands:
  card [--out DIR]          write the card files (copy DIR's contents to the card root)
  check                     endpoint and relay alive (heartbeat), uptime
  lines FILE NAME --slot N  print the UserData lines for a recipe (no camera needed)
  send FILE NAME --slot N   write a recipe into Custom N; --activate also selects Custom N
  raw 'LINE' ...            send UserData / Af_Led lines as they are
  verify FILE NAME          fetch the camera's newest JPEG and compare its maker
                            notes with the recipe (take a photo with Custom N first)
  log [N]                   the last N lines of the card's RECIPE.LOG

The Mac must be on the camera's Wi-Fi; requests are pinned to --iface (en0).
"""
import argparse
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import recipes  # noqa: E402
from af_debug_card import (RELAY_PAD_SH, RELAY_SIZE, REMOTE_PORT, pull_rack_sh, pull_relay_ttl,  # noqa: E402
                           pull_sh, pull_shell_body, ttl_check)
from af_remote import Camera  # noqa: E402

CARD_DIR = "SCRIPT/RECIPE"
D = f"/media/storage1/{CARD_DIR}"
TTL_D = "C:\\SCRIPT\\RECIPE"
WWW = "/tmp/recipewww"
ALLOW = ("UserData", "Af_Led_On", "Af_Led_Off")
MAX_LINES = 8          # per relay batch, as the AF relay
PAUSE_MS = 300         # after each line


# ---- solo runs: the camera works through a script by itself -------------------------
# An arm file NAME.ON next to NAME.SH (in SCRIPT/RECIPE) makes REMOTE.SH run NAME.SH once at
# power-on, before the relay loop starts: it deletes NAME.ON, copies NAME.SH to RAM and starts it
# in the background. Only the first armed name in SOLO_ARMS runs per power-on. The script feeds
# the RTOS through the pull relay like the shell CGI (one batch at a time, the next after the
# ack), but with any engineering-shell line: it comes from the card, not from Wi-Fi. Its log
# lines go to RECIPE.LOG ("solo ..."), written by PULL.SH as every other log line.
SOLO_ARMS = ("SMOKE", "SESSION", "SESSION2")


def solo_lib() -> str:
    """Shell functions for solo scripts, kept in RAM: log, b (one relay batch from stdin)."""
    return (
        f"w={WWW}\n"
        "log() { read t _ </proc/uptime; echo \"solo $* $t\" >>$w/log.pending; }\n"
        "no() { log \"stop: $*\"; exit 1; }\n"
        "nap() { sleep 0.2 2>/dev/null || sleep 1; }\n"
        "b() {\n"
        " k=0; while [ -e $w/relay.next ]; do nap; k=$((k + 1)); [ $k -lt 600 ] || no relay.next not fetched; done\n"
        " q=$(cat $w/relay.seq 2>/dev/null); q=$((${q:-0} + 1))\n"
        " o=$w/solo.tmp; echo \"rseq = $q\" >$o; echo \"if rseq = rdone goto rskip$q\" >>$o\n"
        " cat >>$o\n"
        " echo 'rdone = rseq' >>$o\n"
        f" echo \"sendln 'lcmd sh {WWW}/RACK.SH $q'\" >>$o; echo \":rskip$q\" >>$o\n"
        + "".join(" " + l + "\n" for l in RELAY_PAD_SH.strip().split("\n")) +
        " echo $q >$w/relay.seq; mv $o $w/relay.next\n"
        " k=0; while :; do a=; [ -f $w/relay.ack ] && read a _ <$w/relay.ack; [ \"$a\" = $q ] && break\n"
        "  nap; k=$((k + 1)); [ $k -lt 1200 ] || no batch $q not acknowledged; done\n"
        "}\n")


def solo_frame_bytes() -> int:
    q = 99999
    return (len(f"rseq = {q}\n") + len(f"if rseq = rdone goto rskip{q}\n") + len("rdone = rseq\n")
            + len(f"sendln 'lcmd sh {WWW}/RACK.SH {q}'\n") + len(f":rskip{q}\n"))


def solo_script(name: str, steps) -> bytes:
    """NAME.SH for a solo run. steps: ("log", text), ("cmd", engineering-shell line, pause ms) or
    ("sh", a shell line run on Linux between two batches, e.g. a RAM patch writer).
    Commands between two log or sh steps are packed into as few relay batches as fit one sector."""
    return (f"# solo run {name}: generated, see recipe_card.py solo_script\n. {WWW}/SOLO.LIB\n"
            f"log start {name}\n{solo_body(steps)}log done {name}\n").encode("ascii")


def solo_body(steps) -> str:
    """The shell lines for steps (see solo_script), without the header: also usable inside a shell
    function of a longer script that sources SOLO.LIB itself (WATCH.SH)."""
    budget = RELAY_SIZE - 24 - solo_frame_bytes()
    out = []
    cur = []

    def flush():
        if cur:
            out.append("b <<'END_B'\n" + "".join(cur) + "END_B\n")
            cur.clear()

    for st in steps:
        if st[0] == "log":
            text = st[1]
            if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._+-,=" for c in text):
                raise ValueError(f"log text with unsafe characters: {text!r}")
            flush()
            out.append(f"log {text}\n")
            continue
        if st[0] == "sh":
            if "\n" in st[1] or "END_B" in st[1]:
                raise ValueError(f"bad shell line: {st[1]!r}")
            flush()
            out.append(st[1] + "\n")
            continue
        _, line, pause = st
        if "'" in line or "\n" in line:
            raise ValueError(f"bad command: {line!r}")
        ttl = f"sendln '{line}'\nmpause {int(pause)}\n"
        if len(ttl) > budget:
            raise ValueError(f"command too long for one relay batch: {line}")
        if sum(map(len, cur)) + len(ttl) > budget:
            flush()
        cur.append(ttl)
    flush()
    return "".join(out)


# ---- card --------------------------------------------------------------------------

def startup_ttl() -> bytes:
    """Wait for Linux, start the endpoint, flash the AF lamp twice, then relay."""
    lines = [
        # GRKit: the RTOS builds its host-command tables after the script starts.
        "mpause 3000",
        "sendln 'lcmd_resp on'",
        f"filedelete '{TTL_D}\\BOOT.OK'",
        "attempts = 0",
        ":mark",
        f"sendln 'lcmd sh {D}/MARK.SH'",
        f"filesearch '{TTL_D}\\BOOT.OK'",
        "if result != 0 goto linux",
        "mpause 1000",
        "attempts = attempts + 1",
        "if attempts < 60 goto mark",
        "goto finish",
        ":linux",
        f"sendln 'lcmd sh {D}/REMOTE.SH start'",
        "mpause 4000",
        f"sendln 'lcmd sh {D}/REMOTE.SH check'",
        "mpause 2000",
        f"filesearch '{TTL_D}\\HTTPD.DEAD'",
        "if result = 0 goto served",
        f"sendln 'lcmd sh {D}/REMOTE.SH fg'",
        "mpause 2000",
        ":served",
        "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'", "mpause 500",
        "sendln 'Af_Led_On'", "mpause 500", "sendln 'Af_Led_Off'",
        # The pull relay (af_debug_card.pull_relay_ttl): PULL.SH is the only moment
        # Linux writes the card.
        *pull_relay_ttl(WWW, TTL_D),
        ":finish",
    ]
    ttl_check(lines)
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def card_files() -> dict:
    head = f"#!/bin/sh\nd={D}\nexec 2>>{WWW}/err.log\n"
    text = "printf 'Content-Type: text/plain\\r\\n\\r\\n'\n"
    cgi = {
        "uptime": head + text + "cat /proc/uptime\n",
        "status": head + text + (
            "read u _ </proc/uptime; echo \"uptime $u\"\n"
            f"echo \"relay_seq $(cat {WWW}/relay.seq 2>/dev/null)\"\n"
            f"echo \"relay_ack $(cat {WWW}/relay.ack 2>/dev/null)\"\n"
            f"echo \"relay_hb $(cat {WWW}/relay.hb 2>/dev/null)\"\n"
            "echo \"card recipe\"\n"),
        "log": head + (
            "n=${QUERY_STRING:-20}; case $n in *[!0-9]*) n=20 ;; esac\n"
            + text + f"{{ cat $d/RECIPE.LOG; cat {WWW}/log.pending {WWW}/log.flush 2>/dev/null; }} | busybox tail -n $n\n"),
        # The pull relay's shell CGI (af_debug_card.pull_shell_body), with its own allowlist.
        "shell": head + pull_shell_body(WWW, ALLOW, text, PAUSE_MS, MAX_LINES),
    }
    # Scripts the RTOS runs through lcmd live in RAM, so running them never reads the card.
    rack_sh = pull_rack_sh(WWW)
    # The focus watcher's tag marker (tools/focus/focus_watch.py): it asks in RAM, PULL.SH writes
    # $d/WATCH/TAG, before the batch that sets the tag reaches the RTOS.
    pull_sh_text = pull_sh(WWW, D, "RECIPE.LOG", before=(
        f"if [ -f {WWW}/watch/tag.put ]; then cat {WWW}/watch/tag.put >$d/WATCH/TAG; rm -f {WWW}/watch/tag.put; c=1; fi\n"
        f"if [ -f {WWW}/watch/tag.del ]; then rm -f $d/WATCH/TAG {WWW}/watch/tag.del; c=1; fi\n"))
    here = "".join(f"cat >{WWW}/cgi-bin/{k} <<'END_{k.upper()}'\n{v}END_{k.upper()}\n" for k, v in cgi.items())
    here += f"cat >{WWW}/RACK.SH <<'END_RACK'\n{rack_sh}END_RACK\n"
    here += f"cat >{WWW}/PULL.SH <<'END_PULL'\n{pull_sh_text}END_PULL\n"
    here += f"cat >{WWW}/SOLO.LIB <<'END_SOLO'\n{solo_lib()}END_SOLO\n"
    # At boot the relay loop has not started, so this is the one time REMOTE.SH may touch the card.
    solo = ("solo() {\n"
            f"for s in {' '.join(SOLO_ARMS)}; do\n"
            " [ -f $d/$s.ON ] && [ -f $d/$s.SH ] || continue\n"
            f" rm -f $d/$s.ON; cat $d/$s.SH >{WWW}/solo.sh; echo \"$u solo $s armed\" >>$d/RECIPE.LOG; sync\n"
            f" sh {WWW}/solo.sh </dev/null >/dev/null 2>>{WWW}/err.log &\n"
            " return\n"
            "done\n"
            # WATCH.SH, if there, runs at every power-on when no solo arm is armed: it is not
            # disarmed, and it never writes the card (its log lines go through PULL.SH).
            # Its files in $d/WATCH are copied to RAM here too, while the card is still Linux's alone.
            f"[ -f $d/WATCH.SH ] || return\n"
            f"busybox mkdir -p {WWW}/watch; cp $d/WATCH/* {WWW}/watch/ 2>/dev/null; busybox chmod 755 {WWW}/watch/*\n"
            f"cat $d/WATCH.SH >{WWW}/watch.sh; echo \"$u watch started\" >>$d/RECIPE.LOG; sync\n"
            f"sh {WWW}/watch.sh </dev/null >/dev/null 2>>{WWW}/err.log &\n"
            "}\n")
    remote = (
        f"d={D}\n"
        "exec 2>>$d/ERR.LOG\n"
        "read u _ </proc/uptime\n"
        "setup() {\n"
        f"busybox mkdir -p {WWW}/cgi-bin\n"
        f"o={WWW}/relay.idle; echo 'rseq = 0' >$o\n"
        f"{RELAY_PAD_SH}"
        "cat $o 1<>$d/RELAY.TTL; sync\n"
        + here +
        f"busybox chmod 755 {WWW}/cgi-bin/*\n"
        f"echo 'GR recipe endpoint: /cgi-bin/uptime status log shell' >{WWW}/index.html\n"
        "}\n"
        + solo +
        "case $1 in\n"
        f"start) rm -f $d/HTTPD.DEAD; setup; busybox httpd -p {REMOTE_PORT} -h {WWW}; echo \"$u start $?\" >>$d/RECIPE.LOG; solo ;;\n"
        f"check) if netstat -ltn | grep -q ':{REMOTE_PORT} '; then echo \"$u up\" >>$d/RECIPE.LOG; "
        "else echo dead >$d/HTTPD.DEAD; echo \"$u dead\" >>$d/RECIPE.LOG; fi ;;\n"
        f"fg) echo \"$u fg\" >>$d/RECIPE.LOG; sync; busybox httpd -f -p {REMOTE_PORT} -h {WWW} ;;\n"
        "esac\n"
        "sync\n"
    )
    # RELAY.TTL is not on the card: REMOTE.SH creates it (padded to one sector)
    # before the RTOS loop starts, then only overwrites it in place.
    return {
        "SCRIPT/STARTUP.TTL": startup_ttl(),
        f"{CARD_DIR}/MARK.SH": (f"d={D}\nread t _ </proc/uptime\necho \"$t\" >$d/BOOT.OK\n"
                                "echo \"$t boot\" >>$d/RECIPE.LOG\nexit 0\n").encode(),
        f"{CARD_DIR}/REMOTE.SH": remote.encode("ascii"),
    }


# ---- relay -------------------------------------------------------------------------

def batch_bytes(lines) -> int:
    """Size of the RELAY.TTL the shell CGI writes for these lines (before padding)."""
    q = 99999
    n = len(f"rseq = {q}\n") + len(f"if rseq = rdone goto rskip{q}\n") + len("rdone = rseq\n")
    n += len(f"sendln 'lcmd sh {D}/RACK.SH {q}'\n") + len(f":rskip{q}\n")
    return n + sum(len(f"sendln '{l}'\n") + len(f"mpause {PAUSE_MS}\n") for l in lines)


def batches(lines):
    """Group lines so each RELAY.TTL fits its one sector (with room for the pad line)."""
    out, cur = [], []
    for line in lines:
        if cur and (len(cur) == MAX_LINES or batch_bytes(cur + [line]) > RELAY_SIZE - 24):
            out.append(cur)
            cur = []
        if batch_bytes([line]) > RELAY_SIZE - 24:
            raise SystemExit(f"line too long for the relay: {line}")
        cur.append(line)
    return out + ([cur] if cur else [])


def status(cam: Camera) -> dict:
    """The status CGI as {key: rest of line} (empty string for an empty value)."""
    out = {}
    for line in cam.cgi("status").decode().splitlines():
        k, _, v = line.partition(" ")
        out[k] = v.strip()
    return out


# Stalls (the RTOS loop stopping for good) came from Linux writing the card while the RTOS read
# RELAY.TTL: seq 16 and 42 (2026-11-21), 18, 92, 23, 67 (2026-11-22). Since then the shell CGI
# only queues a batch in RAM, and the RTOS fetches it itself (PULL.SH) before it reads.


def relay(cam: Camera, lines, wait=12.0):
    for line in lines:
        if line.split()[0] not in ALLOW:
            raise SystemExit(f"not allowed on the recipe card: {line}")
    for i, group in enumerate(batches(lines), 1):
        t0 = time.monotonic()
        while True:
            reply = cam.cgi_post("shell", "", ("\n".join(group) + "\n").encode()).decode().strip()
            if reply != "busy" or time.monotonic() - t0 > wait:
                break
            time.sleep(0.2)   # the previous batch is still waiting for the RTOS to fetch it
        if not reply.startswith("queued "):
            raise SystemExit(f"relay refused batch {i}: {reply}")
        seq = int(reply.split()[1])
        t0 = time.monotonic()
        while True:
            st = status(cam)
            if st.get("relay_ack", "").split()[:1] == [str(seq)]:
                break
            if time.monotonic() - t0 > wait:
                raise SystemExit(f"batch {i} (seq {seq}) not acknowledged after {wait:.0f} s: the RTOS loop may have "
                                 f"stopped (heartbeat {st.get('relay_hb', '?')}, uptime {st.get('uptime', '?')}). "
                                 "Power-cycle the camera.")
            time.sleep(0.1)
        print(f"  batch {i}: {len(group)} line(s), ack {seq} after {time.monotonic() - t0:.1f} s")


def recipe_from(file: Path, name: str):
    found = [(n, b) for n, b in recipes.parse(file.read_text()) if n.lower() == name.lower()]
    if not found:
        raise SystemExit(f"no recipe named {name!r} in {file}")
    n, body = found[0]
    errors = recipes.check_recipe(n, body)
    if errors:
        raise SystemExit("\n".join(errors))
    return n, body


# ---- verify ------------------------------------------------------------------------

# Maker-note decoding: tag 0x0247 slots and ImageTone codes, docs/recipe-format.html#camera-check.
TONE = {256: "standard", 257: "vivid", 258: "monotone", 262: "positive_film", 263: "bleach_bypass",
        264: "retro", 267: "negative_film"}
SLOTS = {0: "saturation", 1: "hue", 2: "key", 3: "contrast", 4: "contrast_highlight", 5: "contrast_shadow",
         6: "sharpness", 9: "shading", 10: "clarity"}


def maker_settings(jpeg: Path) -> dict:
    tags = json.loads(subprocess.run(["exiftool", "-j", "-n", "-ImageTone", "-WBShiftAB", "-WBShiftGM", str(jpeg)],
                                     capture_output=True, text=True, check=True).stdout)[0]
    blob = subprocess.run(["exiftool", "-u", "-b", "-Pentax:Pentax_0x0247", str(jpeg)],
                          capture_output=True, check=True).stdout
    slots = struct.unpack("<22h", blob[:44])
    s = {"control": TONE.get(tags.get("ImageTone"), f"ImageTone {tags.get('ImageTone')}")}
    for i, key in SLOTS.items():
        if slots[i] != -32768:
            s[key] = f"{slots[i]:+d}" if slots[i] else "0"
    ab, gm = tags.get("WBShiftAB", 0), tags.get("WBShiftGM", 0)
    s["wb_shift_ab"] = "0" if ab == 0 else (f"b{ab}" if ab > 0 else f"a{-ab}")   # blue positive in the file
    s["wb_shift_gm"] = "0" if gm == 0 else (f"g{gm}" if gm > 0 else f"m{-gm}")
    s["slots"] = list(slots)
    return s


def newest_jpeg(cam: Camera, out: Path) -> Path:
    status, listing = cam.api("GET", "/v1/photos")
    if status != 200:
        raise SystemExit(f"/v1/photos: HTTP {status}")
    files = [(d["name"], f) for d in listing.get("dirs", []) for f in d.get("files", []) if f.upper().endswith(".JPG")]
    if not files:
        raise SystemExit("no JPEG on the card: take a photo with the Custom slot first")
    folder, name = sorted(files)[-1]
    status, data = cam.http(cam.api_port, "GET", f"/v1/photos/{folder}/{name}?size=full")
    if status != 200:
        raise SystemExit(f"download {folder}/{name}: HTTP {status}")
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_bytes(data)
    return path


# ---- main --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.1")
    ap.add_argument("--iface", default="en0", help="Wi-Fi interface joined to the camera ('' = no pinning)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("card")
    c.add_argument("--out", type=Path, default=Path("work/recipe-card"))
    sub.add_parser("check")
    for name in ("lines", "send"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        p.add_argument("name")
        p.add_argument("--slot", type=int, choices=(1, 2), required=True)
        p.add_argument("--activate", action="store_true",
                       help="also send 'UserData Set StillActiveImageControlSet NoN' (select the slot; untested)")
    r = sub.add_parser("raw")
    r.add_argument("line", nargs="+")
    v = sub.add_parser("verify")
    v.add_argument("file", type=Path)
    v.add_argument("name")
    v.add_argument("--out", type=Path, default=Path("work/recipe-tests"))
    lg = sub.add_parser("log")
    lg.add_argument("n", nargs="?", type=int, default=20)
    a = ap.parse_args()

    if a.cmd == "card":
        for rel, data in card_files().items():
            f = a.out / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        print(f"wrote {a.out}/: SCRIPT/STARTUP.TTL and {CARD_DIR}/. Copy both to the card root "
              "(an existing SCRIPT/STARTUP.TTL is replaced; keep a copy). Without the card the camera boots as before.")
        return

    if a.cmd in ("lines", "send"):
        n, body = recipe_from(a.file, a.name)
        u = recipes.userdata(body, a.slot)
        lines = u["lines"] + ([f"UserData Set StillActiveImageControlSet No{a.slot}"] if a.activate else [])
        print(f"[{n}] -> Custom {a.slot}: {len(lines)} line(s) in {len(batches(lines))} batch(es)")
        for line in lines:
            print("  " + line)
        for key, why in u["skipped"]:
            print(f"  not sent: {key} = {body[key]} ({why})")
        if a.cmd == "lines":
            return

    cam = Camera(a.host, a.iface)
    if a.cmd == "check":
        st = status(cam)
        print("\n".join(f"{k} {v}" for k, v in st.items()))
        if st.get("card") != "recipe":
            raise SystemExit("this is not the recipe card's endpoint")
        up, beat = float(st["uptime"]), st.get("relay_hb", "")
        age = up - float(beat) if beat else None
        print("relay loop:", "no heartbeat yet" if age is None else f"last heartbeat {age:.0f} s ago"
              + ("" if age < 45 else " (stopped? power-cycle the camera)"))
    elif a.cmd == "send":
        relay(cam, lines)
        print("sent. Check on the camera: image control Custom", a.slot, "and its settings.")
    elif a.cmd == "raw":
        relay(cam, a.line)
    elif a.cmd == "verify":
        n, body = recipe_from(a.file, a.name)
        jpeg = newest_jpeg(cam, a.out)
        got = maker_settings(jpeg)
        print(f"{jpeg.name}: maker-note slots {got.pop('slots')}")
        print(f"{'setting':22s} {'recipe':10s} photo")
        bad = 0
        for key in ["control", *recipes.PARAMS]:
            if key not in body or (key not in got and key != "control"):
                continue
            want = body[key]
            mark = "" if got.get(key) == want else "   <-- differs"
            bad += bool(mark)
            print(f"{key:22s} {want:10s} {got.get(key, '-')}{mark}")
        unchecked = [k for k in body if k not in got and k not in ("control", "author", "source", "note")]
        if unchecked:
            print("not in these maker-note fields (check on the camera):", ", ".join(unchecked))
        print("all checked settings match" if not bad else f"{bad} setting(s) differ")
    elif a.cmd == "log":
        print(cam.cgi("log", str(a.n)).decode(), end="")


if __name__ == "__main__":
    main()
