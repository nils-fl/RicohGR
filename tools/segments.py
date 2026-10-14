#!/usr/bin/env python3
"""Map the regions of a decoded GR firmware payload (read-only).

Finds the parameter blocks, the RTOS image, Linux zImage, device trees and
gzip streams by signature, validates what it can, and prints a table.
With --split, writes each validated region to work/segments/ for viewing.
"""

import argparse
import re
import struct
import zlib
from pathlib import Path

RTOS_BASE = 0x53000000


def fdt_regions(data: bytes):
    for match in re.finditer(rb"\xd0\x0d\xfe\xed", data):
        off = match.start()
        total, off_struct, off_strings = struct.unpack(">3I", data[off + 4 : off + 16])
        version = struct.unpack(">I", data[off + 20 : off + 24])[0]
        if 16 <= version <= 17 and off_struct < total < 0x200000 and off_strings < total:
            yield off, total


def fdt_props(blob: bytes, wanted=("model", "compatible")):
    off_struct, off_strings = struct.unpack(">2I", blob[8:16])
    pos, depth, path, found = off_struct, 0, [], {}
    while pos < len(blob):
        token = struct.unpack(">I", blob[pos : pos + 4])[0]
        pos += 4
        if token == 1:  # BEGIN_NODE
            end = blob.index(b"\0", pos)
            path.append(blob[pos:end].decode())
            pos = (end + 4) & ~3
        elif token == 2:
            path.pop()
        elif token == 3:
            length, nameoff = struct.unpack(">2I", blob[pos : pos + 8])
            pos += 8
            name = blob[off_strings + nameoff : blob.index(b"\0", off_strings + nameoff)].decode()
            value = blob[pos : pos + length]
            pos = (pos + length + 3) & ~3
            key = "/".join(path) + "/" + name
            if name in wanted or key.endswith(("memory/reg", "chosen/bootargs")):
                found[key or name] = value
        elif token == 9:
            break
    return found


def gzip_regions(data: bytes):
    for match in re.finditer(rb"\x1f\x8b\x08", data):
        off = match.start()
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            out = decomp.decompress(data[off : off + 64 * 1024 * 1024], 256 * 1024 * 1024)
        except zlib.error:
            continue
        consumed = 64 * 1024 * 1024 - len(decomp.unused_data) if decomp.eof else None
        yield off, consumed, len(out), out[:6]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decoded", type=Path)
    parser.add_argument("--split", action="store_true")
    args = parser.parse_args()
    data = args.decoded.read_bytes()
    rows = []

    for name in (b"BOOTPARA", b"DRAMPARA"):
        off = data.find(name)
        if off >= 0:
            rows.append((off, None, name.decode(), "parameter block"))

    src = [m.start() for m in re.finditer(rb"src/[A-Za-z0-9_/]+\.cpp", data)]
    if src:
        # RTOS file offset, derived from MOVW/MOVT string references: GR IIIx 1.60 = 0x10870, GR IV 1.11 = 0x17D10
        rtos_off = 0x10870 if len(data) < 0x3900000 else 0x17D10
        rows.append((rtos_off, None, "RTOS image",
                     f"load base 0x{RTOS_BASE:08x}; {len(src)} source-path strings "
                     f"0x{src[0]:x}..0x{src[-1]:x}"))

    zimage = data.find(struct.pack("<I", 0x016F2818))
    if zimage >= 0:
        start = zimage - 0x24
        size = struct.unpack("<I", data[zimage + 8 : zimage + 12])[0] - struct.unpack("<I", data[zimage + 4 : zimage + 8])[0]
        rows.append((start, size, "Linux zImage", "ARM zImage header"))

    for off, total in fdt_regions(data):
        props = fdt_props(data[off : off + total])
        model = props.get("/model", b"").rstrip(b"\0").decode(errors="replace")
        rows.append((off, total, "device tree", model))

    for off, consumed, size, head in gzip_regions(data):
        rows.append((off, consumed, "gzip stream", f"{size} bytes uncompressed, starts {head!r}"))

    shell = data.find(b"SHELLCMD")
    if shell >= 0:
        rows.append((shell, None, "SHELLCMD table", "engineering command name table (names only)"))

    print(f"{'offset':>10}  {'size':>10}  {'region':16}  notes")
    for off, size, name, notes in sorted(rows):
        print(f"0x{off:08x}  {size if size is not None else '-':>10}  {name:16}  {notes}")

    if args.split:
        outdir = Path("work/segments")
        outdir.mkdir(parents=True, exist_ok=True)
        for off, size, name, _ in rows:
            if size:
                target = outdir / f"{off:08x}-{name.replace(' ', '_')}.bin"
                target.write_bytes(data[off : off + size])
                print(f"wrote {target}")


if __name__ == "__main__":
    main()
