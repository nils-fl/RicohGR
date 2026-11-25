#!/usr/bin/env python3
"""Summarise AF timings you measured yourself from high-speed video.

Input: a CSV with the header  condition,frames,fps
  condition  free text, e.g. "bright-edge AF-S" (one row per trial)
  frames     video frames counted from half-press to AF confirmation
  fps        frame rate of the video (e.g. 240)

Usage:  python3 tools/af/af_timing_tally.py timings.csv
Prints, per condition: n, median, quartiles and range in milliseconds.
Offline arithmetic on your own numbers only. It does not talk to a camera.
"""
import csv, statistics, sys
from collections import defaultdict

def quartiles(xs):
    xs = sorted(xs)
    if len(xs) < 4:
        return xs[0], xs[-1]
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return q[0], q[2]

def main(path):
    ms = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ms[row["condition"].strip()].append(1000.0 * float(row["frames"]) / float(row["fps"]))
    print(f"{'condition':32s} {'n':>3s} {'median':>8s} {'q1':>7s} {'q3':>7s} {'min':>7s} {'max':>7s}  (ms)")
    for cond, xs in sorted(ms.items()):
        q1, q3 = quartiles(xs)
        print(f"{cond:32s} {len(xs):3d} {statistics.median(xs):8.0f} {q1:7.0f} {q3:7.0f} {min(xs):7.0f} {max(xs):7.0f}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
