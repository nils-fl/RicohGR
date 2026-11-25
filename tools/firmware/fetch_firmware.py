#!/usr/bin/env python3
"""Download an official Ricoh GR IIIx firmware package and extract the .bin.

Only fetches from Ricoh's public support site. The firmware is written to
firmware/ (git-ignored) and is never redistributed by this project.
"""

import argparse
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://www.ricoh-imaging.co.jp/resources/japan/support/download/digital/FIRMWARE/"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="gr3x_v160.zip", help="zip name on Ricoh's server")
    parser.add_argument("--out", type=Path, default=Path("firmware"))
    args = parser.parse_args()

    url = BASE + args.package
    print(f"fetching {url}")
    data = urllib.request.urlopen(url, timeout=120).read()
    args.out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".bin"):
                continue
            payload = archive.read(name)
            stem = Path(args.package).stem.split("_")[-1]  # e.g. v160
            target = args.out / f"{Path(name).stem}-{stem}.bin"
            target.write_bytes(payload)
            print(f"wrote {target} ({len(payload)} bytes) sha256={hashlib.sha256(payload).hexdigest()}")


if __name__ == "__main__":
    main()
