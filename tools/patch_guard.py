#!/usr/bin/env python3
"""Check that a modified GR IIIx payload only touches RAW-development code.

Offline only. Compares a modified decoded payload with the original and
refuses the change unless every changed byte (except the exempt ranges the
caller names: gr_repack.py's checksum compensation word):

1. lies in the RTOS block;
2. is outside the protected ranges (RTOS entry, firmware updater, start-up control);
3. is code inside one of the allowed source files (the RAW-development
   controllers, model and playback job), or data that only functions in
   those files refer to.

The extent of a source file is taken from the functions that reference its
assert file name; the compiler keeps a file's functions together. An earlier
version protected the direct-call closure of the Update start-up path instead;
that gave 864 or 15,924 protected functions depending on how leaf functions
were counted, so it could not be trusted. See docs/firmware-mod-feasibility.html §2.2.
"""

import argparse
import array
import bisect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rtos_analysis as ra  # noqa: E402

# payload block holding the RTOS image (docs/firmware-mod-feasibility.html §4.1); equals ra.OFF
RTOS_BLOCK_DATA = 0x10870
RTOS_BLOCK_SIZE = 0x10B55C0

PROTECTED_RANGES = [
    (0x53000000, 0x53001000, "RTOS entry and vectors"),
    (0x53627800, 0x5362D800, "firmware updater (FwUpdate, FwDecoder, FwPartition, Resource)"),
    (0x53228800, 0x53229800, "start-up control (BootFactory, start-up dispatcher)"),
]

ALLOWED_SOURCES = [
    b"Controller/RawDevelopmentTopController.cpp",
    b"Controller/RawDevelopmentZoomController.cpp",
    b"Controller/RawDevelopmentParameterSetController.cpp",
    b"Controller/RawDevelopmentWhiteBalanceDetailSetController.cpp",
    b"Controller/RawDevelopmentImageControlDetailSetController.cpp",
    b"Controller/RawDevelopmentManualWhiteBalanceController.cpp",
    b"Model/RawDevelopmentModel.cpp",
    b"src/PlayJobEditRawDevelopment.cpp",
]

# Opt-in second allow-list (--allow af): the contrast-AF units. Their code runs only while the
# camera focuses in shooting mode, which the Update start-up mode does not start, so a bad AF
# change leaves the rescue path working (docs/firmware-mod-feasibility.html §2.1).
AF_SOURCES = [
    b"src/CafProcess.cpp",
    b"src/CafDetection.cpp",
    b"src/CafWobbling.cpp",
]
# The AF files have few assert users, so their assert-based extents are far too small. They are
# compiled back to back; for them the allowed range is the whole span from the first function of
# the first file to the first function of the file that follows the last one (OtherafFactory).
AF_SPAN_END = b"src/OtherafFactory.cpp"
ALLOW_SETS = {"raw": ALLOWED_SOURCES, "af": AF_SOURCES}

MOV_IP_SP = 0xE1A0C00D


def function_starts():
    """Prologue scan: push {..., lr}, moved back to a preceding mov ip, sp (the usual entry)."""
    words = array.array("I", ra.d[ra.OFF : ra.CODE_END & ~3])
    starts = []
    is_push = lambda x: (x & 0xFFFF4000) == 0xE92D4000
    for i, w in enumerate(words):
        if is_push(w):
            # up to three setup instructions may sit between mov ip, sp and the push
            # (e.g. 0x5340c590: mov ip, sp; movw; lsrs; movt; push)
            back = next((j for j in range(i - 1, max(i - 5, -1), -1) if words[j] == MOV_IP_SP), None)
            if back is not None and not any(is_push(words[k]) for k in range(back + 1, i)):
                starts.append(ra.BASE + 4 * back)
            else:
                starts.append(ra.BASE + 4 * i)
    return sorted(set(starts))


def enclosing(starts, addr):
    k = bisect.bisect_right(starts, addr) - 1
    return starts[k] if k >= 0 else None


def string_users(xrefs, starts, text: bytes):
    i = ra.d.find(text + b"\0")
    while i > 0 and ra.d[i - 1] != 0:  # must be a whole string, not a suffix of a longer one
        i = ra.d.find(text + b"\0", i + 1)
    if i < 0:
        return set()
    return {enclosing(starts, r) for r in xrefs.get(ra.va(i), [])} - {None}


def unit_firsts(xrefs, starts):
    """First function that references each src/*.cpp file name, in address order."""
    import re
    firsts = {}
    for m in re.finditer(rb"src/[A-Za-z0-9_/]+\.cpp(?=\0)", ra.d):
        for r in xrefs.get(ra.va(m.start()), []):
            f = enclosing(starts, r)
            if f is not None:
                firsts[m.group()] = min(firsts.get(m.group(), f), f)
    return firsts


def af_span(xrefs, starts):
    firsts = unit_firsts(xrefs, starts)
    lo = min(firsts[s] for s in AF_SOURCES)
    hi = firsts[AF_SPAN_END]
    between = sorted((f, n.decode()) for n, f in firsts.items() if lo <= f < hi)
    names = [n for _, n in between]
    if set(names) != {s.decode() for s in AF_SOURCES}:
        raise SystemExit(f"AF span 0x{lo:08x}-0x{hi:08x} holds other files too: {names}")
    return [(lo, hi, " + ".join(s.decode() for s in AF_SOURCES))]


def references_to(target):
    """Every place that names a function: B/BL/BLX to it, movw/movt of it, or a word equal to it."""
    words = array.array("I", ra.d[ra.OFF : ra.OFF + RTOS_BLOCK_SIZE])
    out = []
    code_words = (ra.CODE_END - ra.OFF) // 4
    for i, w in enumerate(words):
        va = ra.BASE + 4 * i
        if i < code_words and (w & 0x0E000000) == 0x0A000000:  # B/BL (and BLX imm)
            off = w & 0xFFFFFF
            off = (off - (1 << 24) if off & 0x800000 else off) * 4
            if va + 8 + off == target:
                out.append(va)
        elif w == target or w == target | 1:
            out.append(va)
    out += ra.build_xrefs().get(target, [])
    return sorted(set(out))


def allowed_ranges(xrefs, starts, sources=ALLOWED_SOURCES):
    if sources is AF_SOURCES:
        return af_span(xrefs, starts)
    ranges = []
    for src in sources:
        users = sorted(string_users(xrefs, starts, src))
        if not users:
            raise SystemExit(f"allowed source not found: {src.decode()}")
        k = bisect.bisect_right(starts, users[-1])
        ranges.append((users[0], starts[k] if k < len(starts) else users[-1] + 4, src.decode()))
    return ranges


def changed_ranges(old: bytes, new: bytes, chunk: int = 4096):
    out = []
    for base in range(0, len(old), chunk):
        if old[base : base + chunk] == new[base : base + chunk]:
            continue
        i, end = base, min(base + chunk, len(old))
        while i < end:
            if old[i] != new[i]:
                j = i
                while j < len(old) and old[j] != new[j]:
                    j += 1
                if out and out[-1][1] == i:
                    out[-1] = (out[-1][0], j)
                else:
                    out.append((i, j))
                i = j
            else:
                i += 1
    return out


def data_users(xrefs, starts, vlo, vhi, lookback=0x100):
    """Functions that build a pointer into [vlo - lookback, vhi) with movw/movt."""
    users = set()
    for target, refs in xrefs.items():
        if vlo - lookback <= target < vhi:
            users.update(enclosing(starts, r) for r in refs)
    return users - {None}


def guard(old: bytes, new: bytes, exempt=(), allow=("raw",)):
    """Return (ok, report lines). allow names the allow-lists: "raw" (default), "af"."""
    if len(old) != len(new):
        return False, ["payload length changed"]
    xrefs = ra.build_xrefs()
    starts = function_starts()
    allowed = [r for name in allow for r in allowed_ranges(xrefs, starts, ALLOW_SETS[name])]

    def in_allowed(addr):
        return next((name for lo, hi, name in allowed if lo <= addr < hi), None)

    lines, ok = [f"allowed: {len(allowed)} source files ({', '.join(allow)})"], True
    diffs = [r for r in changed_ranges(old, new) if not any(lo <= r[0] and r[1] <= hi for lo, hi in exempt)]
    for lo, hi in diffs:
        if not (RTOS_BLOCK_DATA <= lo and hi <= RTOS_BLOCK_DATA + RTOS_BLOCK_SIZE):
            ok = False
            lines.append(f"REJECT payload 0x{lo:x}-0x{hi:x}: outside the RTOS block")
            continue
        vlo = ra.va(lo)
        vhi = vlo + (hi - lo)
        reasons = [name for a, b, name in PROTECTED_RANGES if vlo < b and a < vhi]
        if ra.is_code(vlo):
            fn = enclosing(starts, vlo)
            where = f"code in function 0x{fn:08x}"
            if not (in_allowed(vlo) and in_allowed(vhi - 1)):
                reasons.append("code outside the allowed source files")
            elif "af" in allow:
                refs = references_to(fn)
                where += f", {len(refs)} reference(s)"
                foreign = [r for r in refs if not in_allowed(r)]  # a vtable word in data also counts as foreign
                if not refs:
                    reasons.append("function with no visible reference (cannot prove who calls it)")
                elif foreign:
                    reasons.append("function reached from outside the allowed files, e.g. "
                                   + ", ".join(f"0x{r:08x}" for r in sorted(set(foreign))[:3]))
        else:
            users = data_users(xrefs, starts, vlo, vhi)
            where = f"data used by {len(users)} function(s)"
            outside = sorted(f for f in users if not in_allowed(f))
            if not users:
                reasons.append("data with no visible user (cannot prove it is safe)")
            elif outside:
                reasons.append("data also used outside the allowed files, e.g. " + ", ".join(f"0x{f:08x}" for f in outside[:3]))
        if reasons:
            ok = False
            lines.append(f"REJECT 0x{vlo:08x}-0x{vhi:08x} ({where}): " + "; ".join(reasons))
        else:
            lines.append(f"ok     0x{vlo:08x}-0x{vhi:08x} ({where}, {in_allowed(vlo) or 'data of the allowed files'})")
    if not diffs:
        lines.append("no changes besides the exempt ranges")
    return ok, lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original", type=Path, help="original decoded payload")
    parser.add_argument("modified", type=Path, help="modified decoded payload")
    parser.add_argument("--allow", action="append", choices=sorted(ALLOW_SETS),
                        help="allow-list to use (repeatable; default raw)")
    args = parser.parse_args()
    from gr_repack import COMP_OFFSET

    ok, lines = guard(args.original.read_bytes(), args.modified.read_bytes(), exempt=[(COMP_OFFSET, COMP_OFFSET + 4)],
                     allow=tuple(args.allow or ["raw"]))
    print("\n".join(lines))
    print("PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
