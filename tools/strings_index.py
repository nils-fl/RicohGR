#!/usr/bin/env python3
"""Index RTOS source files, C++ class names and log strings by subsystem.

Reads a decoded GR firmware payload and writes work/index.json plus a
Markdown summary to stdout. Offline, read-only.
"""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

SUBSYSTEMS = {
    "af": r"^(Af(?!fine)|Caf|Zaf|HybridAf|StatAf|Otheraf|AeAf|AFDF|CaptureZaf|Focus)",
    "lens": r"^(Lens|Mecha|Iris|Shutter|Nd|Stepping)",
    "ae_awb": r"^(Ae|Awb|Exposure|Photometry|Flicker)",
    "stabilisation": r"^(Sr|Gyro|Shake|Affine)",
    "image_pipeline": r"^(Capture|Develop|Raw|Jpeg|Isp|Ppu|Image|Nr|Hdr|Dr)",
    "liveview_display": r"^(LiveView|Lcd|Display|Screen|Osd|View)",
    "ui": r"(Controller|Menu|Page|Dialog)",
    "system": r"^(System|Power|Battery|Temperature|Thermal|Boot|Startup|Shutdown)",
    "comms": r"^(Usb|Mtp|Wlan|Wifi|Ble|Bt|Net|Ipcu|Sric)",
}


def classify(name: str) -> str:
    for key, pattern in SUBSYSTEMS.items():
        if re.search(pattern, name):
            return key
    return "other"


def build(data: bytes) -> dict:
    sources = sorted({m.decode() for m in re.findall(rb"src/[A-Za-z0-9_/]+\.cpp", data)})
    # Itanium-ABI RTTI names: e.g. "25DevelopmentMenuController\0" or nested "N5Zaf...E\0"
    classes = sorted({m.decode() for m in re.findall(rb"(?<=[0-9])([A-Z][A-Za-z0-9_]{3,60})(?=E?\x00)", data)
                      if re.fullmatch(rb"[A-Z][a-z]+[A-Z][A-Za-z0-9_]+", m)})
    af_logs = sorted({m.decode() for m in re.findall(rb"\[(?:AF|CAF|ZAF|Zaf|Caf)\][ -~]{3,100}", data)})
    index = {"sources": defaultdict(list), "classes": defaultdict(list), "af_log_strings": af_logs}
    for path in sources:
        index["sources"][classify(Path(path).stem.split("/")[-1])].append(path)
    for name in classes:
        index["classes"][classify(name)].append(name)
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decoded", type=Path)
    parser.add_argument("--json", type=Path, default=Path("work/index.json"))
    args = parser.parse_args()
    index = build(args.decoded.read_bytes())
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(index, indent=2))
    print(f"# String index ({args.decoded.name})\n")
    for kind in ("sources", "classes"):
        print(f"## {kind}")
        for key in sorted(index[kind], key=lambda k: -len(index[kind][k])):
            print(f"- {key}: {len(index[kind][key])}")
        print()
    print(f"## AF log strings: {len(index['af_log_strings'])}")
    print(f"\nfull index -> {args.json}")


if __name__ == "__main__":
    main()
