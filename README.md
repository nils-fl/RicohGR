# Ricoh GR IIIx firmware: offline AF analysis

[![Documentation](https://img.shields.io/badge/docs-index-9a3412)](docs/index.html)

A personal, read-only study of how the Ricoh GR IIIx autofocus works, built
from the official firmware package.

**Scope**
- Everything here is **offline analysis**. No tool in this repository talks to
  a camera or writes to an SD card.
- `tools/gr_repack.py` can write a firmware file that would pass the camera's
  update checks. Nothing in this repository has been flashed. A modified
  firmware that fails to boot is likely unrecoverable; read
  [`docs/firmware-mod-feasibility.html`](docs/firmware-mod-feasibility.html)
  first.
- The repository **does not contain firmware**. `firmware/` and `work/` are
  git-ignored. Download the firmware yourself from Ricoh's official support
  page; the tools below do that for you.

## Documents

Open [`docs/index.html`](docs/index.html) in a browser to read them.

| Document | Content |
| --- | --- |
| [`docs/af-subsystem.html`](docs/af-subsystem.html) | How the GR IIIx autofocus works: phase detection, contrast detection, how the two are combined, lens calibration, continuous AF, where the time goes (§9), and software levers with practical tips (§10) |
| [`docs/caf-search-state-machine.html`](docs/caf-search-state-machine.html) | Outline of the per-frame contrast-AF search: action codes, search paths, reversal cap, peak checks. Partial decode with unresolved points listed. |
| [`docs/remote-control.html`](docs/remote-control.html) | What the Wi-Fi API and USB (MTP, Pentax vendor mode, factory channel) can set, for a recipe manager |
| [`docs/firmware-mod-feasibility.html`](docs/firmware-mod-feasibility.html) | Whether a modified firmware would be accepted (checksum and version rule, no signature) and what happens if it fails (no recovery found) |
| [`docs/recipe-format.html`](docs/recipe-format.html) | The plain-text `RECIPES.TXT` format for a recipe library and the offline checker; needs no firmware change |
| [`docs/gr3x-vs-gr4.html`](docs/gr3x-vs-gr4.html) | GR IIIx vs GR IV firmware comparison: container, platform, subsystem diff, and an address-level comparison of the AF code (§5) |
| [`docs/gr-mod-landscape.html`](docs/gr-mod-landscape.html) | Community mods for the GR IIIx (GRKit, GR Mod, others) and how our AF work could build on GRKit; built-in AF debug logging; recipes via `UserData Set` |

Statements in the documents are labelled **Read** (taken from the code or
data), **Structural** (from names, strings and dependencies) or **Inferred**
(interpreted). No timings were measured.

## Tools

Requirements: Python 3. `tools/rtos_analysis.py` also needs
[Capstone](https://www.capstone-engine.org/) (`pip install capstone`).

```sh
# 1. Download the official GR IIIx 1.60 package and extract the .bin into firmware/
python3 tools/fetch_firmware.py

# 2. Decode the container into work/ and print header, version and checksum
python3 tools/gr_unpack.py firmware/fwdc243b-v160.bin

# 3. Map the regions of the decoded payload (parameter blocks, RTOS, kernel, device tree, rootfs)
python3 tools/segments.py work/fwdc243b-v160.decoded.bin

# 4. Index source-file names, C++ class names and AF log strings by subsystem
python3 tools/strings_index.py work/fwdc243b-v160.decoded.bin
```

| Tool | Purpose |
| --- | --- |
| `tools/fetch_firmware.py` | Downloads an official package from Ricoh (`--package gr3x_v160.zip` by default) and records its SHA-256 |
| `tools/gr_unpack.py` | Decodes the firmware container for analysis |
| `tools/segments.py` | Lists payload regions and device-tree properties; `--split` writes them to `work/segments/` |
| `tools/strings_index.py` | Writes `work/index.json`, grouping names by subsystem (AF, lens, AE, stabilisation, …) |
| `tools/af_timing_tally.py` | Summarises AF timings you measured from high-speed video (offline arithmetic on your own CSV; see `docs/af-subsystem.html` §10.6) |
| `tools/initramfs.py` | Lists or extracts files from the Linux initramfs (e.g. `webapid`, `mtpd`) into `work/initramfs/` |
| `tools/gr_repack.py` | Rebuilds a container from a decoded payload and re-runs the camera's update checks (offline; writes a file, installs nothing) |
| `tools/patch_guard.py` | Allows payload changes only in RAW-development code and data, keeping the updater and start-up untouched (run by `gr_repack.py`) |
| `tools/recipes.py` | Validates a `RECIPES.TXT` recipe library against the camera capability table and prints each recipe as a dial-in list |
| `tools/rtos_analysis.py` | Interactive helpers for reading the RTOS image (disassembly, cross-references, vtables) |

Using `tools/rtos_analysis.py` from Python:

```python
import sys; sys.path.insert(0, "tools")
from rtos_analysis import *          # reads work/fwdc243b-v160.decoded.bin (override with GR_FW)
show(0x5340ed10, 40)                 # disassemble CalcQuadraticCurve
vtables_for("ZafCorrelation")        # find a class's virtual methods
```

The first cross-reference build takes a few minutes and is cached in
`work/xrefs-w16.pkl` (GR IIIx only; `build_xrefs()` refuses other firmware).

Verified reference values for GR IIIx 1.60:
- model `GR IIIx`, version `1.60.11.17`;
- package SHA-256 `708e79f3…31c7`;
- decoded payload 59,718,388 bytes.

## Key AF findings in brief

- AF runs entirely in the real-time OS. Linux only relays settings.
- **Phase detection** correlates left- and right-looking pixel signals block
  by block. It uses SAD with NEON, verifies the minimum, grades reliability
  against the scene's contrast, and interpolates the shift to a fraction of a
  sample. The shift is converted to defocus with a per-aperture table and an
  exit-pupil correction.
- **Contrast AF** finishes the job. When a phase result is reliable, it jumps
  the lens to the phase target. It then searches a small window
  (about 300 × f-number µm) and fits a parabola to find the sharpness peak.
- Every AF cycle ends with this contrast confirmation. That, together with
  scenes where phase detection is unreliable, accounts for most of the AF
  time. See §9 and §10 of the AF document.

## Credits and related work

- [yeahnope/gr_unpack](https://github.com/yeahnope/gr_unpack): documentation
  of the GR III firmware container format that `tools/gr_unpack.py`
  implements.
- [radium-wang/ricoh-gr4-firmware-analysis-and-feature-expansion](https://github.com/radium-wang/ricoh-gr4-firmware-analysis-and-feature-expansion):
  GR IV firmware research that prompted this project. No code from it is
  included here, and its licence (noncommercial source-available for newer
  material) applies to that project, not this one.
- [hhornbacher/gr3x-fw-hack](https://github.com/hhornbacher/gr3x-fw-hack):
  earlier GR III/IIIx work on the Linux side.
- [ahonn/GRKit](https://github.com/ahonn/GRKit): SD-card plugin system for
  GR III, GR IIIx and GR IV that patches the RTOS in RAM (MIT). Its loader,
  hook and relocation design is summarised in
  [`docs/gr-mod-landscape.html`](docs/gr-mod-landscape.html). No code from it
  is included here.
- [Maxwooow/Ricoh-GRMod](https://github.com/Maxwooow/Ricoh-GRMod): firmware
  modding tool for GR III, GR IIIx and GR IV (GPL-2.0-only). Its container
  code was used to test `tools/gr_repack.py`; no code from it is included
  here.

## Disclaimer

- Unofficial and not affiliated with Ricoh Imaging.
- Ricoh, GR and related marks belong to their owners.
- The findings come from static analysis of one firmware version and may be
  incomplete or wrong.
- This project gives no instructions for modifying a camera.
- No licence has been chosen for this repository yet.
