#!/usr/bin/env python3
"""List or extract files from the Linux initramfs in a decoded GR payload.

The GR IIIx payload carries the Linux user space as a gzip-compressed newc
cpio archive. This finds it, lists its entries, and with --extract writes
the named files to work/initramfs/ for offline reading. Read-only on the
payload; nothing is written outside work/.
"""

import argparse
import sys
import zlib
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
from segments import gzip_regions  # noqa: E402


def find_cpio(data: bytes) -> bytes:
    for off, _consumed, _size, head in gzip_regions(data):
        if head.startswith(b"070701"):
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[off:], 256 * 1024 * 1024)
    raise SystemExit("no gzip cpio (newc) archive found")


def entries(cpio: bytes):
    pos = 0
    while cpio[pos : pos + 6] == b"070701":
        mode = int(cpio[pos + 14 : pos + 22], 16)
        filesize = int(cpio[pos + 54 : pos + 62], 16)
        namesize = int(cpio[pos + 94 : pos + 102], 16)
        name = cpio[pos + 110 : pos + 110 + namesize - 1].decode(errors="replace")
        start = (pos + 110 + namesize + 3) & ~3
        yield name, mode, cpio[start : start + filesize]
        if name == "TRAILER!!!":
            return
        pos = (start + filesize + 3) & ~3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decoded", type=Path)
    parser.add_argument("--list", action="store_true", help="print size and path of every entry")
    parser.add_argument("--extract", nargs="+", metavar="PATH", help="entries to write to work/initramfs/")
    args = parser.parse_args()

    found = list(entries(find_cpio(args.decoded.read_bytes())))
    if args.list:
        for name, _mode, body in found:
            print(f"{len(body):>10}  {name}")
        print(f"{len(found)} entries")
    for wanted in args.extract or ():
        match = [body for name, mode, body in found if name == wanted and mode & 0o170000 == 0o100000]
        if not match:
            print(f"not a regular file in the archive: {wanted}", file=sys.stderr)
            continue
        target = Path("work/initramfs") / wanted
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(match[0])
        print(f"wrote {target} ({len(match[0])} bytes)")


if __name__ == "__main__":
    main()
