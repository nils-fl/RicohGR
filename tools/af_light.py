#!/usr/bin/env python3
"""How much light each contrast-AF sweep had, how noisy it was, and how far its
peak stood above the noise and above the camera's threshold.

Usage:  python3 tools/af_light.py [DIR ...]      (default: every work/af-logs/*/csv)
Reads the camera's debug CSVs (see docs/af-debug-log.html) and prints, per
sensor mode and outcome, medians of:
  px      pixels of AF area 0 (MainAreaSizeH x MainAreaSizeV, in readout lines)
  Y/px    brightness per pixel (YData of area 0 / px)
  nY      frame-to-frame jitter of YData, relative (should be ~constant in a sweep)
  nH      sample noise of the contrast series ContrastAfDataH, relative: robust
          spread of the second difference around the local mean (MAD x 1.4826 x
          sqrt(2/3)), which removes the smooth focus curve
  mod     contrast rise of the sweep, (max - min) / min of the [1 2 1]/4 smoothed series
  snr     (max - min) / noise, both in counts: how many noise widths the peak stands out
  thr     the camera's Threshold*1000000 for area 0, as a relative value
  peak    share of sweeps where the camera confirmed a peak in any area
Offline; reads your own files.
"""
import re, statistics, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import af_csv  # noqa: E402  (run, key, samples, first)

LOGS = Path(__file__).resolve().parent.parent / "work" / "af-logs"


def mad_noise(xs):
    """Noise of a slowly varying series from its second differences."""
    if len(xs) < 5:
        return None
    e = [xs[k] - (xs[k - 1] + xs[k + 1]) / 2 for k in range(1, len(xs) - 1)]
    m = statistics.median(e)
    return statistics.median(abs(x - m) for x in e) * 1.4826 * (2 / 3) ** 0.5


def algorithm_row(text, area=0):
    m = re.search(r"\[-Horizontal-\]\n(.*?)\n \[-Vertical-\]", text, re.S)
    rows = [[int(x) for x in line.split()] for line in m.group(1).splitlines()] if m else []
    return rows[area] if len(rows) > area else None


def sweep(path, text):
    r = af_csv.run(path)
    rows = af_csv.samples(text)
    if not r["n"] or r["fps"] is None or len(rows) < 5:
        return None
    g = lambda label: af_csv.first(text, label)
    h = [row[1] for row in rows]
    y = [row[3] for row in rows] if len(rows[0]) > 3 else None
    if y is None or min(h) <= 0 or min(y) <= 0:
        return None
    sm = [(h[k - 1] + 2 * h[k] + h[k + 1]) / 4 for k in range(1, len(h) - 1)]
    noise_h = mad_noise(h)
    px = g("MainAreaSizeH") * g("MainAreaSizeV")
    alg = algorithm_row(text)
    return {
        "fps": r["fps"], "integ": g("IntegTimeUs"), "gain": g("Gain"), "px": px,
        "lines": g("MainAreaSizeV"), "areas": len(r["areas"].split()),
        "y_px": statistics.median(y) / px,
        "n_y": mad_noise(y) / statistics.median(y),
        "n_h": noise_h / statistics.median(h),
        "mod": (max(sm) - min(sm)) / min(sm),
        "snr": (max(sm) - min(sm)) / noise_h if noise_h else float("inf"),
        "thr": alg[3] / 1e6 if alg else None,
        "peak": "*" in r["areas"], "path": r["path"], "n": r["n"],
    }


def collect(dirs):
    out = []
    for d in dirs:
        seen = set()
        for p in sorted(Path(d).glob("*.csv")):
            text = p.read_text(encoding="latin-1")
            k = af_csv.key(text)
            if k in seen:
                continue
            seen.add(k)
            s = sweep(p, text)
            if s:
                s["session"] = Path(d).parent.name
                out.append(s)
    return out


def med(xs, fmt="{:.3g}"):
    xs = [x for x in xs if x is not None]
    return fmt.format(statistics.median(xs)) if xs else "-"


def main(dirs):
    sweeps = collect(dirs)
    groups = defaultdict(list)
    for s in sweeps:
        mode = ("forced " if s["session"] == "force100" else "") + f"{s['fps']} fps"
        groups[(mode, s["peak"])].append(s)
    print(f"{'mode':15s} {'peak':>5s} {'n':>4s} {'px':>6s} {'Y/px':>6s} {'nY':>7s} {'nH':>7s} "
          f"{'mod':>6s} {'snr':>6s} {'thr':>7s}")
    for (mode, peak), xs in sorted(groups.items()):
        print(f"{mode:15s} {'yes' if peak else 'no':>5s} {len(xs):4d} {med([x['px'] for x in xs], '{:.0f}'):>6s} "
              f"{med([x['y_px'] for x in xs], '{:.0f}'):>6s} {med([x['n_y'] for x in xs]):>7s} "
              f"{med([x['n_h'] for x in xs]):>7s} {med([x['mod'] for x in xs]):>6s} "
              f"{med([x['snr'] for x in xs], '{:.1f}'):>6s} {med([x['thr'] for x in xs]):>7s}")
    return sweeps


if __name__ == "__main__":
    args = sys.argv[1:]
    dirs = [Path(a) for a in args] if args else sorted(p for p in LOGS.glob("*/csv") if p.is_dir())
    main(dirs)
