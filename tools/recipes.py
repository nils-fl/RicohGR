#!/usr/bin/env python3
"""Validate a GR recipe file and print each recipe as a dial-in list.

Offline. The recipe file (RECIPES.TXT) is plain text the owner edits. This
checks each recipe against the GR IIIx image-control capability table decoded
in docs/firmware-mod-feasibility.html §4.3-4.4 and the value sets in
docs/recipe-format.html, and prints the settings to dial in on the camera.
Format 1 holds the image control and its parameters; format 2 adds shooting
settings (white balance, WB shift, exposure, ISO, corrections, NR) and recipe
information (author, source, note).

It talks to no camera and writes nothing to a card.
"""

import argparse
import sys
from pathlib import Path

# Image controls, by the name used in the recipe file.
CONTROLS = {
    "standard": "Standard",
    "vivid": "Vivid",
    "monotone": "Monotone",
    "soft_monotone": "Soft Monotone",
    "hard_monotone": "Hard Monotone",
    "hi_contrast_bw": "Hi-Contrast B&W",
    "negative_film": "Negative Film",
    "positive_film": "Positive Film",
    "bleach_bypass": "Bleach Bypass",
    "retro": "Retro",
    "hdr_tone": "HDR Tone",
    "cross_process": "Cross Process",
}

# Parameters: recipe key -> (display name, allowed values). Ranges are from the
# formatters in Model/DataDispModel.cpp; see the feasibility doc §4.4. Value
# sets were checked against the engineering shell's "UserData Set" value
# matchers (docs/recipe-format.html).
STEP9 = range(-4, 5)  # nine steps, shown -4..+4
TEXT = "text"         # free text, case kept
PARAMS = {
    "saturation": ("Saturation", STEP9),
    "hue": ("Hue", STEP9),
    "key": ("High/Low Key", STEP9),
    "contrast": ("Contrast", STEP9),
    "contrast_highlight": ("Contrast (Highlight)", STEP9),
    "contrast_shadow": ("Contrast (Shadow)", STEP9),
    "sharpness": ("Sharpness", STEP9),
    "shading": ("Shading", STEP9),
    "clarity": ("Clarity", STEP9),
    "grain": ("Grain", range(0, 4)),  # off, 1..3 (firmware: Off, Low, Mid, High)
    "filter": ("Filter Effect", ("none", "1", "2", "3", "4", "custom")),
    "toning": ("Toning", None),  # checked per control below
    "hdr_strength": ("HDR Strength", range(1, 4)),  # firmware: Low, Mid, High
}

# Format 2: shooting settings and recipe information. Valid with every image
# control. The camera keeps them per Custom slot (UserData Still* names).
WB_MODES = ("auto", "multi_auto", "daylight", "shade", "cloudy", "fluorescent_d", "fluorescent_n",
            "fluorescent_w", "fluorescent_l", "tungsten", "cte", "kelvin")
WB_SHIFT_AB = tuple(f"b{i}" for i in range(14, 0, -1)) + ("0",) + tuple(f"a{i}" for i in range(1, 15))
WB_SHIFT_GM = tuple(f"g{i}" for i in range(14, 0, -1)) + ("0",) + tuple(f"m{i}" for i in range(1, 15))
EXPOSURE = tuple("0.0" if i == 0 else f"{i / 3:+.1f}" for i in range(-15, 16))  # -5.0..+5.0 EV in thirds
ISO = ("100", "200", "400", "800", "1600", "3200", "6400", "12800", "25600", "51200", "102400")
SHOOTING = {
    "wb": ("White Balance", WB_MODES),
    "wb_kelvin": ("Color Temperature", range(2500, 10001)),
    "wb_shift_ab": ("WB Shift A-B", WB_SHIFT_AB),
    "wb_shift_gm": ("WB Shift G-M", WB_SHIFT_GM),
    "exposure": ("Exposure Comp.", EXPOSURE),
    "iso": ("ISO", ISO),
    "iso_max": ("Auto ISO Upper Limit", ISO),
    "highlight_correction": ("Highlight Correction", ("auto", "on", "off")),
    "shadow_correction": ("Shadow Correction", ("auto", "low", "mid", "high", "off")),
    "peripheral_correction": ("Peripheral Illum. Corr.", ("on", "off")),
    "high_iso_nr": ("High-ISO NR", ("auto", "low", "mid", "high", "off")),
}
INFO = {
    "author": ("Author", TEXT),
    "source": ("Source", TEXT),
    "note": ("Note", TEXT),
}
GROUP = {**{k: "image" for k in PARAMS}, **{k: "shooting" for k in SHOOTING}, **{k: "info" for k in INFO}}
PARAMS.update(SHOOTING)
PARAMS.update(INFO)
FORMAT2_KEYS = set(SHOOTING) | set(INFO)
IMAGE_KEYS = {k for k, g in GROUP.items() if g == "image"}

# Toning values, by control, in the order the camera lists them (UserData
# value matchers: ColorToneMonotone, ColorToneBleechBypass, ColorToneRetro,
# ColorToneHdrIntense, ColorToneCrossProcess).
_MONO_TONING = ("none", "sepia", "red", "green", "blue", "purple")
TONING = {
    "monotone": _MONO_TONING,
    "soft_monotone": _MONO_TONING,
    "hard_monotone": _MONO_TONING,
    "hi_contrast_bw": _MONO_TONING,
    # The menu shows "--", C, W; the firmware calls them Normal, Cool, Warm
    # (codes 1-3, maker note 0x0247 slot 14). "none" is "--": checked on the
    # camera 2026-11-20 (R0006333).
    "bleach_bypass": ("none", "cool", "warm"),
    "retro": tuple(f"{i:+d}" if i else "0" for i in STEP9),
    "hdr_tone": ("none", "bw", "sepia"),
    "cross_process": ("basic", "magenta", "yellow"),
}

# Which parameters each control supports (capability table at 0x53b5a750;
# Cross Process, added in firmware 1.30, from its UserData names).
_COMMON = {"key", "contrast", "contrast_highlight", "contrast_shadow", "sharpness", "shading"}
_COLOUR = _COMMON | {"saturation", "hue", "clarity"}
_MONO = _COMMON | {"toning", "filter", "clarity", "grain"}
SUPPORTED = {
    "standard": _COLOUR,
    "vivid": _COLOUR,
    "positive_film": _COLOUR,
    "negative_film": _COLOUR | {"grain"},
    "bleach_bypass": _COLOUR | {"toning"},
    "retro": _COLOUR | {"toning"},
    "monotone": _MONO,
    "soft_monotone": _MONO - {"clarity"},
    "hard_monotone": _MONO - {"filter"},
    "hi_contrast_bw": _MONO,
    "hdr_tone": {"saturation", "hue", "toning", "hdr_strength"},
    "cross_process": _COLOUR | {"toning"},
}

MAX_NAME = 32
MAX_RECIPES = 64
MAX_TEXT = 300


class RecipeError(Exception):
    pass


def check_name(name: str):
    """Why a recipe name cannot be written to a recipe file, or None. '#' starts
    a comment, so a name holding one could never be read back."""
    if not name.strip():
        return "empty recipe name"
    if name != name.strip():
        return f"name {name!r} starts or ends with a space"
    if len(name) > MAX_NAME:
        return f"name {name!r} is longer than {MAX_NAME} characters"
    if "#" in name or "\n" in name or "\r" in name:
        return f"name {name!r} contains '#' or a line break"
    if not name.isprintable():
        return f"name {name!r} contains a control character"
    return None


def parse(text: str):
    recipes, name, body, version = [], None, {}, None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if version is None:
            if not line.lower().startswith("format"):
                raise RecipeError(f"line {n}: file must start with a 'format N' line (got {raw.strip()!r})")
            version = line.split()[-1]
            if version not in ("1", "2"):
                raise RecipeError(f"line {n}: unknown format version {version!r}; this tool handles formats 1 and 2")
            continue
        if line.startswith("[") and line.endswith("]"):
            if name is not None:
                recipes.append((name, body))
            name, body = line[1:-1].strip(), {}
            problem = check_name(name)
            if problem:
                raise RecipeError(f"line {n}: {problem}")
        elif "=" in line:
            if name is None:
                raise RecipeError(f"line {n}: setting outside any [recipe]")
            key, _, value = line.partition("=")
            key = key.strip().lower()
            if key in FORMAT2_KEYS and version == "1":
                raise RecipeError(f"line {n}: {key!r} needs 'format 2'")
            text_value = key in PARAMS and PARAMS[key][1] == TEXT
            body[key] = value.strip() if text_value else value.strip().lower()
        else:
            raise RecipeError(f"line {n}: expected '[name]' or 'key = value', got {raw.strip()!r}")
    if name is not None:
        recipes.append((name, body))
    if len(recipes) > MAX_RECIPES:
        raise RecipeError(f"{len(recipes)} recipes; at most {MAX_RECIPES} are allowed")
    return recipes


def check_recipe(name: str, body: dict):
    errors = []
    control = body.get("control")
    if control is None:
        return [f"{name}: no 'control =' line"]
    if control not in CONTROLS:
        return [f"{name}: unknown control {control!r}; one of {', '.join(sorted(CONTROLS))}"]
    allowed = SUPPORTED[control] | (set(PARAMS) - IMAGE_KEYS)
    for key, value in body.items():
        if key == "control":
            continue
        if key not in PARAMS:
            errors.append(f"{name}: unknown setting {key!r}")
            continue
        label, values = PARAMS[key]
        if key not in allowed:
            errors.append(f"{name}: {CONTROLS[control]} has no {label}")
            continue
        if key == "toning":
            values = TONING[control]
            if value not in values:
                errors.append(f"{name}: toning {value!r} not valid for {CONTROLS[control]} ({', '.join(values)})")
        elif values == TEXT:
            if "#" in value or "\n" in value or "\r" in value:
                # Only an editor can produce these; a file could not hold them.
                errors.append(f"{name}: {label} contains '#' or a line break")
            elif len(value) > MAX_TEXT:
                errors.append(f"{name}: {label} is longer than {MAX_TEXT} characters")
            elif not value.isprintable():
                errors.append(f"{name}: {label} contains a control character")
        elif not isinstance(values, range):
            if value not in values:
                errors.append(f"{name}: {key} {value!r} not one of {', '.join(values)}")
        else:
            try:
                num = int(value, 10)
            except ValueError:
                errors.append(f"{name}: {label} needs a number, got {value!r}")
                continue
            if num not in values:
                sign = "+" if values.start < 0 else ""
                errors.append(f"{name}: {label} {num:{sign}d} out of range "
                              f"({values.start:{sign}d}..{values.stop - 1:{sign}d})")
    if "wb_kelvin" in body and body.get("wb") != "kelvin":
        errors.append(f"{name}: wb_kelvin needs wb = kelvin")
    if body.get("wb") == "kelvin" and "wb_kelvin" not in body:
        errors.append(f"{name}: wb = kelvin needs wb_kelvin")
    if "iso" in body and "iso_max" in body:
        errors.append(f"{name}: set iso or iso_max, not both")
    return errors


def dial_in(name: str, body: dict) -> str:
    control = body["control"]
    lines = [f"  {name}", f"    Image Control: {CONTROLS[control]}"]
    for key, (label, values) in PARAMS.items():
        if key in body:
            value = body[key]
            if isinstance(values, range) and values.start < 0:
                value = f"{int(value):+d}"
            elif isinstance(values, range) and values.start == 0:
                value = value if int(value) else "off"
            lines.append(f"    {label}: {value}")
    return "\n".join(lines)


def format_value(key: str, value: str) -> str:
    """The canonical spelling of a valid value: signed for the nine-step
    parameters (0 without a sign), plain for the rest."""
    if key in PARAMS and isinstance(PARAMS[key][1], range):
        num = int(value, 10)
        return f"{num:+d}" if num and PARAMS[key][1].start < 0 else str(num)
    return value


def file_format(recipes) -> int:
    """1 unless a recipe uses a format-2 setting."""
    return 2 if any(key in FORMAT2_KEYS for _, body in recipes for key in body) else 1


def format_file(recipes) -> str:
    """Write recipes as a canonical file: format 1 when it suffices, settings in
    PARAMS order, no comments. parse(format_file(r)) gives r back for valid
    recipes."""
    out = [f"format {file_format(recipes)}"]
    for name, body in recipes:
        out += ["", f"[{name}]", f"control = {body['control']}"]
        out += [f"{key} = {format_value(key, body[key])}" for key in PARAMS if key in body]
    return "\n".join(out) + "\n"


def schema() -> dict:
    """Everything an editor needs to offer only valid recipes, as plain JSON.
    The the companion app Android app (repository its own repository, which has a copy of this
    file) reads this from its assets (android/app/src/main/assets/)."""
    params = []
    for key, (label, values) in PARAMS.items():
        p = {"key": key, "label": label, "group": GROUP[key], "since": 2 if key in FORMAT2_KEYS else 1}
        if isinstance(values, range):
            p.update(kind="int", min=values.start, max=values.stop - 1)
        elif values is None:
            p.update(kind="toning")
        elif values == TEXT:
            p.update(kind="text")
        else:
            p.update(kind="enum", values=list(values))
        params.append(p)
    return {
        "format": 2,
        "max_name": MAX_NAME,
        "max_recipes": MAX_RECIPES,
        "max_text": MAX_TEXT,
        "controls": [{"key": k, "label": v} for k, v in CONTROLS.items()],
        "params": params,
        "supported": {c: [k for k in PARAMS if k in SUPPORTED[c]] for c in CONTROLS},
        "toning": {c: list(v) for c, v in TONING.items()},
    }


def schema_json() -> str:
    import json
    return json.dumps(schema(), indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recipes", type=Path, nargs="?", help="recipe file to check (e.g. RECIPES.TXT)")
    parser.add_argument("--schema-json", type=Path, metavar="PATH",
                        help="write the controls, parameters and value sets as JSON (for the Android app)")
    parser.add_argument("--canonical", action="store_true",
                        help="after checking, print the file in canonical form instead of the dial-in list")
    args = parser.parse_args()

    if args.schema_json:
        args.schema_json.write_text(schema_json())
        print(f"wrote {args.schema_json}")
        if args.recipes is None:
            return
    if args.recipes is None:
        parser.error("give a recipe file, or --schema-json PATH")

    try:
        recipes = parse(args.recipes.read_text())
    except RecipeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)

    all_errors = []
    for name, body in recipes:
        all_errors += check_recipe(name, body)
    if all_errors:
        print("\n".join(all_errors), file=sys.stderr)
        print(f"\n{len(all_errors)} problem(s) in {len(recipes)} recipe(s)", file=sys.stderr)
        raise SystemExit(1)

    if args.canonical:
        print(format_file(recipes), end="")
        return
    print(f"{len(recipes)} recipe(s), all valid. Dial in on the camera:\n")
    print("\n\n".join(dial_in(name, body) for name, body in recipes))


if __name__ == "__main__":
    main()
