#!/usr/bin/env python3
"""Drive AF experiments on the GR IIIx from the Mac over the camera's Wi-Fi.

The camera runs a remote card (tools/af_debug_card.py --remote): the AF trace
is on, and a small endpoint (busybox httpd + CGI, port 8080) switches the
patch arms and returns the newest trace records. Presses go through the
camera's own API, POST /v1/lens/focus on port 80, which runs the same AF as a
half-press (docs/remote-control.html#6-camera-side-probe).

The Mac's wired network may use the same 192.168.0.x range as the camera's
Wi-Fi, so every request is pinned to the Wi-Fi interface (IP_BOUND_IF).

Commands:
  check                 API and endpoint reachable, clock offset, a few settings
  defocus-test          find how the API sets focusSetting (inf and back)
  press [--arm A]       one press, printed
  session ...           arms in shuffled groups per light band, with spoken
                        "darker" / "brighter" until a test press is in the band
  fetch NAME...         copy files from the card (AFDBG or card root)
  status                uptime, current arm, trace size, free card and memory, uploads
  log [N]               the last N lines of REMOTE.LOG
  put NAME [FILE]       upload a patch into the camera's RAM (gone at power-off),
                        usable in arms at once: FILE, or --sites ADDR:STOCK:NEW,...,
                        or NAME_SITES from af_debug_card.py; --tool for a tool
  rm NAME               restore and delete an uploaded patch (or delete a tool)
  run NAME [ARG...]     run an uploaded tool (30 s limit)
  peek ADDR [N]         N words of RTOS memory (read-only, 0x53000000..0x5fefffff)
"""
import argparse, hashlib, json, random, socket, statistics, struct, subprocess, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import af_debug_card  # noqa: E402
from af_debug_card import AF_FPS, AUTO_PATCHES, arm_conflict, multipatch_bin, trace_presses  # noqa: E402

IP_BOUND_IF = 25          # macOS: <netinet/in.h>
PRIVATE_PROPS = ("ssid", "key", "serialNo", "macAddress", "bdName")
SETTLE_S, DEFOCUS_S = 0.7, 0.8


class Camera:
    def __init__(self, host, iface, api_port=80, cgi_port=8080, timeout=10.0):
        self.host, self.iface, self.api_port, self.cgi_port, self.timeout = host, iface, api_port, cgi_port, timeout
        self.ifindex = socket.if_nametoindex(iface) if iface else 0
        self.offset = None              # camera uptime - Mac monotonic
        self.defocus_method = None

    def http(self, port, method, path, body=b"", ctype="application/x-www-form-urlencoded"):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if self.ifindex:
            s.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, self.ifindex)
        s.settimeout(self.timeout)
        try:
            s.connect((self.host, port))
            head = (f"{method} {path} HTTP/1.0\r\nHost: {self.host}\r\nContent-Type: {ctype}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n").encode()
            s.sendall(head + body)
            chunks = []
            while True:
                c = s.recv(65536)
                if not c:
                    break
                chunks.append(c)
        finally:
            s.close()
        raw = b"".join(chunks)
        hdr, _, data = raw.partition(b"\r\n\r\n")
        status = int(hdr.split(b" ", 2)[1]) if hdr.startswith(b"HTTP") else 0
        return status, data

    def api(self, method, path, body=b"", ctype="application/x-www-form-urlencoded"):
        status, data = self.http(self.api_port, method, path, body, ctype)
        try:
            return status, json.loads(data or b"{}")
        except ValueError:
            return status, {"raw": data.decode("latin-1")[:200]}

    def cgi(self, name, query=""):
        status, data = self.http(self.cgi_port, "GET", f"/cgi-bin/{name}" + (f"?{query}" if query else ""))
        if status != 200:
            raise RuntimeError(f"endpoint {name}: HTTP {status}")
        return data

    def cgi_post(self, name, query, body):
        status, data = self.http(self.cgi_port, "POST", f"/cgi-bin/{name}?{query}", body,
                                 "application/octet-stream")
        if status != 200:
            raise RuntimeError(f"endpoint {name}: HTTP {status}")
        return data

    def shell(self, lines, wait=8.0):
        """Queue engineering-shell lines through the card's relay (allowlist in
        af_debug_card.SHELL_ALLOW) and wait for the RTOS script's ack.
        Returns (sequence, acked)."""
        from af_debug_card import SHELL_ALLOW
        for line in lines:
            if line.split()[0] not in SHELL_ALLOW:
                raise SystemExit(f"not in the relay allowlist: {line}")
        if any(line.split()[0].startswith("ND_") for line in lines):
            self.wait_af_idle()
        reply = self.cgi_post("shell", "", ("\n".join(lines) + "\n").encode()).decode().strip()
        if not reply.startswith("queued "):
            raise SystemExit(f"relay refused: {reply}")
        seq = int(reply.split()[1])
        t0 = time.monotonic()
        while time.monotonic() - t0 < wait:
            for line in self.cgi("status").decode().splitlines():
                f = line.split()
                if len(f) >= 2 and f[0] == "relay_ack" and f[1] == str(seq):
                    return seq, True
            time.sleep(0.3)
        return seq, False

    def wait_af_idle(self, quiet=1.0, timeout=15.0):
        """Wait until the AF action word (CAF +0xc44c, 1 = idle) has read 1 for
        `quiet` seconds. All four relay stalls of 2026-11-19 hit an ND command
        sent right after a press; on the last one the relay heartbeat stopped
        with it, so the RTOS script died in that command."""
        t0 = since = time.monotonic()
        while time.monotonic() - t0 < timeout:
            word = self.peek(AF_ACTION & ~3, 1)[0]
            if (word >> (16 if AF_ACTION & 2 else 0)) & 0xffff != 1:
                since = time.monotonic()
            elif time.monotonic() - since >= quiet:
                return True
            time.sleep(0.1)
        return False

    def uploaded(self, kind="patch"):
        """Names of the patches (or tools) uploaded since power-on."""
        names = []
        for line in self.cgi("status").decode().splitlines():
            f = line.split()
            if len(f) >= 3 and f[0] == "up" and f[1] == kind:
                names.append(f[2].rsplit(".", 1)[0])
        return names

    def peek(self, addr, n=1):
        status, data = self.http(self.cgi_port, "GET", f"/cgi-bin/peek?{addr:x}+{n:x}")
        if status != 200 or len(data) != 4 * n:
            raise RuntimeError(f"peek {addr:#x}+{n}: HTTP {status}, {len(data)} bytes: {data[:80]!r}")
        return list(struct.unpack(f"<{n}I", data))

    def uptime(self):
        return float(self.cgi("uptime").split()[0])

    def sync_clock(self, n=5):
        best = None
        for _ in range(n):
            t0 = time.monotonic(); u = self.uptime(); t1 = time.monotonic()
            if best is None or t1 - t0 < best[0]:
                best = (t1 - t0, u - (t0 + t1) / 2)
        self.offset = best[1]
        return best

    def cam_now(self):
        return time.monotonic() + self.offset

    def props(self):
        return self.api("GET", "/v1/props")[1]

    def set_focus_setting(self, value, method=None):
        path, kind = method or self.defocus_method
        body = json.dumps({"focusSetting": value}).encode() if kind == "json" else f"focusSetting={value}".encode()
        return self.api("PUT", path, body, "application/json" if kind == "json" else
                        "application/x-www-form-urlencoded")

    def find_defocus(self):
        orig = self.props().get("focusSetting")
        # /v1/params/lens with a form body works on the GR IIIx 1.60 (2026-11-19);
        # /v1/params/camera answers "Bad Request".
        for method in (("/v1/params/lens", "form"), ("/v1/params/lens", "json"),
                       ("/v1/params/camera", "form"), ("/v1/params/camera", "json")):
            st, resp = self.set_focus_setting("inf", method)
            time.sleep(0.3)
            now = self.props().get("focusSetting")
            print(f"  PUT {method[0]} ({method[1]}): HTTP {st} {resp.get('errMsg', resp)} -> focusSetting {now}")
            if now == "inf":
                self.set_focus_setting(orig, method)
                time.sleep(0.3)
                back = self.props().get("focusSetting")
                print(f"  back to {orig}: {back}")
                if back == orig:
                    self.defocus_method = method
                    return method, orig
        return None, orig


def run_from_tail(cam, t_cam, timeout=7.0, records=2000, keep=None):
    """Wait for the AF run that started after t_cam to end in the trace tail."""
    deadline = time.monotonic() + timeout
    time.sleep(0.8)
    while True:
        data = cam.cgi("tail", str(records))
        with tempfile.NamedTemporaryFile(suffix=".DAT", delete=False) as f:
            f.write(data)
            path = f.name
        runs = []
        try:
            runs = trace_presses(path)[1]
        except SystemExit:
            pass
        mine = [r for r in runs if r["t"] >= t_cam - 0.3]
        if mine and ("end" in mine[0] or time.monotonic() > deadline):
            if keep:
                Path(path).replace(keep)
            else:
                Path(path).unlink()
            return mine[0], data
        Path(path).unlink()
        if time.monotonic() > deadline:
            return None, data
        time.sleep(0.5)


def press(cam, arm, defocus=True, orig_setting="spot", keep=None):
    status = cam.cgi("arm", arm).decode().split()
    if defocus and cam.defocus_method:
        cam.set_focus_setting("inf"); time.sleep(DEFOCUS_S)
        cam.set_focus_setting(orig_setting); time.sleep(SETTLE_S)
    t_cam = cam.cam_now()
    pos = getattr(cam, "press_pos", None)   # "x,y" in percent of the frame, or None (the set AF point)
    st, resp = cam.api("POST", "/v1/lens/focus", f"pos={pos}".encode() if pos else b"")
    run, _ = run_from_tail(cam, t_cam, keep=keep)
    rec = {"arm": arm, "arm_status": status[1] if len(status) > 1 else "?", "t": round(t_cam, 3),
           "api": st, "captureId": resp.get("captureId")}
    if run is None:
        rec.update(outcome="none")
        return rec
    rec.update(outcome=run["outcome"], inferred=bool(run.get("inferred")), ms=run["ms"], final=run["final"],
               fps=AF_FPS.get(run["level"]), passes=run["passes"], mode=run.get("mode"),
               y=statistics.median(run["y"]) if run["y"] else None,
               scale=max(run["scale"]) if run["scale"] else None, delay=round(run["t"] - t_cam, 3))
    return rec


def say(text, quiet=False):
    print(f">>> {text}")
    if not quiet:
        subprocess.run(["say", text], check=False)


def fmt(r):
    if r["outcome"] == "none":
        return f"{r['arm']:15s} no AF run seen (API {r['api']})"
    y = f"{r['y']:8.0f}" if r["y"] is not None else "       -"
    sc = f"{r['scale']:5.2f}" if r["scale"] is not None else "    -"
    ms = f"{r['ms']:5.0f}" if r["ms"] else "    -"
    return (f"{r['arm']:15s} {r['outcome']:5s}{'*' if r.get('inferred') else ' '}{' WRONG' if r.get('wrong') else '      '}"
            f" {ms} ms  {r['fps'] or '?':>3} fps  Y {y}  scale {sc}  final {r['final']!s:>6s}  status {r['arm_status']}"
            f"  passes {r.get('passes', '?')}  mode {r.get('mode') or '?'}")


def mark_wrong(recs, pulses, ref=None):
    finals = [r["final"] for r in recs if r["outcome"] in ("lock", "done") and r.get("final") is not None]
    if ref is None and not finals:
        return None
    ref = statistics.median(finals) if ref is None else ref
    for r in recs:
        r["wrong"] = (r["outcome"] in ("lock", "done") and r.get("final") is not None
                      and abs(r["final"] - ref) > pulses)
    return ref


def table(recs, arms):
    for band in sorted({r["band"] for r in recs}):
        print(f"\nband {band}:")
        for a in arms:
            rs = [r for r in recs if r["band"] == band and r["arm"] == a]
            if not rs:
                continue
            ok = [r for r in rs if r["outcome"] in ("lock", "done") and not r.get("wrong")]
            ms = [r["ms"] for r in ok if r["ms"]]
            ys = [r["y"] for r in rs if r.get("y")]
            print(f"  {a:15s} {len(rs):3d} presses: {len(ok)} locked, {sum(bool(r.get('wrong')) for r in rs)} wrong, "
                  f"{sum(r['outcome'] == 'fail' for r in rs)} failed, "
                  f"{sum(r['outcome'] in ('abort', 'none') for r in rs)} aborted/none"
                  + (f"; median {statistics.median(ms):.0f} ms" if ms else "")
                  + (f"; Y {statistics.median(ys):.0f}" if ys else ""))


CAF = 0x5506C938
AF_ACTION = CAF + 0xC44C          # halfword, the AF state 1/2/4/8/128 (strh at 0x5340a354); 1 = idle
PHASE_VALUE, PHASE_MIN = CAF + 0x14BAC, 0x55012E74   # phase usable when value >= [PHASE_MIN] (0x533f87dc)
GRADES = CAF + 0x43C0                                # +0x43c1 + area: decision grade


def phase_press(cam, arm, orig):
    """A press that also samples the phase value (+0x14bac) and the area-0
    decision grade while the AF runs: (press result, phase values, grades)."""
    import threading
    vals, grades, done = [], [], threading.Event()
    def sample():
        while not done.is_set():
            try:
                vals.append(cam.peek(PHASE_VALUE, 1)[0])
                grades.append((cam.peek(GRADES, 1)[0] >> 8) & 0xff)
            except (OSError, RuntimeError):
                pass
    th = threading.Thread(target=sample); th.start()
    try:
        r = press(cam, arm, True, orig)
    finally:
        done.set(); th.join()
    return r, vals, grades


def check_arms(arms, extra=()):
    for arm in arms:
        parts = [] if arm == "STOCK" else arm.split("+")
        bad = [p for p in parts if p not in AUTO_PATCHES and p not in extra]
        if bad:
            sys.exit(f"unknown patch in arm {arm}: {', '.join(bad)}")
        group = arm_conflict(parts)
        if group:
            sys.exit(f"arm {arm}: {', '.join(group)} share the same words, use one at a time")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.1")
    ap.add_argument("--iface", default="en0", help="Wi-Fi interface joined to the camera (default en0; '' = no pinning)")
    ap.add_argument("--speak", action="store_true", help="also speak the prompts (macOS say); default print only")
    ap.add_argument("--pos", help="AF point for every press, 'x,y' in percent of the frame (API pos=)")
    ap.add_argument("--api-port", type=int, default=80, help=argparse.SUPPRESS)
    ap.add_argument("--cgi-port", type=int, default=8080, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("defocus-test")
    p = sub.add_parser("press"); p.add_argument("--arm", default="STOCK"); p.add_argument("--no-defocus", action="store_true")
    s = sub.add_parser("session")
    s.add_argument("--arms", default="STOCK,SCALE75,SQRTNS,X875,HALFB,FALLS2,SCALE75+FALLS2")
    s.add_argument("--bands", default="150000,80000,40000",
                   help="target AF signal Y per light band, at 29 fps (default %(default)s)")
    s.add_argument("--fps", type=int, default=29,
                   help="AF frame rate a band needs (default %(default)s, the dim-light rate; 0 = any)")
    s.add_argument("--tolerance", type=float, default=0.35, help="band half-width as a fraction (default %(default)s)")
    s.add_argument("--groups", type=int, default=4, help="shuffled groups of all arms per band (default %(default)s)")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--adjust-s", type=float, default=6.0, help="pause before each test press (default %(default)s)")
    s.add_argument("--wrong-pulses", type=int, default=150)
    s.add_argument("--ref", type=int, default=None,
                   help="in-focus lens position (pulses) for wrong locks; default the median of all locks")
    s.add_argument("--out", type=Path, default=None)
    s.add_argument("--no-defocus", action="store_true")
    s.add_argument("--as-is", action="store_true",
                   help="one band in the light as it is, no test presses or light prompts")
    s.add_argument("--label", default="", help="name for the band in the log and table (with --as-is)")
    f = sub.add_parser("fetch"); f.add_argument("names", nargs="+"); f.add_argument("--out", type=Path, default=Path("."))
    sub.add_parser("status")
    sh = sub.add_parser("shell", help="engineering-shell lines via the card relay (allowlist)")
    sh.add_argument("line", nargs="+", help="one quoted line per command, e.g. 'ND_close'")
    nd = sub.add_parser("nd", help="ND filter in (on) or out (off) via the relay")
    nd.add_argument("state", choices=("on", "off"))
    lg = sub.add_parser("log"); lg.add_argument("n", nargs="?", type=int, default=20)
    pu = sub.add_parser("put"); pu.add_argument("name"); pu.add_argument("file", nargs="?", type=Path)
    pu.add_argument("--sites", help="ADDR:STOCK:NEW[,...] in hex: build a multipatch")
    pu.add_argument("--tool", action="store_true", help="a tool for run, not a patch")
    rmp = sub.add_parser("rm"); rmp.add_argument("name")
    ru = sub.add_parser("run"); ru.add_argument("name"); ru.add_argument("args", nargs="*")
    pk = sub.add_parser("peek"); pk.add_argument("addr"); pk.add_argument("n", nargs="?", default="1")
    me = sub.add_parser("meter", help="presses that report the phase value and grade, for setting up a scene")
    me.add_argument("--n", type=int, default=20); me.add_argument("--arm", default="STOCK")
    me.add_argument("--pause", type=float, default=3.0)
    args = ap.parse_args()

    cam = Camera(args.host, args.iface, args.api_port, args.cgi_port)
    if args.pos:
        x, y = (int(v) for v in args.pos.split(","))
        if not (0 <= x <= 100 and 0 <= y <= 100):
            raise SystemExit("--pos: x,y in 0..100")
        cam.press_pos = f"{x},{y}"
    if args.cmd == "check":
        st, ping = cam.api("GET", "/v1/ping")
        print(f"API ping: HTTP {st} {ping.get('errMsg')}")
        pr = cam.props()
        print("props:", {k: pr.get(k) for k in ("model", "firmwareVersion", "battery", "operationMode",
                                                "focusMode", "focusSetting", "focused", "liveState")})
        rtt, off = cam.sync_clock()
        print(f"endpoint uptime ok, clock offset {off:.3f} s (round trip {1000 * rtt:.0f} ms)")
        print(cam.cgi("ls").decode()[-1500:])
        return
    if args.cmd == "fetch":
        args.out.mkdir(parents=True, exist_ok=True)
        for n in args.names:
            data = cam.cgi("file", n.upper())   # the camera's Linux side sees names in capitals
            (args.out / n).write_bytes(data)
            print(f"{n}: {len(data)} bytes")
        return
    if args.cmd in ("shell", "nd"):
        lines = args.line if args.cmd == "shell" else ["ND_close" if args.state == "on" else "ND_open"]
        seq, ok = cam.shell(lines)
        print(f"relay {seq}: {'acked' if ok else 'NO ACK (relay not running, or the RTOS did not see the file)'}")
        return
    if args.cmd == "status":
        print(cam.cgi("status").decode(), end="")
        return
    if args.cmd == "log":
        print(cam.cgi("log", str(args.n)).decode(), end="")
        return
    if args.cmd == "put":
        name = args.name.upper()
        if args.file:
            body = args.file.read_bytes()
        elif args.sites:
            body = multipatch_bin([tuple(int(x, 16) for x in site.split(":")) for site in args.sites.split(",")])
        elif hasattr(af_debug_card, f"{name}_SITES"):
            body = multipatch_bin(getattr(af_debug_card, f"{name}_SITES"))
        else:
            sys.exit(f"put {name}: give a FILE, --sites, or a name with {name}_SITES in af_debug_card.py")
        reply = cam.cgi_post("put", name + ("+tool" if args.tool else ""), body).decode().strip()
        if reply.split()[:1] not in (["patch"], ["tool"]):
            sys.exit(f"put {name}: {reply}")
        ok = reply.split()[-1:] == [hashlib.md5(body).hexdigest()]
        print(f"{reply}  ({'md5 matches' if ok else 'MD5 DIFFERS: ' + hashlib.md5(body).hexdigest()})")
        if not ok:
            sys.exit(1)
        return
    if args.cmd == "rm":
        print(cam.cgi("rm", args.name.upper()).decode(), end="")
        return
    if args.cmd == "run":
        print(cam.cgi("run", "+".join([args.name.upper()] + args.args)).decode(), end="")
        return
    if args.cmd == "peek":
        addr, n = int(args.addr, 16), int(args.n, 0)
        for i, w in enumerate(cam.peek(addr, n)):
            print(f"{addr + 4 * i:#010x}: {w:#010x}  {struct.unpack('<f', struct.pack('<I', w))[0]:.6g}")
        return
    cam.sync_clock()
    if args.cmd == "defocus-test":
        method, orig = cam.find_defocus()
        print(f"defocus method: {method or 'none found'} (focusSetting was {orig})")
        return
    if args.cmd == "meter":
        check_arms([args.arm], cam.uploaded())
        orig = cam.props().get("focusSetting", "spot")
        cam.find_defocus()
        limit = cam.peek(PHASE_MIN, 1)[0]
        print(f"phase usable from {limit}; grade 0 = not usable")
        for i in range(args.n):
            r, vals, grades = phase_press(cam, args.arm, orig)
            v = min(vals) if vals else None
            g = "".join(str(x) for k, x in enumerate(grades) if k == 0 or x != grades[k - 1])
            print(f"{i + 1:3d} phase {v!s:>5s}  grades {g or '?':5s}  {fmt(r)}")
            say(f"phase {v}, {'grade zero' if v is not None and v < limit else 'grade one'}", not args.speak)
            time.sleep(args.pause)
        return
    if args.cmd == "press":
        check_arms([args.arm], cam.uploaded())
        orig = cam.props().get("focusSetting", "spot")
        if not args.no_defocus:
            cam.find_defocus()
        print(fmt(press(cam, args.arm, not args.no_defocus, orig)))
        return

    # session
    arms = args.arms.split(",")
    check_arms(arms, cam.uploaded())
    bands = [None] if args.as_is else [int(b) for b in args.bands.split(",")]
    out = args.out or Path("work/af-logs") / time.strftime("remote-%Y%m%d-%H%M")
    (out / "tails").mkdir(parents=True, exist_ok=True)
    orig = cam.props().get("focusSetting", "spot")
    if not args.no_defocus:
        method, _ = cam.find_defocus()
        if not method:
            sys.exit("no way found to set focusSetting over the API; run with --no-defocus or check defocus-test")
    log = (out / "presses.jsonl").open("a")
    rng, recs, n = random.Random(args.seed), [], 0
    print(f"session: arms {arms}, bands {bands} (+/-{args.tolerance:.0%}), {args.groups} groups per band -> {out}")
    for bi, target in enumerate(bands, 1):
        if target is None:
            lo, hi = 0, float("inf")
            say("Starting. Leave the light as it is.", not args.speak)
        else:
            lo, hi = target * (1 - args.tolerance), target * (1 + args.tolerance)
            say(f"Light level {bi} of {len(bands)}. Adjust the light.", not args.speak)
        while target is not None:         # test presses until the light is in the band
            time.sleep(args.adjust_s)
            r = press(cam, "STOCK", not args.no_defocus, orig)
            print("  test:", fmt(r))
            if r["outcome"] == "none" or r["y"] is None:
                say("No AF run seen. Check the camera.", not args.speak); continue
            if args.fps and r["fps"] != args.fps:
                say("Darker.", not args.speak); continue
            if r["y"] > hi:
                say("Darker.", not args.speak); continue
            if r["y"] < lo:
                say("A little brighter.", not args.speak); continue
            say("Hold this light.", not args.speak)
            break
        for g in range(args.groups):
            group = list(arms); rng.shuffle(group)
            for arm in group:
                n += 1
                r = press(cam, arm, not args.no_defocus, orig, keep=out / "tails" / f"{n:04d}.DAT")
                r.update(n=n, band=args.label or bi, group=g)
                recs.append(r)
                mark_wrong(recs, args.wrong_pulses, args.ref)
                print(f"{n:4d} b{bi} g{g} {fmt(r)}")
                log.write(json.dumps(r) + "\n"); log.flush()
                if target is not None and r["y"] and not (lo / 1.5 < r["y"] < hi * 1.5):
                    say("The light has changed.", not args.speak)
    ref = mark_wrong(recs, args.wrong_pulses, args.ref)
    print(f"\nreference lens position {ref}; wrong = more than {args.wrong_pulses} pulses off")
    table(recs, arms)
    say("Session done.", not args.speak)


if __name__ == "__main__":
    main()
