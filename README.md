# Ricoh GR IIIx firmware: AF study and camera tools

[![Documentation](https://img.shields.io/badge/docs-index-9a3412)](docs/index.html)

A study of how the Ricoh GR IIIx autofocus works, read from the official
firmware 1.60, and the tools built on it:

- **Offline analysis**: unpack the update file, map its regions, disassemble
  the RTOS, index names by subsystem.
- **SD-card setups** that use the camera's own script and engineering-shell
  features: the built-in AF debug log, a Wi-Fi test bench for AF patches in
  RAM, and a card that writes recipes into the Custom 1/2 image-control slots.
- **A firmware build** with a tested dim-light AF setting
  (`tools/af_firmware.py`), flashed on the maintainer's camera on 2026-11-20.

Everything is for the **GR IIIx with firmware 1.60** only. Other models and
versions are refused by the tools or simply not covered.

> **Read this before using anything on a camera.**
> - The SD-card setups change no firmware. A power-off undoes everything they
>   do in RAM, and without the card the camera starts as before.
> - Flashing a modified update file is different. If a modified RTOS crashed
>   before the update check, the camera could not reach its own updater
>   again, and no recovery path is known
>   ([`docs/firmware-mod-feasibility.html`](docs/firmware-mod-feasibility.html) §2).
>   The AF build changes only contrast-AF code, which the update start-up
>   mode does not run, and the official 1.60 file can be flashed back over it.
>   It is still at your own risk.
> - Use a separate card, formatted in the camera, with a full battery.

## Repository layout

| Path | What |
| --- | --- |
| `docs/` | The documents, as HTML. Start at [`docs/index.html`](docs/index.html) |
| `tools/` | Python tools (table below); `tools/tests/` for pytest |
| `recipes/`, `RECIPES.TXT.example` | Community recipes and the recipe file format |
| `site/` | Static summary page (Cloudflare Workers assets); the maintainer's deployment |
| `firmware/` | *Ignored.* Official update files, downloaded by `tools/fetch_firmware.py` |
| `work/` | *Ignored.* Everything generated: decoded images, caches, card files, camera logs |

The repository contains no firmware and none of its decoded content. Each
developer downloads the official file from Ricoh.

## Setup

Requirements:
- Python 3.10 or newer;
- `pip install -r requirements.txt` (Capstone for the RTOS tools, Optuna for
  the parameter search, pytest);
- `exiftool` for `recipe_card.py verify`;
- macOS was used throughout. Card and Wi-Fi steps assume it, the Python tools
  do not.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Download the official GR IIIx 1.60 package and extract the .bin into firmware/
python3 tools/fetch_firmware.py

# Decode the container into work/ and print header, version and checksum
python3 tools/gr_unpack.py firmware/fwdc243b-v160.bin

# Map the payload regions, index names by subsystem
python3 tools/segments.py work/fwdc243b-v160.decoded.bin
python3 tools/strings_index.py work/fwdc243b-v160.decoded.bin

pytest tools/tests
```

Check the reference values for 1.60:
- model `GR IIIx`, version `1.60.11.17`;
- package SHA-256 `708e79f3…31c7`;
- decoded payload 59,718,388 bytes.

Reading the RTOS from Python:

```python
import sys; sys.path.insert(0, "tools")
from rtos_analysis import *          # reads work/fwdc243b-v160.decoded.bin (override with GR_FW)
show(0x5340ed10, 40)                 # disassemble CalcQuadraticCurve
vtables_for("ZafCorrelation")        # find a class's virtual methods
```

The first cross-reference build takes a few minutes and is cached in
`work/xrefs-w16.pkl`.

## Camera setups

Every card tool writes files into `work/`, never to a card directly. Copy
the folder's contents to the card root yourself. On a Mac, remove the `._*`
files macOS leaves on exFAT cards before ejecting (`dot_clean -m /Volumes/<card>`).

### 1. Turn SD scripts on (once)

```sh
python3 tools/af_debug_card.py      # writes work/af-debug-card/{enable,log,disable}
```

1. Copy the contents of `work/af-debug-card/enable/` to the card root.
2. Hold MENU and switch on. The camera shows "Mode Set Command", then
   "Complete", and switches itself off.
3. Delete the two enable files from the card.

From now on the camera runs `SCRIPT/STARTUP.TTL` from the card at each
power-on. `disable/` turns this off again the same way. Full procedure and
what to do if the camera shows something else:
[`docs/af-debug-log.html`](docs/af-debug-log.html) §2–3 and §7.

Only one setup can be on a card at a time, since each brings its own
`SCRIPT/STARTUP.TTL`.

### 2. AF debug log

Copy `work/af-debug-card/log/SCRIPT` to the card. At each power-on it turns
on the camera's own contrast-AF CSV log. Analyse the CSVs with
`tools/af_csv.py` and `tools/af_light.py`; `tools/af_charts.py` rebuilds
[`docs/af-charts.html`](docs/af-charts.html) from your logs in
`work/af-logs/`. Procedure, sessions and results:
[`docs/af-debug-log.html`](docs/af-debug-log.html).

### 3. Wi-Fi AF test bench (stock firmware only)

`python3 tools/af_debug_card.py --remote` writes a card that serves a small
endpoint on port 8080. With it `tools/af_remote.py` presses AF through the
camera's Wi-Fi API, switches RAM patches ("arms") and reads the AF trace.
`tools/af_optimize.py` runs the Optuna parameter search on top of it.

- Join the camera's Wi-Fi on the Mac. Requests are pinned to `--iface`
  (default `en0`) in case the wired network uses the same 192.168.0.x range.
- Patches live in RAM and are gone at power-off.
- **Not on a camera with the AF build flashed.** The bench expects stock
  words at every patch site, and `arm STOCK` writes stock words into RAM,
  which mixes the flashed setting with stock code until the next power-on.

### 4. Recipes into Custom 1 / Custom 2 (test, not yet run on a camera)

```sh
python3 tools/recipe_card.py card                      # writes work/recipe-card/
python3 tools/recipe_card.py lines recipes/ricoh-recipes.txt "Reggie's Color Negative" --slot 1
```

Copy `work/recipe-card/*` to the card root, switch on (the AF lamp flashes
twice when ready) and join the camera's Wi-Fi. `check`, then `send … --slot N`
writes the recipe with the engineering shell's `UserData Set`. The relay on
this card accepts only `UserData` and the AF lamp. Test plan and the
settings that are not sent yet:
[`docs/recipe-format.html#send-to-camera`](docs/recipe-format.html#send-to-camera).

### 5. The AF firmware build (flash at your own risk)

```sh
python3 tools/af_firmware.py        # writes work/firmware-af/FWDC243B.BIN
python3 tools/af_firmware.py --afc  # + AF-C fast drive: work/firmware-af-afc/FWDC243B.BIN
```

The build changes 19 words in the contrast-AF code, the setting tested on
the bench as X875+FALLS2+OPT+NO29: better dim-light AF, 49 instead of 29 fps
in dim light. `tools/gr_repack.py` re-runs the updater's container checks
and, with `--allow af`, `tools/patch_guard.py` refuses any change outside
the contrast-AF units. What changes, the AF-C check before flashing and the
first results: [`docs/af-debug-log.html#af-flash`](docs/af-debug-log.html#af-flash).
`--afc` adds one more word: AF-C searches use the 200 fps AF drive instead of
the 59 fps live-view drive. That build is prepared, not flashed; what is still
untested is listed in
[`docs/af-debug-log.html#af-flash-afc-build`](docs/af-debug-log.html#af-flash-afc-build).

To flash: full battery, a card with only `FWDC243B.BIN` in its root, hold
MENU while switching on, confirm the update, switch off when it says
completed. Remove the file from the card afterwards: MENU at power-on also
starts the update. **To go back**, flash the official 1.60 file the same way.

### 6. Grain on the colour controls and a stronger grain (RAM test, tested 2026-11-25)

```sh
python3 tools/effects_card.py                       # writes work/effects-card/
python3 tools/effects_card.py --grain 0x80,0x180,0x280   # other Low/Mid/High scales
```

Copy `work/effects-card/*` to the root of a card on which SD scripts are
already on (setup 1), and switch on. The card writes nothing to firmware.
It writes two changes into RAM:
- **A**: a working Grain row in RAW development for Standard, Vivid,
  Positive Film, Negative Film, Bleach Bypass, Retro and Cross Process
  (40 words: capability table, row lists, ImageTuning, still planner).
- **B**: stronger grain levels. By default Low, Mid and High become 1×,
  2× and 3× the stock High. This also applies to shots taken with the
  monotone controls and Negative Film.

Delete `SCRIPT/EFFECTS/A.ON` or `B.ON` to leave one out. The AF lamp
flashes twice when the changes are in place and stays on for 3 s when
they failed (see `SCRIPT/EFFECTS/EFFECTS.LOG`). Power off, or start
without the card, and the camera is stock again. It is safe with the AF
build flashed: no word is shared with it, and no bench is used.
Automated instead: `python3 tools/effects_solo.py card` writes a recipe card whose
solo run shoots stock and patched grain by itself after one power-on (11 shots,
about 3 minutes), and `effects_solo.py import` measures the grain afterwards.
`effects_solo.py card --test leak` checks that colour shots do not pick up
grain from an earlier monotone shot while A is loaded (they did not).
Findings, test order and what is still open:
[`docs/image-effects.html`](docs/image-effects.html).

### 7. The AF + grain firmware build (flash at your own risk; built, not flashed)

```sh
python3 tools/effects_firmware.py                    # writes work/firmware-af-effects/FWDC243B.BIN
python3 tools/effects_firmware.py --grain 0x80,0x180,0x280   # other Low/Mid/High scales
```

The 19 AF words of setup 5 plus A and B of setup 6 (62 words; B at 1×, 2×
and 3× the stock High). `tools/gr_repack.py` runs `tools/patch_guard.py`
with `--allow af --allow effects`. The effects list has no source files:
it accepts only the exact words tested in RAM (`effects_map.effect_word_kind`).
Their data must be used only by RAW-development files, and the row lists
are checked exactly (`effects_map.row_list_proof`). Eight of the words are
code in shared image-processing functions (ImageTuning, the still planner,
the grain routine) whose callers cannot all be seen, so the guard vouches
for them by test, not by unit. The protected ranges stay locked. Flash as
in setup 5. To undo, flash `work/firmware-af/FWDC243B.BIN` or the official
file. Details: [`docs/image-effects.html#flash`](docs/image-effects.html#flash).

## Tools

| Tool | Purpose |
| --- | --- |
| `fetch_firmware.py` | Downloads an official package from Ricoh (`--package gr3x_v160.zip` by default) and records its SHA-256 |
| `gr_unpack.py` | Decodes the firmware container |
| `segments.py` | Lists payload regions and device-tree properties; `--split` writes them to `work/segments/` |
| `strings_index.py` | Writes `work/index.json`, grouping names by subsystem (AF, lens, AE, stabilisation, …) |
| `initramfs.py` | Lists or extracts files from the Linux initramfs (e.g. `webapid`, `mtpd`) |
| `rtos_analysis.py` | Interactive helpers for reading the RTOS image (disassembly, cross-references, vtables) |
| `gr_repack.py` | Rebuilds a container from a decoded payload and re-runs the updater's checks (writes a file, installs nothing) |
| `patch_guard.py` | Allows payload changes only in RAW-development code (default) or contrast-AF code (`--allow af`) |
| `af_firmware.py` | Builds the AF update file of setup 5 |
| `af_debug_card.py` | Card files for SD scripts, the AF debug log, the Wi-Fi bench (`--remote`) and AF trace decoding |
| `af_remote.py` | Drives the Wi-Fi bench from the Mac: presses, arms, sessions, trace fetch |
| `af_optimize.py` | Optuna search over the contrast-AF detector constants, on the bench |
| `af_csv.py` | One row per contrast-AF run from the debug CSVs |
| `af_light.py` | Light, noise and peak margin of each logged sweep, by sensor mode |
| `af_replay.py` | Replays recorded AF passes offline: detector and stop-decision port, other stop rules, peak fits |
| `af_tolerance.py` | Focus tolerance on the flashed camera: AF-S lock, motor offset, photo, sharpness per patch |
| `af_charts.py` | Writes `docs/af-charts.html` from your logs |
| `af_timing_tally.py` | Summarises AF timings measured from high-speed video |
| `effects_map.py` | Read-only map of the grain path and the candidate effect words, checked against the 1.60 image |
| `effects_card.py` | RAM test card for the grain changes A and B (setup 6) |
| `effects_solo.py` | Automated grain sessions (stock vs arm B; `--test leak`) as recipe-card solo runs, and their `import` with grain measurement |
| `effects_firmware.py` | AF + grain update file (setup 7), guarded by `patch_guard.py --allow af --allow effects` |
| `recipes.py` | Checks a `RECIPES.TXT` library, prints dial-in lists and `UserData Set` lines (`--userdata N`) |
| `recipe_card.py` | The recipe card of setup 4 and its Mac commands |

## Documents

| Document | Content |
| --- | --- |
| [`af-subsystem.html`](docs/af-subsystem.html) | How the AF works: phase and contrast detection, how they combine, lens calibration, continuous AF, where the time goes (§9), software levers (§10) |
| [`caf-search-state-machine.html`](docs/caf-search-state-machine.html) | The per-frame contrast-AF search: action codes, paths, reversal cap, peak checks (partial decode) |
| [`af-debug-log.html`](docs/af-debug-log.html) | Camera procedures, all sessions, the RAM patches, the parameter search and the flashed setting |
| [`af-charts.html`](docs/af-charts.html) | Charts of the AF measurements |
| [`firmware-mod-feasibility.html`](docs/firmware-mod-feasibility.html) | Whether a modified firmware is accepted (checksum and version rule, no signature) and what a failure means |
| [`remote-control.html`](docs/remote-control.html) | What the Wi-Fi API and USB can set |
| [`recipe-format.html`](docs/recipe-format.html) | The `RECIPES.TXT` format, the firmware's value sets and sending recipes to Custom 1/2 |
| [`image-effects.html`](docs/image-effects.html) | Grain and the digital-filter stage: where grain is gated per control, grain strength, candidate changes |
| [`gr-mod-landscape.html`](docs/gr-mod-landscape.html) | Community mods (GRKit, GR Mod, others), built-in AF debug logging, recipes via `UserData Set` |
| [`gr3x-vs-gr4.html`](docs/gr3x-vs-gr4.html) | GR IIIx vs GR IV firmware: container, platform, AF code at address level |

Statements in the documents are labelled **Read** (taken from the code or
data), **Structural** (from names, strings and dependencies) or **Inferred**
(interpreted).

## Key AF findings in brief

- AF runs entirely in the real-time OS. Linux only relays settings.
- **Phase detection** correlates left- and right-looking pixel signals block
  by block (SAD with NEON), grades reliability against the scene's contrast
  and interpolates the shift to a fraction of a sample.
- **Contrast AF** finishes the job: it jumps to the phase target when that is
  reliable, then searches a small window (about 300 × f-number µm) and fits a
  parabola to the sharpness peak.
- In dim light the peak check rejects clear peaks (margin curve, three falls,
  29 fps). The flashed setting relaxes exactly these; see
  [`docs/af-debug-log.html`](docs/af-debug-log.html).

## Contributing

- Docs are HTML in `docs/`. Link a new page from `docs/index.html`, and label
  statements Read / Structural / Inferred.
- Never commit firmware, decoded images or anything from `work/`. Camera logs
  stay local; `docs/af-charts.html` carries only the data its charts draw.
- Never log or commit the camera's Wi-Fi SSID, key, serial or MAC address.
  `/v1/props` returns them; `af_remote.py` drops them (`PRIVATE_PROPS`).
- Run `pytest tools/tests` before committing. Changes to the recipe rules in
  `tools/recipes.py` must also go into the companion copy of the file, which
  has the shared test vectors.

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

## Licence and disclaimer

The code and documents are under the [MIT licence](LICENSE). The firmware
belongs to Ricoh Imaging and is not part of this repository.

- Unofficial and not affiliated with Ricoh Imaging. Ricoh, GR and related
  marks belong to their owners.
- The findings come from one firmware version and may be incomplete or
  wrong.
- Everything that touches a camera is at your own risk. A failed firmware
  flash may need a factory repair.
