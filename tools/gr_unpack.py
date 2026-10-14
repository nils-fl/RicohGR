#!/usr/bin/env python3
"""Decode a Ricoh GR III / GR IIIx / GR IV firmware container for offline analysis.

Frame format documented by yeahnope/gr_unpack (GR III); the same format was
used by radium-wang's GR IV inspector. Output is analysis data only and is not
installable on a camera.
"""

import argparse
import hashlib
import struct
from pathlib import Path

HEADER_SIZE = 0x80


def checksum(data: bytes) -> int:
    return sum(w for (w,) in struct.iter_unpack("<I", data[: len(data) & ~3])) & 0xFFFFFFFF


def header_info(data: bytes) -> dict:
    return {
        "model": data[8:20].split(b"\0")[0].decode("ascii", "replace"),
        "version": ".".join(map(str, data[0x38:0x3C])),
        "length": len(data),
        "word_sum_mod_2^32": f"0x{checksum(data):08x}",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def unpack(source: bytes) -> bytes:
    out = bytearray()
    pos = HEADER_SIZE
    while pos + 2 <= len(source):
        prefix = int.from_bytes(source[pos : pos + 2], "big")
        pos += 2
        if prefix == 0:
            break
        end = pos + (prefix & 0x7FFF)
        if end > len(source):
            raise ValueError(f"frame exceeds input at 0x{pos - 2:x}")
        if prefix & 0x8000:  # literal frame
            out += source[pos:end]
            pos = end
            continue
        while pos < end:
            flags = int.from_bytes(source[pos : pos + 2], "big")
            pos += 2
            for bit in range(15, -1, -1):
                if pos >= end:
                    break
                if not flags & (1 << bit):
                    out.append(source[pos])
                    pos += 1
                    continue
                first, second = source[pos : pos + 2]
                pos += 2
                distance = ((first & 0xF8) << 5) + second
                count = first & 7
                if count == 7:
                    ext = source[pos]
                    pos += 1
                    count += ext
                    while ext == 255:
                        ext = source[pos]
                        pos += 1
                        count += ext
                if not distance:
                    break
                if distance > len(out):
                    raise ValueError(f"invalid back reference at 0x{pos:x}")
                for _ in range(count + 3):
                    out.append(out[-distance])
        pos = end
    return bytes(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path)
    parser.add_argument("--out", type=Path, help="decoded output (default: work/<name>.decoded.bin)")
    args = parser.parse_args()

    data = args.firmware.read_bytes()
    for key, value in header_info(data).items():
        print(f"{key:18s} {value}")
    decoded = unpack(data)
    out = args.out or Path("work") / f"{args.firmware.stem}.decoded.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(decoded)
    print(f"{'decoded':18s} {len(decoded)} bytes -> {out}")
    print(f"{'decoded sha256':18s} {hashlib.sha256(decoded).hexdigest()}")


if __name__ == "__main__":
    main()
