#!/usr/bin/env python3
"""Validate a GR recipe file and print each recipe as a dial-in list.

Offline. The recipe file (RECIPES.TXT) is plain text the owner edits. This
checks each recipe against the GR IIIx image-control capability table decoded
in docs/firmware-mod-feasibility.html §4.3-4.4, and prints the settings to dial
in on the in-camera RAW development screen (or to switch to over Wi-Fi).

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
# formatters in Model/DataDispModel.cpp; see the feasibility doc §4.4.
STEP9 = range(-4, 5)  # nine steps, shown -4..+4
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
    "grain": ("Grain", range(0, 4)),  # off, 1..3
    "filter": ("Filter Effect", {"none", "1", "2", "3", "4", "custom"}),
    "toning": ("Toning", None),  # checked per control below
    "hdr_strength": ("HDR Strength", range(0, 4)),
}

# Toning value sets, by control (decoded from the toning formatters).
TONING = {
    "monotone": {"none", "sepia", "red", "green", "blue", "purple"},
    "soft_monotone": {"none", "sepia", "red", "green", "blue", "purple"},
    "hard_monotone": {"none", "sepia", "red", "green", "blue", "purple"},
    "hi_contrast_bw": {"none", "sepia", "red", "green", "blue", "purple"},
    "bleach_bypass": {"none", "1", "2", "3"},
    "retro": {"none", "1", "2", "3"},
    "hdr_tone": {"none", "1", "2", "3"},
    # Cross Process has three types; formatter 0x532acf54 shows them as B, M, Y.
    "cross_process": {"basic", "magenta", "yellow"},
}

# Which parameters each control supports (capability table at 0x53b5a750).
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
    "cross_process": {"saturation", "hue", "toning"},
}

MAX_NAME = 16
MAX_RECIPES = 32


class RecipeError(Exception):
    pass


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
            if version != "1":
                raise RecipeError(f"line {n}: unknown format version {version!r}; this tool handles format 1")
            continue
        if line.startswith("[") and line.endswith("]"):
            if name is not None:
                recipes.append((name, body))
            name, body = line[1:-1].strip(), {}
            if not name:
                raise RecipeError(f"line {n}: empty recipe name")
            if len(name) > MAX_NAME:
                raise RecipeError(f"line {n}: name {name!r} is longer than {MAX_NAME} characters")
        elif "=" in line:
            if name is None:
                raise RecipeError(f"line {n}: setting outside any [recipe]")
            key, _, value = line.partition("=")
            body[key.strip().lower()] = value.strip().lower()
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
    allowed = SUPPORTED[control]
    for key, value in body.items():
        if key == "control":
            continue
        if key not in PARAMS:
            errors.append(f"{name}: unknown setting {key!r}")
            continue
        if key not in allowed:
            errors.append(f"{name}: {CONTROLS[control]} has no {PARAMS[key][0]}")
            continue
        if key == "toning":
            values = TONING[control]
            if value not in values:
                errors.append(f"{name}: toning {value!r} not valid for {CONTROLS[control]} ({', '.join(sorted(values))})")
        elif key in ("filter",):
            if value not in PARAMS[key][1]:
                errors.append(f"{name}: filter {value!r} not one of {', '.join(sorted(PARAMS[key][1]))}")
        else:
            try:
                num = int(value, 10)
            except ValueError:
                errors.append(f"{name}: {PARAMS[key][0]} needs a number, got {value!r}")
                continue
            rng = PARAMS[key][1]
            if num not in rng:
                errors.append(f"{name}: {PARAMS[key][0]} {num:+d} out of range ({rng.start:+d}..{rng.stop - 1:+d})")
    return errors


def dial_in(name: str, body: dict) -> str:
    control = body["control"]
    lines = [f"  {name}", f"    Image Control: {CONTROLS[control]}"]
    for key, (label, _) in PARAMS.items():
        if key in body:
            value = body[key]
            if key in ("grain", "hdr_strength"):
                value = value if int(value) else "off"
            elif key not in ("toning", "filter"):
                value = f"{int(value):+d}"
            lines.append(f"    {label}: {value}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recipes", type=Path, help="recipe file to check (e.g. RECIPES.TXT)")
    args = parser.parse_args()

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

    print(f"{len(recipes)} recipe(s), all valid. Dial in on the RAW development screen:\n")
    print("\n\n".join(dial_in(name, body) for name, body in recipes))


if __name__ == "__main__":
    main()
