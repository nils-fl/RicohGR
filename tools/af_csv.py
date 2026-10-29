#!/usr/bin/env python3
"""Summarise the contrast-AF debug CSVs that the camera writes to the card.

Usage:  python3 tools/af_csv.py DIR [DIR ...]
Reads every *.csv in each DIR (files the camera wrote with
"CAF_Debug SaveDebugFile on", see docs/af-debug-log.html) and prints one row
per AF run: phase result (direction, defocus, decision grade), sample rate,
step width, number of contrast samples, the lens pulse of the contrast peak,
and the samples taken after the peak, from the camera's own per-area result
(the Algorithm block), with the end of the scan window. Left out but counted:
repeats (the camera dumps the last run again when an AF start has no new
search; only the live lens position and first table row differ), dumps taken
mid-search (no search header yet) and header-only files (no search yet).
A summary line per call follows. Offline; reads your own files.
"""
import hashlib, re, statistics, sys
from pathlib import Path


def first(text, label):
    m = re.search(r"^\[" + re.escape(label) + r"\]\t\s*(-?\d+)", text, re.M)
    return int(m.group(1)) if m else None


def samples(text):
    """Rows of the [LensPulsePosition] table: pulse, then H, V, Y per area.
    The first row is the value before the search and is left out."""
    m = re.search(r"\[LensPulsePosition\][^\n]*\n((?:[ \t]*-?\d+(?:\t[ \t]*-?\d+)+[ \t]*\n?)+)", text)
    if not m:
        return []
    rows = [[int(x) for x in line.split()] for line in m.group(1).strip().splitlines()]
    return rows[1:]


def key(text):
    """The file without the values that are live at dump time rather than
    search data: the first row of the sample table and the leading
    [LensPosition[mm]] / [LensPosition[pulse]] lines."""
    text = re.sub(r"(\[LensPulsePosition\][^\n]*\n)[^\n]*\n", r"\1", text, count=1)
    return re.sub(r"\A(?:\[LensPosition\[(?:mm|pulse)\]\][^\n]*\n)+", "", text)


def run(path):
    text = path.read_text(encoding="latin-1")
    defocus = re.findall(r"^\[ZafDefocusUm\]\t\s*(-?\d+)\s*$", text, re.M)
    grade = re.search(r"^\[DicisionGrade\]\t(\d+)\t(\d+)", text, re.M)
    rows = samples(text)
    # The camera's own verdict per AF area: the [-Horizontal-] rows of the
    # [-----Algorithm-----] block are [Point] [MaxCounter] [Pulse] ...
    # [DownRatio] ...; MaxCounter is the 1-based sample of the maximum, Pulse
    # its lens pulse, and DownRatio is 0 when the area confirmed no peak.
    areas = []
    m = re.search(r"\[-Horizontal-\]\n(.*?)\n \[-Vertical-\]", text, re.S)
    for line in (m.group(1).splitlines() if m else []):
        v = [int(x) for x in line.split()]
        areas.append({"max_counter": v[1], "pulse": v[2], "confirmed": v[9] > 0})
    confirmed = [a for a in areas if a["confirmed"]]
    # The search ends a few samples after the confirming area's maximum, or
    # runs on to the end of the scan window when the deciding area is flat.
    decider = max(confirmed, key=lambda a: a["max_counter"]) if confirmed else None
    return {
        "file": path.name,
        "fps": first(text, "CafFlameRate"),
        "frame_us": first(text, "VsyncTimeUs"),
        "step": first(text, "CafStepWidth"),
        "grade": "/".join(grade.groups()) if grade else "-",
        "direction": {0: "none", 1: "far", 2: "near"}.get(first(text, "ZafSearchDirection/NOP:0/Far:1/Near:2"), "?"),
        "defocus_um": int(defocus[-1]) if defocus else None,
        "path": first(text, "ContrastAfSearchPath"),
        "n": len(rows),
        "areas": " ".join(f"{a['pulse']}{'*' if a['confirmed'] else '-'}" for a in areas),
        "peak_pulse": decider["pulse"] if decider else None,
        "after_peak": len(rows) - decider["max_counter"] if decider else None,
        "end_pulse": first(text, "DetectEndFocusPulse"),
        "pulses": [r[0] for r in rows],
    }


def main(dirs):
    seen, runs, empty, repeats, partial = set(), [], 0, 0, 0
    for d in dirs:
        for path in sorted(Path(d).glob("*.csv")):
            digest = hashlib.sha256(key(path.read_text(encoding="latin-1")).encode("latin-1")).hexdigest()
            if digest in seen:
                repeats += 1
                continue
            seen.add(digest)
            r = run(path)
            if r["n"] == 0:
                empty += 1
            elif r["fps"] is None:
                # Dumped while the search was still running: no search header
                # yet, and the final dump of the same run follows.
                partial += 1
            else:
                runs.append(r)
    print(f"{'file':14s} {'fps':>4s} {'step':>4s} {'grade':>5s} {'dir':>4s} {'defocus':>7s} {'path':>4s} "
          f"{'n':>3s} {'ms':>4s} {'areas':>12s} {'peak':>5s} {'after':>5s} {'end':>4s}  pulses")
    for r in runs:
        ms = r["n"] * r["frame_us"] / 1000 if r["frame_us"] else 0
        print(f"{r['file']:14s} {r['fps']!s:>4s} {r['step']!s:>4s} {r['grade']:>5s} {r['direction']:>4s} "
              f"{r['defocus_um']!s:>7s} {r['path']!s:>4s} {r['n']:3d} {ms:4.0f} {r['areas']:>12s} {r['peak_pulse']!s:>5s} "
              f"{r['after_peak']!s:>5s} {r['end_pulse']!s:>4s}  {r['pulses']}")
    print(f"\n{len(runs)} runs; left out: {repeats} repeats, {partial} mid-search dumps, {empty} without a search.")
    if runs:
        ms = [r["n"] * r["frame_us"] / 1000 for r in runs]
        count = lambda field: {v: sum(1 for r in runs if r[field] == v) for v in sorted({r[field] for r in runs})}
        long_runs = sum(1 for r in runs if (r["after_peak"] or 0) > 4)
        print(f"samples median {statistics.median(r['n'] for r in runs):g} "
              f"(range {min(r['n'] for r in runs)}-{max(r['n'] for r in runs)}); "
              f"sampling median {statistics.median(ms):.0f} ms (range {min(ms):.0f}-{max(ms):.0f})")
        print(f"fps {count('fps')}; search path {count('path')}; grade {count('grade')}; "
              f"more than 4 samples after the peak: {long_runs}")
    print("ms = samples x frame time (VsyncTimeUs): sampling time of the sweep, not the whole AF lag.")
    print("areas = peak pulse per AF area from the camera's Algorithm block, * confirmed, - no peak;")
    print("peak/after = the confirmed area with the latest maximum; end = scan window end (DetectEndFocusPulse).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
