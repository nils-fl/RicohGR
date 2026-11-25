#!/usr/bin/env python3
"""Focus tolerance curve: how much sharpness a photo loses per lens pulse away from the AF lock.

Hands-free, on a camera with the flashed AF build and the flashed bench card (af_debug_card.py
--flashed). Per trial, over Wi-Fi and the card's pull relay:

  1. RAM trace on, half-press (SW_sws 1): AF-S locks; the trace gives the lock position (+0xc434
     at action 128, AF pulses);
  2. AF_drive <dir> <n> moves the focus motor n of its pulses away from the lock;
  3. full press (SW_swr 1/0) takes the photo without refocusing (checked: no AF run after it);
  4. the new JPEG is downloaded and its sharpness measured on a grid of patches.

AF_drive's pulse is not necessarily the AF's pulse. Both scales are fitted together offline:
sharpness = f(lock + c * drive), with the scatter of the locks (in AF pulses) on a still scene
giving c (af_tolerance.py analyse).

Sharpness is measured at four scales (the JPEG block-averaged 1x, 2x, 4x, 8x before the
Laplacian): pixel-level detail is gone within about 25 AF_drive pulses at f/2.8, so the coarser
scales carry the curve further out (2026-11-22).

  af_tolerance.py run --out work/af-logs/tolerance/f28 --av 2.8 [--reps 2] [--seed 1]
  af_tolerance.py analyse work/af-logs/tolerance/f28
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_debug_card  # noqa: E402
import af_remote as R  # noqa: E402

OFFSETS = (0, -4, 4, -8, 8, -12, 12, -16, 16, -24, 24, -32, 32, -48, 48)   # AF_drive pulses; - = dir 0
SCALES = (1, 2, 4, 8)
TRACE_S = af_debug_card.FLASHED_TRACE_RECORDS * 0.004
GRID = (6, 4)                                                     # patches across, down


def s32(v):
    return v - (1 << 32) if v >= 1 << 31 else v


def lock_of(path):
    """Absolute lock positions (+0xc434 when action 128 starts) of the AF runs in a tail."""
    out, prev = [], None
    for t, w in af_debug_card.trace_records(path):
        act = w[0] & 0xffff
        if act == 128 and prev != 128:
            out.append((round(t, 3), s32(w[6])))
        prev = act
    return out


def sharpness(jpeg):
    """{scale: per-patch sharpness} for SCALES."""
    g = np.asarray(Image.open(jpeg).convert("L"), dtype=np.float32)
    out = {}
    for k in SCALES:
        h, w = (g.shape[0] // k) * k, (g.shape[1] // k) * k
        out[str(k)] = sharpness_of(g[:h, :w].reshape(h // k, k, w // k, k).mean(axis=(1, 3)))
    return out


def sharpness_of(g):
    """Mean squared 4-neighbour Laplacian over mean brightness squared, per grid patch."""
    h, w = g.shape
    lap = (g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1])
    gx, gy = GRID
    res = []
    for j in range(gy):
        for i in range(gx):
            ys, xs = slice(j * (h - 2) // gy, (j + 1) * (h - 2) // gy), slice(i * (w - 2) // gx, (i + 1) * (w - 2) // gx)
            m = float(g[1:-1, 1:-1][ys, xs].mean()) or 1.0
            res.append(float((lap[ys, xs] ** 2).mean()) / (m * m))
    return res


def jpegs(cam):
    st, listing = cam.api("GET", "/v1/photos")
    if st != 200:
        raise SystemExit(f"/v1/photos: HTTP {st}")
    return sorted((d["name"], f) for d in listing.get("dirs", []) for f in d.get("files", [])
                  if f.upper().endswith(".JPG"))


def camera_idle(cam, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        p = cam.props()
        if not p.get("capturing") and p.get("stateStill") == "idle":
            return True
        time.sleep(0.5)
    return False


def trial(cam, offset, out, n):
    """The lens is driven back by the same amount after the shot: AF_drive moves the motor
    without updating the AF's own position, so an undriven lens would start the next search
    off its believed position (seen: 4 of 11 searches aborted after drives of 100-400)."""
    if not cam.wait_af_idle(quiet=0.5, timeout=10) or not camera_idle(cam):
        raise SystemExit("AF or camera not idle")
    before = set(jpegs(cam))
    cam.cgi("trace")
    t_trace = time.monotonic()
    time.sleep(0.3)
    ev = {}
    try:
        ev["on"] = R.relay_now(cam, "SW_sws 1")
        time.sleep(1.3)
        if offset:
            ev["drive"] = R.relay_now(cam, f"AF_drive {0 if offset < 0 else 1} {abs(offset)}")
            time.sleep(0.8)
        ev["shot"] = R.relay_now(cam, "SW_swr 1")
        time.sleep(0.5)
        ev["shot_off"] = R.relay_now(cam, "SW_swr 0")
        if offset:
            time.sleep(0.3)
            ev["back"] = R.relay_now(cam, f"AF_drive {1 if offset < 0 else 0} {abs(offset)}")
            time.sleep(0.8)
    finally:
        ev["off"] = R.relay_now(cam, "SW_sws 0") or R.relay_now(cam, "SW_sws 0")
    if None in ev.values():
        raise SystemExit(f"relay ack missing: {ev}")
    # The card's trace_running flag stays set after the tracer ends (a zombie answers kill -0),
    # so wait out the trace length instead.
    time.sleep(max(0.0, t_trace + TRACE_S + 0.5 - time.monotonic()))
    trace_ok = True
    tail = out / f"t{n:03d}.DAT"
    tail.write_bytes(cam.cgi("tail", str(af_debug_card.FLASHED_TRACE_RECORDS)))
    locks = lock_of(str(tail))
    new = []
    for _ in range(40):
        new = [x for x in jpegs(cam) if x not in before]
        if new:
            break
        time.sleep(0.5)
    if len(new) != 1 and not locks:
        raise SystemExit("no AF run and no photo: the camera ignores the relayed presses (asleep?)")
    if len(new) != 1:
        return dict(n=n, offset=offset, file=None, locks=locks, refocused=False, trace_ok=trace_ok,
                    lock=locks[0][1] if locks else None, ev={k: round(v, 3) for k, v in ev.items()}, sharp=None)
    folder, name = new[0]
    st, data = cam.http(cam.api_port, "GET", f"/v1/photos/{folder}/{name}?size=full")
    if st != 200:
        raise SystemExit(f"download {name}: HTTP {st}")
    (out / name).write_bytes(data)
    after_shot = [lk for lk in locks if lk[0] > ev["on"] + 0.5 + 0.3]   # an AF run after the drive
    sh = sharpness(out / name)
    return dict(n=n, offset=offset, file=name, locks=locks, refocused=bool(after_shot),
                trace_ok=trace_ok, lock=locks[0][1] if locks else None,
                ev={k: round(v, 3) for k, v in ev.items()}, sharp=sh["1"], sharp_s=sh)


def set_av(cam, av):
    st, _ = cam.api("PUT", "/v1/params/camera", f"av={av}".encode(), "application/x-www-form-urlencoded")
    time.sleep(0.5)
    if cam.props().get("av") != av:
        raise SystemExit(f"could not set av {av} (HTTP {st})")


def cmd_run(a):
    cam = R.Camera(a.host, a.iface)
    cam.sync_clock()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if cam.status().get("card") != "flashed":
        raise SystemExit("needs the flashed bench card")
    bad = R.flashed_mismatches(cam, False)
    if bad:
        raise SystemExit(f"flashed words: {bad}")
    av0 = cam.props().get("av")
    if a.av:
        set_av(cam, a.av)
    p = cam.props()
    print(f"av {p.get('av')} tv {p.get('tv')} sv {p.get('sv')} focus {p.get('focusSetting')}", flush=True)
    if p.get("focusSetting") != "spot":
        raise SystemExit("set focus to spot (AF-S) first")
    offsets = tuple(int(x) for x in a.offsets.split(",")) if a.offsets else OFFSETS
    rng = random.Random(a.seed)
    order = []
    for _ in range(a.reps):
        block = list(offsets)
        rng.shuffle(block)
        order += block
    done = sum(1 for _ in open(out / "trials.jsonl")) if (out / "trials.jsonl").exists() else 0
    fails = 0
    for n, off in enumerate(order[done:], done + 1):
        rec = trial(cam, off, out, n)
        if rec["file"] is None:
            fails += 1
            print(f"{n:3d}/{len(order)} drive {off:+4d}: NO PHOTO (lock {rec['lock']})", flush=True)
            if fails >= 3:
                raise SystemExit("3 trials without a photo")
        rec.update(av=p.get("av"))
        with open(out / "trials.jsonl", "a") as f:
            f.write(json.dumps(rec) + "\n")
        if rec["file"]:
            print(f"{n:3d}/{len(order)} drive {off:+4d} lock {rec['lock']} {rec['file']}"
                  f"{' REFOCUSED' if rec['refocused'] else ''}", flush=True)
        if n % 10 == 0 and R.flashed_mismatches(cam, False):
            raise SystemExit("flashed words changed")
    print("mismatch:", R.flashed_mismatches(cam, False) or "none")
    if a.av and a.restore_av:
        set_av(cam, av0)
        print("av back to", av0)


def cmd_analyse(a):
    """Per scale: the patches whose sharpness peaks at drive 0 (the AF area and what lies at its
    distance), a Gaussian in AF_drive pulses fitted to their normalised sharpness, and the drive
    at which 10 % and 50 % of the peak (above the far baseline) are lost. AF pulses = drive x c."""
    from scipy.optimize import curve_fit
    recs = [json.loads(x) for x in open(Path(a.dir) / "trials.jsonl")]
    recs = [r for r in recs if r["lock"] is not None and not r["refocused"] and r.get("sharp_s")]
    off = np.array([r["offset"] for r in recs], float)
    lock = np.array([r["lock"] for r in recs], float)
    print(f"{len(recs)} trials with a lock and a photo; locks {lock.min():.0f}..{lock.max():.0f} AF pulses "
          f"(median abs deviation {np.median(np.abs(lock - np.median(lock))):.0f}); c = {a.c} AF pulse per drive pulse")
    gauss = lambda x, h, x0, w, b: b + h * np.exp(-0.5 * ((x - x0) / w) ** 2)
    result = {}
    for k in SCALES:
        S = np.array([r["sharp_s"][str(k)] for r in recs])
        rows = []
        for q in range(S.shape[1]):
            med = {o: float(np.median(S[off == o, q])) for o in sorted(set(off))}
            pk = max(med, key=med.get)
            drop = med[pk] / max(min(med[min(med)], med[max(med)]), 1e-12)
            if abs(pk) <= a.peak_tol and drop >= 1.5:
                y = S[:, q] / med[pk]
                try:
                    pr, _ = curve_fit(gauss, off, y, p0=(0.8, 0.0, 15.0, 0.2), maxfev=20000,
                                      bounds=((0, -60, 1, -0.5), (5, 60, 500, 1.5)))
                except RuntimeError:
                    continue
                r2 = 1 - float(((y - gauss(off, *pr)) ** 2).sum()) / float(((y - y.mean()) ** 2).sum())
                rows.append((q, pk, drop, pr, r2))
        print(f"\nscale {k}x: {len(rows)} patches peak within {a.peak_tol} drive pulses of the lock")
        for q, pk, drop, (h, x0, w, bb), r2 in rows:
            # loss of the peak height above baseline: 10 % at 0.459 w, 50 % at 1.177 w
            print(f"  patch {q:2d} ({q % GRID[0]},{q // GRID[0]}): drop x{drop:5.1f}, centre {x0:+5.1f}, sigma {w:5.1f} drive"
                  f" = {w * a.c:4.1f} AF p; -10 % at {0.459 * w * a.c:4.1f}, -50 % at {1.177 * w * a.c:4.1f} AF p; R2 {r2:.2f}")
        if rows:
            ws = [w for *_, (h, x0, w, bb), r2 in rows if r2 > 0.5]
            if ws:
                result[k] = float(np.median(ws))
                print(f"  median sigma {result[k]:.1f} drive = {result[k] * a.c:.1f} AF pulses "
                      f"(-10 % within {0.459 * result[k] * a.c:.1f}, -50 % within {1.177 * result[k] * a.c:.1f} AF pulses)")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--reps", type=int, default=2)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--av", help="aperture for the run, e.g. 2.8")
    r.add_argument("--restore-av", action="store_true", help="set the aperture back afterwards")
    r.add_argument("--offsets", help="comma-separated AF_drive offsets (default %s)" % ",".join(map(str, OFFSETS)))
    r.add_argument("--host", default="192.168.0.1")
    r.add_argument("--iface", default="en0")
    an = sub.add_parser("analyse")
    an.add_argument("dir")
    an.add_argument("--c", type=float, default=0.5, help="AF pulses per AF_drive pulse (default %(default)s)")
    an.add_argument("--peak-tol", type=int, default=8, help="patch peak within this many drive pulses of 0")
    a = ap.parse_args()
    {"run": cmd_run, "analyse": cmd_analyse}[a.cmd](a)


if __name__ == "__main__":
    main()
