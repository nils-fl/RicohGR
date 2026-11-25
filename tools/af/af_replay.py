#!/usr/bin/env python3
"""Offline replay of recorded contrast-AF passes (GR IIIx 1.60): test other focus rules
without the camera.

Data: the remote sessions under work/af-logs/<session>/, presses.jsonl plus tails/NNNN.DAT
(tracer v3, with the detector arrays), as tools/af/af_remote.py writes them. For every press the
tool takes the AF run that af_remote matched, splits it into passes (action 4) and rebuilds,
for both AF areas (0 narrow, 1 wide):

  pos  lens position per sample, relative to the pass start (detector +0)
  arr  the detector's value array (detector +4): the smoothed value
       (b[i-1] + 2 b[i] + b[i+1]) / 4, with the prediction (b[n-2] + 3 b[n-1]) / 4 as the
       newest entry
  b    the noise-normalised contrast per sample, solved by least squares from arr and the
       b0/b4/b8 fields of the records
  T    the margin f x scale (+0x4c x +0x80) per frame; the floor +0x28 follows from the arm
       and the detector mode

Commands (all read-only, offline):
  gate   the detector port (0x5340dbfc) against the camera's own rise/fall counts and state
         byte +0x26, frame by frame
  rules  stop rules against the recorded camera behaviour
  fits   peak-fit accuracy (3-point parabola, 5-point quadratic, Gaussian) by split-half
         sampling
  turns  the camera's turn on the first pass ("peak behind", 0x5340d100 without a candidate)
         against the recorded turns, and what turning sooner or jumping back would save

What the replay can say: it is open loop and has only the frames the camera took. A rule
that stops EARLIER than the camera used nothing the camera did not see, so its time saving
and its lock are exact. A rule that accepts a pass the camera rejected is checked against
the camera's later lock in the same press (positions chain across a turn with an offset of
about 14 pulses, measured); in presses the camera failed there is nothing to check against.
Turn rules can be replayed only up to the turn: an earlier turn shortens the pass and the
retrace by the same number of frames, but what a turn away from a real peak would cost is not
in the recording (command turns). The phase value is not in the tails, so a phase prior cannot
be tested here. The camera's stop (the accept test 0x5340d100 after the
detector, and 0x5340afc0) is ported and gated against the recorded stops, but two of its
inputs are not traced (the +0xac setup byte and the brightness array), so the rules are still
measured against the camera's recorded stop.

See docs/af-subsystem.html#offline-replay.
"""
import argparse
import glob
import json
import math
import statistics as st
import struct
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_debug_card as card  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "work" / "af-logs"
NF = len(card.TRACE_OFFSETS)
W = card.TRACE_ARRAY_WORDS
# Per area: record word indices of b0, b4, b8, the rise/fall counts, the detector header
# (+0xc samples, +0x18, +0x1c, +0x20, +0x24 words); see card.TRACE_OFFSETS.
AREA_WORDS = ((18, 19, 20, 12, 29), (25, 26, 27, 28, 34))
# +0xf40 + area, written by the frame loop 0x533fd9b8 when an area accepts: 0 the horizontal
# detector (+0x102c), 1 the vertical one (+0x2724, not traced), 2 both; 3 = no result.
F40 = card.TRACE_OFFSETS.index(0x0F40)
F7C = card.TRACE_OFFSETS.index(0x0F7C)      # area index; -2 = stop the sweep, decide at the end
C434 = card.TRACE_OFFSETS.index(0xC434)     # lens position where the sweep was stopped
NO_SIGNAL = {"remote-g0real", "remote-nd1"}    # docs: no usable peak in these scenes
TURN_OFFSET = 14     # pulses: next pass peak = previous pass end - peak + 14 (measured, IQR 13-21)
SAME_PEAK = 40       # pulses: a lock this close to the camera's lock is the same peak
SKIP_ARMS = ("NOFB", "NOFO", "OFFS")    # guard and offset patches the port does not model

f32 = lambda x: struct.unpack("<f", struct.pack("<I", x))[0]
signed = lambda x: x - (1 << 32) if x >> 31 else x


# ---------------------------------------------------------------- loading

def passes_of(records, t_from, t_to):
    """The passes (runs of action 4) between t_from and t_to: per pass the path, drive level,
    detector mode, the next action and area index, and per area the recorded b values by
    sample index, the per-frame (n, rise, fall, T, state +0x26) and the final arrays. Also
    minus2, the area-0 sample count of the first record with area index -2 (None if none), and
    for a first pass turn_pos, the first lens position +0xc434 the camera stored after it."""
    out, cur = [], None
    for t, w in records:
        act = w[0] & 0xffff
        if out and out[-1]["path"] == 0 and "turn_pos" not in out[-1] and t > out[-1]["t_end"] \
                and signed(w[C434]) != 0 and t <= t_to + 0.05:
            out[-1]["turn_pos"] = signed(w[C434])
        if act == 4 and t_from - 0.05 <= t <= t_to + 0.05:
            if cur is None:
                cur = dict(t=t, path=(w[1] >> 16) & 0xff, level=(w[10] >> 16) & 0xff, minus2=None,
                           areas=[dict(b={}, frames=[]) for _ in (0, 1)])
            if signed(w[F7C]) == -2 and cur["minus2"] is None:
                cur["minus2"] = w[AREA_WORDS[0][4]]
            for a, (i0, i4, i8, ic, ih) in enumerate(AREA_WORDS):
                n, A = w[ih], cur["areas"][a]
                for k, idx in ((n - 1, i8), (n - 2, i4), (n - 3, i0)):
                    if k >= 0:
                        A["b"][k] = f32(w[idx])
                frame = (n, w[ic] & 0xffff, w[ic] >> 16, f32(w[21]) * f32(w[22]), (w[ih + 4] >> 16) & 0xff)
                if A["frames"] and A["frames"][-1][0] == n:
                    A["frames"][-1] = frame     # the last record of a frame has its final counts
                else:
                    A["frames"].append(frame)
            cur.update(mode=w[32] & 0xff, T=f32(w[21]) * f32(w[22]), t_end=t, last=w)
        elif cur is not None:
            pw = cur.pop("last")
            # The frame that ends a pass adds its sample and changes the action in one go, so
            # that sample is only in the first record after the pass (unless a new pass has
            # already reset the detector).
            if all(0 <= w[ih] - pw[ih] <= 1 for *_, ih in AREA_WORDS):
                pw = w
            cur["f40"] = [(w[F40] >> (8 * k)) & 0xff for k in (0, 1)]
            for a, (*_, ih) in enumerate(AREA_WORDS):
                A, cnt = cur["areas"][a], min(pw[ih], W)
                A["n"] = pw[ih]
                A["pos"] = [signed(x) for x in pw[NF + 2 * a * W:][:cnt]]
                A["arr"] = [f32(x) for x in pw[NF + (2 * a + 1) * W:][:cnt]]
            cur.update(act=act, area=signed(w[5]))
            out.append(cur)
            cur = None
    return out


def sessions_with_tails():
    return sorted(Path(p).parent.name for p in glob.glob(str(LOGS / "*" / "presses.jsonl"))
                  if glob.glob(str(Path(p).parent / "tails" / "*.DAT")))


def load(sessions):
    """Presses of the given sessions that have a v3 tail and a complete AF run."""
    presses = []
    for s in sessions:
        lines = (LOGS / s / "presses.jsonl").read_text().splitlines()
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            rec = json.loads(line)
            tail = LOGS / s / "tails" / f"{i + 1:04d}.DAT"
            if not tail.exists():
                continue
            try:
                records = card.trace_records(tail)
                if len(records[0][1]) < NF + 4 * W:
                    continue
                runs = card.trace_presses(tail)[1]
            except SystemExit:
                continue
            mine = [u for u in runs if u["t"] >= rec["t"] - 0.3]   # as af_remote.run_from_tail
            if not mine or "end" not in mine[0]:
                continue
            u = mine[0]
            presses.append(dict(session=s, n=i + 1, arm=rec["arm"], outcome=u["outcome"], t=u["t"],
                                end=u["end"], ms=u["ms"], final=u["final"], wrong=bool(rec.get("wrong")),
                                passes=passes_of(records, u["t"], u["end"])))
    return presses


def bsolve(A):
    """The b series of one area in one pass, by least squares from the final array and the
    b values the records saw (a frame can fall between two 4 ms records at 200 fps)."""
    arr = A["arr"]
    n = len(arr)
    if n < 3 or any(abs(x) > 5 for x in arr):     # a stale entry (seen: 3285.3) breaks the pass
        return None
    rows, rhs = [], []

    def eq(coefs, y, weight=1.0):
        r = np.zeros(n)
        for k, c in coefs:
            r[k] = c
        rows.append(r * weight)
        rhs.append(y * weight)
    for k, v in A["b"].items():
        if 0 <= k < n:
            eq([(k, 1.0)], v, 2.0)
    for i in range(1, n - 1):
        eq([(i - 1, .25), (i, .5), (i + 1, .25)], arr[i])
    eq([(n - 2, .25), (n - 1, .75)], arr[-1])
    eq([(0, 1.0)], arr[0], 0.5)
    return list(np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)[0])


# ---------------------------------------------------------------- the camera's detector

def floor_of(arm, mode):
    """+0x28, the absolute floor of the margin: table 0x53c16d14 by detector mode (mode 0 =
    phase grade 0), changed by the RISE05/RISE10 patches and by the OPT data words."""
    if mode != 0:
        return 0.001
    return 0.005 if "RISE05" in arm else 0.01 if "RISE10" in arm else 0.014 if "OPT" in arm else 0.02


class Detector:
    """Port of the contrast detector update 0x5340dbfc (horizontal, one AF area), called once
    per frame with the value array as the camera holds it at that frame, and of the accept
    test that follows it in 0x5340def0. Constants: falls before the fall test 0x53c16bd0 = 1,
    rises for the rise test 0x53c16c50 = 2, rises for the return value 0x53c16cc0 = 3
    (FALLS2: 2). Not ported: the second-peak test 0x5340db4c (off unless +0xae).

    +0xc0 is the peak candidate: set at a new maximum from sample 3 on if the value has risen
    more than floor + T above the pass minimum (with +0xac set: from sample 2 on, without that
    test), cleared by a second rise in a row and by a rejection."""

    def __init__(self, ac=False):
        self.rise = self.fall = self.f1d = self.state = 0
        self.max_i = self.min_i = 0
        self.c0, self.ac = 0, ac

    def update(self, v, i, floor, T, rises_r0=3):
        cur, r0 = v[i], False
        if v[i - 1] < cur:
            self.rise += 1
            if self.f1d:                        # second rise in a row: clear the falls
                self.fall, self.f1d, self.c0 = 0, 0, 0
            else:                               # a single rise only takes one fall back
                self.fall, self.f1d = max(self.fall - 1, 0), 1
            if cur > v[self.max_i]:
                self.max_i = i
                if i >= (2 if self.ac else 3):
                    self.c0 = 1 if self.ac else int(cur - v[self.min_i] > floor + T)
            r = self.rise
            if i >= r:
                gain = cur - v[i - r]           # over the current rising run
                r0 = r >= rises_r0 and gain > floor + T
                if r >= 2 and gain > 0.5 * floor + T and self.state == 0:
                    self.state = 1              # a real rise
        else:
            self.rise, self.fall, self.f1d = 0, self.fall + 1, 0
            if (i >= self.fall and v[self.max_i] - cur > 0.25 * floor + T and self.state == 1):
                self.state = 3                  # rise, then a real fall: confirmed peak
        if cur < v[self.min_i]:
            self.min_i = i
        return r0

    def accepts(self, v, i, floor, T, mode):
        """The camera's stop for this area at sample i (AF-S, horizontal detector):
        0x5340d100 with a candidate: 3 falls (0x53c16c4c, the hysteresis count above), at
        most 40 samples after the maximum (0x53c16bfc), and a drop of more than floor + T
        from it. The shape tests that follow (fall-side width over rise-side width at the
        current level in 0.5-2, else the brightness max/min of the pass <= 5 and around the
        peak <= 1.5) need the brightness array, which is not traced: assumed passed.
        0x5340afc0 (through 0x5340ced4, mode 0 only): 11-19 samples after a maximum past
        sample 1 with a drop of more than floor + 2T. Not modelled: the window start +0xa8
        (the test runs only once the lens is past it) and the vetoes 0x5340c1dc (too little
        contrast per pixel) and 0x533f77a8 (brightness slope at the peak)."""
        drop = v[self.max_i] - v[i]
        if self.c0 and self.fall >= 3:
            if i > self.max_i + 40:
                self.c0 = 0
            elif drop > floor + T:
                return True
        return (mode == 0 and self.max_i > 1 and self.max_i + 10 < i < self.max_i + 20
                and drop > floor + 2 * T)

    def behind(self, v, i, floor, T, falls=5, margin=1.0):
        """+0x1c, "peak behind": 0x5340d100 without a candidate sets it after 5 falls
        (0x53c16cc8) when the drop from the stored maximum is more than floor + T (the same
        margin as the accept); horizontal detector unconditionally, the vertical one only with
        +0x6c in 0.5-2 (0x53c16cd4/d8). A new maximum clears it (0x5340dda4), and so does the
        contrast veto 0x5340c1dc (not modelled). falls and margin (the factor on the floor) are
        the knobs the turn rules vary."""
        return (not self.c0 and self.fall >= falls
                and v[self.max_i] - v[i] > margin * floor + T)


def frame_arrays(A, b):
    """(m, values) for each frame m = 3..n: the array as the detector saw it then (the
    camera skips the comparison at the second sample)."""
    arr = A["arr"]
    for m in range(3, len(arr) + 1):
        yield m, list(arr[:m - 1]) + [(b[m - 2] + 3 * b[m - 1]) / 4]


def replay_area(press, q, a):
    """Run the detector port over one area of one pass: [(m, rise, fall, state, recorded
    frame or None)], or None if the pass is too short."""
    A = q["areas"][a]
    b = bsolve(A)
    if b is None:
        return None
    rec = {fr[0]: fr for fr in A["frames"]}
    det, out, T = Detector(), [], q["T"]
    floor, rises = floor_of(press["arm"], q["mode"]), 2 if "FALLS2" in press["arm"] else 3
    for m, v in frame_arrays(A, b):
        T = rec[m][3] if m in rec else T
        det.update(v, m - 1, floor, T, rises)
        out.append((m, det.rise, det.fall, det.state, rec.get(m)))
    return out


def camera_stop(press, q, a, ac=False):
    """First frame at which the ported accept test stops area a of pass q, or None."""
    A = q["areas"][a]
    b = bsolve(A)
    if b is None:
        return None
    rec = {fr[0]: fr for fr in A["frames"]}
    det, T = Detector(ac), q["T"]
    floor, rises = floor_of(press["arm"], q["mode"]), 2 if "FALLS2" in press["arm"] else 3
    for m, v in frame_arrays(A, b):
        T = rec[m][3] if m in rec else T
        det.update(v, m - 1, floor, T, rises)
        if det.accepts(v, m - 1, floor, T, q["mode"]):
            return m
    return None


def turn_frame(press, q, a=0, falls=5, margin=1.0, k=0):
    """First frame at which the H detector of area a sets "peak behind" (the camera then stops
    the sweep and decides at the end of the pass, 0x53403808), or None if the port accepts
    first or never sets it. k drops the first k samples of the pass (see accept_from)."""
    for m, v, det, floor, T in _frames(press, q, a, k):
        if det.accepts(v, m - 1, floor, T, q["mode"]):
            return None
        if det.behind(v, m - 1, floor, T, falls, margin):
            return m
    return None


def accept_from(press, q, a, k):
    """The ported accept on pass q with its first k samples left out, as if the pass had started
    k samples later (a jump back over the retrace): (frame, peak sample index in the full pass)
    or None. The value array is rebuilt from the solved b values."""
    for m, v, det, floor, T in _frames(press, q, a, k):
        if det.accepts(v, m - 1, floor, T, q["mode"]):
            return m, det.max_i + k
    return None


def _frames(press, q, a, k=0):
    """Run the detector port over pass q from sample k on, yielding (m, values, detector,
    floor, T) after each frame's update."""
    A = q["areas"][a]
    b = bsolve(A)
    if b is None or len(b) - k < 4:
        return
    rec = {fr[0]: fr for fr in A["frames"]}
    det, T = Detector(), q["T"]
    floor, rises = floor_of(press["arm"], q["mode"]), 2 if "FALLS2" in press["arm"] else 3
    if k == 0:
        frames = frame_arrays(A, b)
    else:
        bb = b[k:]
        arr = [bb[0]] + [(bb[i - 1] + 2 * bb[i] + bb[i + 1]) / 4 for i in range(1, len(bb) - 1)]
        frames = ((m, arr[:m - 1] + [(bb[m - 2] + 3 * bb[m - 1]) / 4]) for m in range(3, len(bb) + 1))
    for m, v in frames:
        T = rec[m + k][3] if m + k in rec else T
        det.update(v, m - 1, floor, T, rises)
        yield m, v, det, floor, T


# ---------------------------------------------------------------- peak fits

def parabola3(x, y, k):
    """CalcQuadraticCurve (0x5340ed10): vertex of the parabola through samples k-1, k, k+1;
    the middle sample if the curve is flat or not a maximum."""
    if k <= 0 or k >= len(y) - 1:
        return float(x[k])
    (x0, x1, x2), (y0, y1, y2) = x[k - 1:k + 2], y[k - 1:k + 2]
    den = (x0 - x1) * (x0 - x2) * (x1 - x2)
    if den == 0:
        return float(x1)
    a = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / den
    b = (x2 * x2 * (y0 - y1) + x1 * x1 * (y2 - y0) + x0 * x0 * (y1 - y2)) / den
    return float(x1) if a >= -1e-12 else -b / (2 * a)


def quad5(x, y, k):
    """Least-squares quadratic over the five samples around k (three at an edge)."""
    lo, hi = max(k - 2, 0), min(k + 3, len(y))
    if hi - lo < 3:
        return float(x[k])
    xs = np.array(x[lo:hi], float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # repeated positions (lens stalls) make it rank-poor
        c = np.polyfit(xs - xs.mean(), np.array(y[lo:hi], float), 2)
    if c[0] >= -1e-12:
        return float(x[k])
    v = -c[1] / (2 * c[0]) + xs.mean()
    return float(min(max(v, xs.min()), xs.max()))


def gauss3(x, y, k):
    """Gaussian peak: the parabola through the logs of the three samples around k, above a
    baseline just under the pass minimum (focus curves are bell-shaped, not parabolic)."""
    base = min(y) - 0.05 * (max(y) - min(y) or 1e-6)
    return parabola3(x, [math.log(v - base) for v in y], k)


FITS = {"parabola3 (camera)": parabola3, "quadratic5": quad5, "gauss3": gauss3}


# ---------------------------------------------------------------- stop rules

def sigma_online(b, m):
    """Noise of b from the pass so far: robust spread of the second differences (a smooth
    curve has small second differences; white noise of sigma gives sigma x sqrt(6))."""
    d = [b[i] - 2 * b[i - 1] + b[i - 2] for i in range(2, m)]
    if len(d) < 3:
        return None
    med = st.median(d)
    mad = st.median(abs(x - med) for x in d)
    return 1.4826 * mad / math.sqrt(6) if mad > 0 else None


def rule_state3(press, q, a, out, b):
    """Stop as soon as the camera's own detector has a confirmed peak (+0x26 = 3)."""
    return next((m for m, _, _, state, _ in out if state == 3), None)


def rule_noise(k, falls=2, sigma_min=0.0005):
    """Stop when the smoothed peak rose and fell by k noise widths of b (the smoothing makes
    the array about 0.6 times as noisy, so this is conservative), falls >= `falls` in a row. A
    one-sided z-test: for a drop in Gaussian noise the sequential probability ratio test
    reduces to such a threshold on the size of the drop."""
    def rule(press, q, a, out, b):
        arr = q["areas"][a]["arr"]
        for m in range(5, len(arr) + 1):
            s = sigma_online(b, m)
            if s is None:
                continue
            s = max(s, sigma_min)
            v = list(arr[:m - 1]) + [(b[m - 2] + 3 * b[m - 1]) / 4]
            km = max(range(m), key=v.__getitem__)
            if m - 1 - km < falls or not all(v[j] < v[j - 1] for j in range(km + 1, m)):
                continue
            if v[km] - min(v[:km + 1]) > k * s and v[km] - v[m - 1] > k * s:
                return m
        return None
    rule.__doc__ = f"Stop when the peak rose and fell by {k} noise widths (sigma from the pass)."
    return rule


RULES = {"state3": rule_state3, "noise6": rule_noise(6), "noise8": rule_noise(8), "noise12": rule_noise(12)}


def first_stop(press, q, rule):
    """(frame, area) where the rule would stop this pass first, over both areas, or None."""
    hits = []
    for a in (0, 1):
        out = replay_area(press, q, a)
        if out:
            m = rule(press, q, a, out, bsolve(q["areas"][a]))
            if m:
                hits.append((m, a))
    return min(hits) if hits else None


def lock_in(q, a, m=None):
    """The lens position (pass coordinates) a CalcQuadraticCurve lock on the first m samples
    of area a would give."""
    A = q["areas"][a]
    arr, pos = list(A["arr"][:m]), A["pos"][:m]
    if len(pos) != len(arr) or not arr:
        return None
    if m is not None and 2 <= m < len(A["arr"]):
        b = bsolve(A)      # at frame m the newest entry was still the prediction
        if b is not None:
            arr[-1] = (b[m - 2] + 3 * b[m - 1]) / 4
    return parabola3(pos, arr, max(range(len(arr)), key=arr.__getitem__))


def chain(press, j, k, x):
    """Position x of pass j in the coordinates of pass k > j: every turn mirrors the
    position about the end of the pass (plus TURN_OFFSET)."""
    for i in range(j, k):
        pos = press["passes"][i]["areas"][0]["pos"]
        if not pos or press["passes"][i]["areas"][0]["n"] > W:
            return None
        x = pos[-1] - x + TURN_OFFSET
    return x


# ---------------------------------------------------------------- commands

def cmd_gate(presses):
    by = {}
    for p in presses:
        if any(s in p["arm"] for s in SKIP_ARMS):
            continue
        for q in p["passes"]:
            for a in (0, 1):
                for m, r, f, state, rec in replay_area(p, q, a) or ():
                    if rec is None:
                        continue
                    s = by.setdefault(q["level"], [0, 0, 0])
                    s[0] += 1
                    s[1] += (r, f) == (rec[1], rec[2])
                    s[2] += state == rec[4]
    print("Detector port 0x5340dbfc against the recorded frames (both areas):")
    print(f"  {'fps':>4s} {'frames':>7s} {'counts':>7s} {'state':>7s}")
    tot = [sum(v[i] for v in by.values()) for i in range(3)]
    for lv in sorted(by) + ["all"]:
        f, c, s = tot if lv == "all" else by[lv]
        name = "all" if lv == "all" else str(card.AF_FPS[lv])
        print(f"  {name:>4s} {f:7d} {c / f:7.1%} {s / f:7.1%}")
    print("  Misses: at 200 fps mostly the tracer reading the counts and the sample count of one frame\n"
          "  at different moments (one 4 ms record per 5 ms frame); at 29 fps mostly passes whose first\n"
          "  comparison differs (the camera sometimes compares at the second sample already), which\n"
          "  carries into the counts.")
    stop = stop_gate(presses)
    print("\nAccept test (0x5340d100 / 0x5340afc0) against the camera's stops, AF-S (action 8):")
    print(f"  {'+0xac':>6s} {'passes':>7s} {'exact':>7s} {'early':>7s} {'late':>6s} {'missed':>7s}"
          f" | {'rejected':>8s} {'accepted by port':>17s}")
    for ac, r in stop.items():
        n = r["exact"] + r["early"] + r["late"] + r["miss"]
        print(f"  {ac:>6s} {n:7d} {r['exact'] / n:7.1%} {r['early'] / n:7.1%} {r['late'] / n:6.1%}"
              f" {r['miss'] / n:7.1%} | {r['rej']:8d} {r['rej_false'] / r['rej']:17.1%}")
    v = stop["0"]["vertical"]
    print(f"  Passes the vertical detector accepted (+0xf40 = 1, not traced) are left out: {v}.\n"
          "  +0xac (from the object byte +0x43c0) is not traced; mode 1 = set for detector mode 1 only.\n"
          "  early: the port stops sooner; not modelled are the window start +0xa8 and the brightness\n"
          "  vetoes (shape tests, post-filter 0x533f77a8). missed: mostly +0xac (bright 100 fps sessions).")
    return dict(by_fps={str(card.AF_FPS[k]): v for k, v in by.items()}, total=tot, stop=stop)


def stop_gate(presses):
    """The ported accept test against the camera's stops: per +0xac assumption, accepted AF-S
    passes (horizontal detector, area the camera chose) by where the port stops, and rejected
    passes the port would have accepted in either area."""
    out = {}
    for ac in ("0", "mode 1", "1"):
        r = dict(exact=0, early=0, late=0, miss=0, rej=0, rej_false=0, vertical=0)
        for p in presses:
            if any(s in p["arm"] for s in SKIP_ARMS):
                continue
            for q in p["passes"]:
                flag = ac == "1" or (ac == "mode 1" and q["mode"] == 1)
                if q["act"] == 8:
                    a = q["area"] if q["area"] in (0, 1) else 0
                    if q["f40"][a] not in (0, 2):
                        r["vertical"] += q["f40"][a] == 1
                        continue
                    m, n = camera_stop(p, q, a, flag), len(q["areas"][a]["arr"])
                    r["miss" if m is None else "exact" if m == n else "early" if m < n else "late"] += 1
                elif q["act"] in (1, 2, 64):
                    r["rej"] += 1
                    r["rej_false"] += any(camera_stop(p, q, a, flag) for a in (0, 1))
        out[ac] = r
    return out


def press_outcome(p, rule):
    """What the press would have done with `rule` added to the camera's own stop (the rule can
    only stop sooner): (kind, saved ms, lock offset from the camera's lock in pulses, detail).
    kind: same (no change), earlier (same pass, sooner), rescue (a pass the camera rejected),
    false (a lock in a no-signal scene the camera did not lock; if the camera locked too, the
    rule only locks sooner: false_too), unchecked (a rescue in a press without a camera lock)."""
    accepted = [i for i, q in enumerate(p["passes"]) if q["act"] in (8, 128)]
    if not accepted and p["outcome"] == "lock" and p["passes"]:
        accepted = [len(p["passes"]) - 1]   # inferred lock: action 8 fell between two records
    last_acc = accepted[-1] if accepted and p["outcome"] == "lock" else None
    for j, q in enumerate(p["passes"]):
        acc = j in accepted
        hit = first_stop(p, q, rule)
        n = len(q["areas"][hit[1]]["arr"]) if hit else 0
        frame_ms = 1000 / card.AF_FPS[q["level"]]
        if hit and (hit[0] < n or not acc):
            where = f"{p['session']} #{p['n']} {p['arm']} pass {j + 1}/{len(p['passes'])} frame {hit[0]}/{n}"
            if p["session"] in NO_SIGNAL:
                return ("false_too" if p["outcome"] == "lock" else "false"), 0, None, where
            if acc:          # sooner in the pass the camera accepted
                ca = q["area"] if q["area"] in (0, 1) else 0
                alt, cam = lock_in(q, hit[1], hit[0]), lock_in(q, ca)
                return "earlier", (n - hit[0]) * frame_ms, None if alt is None or cam is None else alt - cam, where
            if last_acc is None or last_acc < j:
                return "unchecked", 0, None, where + f" ({p['outcome']})"
            qa = p["passes"][last_acc]
            alt = lock_in(q, hit[1], hit[0])
            alt = chain(p, j, last_acc, alt) if alt is not None else None
            cam = lock_in(qa, qa["area"] if qa["area"] in (0, 1) else 0)
            if alt is None or cam is None:
                return "unchecked", 0, None, where
            # Time to the camera's own stop in its accepting pass, minus the rule's sampling in
            # pass j; the final move to the peak comes after both and is left out.
            saved = 1000 * (qa["t_end"] - q["t"]) - hit[0] * frame_ms
            return "rescue", saved, alt - cam, where
        if acc:
            return "same", 0, 0, ""
    return "same", 0, None, ""


def cmd_rules(presses, verbose=False):
    results = {}
    modelled = [p for p in presses if not any(s in p["arm"] for s in SKIP_ARMS)]
    nosig = sum(p["session"] in NO_SIGNAL for p in modelled)
    print(f"Stop rules added to the camera's own ({len(modelled)} presses of modelled arms, {nosig} of "
          f"them in no-signal scenes; area 0 or 1, whichever stops first):")
    print(f"  {'rule':8s} {'sooner in the same pass':>34s} | {'rescued rejected passes':>40s} | "
          f"{'no-signal':>14s}")
    print(f"  {'':8s} {'presses':>8s} {'median':>7s} {'total':>7s} {'moved':>6s} | {'same peak':>9s} "
          f"{'other':>6s} {'median':>7s} {'total':>7s} {'unchkd':>7s} | {'locks added':>14s}")
    for name, rule in RULES.items():
        rows = [press_outcome(p, rule) for p in modelled]
        early = [r for r in rows if r[0] == "earlier"]
        resc = [r for r in rows if r[0] == "rescue"]
        same_peak = [r for r in resc if r[2] is not None and abs(r[2]) <= SAME_PEAK]
        other = [r for r in resc if r not in same_peak]
        moved = [r for r in early if r[2] is not None and abs(r[2]) > SAME_PEAK]
        false = [r for r in rows if r[0] == "false"]
        false_too = [r for r in rows if r[0] == "false_too"]
        unchecked = [r for r in rows if r[0] == "unchecked"]
        med = lambda xs: st.median(x[1] for x in xs) if xs else 0
        tot = lambda xs: sum(x[1] for x in xs) / 1000
        print(f"  {name:8s} {len(early):8d} {med(early):5.0f}ms {tot(early):6.1f}s {len(moved):6d} | "
              f"{len(same_peak):9d} {len(other):6d} {med(same_peak):5.0f}ms {tot(same_peak):6.1f}s "
              f"{len(unchecked):7d} | {len(false):5d} (+{len(false_too)} the camera too)")
        if verbose:
            for kind, xs in (("moved", moved), ("other peak", other), ("false", false),
                             ("false, camera too", false_too), ("unchecked", unchecked)):
                for x in xs:
                    print(f"      {kind}: {x[3]}" + (f", {x[2]:+.0f} p" if x[2] is not None else ""))
        results[name] = dict(presses=len(modelled), nosig_presses=nosig, earlier=len(early),
                             earlier_ms_median=med(early), earlier_s_total=tot(early), moved=len(moved),
                             rescued=len(same_peak), rescued_other_peak=len(other),
                             rescued_ms_median=med(same_peak), rescued_s_total=tot(same_peak),
                             unchecked=len(unchecked), false_locks=len(false), false_locks_camera_too=len(false_too))
    print("  moved/other: lock more than 40 pulses from the camera's lock. unchkd: the rule accepts a pass\n"
          "  in a press the camera failed or aborted, so there is no lock to compare with.")
    return results


def cmd_fits(presses):
    """Split-half test: fit the peak from the even samples and from the odd samples (each at
    twice the camera's step). No ground truth is needed: both halves see the same peak, so a
    better estimator gives halves that agree. Also each fit at full step against the camera's."""
    halves = {k: [] for k in FITS}
    full = {k: [] for k in FITS}
    used = 0
    for p in presses:
        for q in p["passes"]:
            if q["act"] not in (8, 128):
                continue
            a = q["area"] if q["area"] in (0, 1) else 0
            arr, pos = q["areas"][a]["arr"], q["areas"][a]["pos"]
            if len(pos) != len(arr) or len(arr) < 9:
                continue
            k = max(range(len(arr)), key=arr.__getitem__)
            if k < 2 or k > len(arr) - 3:
                continue
            used += 1
            for name, fit in FITS.items():
                est = []
                for h in (0, 1):
                    xs, ys = pos[h::2], arr[h::2]
                    est.append(fit(xs, ys, max(range(len(ys)), key=ys.__getitem__)))
                halves[name].append(abs(est[0] - est[1]))
                full[name].append(abs(fit(pos, arr, k) - parabola3(pos, arr, k)))
    print(f"Peak fits on {used} accepted passes (peak 2+ samples from both ends), pulses:")
    print(f"  {'fit':20s} {'even vs odd half (twice the step)':>34s} {'full step vs camera':>20s}")
    out = {}
    for name in FITS:
        h, f = sorted(halves[name]), sorted(full[name])
        out[name] = dict(halves_median=st.median(h), halves_p90=h[int(0.9 * len(h))], full_vs_camera_median=st.median(f))
        print(f"  {name:20s} {'median':>14s} {out[name]['halves_median']:4.1f}, 90% {out[name]['halves_p90']:4.1f} "
              f"{out[name]['full_vs_camera_median']:20.1f}")
    return dict(passes=used, fits=out)


def first_passes(presses):
    """First passes (path 0) of the modelled presses with what cmd_turns needs: the frame time
    measured over the pass, its band, how the pass ended and, for a turn, its shape (only falls:
    the stored maximum never moved past sample 1, i.e. the sweep left a peak at or behind its
    start; rise then fall: a peak inside the pass that was not accepted)."""
    rows = []
    for p in presses:
        if p["session"] in NO_SIGNAL or any(s in p["arm"] for s in SKIP_ARMS) or not p["passes"]:
            continue
        q = p["passes"][0]
        n = len(q["areas"][0]["arr"])
        if q["path"] != 0 or n < 4:
            continue
        last = None
        for last in _frames(p, q, 0):
            pass
        if last is None:
            continue
        dt = (q["t_end"] - q["t"]) / (n - 1)
        kind = {2: "turned", 8: "accepted"}.get(q["act"], "other")
        shape = ("only falls" if last[2].max_i <= 1 else "rise then fall") if kind == "turned" else None
        rows.append(dict(p=p, q=q, n=n, dt=dt, kind=kind, shape=shape,
                         band="29 fps" if dt > 0.026 else "49 fps" if dt > 0.015 else "100-200 fps"))
    return rows


def cmd_turns(presses):
    rows = first_passes(presses)
    bands = ("29 fps", "49 fps", "100-200 fps")
    out = dict(first_passes={b: sum(r["band"] == b for r in rows) for b in bands})
    print("First passes (path 0) by band and ending, turns by shape:")
    print(f"  {'band':12s} {'passes':>7s} {'accepted':>9s} {'turned':>7s} {'only falls':>11s} {'rise, fall':>11s}")
    for b in bands:
        rb = [r for r in rows if r["band"] == b]
        c = lambda f: sum(1 for r in rb if f(r))
        print(f"  {b:12s} {len(rb):7d} {c(lambda r: r['kind'] == 'accepted'):9d} {c(lambda r: r['kind'] == 'turned'):7d}"
              f" {c(lambda r: r['shape'] == 'only falls'):11d} {c(lambda r: r['shape'] == 'rise then fall'):11d}")

    geo = {}
    for r in rows:
        p, q = r["p"], r["q"]
        if r["kind"] == "turned" and p["outcome"] == "lock" and not p["wrong"] \
                and p["final"] is not None and q.get("turn_pos") is not None:
            beyond = abs(p["final"] - q["turn_pos"]) > q["areas"][0]["pos"][-1] + 10
            k = (r["shape"], "lock beyond the start" if beyond else "lock inside the pass")
            geo[k] = geo.get(k, 0) + 1
    print("  Locked presses after a first-pass turn, where the lock lay (turn position +0xc434):")
    for k in sorted(geo):
        print(f"    {k[0]:15s} {k[1]:22s} {geo[k]:4d}")
    out["lock_geometry"] = {" / ".join(k): v for k, v in geo.items()}

    gate = {}
    for r in rows:
        if r["kind"] != "turned":
            continue
        q, cam, port = r["q"], r["q"]["minus2"], turn_frame(r["p"], r["q"])
        if cam is None:
            k = "agree" if port is None or port >= r["n"] else "port early"
        else:
            k = "agree" if port == cam else "port none" if port is None else "port early" if port < cam else "port late"
        key = (r["shape"], "-2 seen" if cam is not None else "window end", k)
        gate[key] = gate.get(key, 0) + 1
    agree = sum(v for k, v in gate.items() if k[2] == "agree")
    total = sum(gate.values())
    print(f"\n\"Peak behind\" port (area 0, H, 5 falls, floor + T) against the camera's turn on {total} turned"
          f" first passes: {agree} agree ({agree / total:.0%}):")
    for k in sorted(gate):
        print(f"    {k[0]:15s} {k[1]:11s} {k[2]:11s} {gate[k]:4d}")
    out["gate"] = {" / ".join(k): v for k, v in gate.items()}
    out["gate_agree"] = agree / total

    print("\nTurning sooner (open loop): saved = 2 x frames x frame time (the pass and the retrace both"
          "\nshorten); new false turns = accepted first passes the rule would have stopped before any accept"
          "\n(and before the stock rule and the camera's own -2):")
    print(f"  {'falls':>5s} {'band':12s} {'only falls':>18s} {'rise then fall':>18s} {'new false turns':>16s}")
    out["rules"] = {}
    for N in (3, 4):
        for b in bands:
            sav = {"only falls": [], "rise then fall": []}
            false = acc = 0
            for r in rows:
                if r["band"] != b:
                    continue
                p, q = r["p"], r["q"]
                m, m5 = turn_frame(p, q, 0, N), turn_frame(p, q)
                if r["kind"] == "turned":
                    cam = q["minus2"] or r["n"]
                    base = min(cam, m5) if m5 else cam
                    sav[r["shape"]].append(2 * max(0, base - m) * r["dt"] * 1000 if m else 0.0)
                elif r["kind"] == "accepted":
                    acc += 1
                    if m is None or m >= r["n"] or (m5 is not None and m5 <= m) \
                            or (q["minus2"] is not None and q["minus2"] <= m):
                        continue
                    if any((s := camera_stop(p, q, a)) is not None and s <= m for a in (0, 1)):
                        continue
                    false += 1
            f = lambda x: f"{len(x):3d} x {st.mean(x):4.0f} ms" if x else f"{0:3d}"
            print(f"  {N:5d} {b:12s} {f(sav['only falls']):>18s} {f(sav['rise then fall']):>18s} {false:7d} of {acc:3d}")
            out["rules"][f"{N} falls, {b}"] = dict(
                only_falls_n=len(sav["only falls"]), only_falls_ms=st.mean(sav["only falls"]) if sav["only falls"] else 0,
                rise_fall_n=len(sav["rise then fall"]),
                rise_fall_ms=st.mean(sav["rise then fall"]) if sav["rise then fall"] else 0,
                new_false=false, accepted=acc)

    print("\nJumping back to the first pass's start (minus an overlap) instead of retracing, on only-falls"
          "\nturns whose return pass the camera accepted; gross saving before the fast move:")
    out["jump_back"] = {}
    for overlap in (4, 6):
        res, saved = {}, {}
        for r in rows:
            ps = r["p"]["passes"]
            if r["shape"] != "only falls" or len(ps) < 2 or ps[1]["path"] != 1 or ps[1]["act"] != 8:
                continue
            q0, q1 = ps[0], ps[1]
            a = 0 if q1["f40"][0] in (0, 2) else 1 if q1["f40"][1] in (0, 2) else 0
            full = accept_from(r["p"], q1, a, 0)
            pos1 = q1["areas"][a]["pos"]
            if full is None or len(pos1) < 4:
                k = "port misses the return accept"
            else:
                step = (pos1[-1] - pos1[0]) / (len(pos1) - 1)
                start = q0["areas"][a]["pos"][-1] + TURN_OFFSET       # first pass start, return pass coordinates
                skip = sum(1 for x in pos1 if x < start - overlap * step)
                cut = accept_from(r["p"], q1, a, skip) if skip else full
                if cut is None:
                    k = "no accept after the jump"
                elif abs(cut[1] - full[1]) <= 1:
                    k = "same peak"
                    saved.setdefault(r["band"], []).append(skip * (q1["t_end"] - q1["t"]) / (len(pos1) - 1) * 1000)
                else:
                    k = "other peak"
            res[k] = res.get(k, 0) + 1
        print(f"  overlap {overlap} samples: " + ", ".join(f"{k} {v}" for k, v in sorted(res.items())) + "; saved "
              + ", ".join(f"{b} {len(v)} x {st.mean(v):.0f} ms" for b, v in sorted(saved.items())))
        out["jump_back"][overlap] = dict(res, saved={b: dict(n=len(v), ms=st.mean(v)) for b, v in saved.items()})
    print("  Limits: open loop. A false turn sends the next pass away from the peak; its cost (a pass in the\n"
          "  wrong direction and a second turn, or a failed press) is not in the recording. The vertical\n"
          "  detector, the setup byte +0xac (bright sessions) and the end-of-pass re-run 0x53403808 are not\n"
          "  modelled; the fast move of a jump back is not measured.")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("gate", "rules", "fits", "turns", "all"))
    ap.add_argument("--sessions", help="comma-separated session names (default: all with tails)")
    ap.add_argument("--json", metavar="FILE", help="also write the numbers to FILE")
    ap.add_argument("-v", "--verbose", action="store_true", help="rules: list the presses behind each risk count")
    a = ap.parse_args()
    sessions = a.sessions.split(",") if a.sessions else sessions_with_tails()
    presses = load(sessions)
    print(f"{len(presses)} presses, {sum(len(p['passes']) for p in presses)} passes "
          f"from {len(sessions)} sessions\n")
    out = {}
    for c in (("gate", "rules", "fits", "turns") if a.command == "all" else (a.command,)):
        out[c] = cmd_rules(presses, a.verbose) if c == "rules" else \
            {"gate": cmd_gate, "fits": cmd_fits, "turns": cmd_turns}[c](presses)
        print()
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
