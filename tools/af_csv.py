#!/usr/bin/env python3
"""Summarise the contrast-AF debug CSVs that the camera writes to the card.

Usage:  python3 tools/af_csv.py DIR [DIR ...]
Reads every *.csv in each DIR (files the camera wrote with
"CAF_Debug SaveDebugFile on", see docs/af-debug-log.html) and prints one row
per AF run: phase result (direction, defocus, decision grade), sample rate,
step width, number of contrast samples, the lens pulse of the contrast peak,
and the samples taken after the peak. Repeats (the camera dumps the last run
again when an AF start has no new search; only the live first table row
differs) and header-only files (no search yet) are counted but not listed.
Offline; reads your own files.
"""
import hashlib, re, sys
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
    """The file without the first row of the sample table, which holds the
    live contrast at dump time rather than search data."""
    return re.sub(r"(\[LensPulsePosition\][^\n]*\n)[^\n]*\n", r"\1", text, count=1)


def run(path):
    text = path.read_text(encoding="latin-1")
    defocus = re.findall(r"^\[ZafDefocusUm\]\t\s*(-?\d+)\s*$", text, re.M)
    grade = re.search(r"^\[DicisionGrade\]\t(\d+)\t(\d+)", text, re.M)
    rows = samples(text)
    h = [r[1] for r in rows]
    peak = max(range(len(h)), key=h.__getitem__) if h else None
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
        "peak_pulse": rows[peak][0] if peak is not None else None,
        "after_peak": len(rows) - 1 - peak if peak is not None else None,
        "pulses": [r[0] for r in rows],
    }


def main(dirs):
    seen, runs, empty, repeats = set(), [], 0, 0
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
            else:
                runs.append(r)
    print(f"{'file':14s} {'fps':>4s} {'step':>4s} {'grade':>5s} {'dir':>4s} {'defocus':>7s} {'path':>4s} "
          f"{'n':>3s} {'ms':>4s} {'peak':>5s} {'after':>5s}  pulses")
    for r in runs:
        ms = r["n"] * r["frame_us"] / 1000 if r["frame_us"] else 0
        print(f"{r['file']:14s} {r['fps']!s:>4s} {r['step']!s:>4s} {r['grade']:>5s} {r['direction']:>4s} "
              f"{r['defocus_um']!s:>7s} {r['path']!s:>4s} {r['n']:3d} {ms:4.0f} {r['peak_pulse']!s:>5s} "
              f"{r['after_peak']!s:>5s}  {r['pulses']}")
    print(f"\n{len(runs)} runs; {repeats} identical repeats and {empty} files without a search left out.")
    print("ms = samples x frame time (VsyncTimeUs): sampling time of the sweep, not the whole AF lag.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
