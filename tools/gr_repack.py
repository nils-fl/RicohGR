#!/usr/bin/env python3
"""Rebuild a GR IIIx firmware container from a (possibly modified) payload.

Offline only. Takes the original update file and a decoded payload of the
same length, and writes a new container that keeps the official frame layout
(the method of Maxwooow/Ricoh-GRMod's container code; no code copied):

- every frame keeps its place and output size;
- a frame whose output changes, or whose back-references (up to 8191 bytes)
  now reach changed bytes, becomes a stored frame of exactly 0x6000 bytes
  (prefix E0 00), the only stored form in the official file (729 frames).
  Affected frames are found by decoding the result again, not by a window;
- the first frame (0x8000 bytes) and the last, partial frame cannot be
  stored that way, so edits there are refused;
- the payload's word sum is brought back to 0 with the compensation word,
  4 bytes inside the assert-only text "MakeParameter" (COMP_VA). The payload's
  last word and every block's word sum stay as they were;
- the footer keeps project, magic and type, gets the stream and payload
  lengths, and its last word balances the whole file to a word sum of 0.

Every check the camera's updater makes (docs/firmware-mod-feasibility.html §1.2),
the layout checks below and the rescue-path guard (tools/patch_guard.py)
are re-run on the result, and the result is decoded again and compared.
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
FRAME_SIZE = 0x6000
STORED_PREFIX = b"\xe0\x00"  # 0x8000 | 0x6000
# Compensation word (Read): VA 0x53dc22e8, bytes "Para" of "MakeParameter\0" at 0x53dc22e4, the
# function name the assert routine 0x536b93d0 gets from movw/movt 0x536e9bdc/0x536e9c08
# (src/R2Y/ToneControlTuning.cpp). Shown only when that assert fails. No other reference: movw/movt
# pairs searched without a distance limit, and no literal word points into 0x53dc22e0..0x53dc22f7.
# GR Mod uses the same word on the GR IV.
RTOS_OFFSET, RTOS_VA = 0x10870, 0x53000000
COMP_VA = 0x53DC22E8
COMP_OFFSET = RTOS_OFFSET + COMP_VA - RTOS_VA
COMP_GUARD = b"MakeParameter\0"
COMP_GUARD_OFFSET = COMP_OFFSET - 4


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


def sections(payload: bytes):
    """(name, data offset, size) of each block: 16-byte header of name[8], two u16, size u32.
    The last block, RES, holds a file count in its size field; its data runs to the end."""
    out, o = [], 0
    while o + 16 <= len(payload):
        name = payload[o : o + 8].rstrip(b"\0")
        if not name or not all(32 <= c < 127 for c in name):
            break
        size = struct.unpack_from("<I", payload, o + 12)[0]
        if name == b"RES":
            out.append((name.decode(), o + 16, len(payload) - o - 16))
            break
        out.append((name.decode(), o + 16, size))
        o += 16 + size
    return out


def compensate(original_payload: bytes, payload: bytearray) -> None:
    """Set the compensation word so the payload's word sum is 0. The caller must not touch it."""
    if original_payload[COMP_GUARD_OFFSET : COMP_GUARD_OFFSET + len(COMP_GUARD)] != COMP_GUARD:
        raise SystemExit("compensation word is not inside \"MakeParameter\": wrong firmware?")
    if payload[COMP_OFFSET : COMP_OFFSET + 4] != original_payload[COMP_OFFSET : COMP_OFFSET + 4]:
        raise SystemExit(f"the payload changes the compensation word at 0x{COMP_OFFSET:x}; leave it to this tool")
    payload[COMP_OFFSET : COMP_OFFSET + 4] = b"\0\0\0\0"
    payload[COMP_OFFSET : COMP_OFFSET + 4] = struct.pack("<I", balance(checksum(bytes(payload))))


def build_stream(original: bytes, payload: bytes, layout, keep) -> bytes:
    stream = bytearray()
    for (start, end, out_start, out_end), k in zip(layout, keep):
        if k:
            stream += original[start:end]
        else:
            stream += STORED_PREFIX + payload[out_start:out_end]
    return bytes(stream + b"\0\0")  # terminator


def repack(original: bytes, payload: bytes) -> bytes:
    old = unpack(original)
    if len(payload) != len(old):
        raise SystemExit(f"payload length {len(payload)} != original {len(old)}; resizing is not supported")
    layout = list(frames(original))
    keep = [payload[a:b] == old[a:b] for _, _, a, b in layout]
    while True:
        for i, ((_, _, a, b), k) in enumerate(zip(layout, keep)):
            if not k and b - a != FRAME_SIZE:
                where = "first" if i == 0 else "last" if i == len(layout) - 1 else f"#{i}"
                why = ("a change falls into it" if payload[a:b] != old[a:b]
                       else "its back-references reach changed bytes in an earlier frame")
                raise SystemExit(f"the {where} frame (payload 0x{a:x}-0x{b:x}, 0x{b - a:x} bytes) would have to be "
                                 f"stored, because {why}; only 0x6000-byte frames can be stored")
        stream = build_stream(original, payload, layout, keep)
        decoded = unpack(original[:HEADER_SIZE] + stream)
        bad = [i for i, (_, _, a, b) in enumerate(layout) if decoded[a:b] != payload[a:b]]
        if not bad:
            break
        for i in bad:
            if not keep[i]:
                raise SystemExit(f"stored frame #{i} decoded wrong")
            keep[i] = False  # its back-references reach changed bytes
    stream_len = len(stream)
    stream += b"\0" * (-len(stream) % 4)

    footer = bytearray(original[-FOOTER_SIZE:])
    struct.pack_into("<II", footer, 0x0C, stream_len, len(payload))
    struct.pack_into("<I", footer, 0x14, 0)
    body = original[:HEADER_SIZE] + stream + bytes(footer)
    struct.pack_into("<I", footer, 0x14, balance(checksum(body)))
    return original[:HEADER_SIZE] + stream + bytes(footer)


def check(original: bytes, container: bytes, expected_payload: bytes) -> list:
    """Re-run the updater's container checks (FwDecoder 0x5362bd44, 0x5362bcc8), a decode,
    and the layout checks: the official frames, or full stored frames, at their official places."""
    project = struct.unpack_from("<I", original, 0x30)[0]
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
    old = unpack(original)
    results += [
        ("footer +0x10 = payload length", struct.unpack_from("<I", f, 0x10)[0] == len(decoded)),
        ("payload word sum = 0", checksum(decoded) == 0),
        ("decodes to the expected payload", decoded == expected_payload),
        ("header identical to the official one", h == original[:HEADER_SIZE]),
        ("payload last word unchanged", decoded[-4:] == old[-4:]),
    ]
    so, sn = sections(old), sections(decoded)
    results += [
        ("block layout unchanged", so == sn),
        ("every block's word sum unchanged",
         so == sn and all(checksum(old[o : o + n]) == checksum(decoded[o : o + n]) for _, o, n in so)),
    ]
    fo, fn = list(frames(original)), list(frames(container))
    same_layout = len(fo) == len(fn) and all(a[2:] == b[2:] for a, b in zip(fo, fn))
    form = same_layout and all(
        container[b[0] : b[1]] == original[a[0] : a[1]]
        or (container[b[0] : b[0] + 2] == STORED_PREFIX and b[1] - b[0] == 2 + FRAME_SIZE
            and container[b[0] + 2 : b[1]] == decoded[b[2] : b[3]])
        for a, b in zip(fo, fn))
    results += [
        ("frame layout unchanged", same_layout),
        ("each frame official or stored with 0x6000 bytes", form),
        ("first and last frame official", same_layout and all(
            container[fn[i][0] : fn[i][1]] == original[fo[i][0] : fo[i][1]] for i in (0, -1))),
    ]
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("original", type=Path, help="official update file, e.g. firmware/fwdc243b-v160.bin")
    parser.add_argument("payload", type=Path, help="decoded payload to pack (same length as the original's)")
    parser.add_argument("--out", type=Path, required=True, help="where to write the rebuilt container")
    parser.add_argument("--allow", action="append", choices=["raw", "af"],
                        help="patch_guard allow-list (repeatable; default raw)")
    args = parser.parse_args()

    original = args.original.read_bytes()
    original_payload = unpack(original)
    payload = bytearray(args.payload.read_bytes())
    compensate(original_payload, payload)
    container = repack(original, bytes(payload))

    ok = True
    for name, passed in check(original, container, bytes(payload)):
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed

    from patch_guard import guard  # loads the RTOS image, so import only when needed

    guard_ok, report = guard(original_payload, bytes(payload), exempt=[(COMP_OFFSET, COMP_OFFSET + 4)],
                             allow=tuple(args.allow or ["raw"]))
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
