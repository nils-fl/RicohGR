#!/usr/bin/env python3
"""Rebuild a GR IIIx firmware container from a (possibly modified) payload.

Offline only. Takes the original update file and a decoded payload of the
same length, and writes a new container:

- frames whose output does not change are copied byte for byte;
- frames whose output changes, or that can back-reference changed bytes
  (the decoder looks back up to 8191 bytes), become literal frames;
- the payload's last word is rebalanced so its 32-bit word sum is 0;
- the footer keeps project, magic and type, gets the stream and payload
  lengths, and its last word balances the whole file to a word sum of 0.

Every check the camera's updater makes (docs/firmware-mod-feasibility.html §1.2)
and the rescue-path guard (tools/patch_guard.py)
is re-run on the result, and the result is decoded again and compared.
Writing the file does not install anything; this tool never talks to a camera.
"""

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gr_unpack import HEADER_SIZE, checksum, unpack  # noqa: E402

FOOTER_SIZE = 0x18
MAGIC = 0xA55A5AA5
MAX_DISTANCE = 0x1FFF
MAX_LITERAL = 0x7FFF


def frames(source: bytes):
    """Yield (start, end, out_start, out_end) for each frame, mirroring gr_unpack.unpack."""
    out_len = 0
    pos = HEADER_SIZE
    while pos + 2 <= len(source):
        prefix = int.from_bytes(source[pos : pos + 2], "big")
        if prefix == 0:
            return
        start, body, end = pos, pos + 2, pos + 2 + (prefix & 0x7FFF)
        if prefix & 0x8000:
            produced = end - body
        else:
            produced, p = 0, body
            while p < end:
                flags = int.from_bytes(source[p : p + 2], "big")
                p += 2
                for bit in range(15, -1, -1):
                    if p >= end:
                        break
                    if not flags & (1 << bit):
                        produced += 1
                        p += 1
                        continue
                    first, second = source[p : p + 2]
                    p += 2
                    count = first & 7
                    if count == 7:
                        ext = source[p]
                        p += 1
                        count += ext
                        while ext == 255:
                            ext = source[p]
                            p += 1
                            count += ext
                    if not ((first & 0xF8) << 5) + second:
                        break
                    produced += count + 3
        yield start, end, out_len, out_len + produced
        out_len += produced
        pos = end


def balance(words_sum: int) -> int:
    return (-words_sum) & 0xFFFFFFFF


def rebalance_payload(payload: bytearray) -> None:
    payload[-4:] = b"\0\0\0\0"
    payload[-4:] = struct.pack("<I", balance(checksum(bytes(payload))))


def literal_frames(data: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(data), MAX_LITERAL):
        chunk = data[i : i + MAX_LITERAL]
        out += struct.pack(">H", 0x8000 | len(chunk)) + chunk
    return bytes(out)


def repack(original: bytes, payload: bytes) -> bytes:
    old = unpack(original)
    if len(payload) != len(old):
        raise SystemExit(f"payload length {len(payload)} != original {len(old)}; resizing is not supported")
    changed = [i for i in range(0, len(old), 4096) if old[i : i + 4096] != payload[i : i + 4096]]
    dirty = []  # output ranges that must not be reused verbatim
    for block in changed:
        lo = block + next(j for j in range(4096) if old[block + j] != payload[block + j])
        hi = block + max(j for j in range(min(4096, len(old) - block)) if old[block + j] != payload[block + j]) + 1
        dirty.append((lo, hi + MAX_DISTANCE))

    stream = bytearray()
    run_start = None  # output offset where the current literal run began
    for start, end, out_start, out_end in frames(original):
        touched = any(out_start < hi and lo < out_end for lo, hi in dirty)
        if touched:
            if run_start is None:
                run_start = out_start
            continue
        if run_start is not None:
            stream += literal_frames(payload[run_start:out_start])
            run_start = None
        stream += original[start:end]
    if run_start is not None:
        stream += literal_frames(payload[run_start:])
    stream += b"\0\0"  # terminator
    stream_len = len(stream)
    stream += b"\0" * (-len(stream) % 4)

    footer = bytearray(original[-FOOTER_SIZE:])
    struct.pack_into("<II", footer, 0x0C, stream_len, len(payload))
    struct.pack_into("<I", footer, 0x14, 0)
    body = original[:HEADER_SIZE] + bytes(stream) + bytes(footer)
    struct.pack_into("<I", footer, 0x14, balance(checksum(body)))
    return original[:HEADER_SIZE] + bytes(stream) + bytes(footer)


def check(container: bytes, expected_payload: bytes, project: int) -> list:
    """Re-run the updater's container checks (FwDecoder 0x5362bd44, 0x5362bcc8) plus a decode."""
    h, f = container[:HEADER_SIZE], container[-FOOTER_SIZE:]
    proj_pair = project | project << 16
    results = [
        ("header +0x30 project", struct.unpack_from("<I", h, 0x30)[0] == project),
        ("header +0x78 project pair", struct.unpack_from("<I", h, 0x78)[0] == proj_pair),
        ("header +0x7c magic", struct.unpack_from("<I", h, 0x7C)[0] == MAGIC),
        ("footer +0x00 project pair", struct.unpack_from("<I", f, 0)[0] == proj_pair),
        ("footer +0x04 magic", struct.unpack_from("<I", f, 4)[0] == MAGIC),
        ("footer +0x08 type = 2 (user update)", struct.unpack_from("<I", f, 8)[0] == 2),
        ("file length multiple of 4", len(container) % 4 == 0),
        ("file word sum = 0", checksum(container) == 0),
    ]
    decoded = unpack(container)
    results += [
        ("footer +0x10 = payload length", struct.unpack_from("<I", f, 0x10)[0] == len(decoded)),
        ("payload word sum = 0", checksum(decoded) == 0),
        ("decodes to the expected payload", decoded == expected_payload),
    ]
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original", type=Path, help="official update file, e.g. firmware/fwdc243b-v160.bin")
    parser.add_argument("payload", type=Path, help="decoded payload to pack (same length as the original's)")
    parser.add_argument("--out", type=Path, required=True, help="where to write the rebuilt container")
    args = parser.parse_args()

    original = args.original.read_bytes()
    payload = bytearray(args.payload.read_bytes())
    rebalance_payload(payload)
    container = repack(original, bytes(payload))
    project = struct.unpack_from("<I", original, 0x30)[0]

    ok = True
    for name, passed in check(container, bytes(payload), project):
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed

    from patch_guard import guard  # loads the RTOS image, so import only when needed

    guard_ok, report = guard(unpack(original), bytes(payload))
    print("\n".join("guard " + line for line in report))
    print(f"{'PASS' if guard_ok else 'FAIL'}  rescue path untouched (tools/patch_guard.py)")
    ok &= guard_ok
    print(f"identical to original: {container == original}")
    print(f"size: {len(container)} bytes (original {len(original)})")
    if not ok:
        raise SystemExit("checks failed; nothing written")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(container)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
