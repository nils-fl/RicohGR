#!/usr/bin/env python3
"""Check that a modified GR IIIx payload only touches RAW-development code.

Offline only. Compares a modified decoded payload with the original and
refuses the change unless every changed byte (except the payload's last,
balancing word):

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

MOV_IP_SP = 0xE1A0C00D


def function_starts():
    """Prologue scan: push {..., lr}, moved back to a preceding mov ip, sp (the usual entry)."""
    words = array.array("I", ra.d[ra.OFF : ra.CODE_END & ~3])
    starts = []
    for i, w in enumerate(words):
        if (w & 0xFFFF4000) == 0xE92D4000:
            if i >= 2 and words[i - 2] == MOV_IP_SP and words[i - 1] != MOV_IP_SP:
                continue  # already added at the mov ip, sp two words back
            starts.append(ra.BASE + 4 * (i - 1 if i and words[i - 1] == MOV_IP_SP else i))
        elif w == MOV_IP_SP and i + 2 < len(words) and (words[i + 2] & 0xFFFF4000) == 0xE92D4000:
            starts.append(ra.BASE + 4 * i)  # mov ip, sp; <one setup instruction>; push
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


def allowed_ranges(xrefs, starts):
    ranges = []
    for src in ALLOWED_SOURCES:
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


def guard(old: bytes, new: bytes):
    """Return (ok, report lines)."""
    if len(old) != len(new):
        return False, ["payload length changed"]
    xrefs = ra.build_xrefs()
    starts = function_starts()
    allowed = allowed_ranges(xrefs, starts)

    def in_allowed(addr):
        return next((name for lo, hi, name in allowed if lo <= addr < hi), None)

    lines, ok = [f"allowed: {len(allowed)} RAW-development source files"], True
    diffs = [r for r in changed_ranges(old, new) if r[0] < len(old) - 4]
    for lo, hi in diffs:
        if not (RTOS_BLOCK_DATA <= lo and hi <= RTOS_BLOCK_DATA + RTOS_BLOCK_SIZE):
            ok = False
            lines.append(f"REJECT payload 0x{lo:x}-0x{hi:x}: outside the RTOS block")
            continue
        vlo = ra.va(lo)
        vhi = vlo + (hi - lo)
        reasons = [name for a, b, name in PROTECTED_RANGES if vlo < b and a < vhi]
        if ra.is_code(vlo):
            where = f"code in function 0x{enclosing(starts, vlo):08x}"
            if not (in_allowed(vlo) and in_allowed(vhi - 1)):
                reasons.append("code outside the allowed RAW-development files")
        else:
            users = data_users(xrefs, starts, vlo, vhi)
            where = f"data used by {len(users)} function(s)"
            outside = sorted(f for f in users if not in_allowed(f))
            if not users:
                reasons.append("data with no visible user (cannot prove it is safe)")
            elif outside:
                reasons.append("data also used outside RAW development, e.g. " + ", ".join(f"0x{f:08x}" for f in outside[:3]))
        if reasons:
            ok = False
            lines.append(f"REJECT 0x{vlo:08x}-0x{vhi:08x} ({where}): " + "; ".join(reasons))
        else:
            lines.append(f"ok     0x{vlo:08x}-0x{vhi:08x} ({where}, {in_allowed(vlo) or 'RAW-development data'})")
    if not diffs:
        lines.append("no changes besides the balancing word")
    return ok, lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original", type=Path, help="original decoded payload")
    parser.add_argument("modified", type=Path, help="modified decoded payload")
    args = parser.parse_args()
    ok, lines = guard(args.original.read_bytes(), args.modified.read_bytes())
    print("\n".join(lines))
    print("PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
