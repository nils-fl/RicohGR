"""Tests for tools/recipes.py and the files the Android app takes from it.

Two generated files are checked in, and these tests fail when either is stale:
- tools/recipe-vectors.json: shared test cases; the Android port runs the same
  cases (android/app/src/test/.../RecipeVectorsTest.kt) and must give the same
  results, error messages included.
- android/app/src/main/assets/recipe-schema.json: the editor's schema. The app
  moved to its own repository (its own repository) with a copy of this file;
  here the schema is only checked and written when android/ exists.

Regenerate both after changing recipes.py:
    python3 tools/tests/test_recipes.py --regen
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import recipes as r  # noqa: E402

VECTORS = ROOT / "tools" / "recipe-vectors.json"
SCHEMA = ROOT / "android" / "app" / "src" / "main" / "assets" / "recipe-schema.json"

H = "format 1\n"
NINE = [("saturation", "+4"), ("hue", "-4"), ("key", "+1"), ("contrast", "-1"), ("contrast_highlight", "+2"),
        ("contrast_shadow", "-2"), ("sharpness", "+3"), ("shading", "-3"), ("clarity", "+1")]
H2 = "format 2\n"
FULL = ("[Reggie's Color Negative]\ncontrol = negative_film\nsaturation = +2\nhue = 0\nkey = 0\ncontrast = +3\n"
        "contrast_highlight = -4\ncontrast_shadow = -1\nsharpness = +1\nshading = 0\nclarity = 0\n"
        "author = Reggie Ballesteros\nsource = https://ricohrecipes.com/the-analog-collection/\n"
        "note = Exposure as published: -1/3 to +1/3 (typically)\n"
        "wb = auto\nwb_shift_ab = a6\nwb_shift_gm = 0\nexposure = 0.0\niso_max = 6400\nhighlight_correction = auto\n"
        "shadow_correction = mid\nperipheral_correction = on\nhigh_iso_nr = off\n")
CASES = [
    ("example file", (ROOT / "RECIPES.TXT.example").read_text()),
    ("minimal", H + "[A]\ncontrol = standard\n"),
    ("zero is unsigned", H + "[A]\ncontrol = vivid\nsaturation = +0\nhue = 0\n"),
    ("keys and values are case-insensitive", H + "[Mono]\nCONTROL = Monotone\nToning = SEPIA\nFilter = Custom\n"),
    ("comments and blank lines", "# lead\n\nformat 1 # v\n\n[A] # c\ncontrol = standard # c\n\n"),
    ("settings reorder to canonical order", H + "[A]\ncontrol = standard\nclarity = +1\nsaturation = -2\n"),
    ("all nine-step params", H + "[All]\ncontrol = standard\n" + "".join(f"{k} = {v}\n" for k, v in NINE)),
    ("cross process toning", H + "[X]\ncontrol = cross_process\ntoning = magenta\nhue = +1\n"),
    ("hdr tone", H + "[H]\ncontrol = hdr_tone\nhdr_strength = 3\ntoning = sepia\n"),
    ("negative film grain", H + "[N]\ncontrol = negative_film\ngrain = 1\n"),
    ("windows line ends", "format 1\r\n[A]\r\ncontrol = standard\r\nkey = -1\r\n"),
    ("no format line", "[A]\ncontrol = standard\n"),
    ("format 2 without format-2 settings writes format 1", "format 2\n[A]\ncontrol = standard\n"),
    ("format 3", "format 3\n[A]\ncontrol = standard\n"),
    ("empty name", H + "[ ]\ncontrol = standard\n"),
    ("name too long", H + "[" + "Thirty-three characters, too long" + "]\ncontrol = standard\n"),
    ("name of exactly 32", H + "[" + "Reggie's Color Negative, take 2!" + "]\ncontrol = standard\n"),
    ("long name with quote", H + "[Kid's Long Mono, far too long for it]\ncontrol = monotone\n"),
    ("setting before any recipe", H + "control = standard\n"),
    ("garbage line", H + "[A]\ncontrol standard\n"),
    ("too many recipes", H + "".join(f"[R{i}]\ncontrol = standard\n" for i in range(65))),
    ("exactly 64 recipes", H + "".join(f"[R{i}]\ncontrol = standard\n" for i in range(64))),
    ("no control", H + "[A]\nsaturation = +1\n"),
    ("unknown control", H + "[A]\ncontrol = cinema\n"),
    ("unknown setting", H + "[A]\ncontrol = standard\nwhite_balance = auto\n"),
    ("unsupported for control", H + "[A]\ncontrol = monotone\nsaturation = +1\n"),
    ("hdr tone has no contrast", H + "[A]\ncontrol = hdr_tone\ncontrast = +1\n"),
    ("out of range", H + "[A]\ncontrol = standard\ncontrast = +5\n"),
    ("not a number", H + "[A]\ncontrol = standard\ncontrast = lots\n"),
    ("grain out of range", H + "[A]\ncontrol = monotone\ngrain = 4\n"),
    ("bad toning for control", H + "[A]\ncontrol = retro\ntoning = sepia\n"),
    ("bad filter", H + "[A]\ncontrol = monotone\nfilter = 5\n"),
    ("several errors in two recipes",
     H + "[A]\ncontrol = monotone\nsaturation = +1\ncontrast = 9\n[B]\ncontrol = vivid\ntoning = red\n"),
    ("quote in name", H + "[Kid's Mono]\ncontrol = monotone\n"),
    ("non-ascii name", H + "[Grün]\ncontrol = standard\n"),
    ("format 2 full recipe", H2 + FULL),
    ("format 2 keys are case-insensitive, text keeps case", H2 + "[A]\nControl = Standard\nWB = Daylight\n"
     "WB_Shift_AB = B7\nAuthor = Luke Taylor\nNote =  Mixed Case, kept  \n"),
    ("kelvin white balance", H2 + "[K]\ncontrol = positive_film\nwb = kelvin\nwb_kelvin = 5250\nwb_shift_gm = m6\n"),
    ("format-2 setting in a format-1 file", H + "[A]\ncontrol = standard\nwb = auto\n"),
    ("text key in a format-1 file", H + "[A]\ncontrol = standard\nauthor = Me\n"),
    ("kelvin without wb = kelvin", H2 + "[A]\ncontrol = standard\nwb = daylight\nwb_kelvin = 5000\n"),
    ("wb = kelvin without a kelvin value", H2 + "[A]\ncontrol = standard\nwb = kelvin\n"),
    ("kelvin out of range", H2 + "[A]\ncontrol = standard\nwb = kelvin\nwb_kelvin = 12000\n"),
    ("iso and iso_max", H2 + "[A]\ncontrol = standard\niso = 400\niso_max = 6400\n"),
    ("bad wb shift", H2 + "[A]\ncontrol = standard\nwb_shift_ab = a15\nwb_shift_gm = +3\n"),
    ("exposure off the third-stop grid", H2 + "[A]\ncontrol = standard\nexposure = +0.5\n"),
    ("unknown white balance", H2 + "[A]\ncontrol = standard\nwb = sunny\n"),
    ("text too long", H2 + "[A]\ncontrol = standard\nnote = " + "x" * 301 + "\n"),
    ("text with a tab", H2 + "[A]\ncontrol = standard\nnote = a\tb\n"),
    ("retro toning is a scale", H2 + "[R]\ncontrol = retro\ntoning = +3\n[S]\ncontrol = retro\ntoning = 0\n"),
    ("bleach bypass toning", H2 + "[B]\ncontrol = bleach_bypass\ntoning = cool\n"),
    ("bleach bypass toning not in firmware", H2 + "[B]\ncontrol = bleach_bypass\ntoning = white\n"),
    ("cross process has contrast", H2 + "[X]\ncontrol = cross_process\ncontrast = -4\ntoning = yellow\n"),
    ("hdr strength has no off", H2 + "[H]\ncontrol = hdr_tone\nhdr_strength = 0\n"),
]


def evaluate(text):
    """What recipes.py makes of a file: a parse error, or the recipes, their
    errors, and (when valid) the canonical form."""
    try:
        recs = r.parse(text)
    except r.RecipeError as exc:
        return {"parse_error": str(exc)}
    errors = [e for name, body in recs for e in r.check_recipe(name, body)]
    out = {"recipes": [{"name": n, "settings": b} for n, b in recs], "errors": errors}
    if not errors:
        out["canonical"] = r.format_file(recs)
        out["dial_in"] = "\n\n".join(r.dial_in(name, body) for name, body in recs)
    return out


def vectors_json():
    data = {
        "about": "Shared test vectors for tools/recipes.py and its Android port. 'expect' is what "
                 "recipes.py gives. Regenerate with: python3 tools/tests/test_recipes.py --regen",
        "cases": [{"name": n, "text": t, "expect": evaluate(t)} for n, t in CASES],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def test_vectors_file_is_current():
    assert VECTORS.read_text() == vectors_json(), "stale: run python3 tools/tests/test_recipes.py --regen"


def test_schema_asset_is_current():
    if not SCHEMA.parent.exists():
        pytest.skip("no android/ here: the app lives in its own repository")
    assert SCHEMA.read_text() == r.schema_json(), "stale: run python3 tools/tests/test_recipes.py --regen"


def test_canonical_round_trip():
    for name, text in CASES:
        out = evaluate(text)
        if "canonical" in out:
            again = r.parse(out["canonical"])
            assert r.format_file(again) == out["canonical"], name
            assert not [e for n, b in again for e in r.check_recipe(n, b)], name


def test_schema_matches_tables():
    s = r.schema()
    assert [c["key"] for c in s["controls"]] == list(r.CONTROLS)
    assert [p["key"] for p in s["params"]] == list(r.PARAMS)
    for control, keys in s["supported"].items():
        assert set(keys) == r.SUPPORTED[control]
        if "toning" in keys:
            assert control in s["toning"]


def test_every_supported_value_is_accepted():
    """Each value the schema offers for a control passes check_recipe, and the
    neighbours just outside the integer ranges do not."""
    s = r.schema()
    params = {p["key"]: p for p in s["params"]}
    for control, keys in s["supported"].items():
        for key in keys:
            p = params[key]
            if p["kind"] == "int":
                good = range(p["min"], p["max"] + 1)
                for bad in (p["min"] - 1, p["max"] + 1):
                    assert r.check_recipe("T", {"control": control, key: str(bad)}), (control, key, bad)
            else:
                good = s["toning"][control] if p["kind"] == "toning" else p["values"]
                assert r.check_recipe("T", {"control": control, key: "nonsense"}), (control, key)
            for value in good:
                assert not r.check_recipe("T", {"control": control, key: str(value)}), (control, key, value)


def test_shooting_values_are_accepted_on_every_control():
    s = r.schema()
    for p in s["params"]:
        if p["group"] == "image":
            continue
        assert p["since"] == 2
        values = p.get("values") or (["5000"] if p["kind"] == "int" else ["Any Text"])
        for control in r.CONTROLS:
            for value in values:
                body = {"control": control, p["key"]: value}
                if p["key"] == "wb_kelvin" or value == "kelvin":
                    body.update(wb="kelvin", wb_kelvin="5000")
                assert not r.check_recipe("T", body), (control, p["key"], value)


def test_format_1_files_stay_format_1():
    out = evaluate(H + "[A]\ncontrol = standard\n")
    assert out["canonical"].startswith("format 1\n")


def test_bundled_recipe_files_are_valid():
    """recipes/*.txt ship in the app as its community collections."""
    files = sorted((ROOT / "recipes").glob("*.txt"))
    assert files
    for path in files:
        recs = r.parse(path.read_text())
        assert recs, path
        assert not [e for n, b in recs for e in r.check_recipe(n, b)], path
        for name, body in recs:
            assert body.get("source", "").startswith("https://"), (path.name, name)


def test_text_that_a_file_cannot_hold():
    for bad in ("see #2", "two\nlines"):
        assert r.check_recipe("T", {"control": "standard", "note": bad}) == ["T: Note contains '#' or a line break"]


def test_check_name():
    assert r.check_name("Warm Slide") is None
    assert r.check_name("A" * 32) is None
    assert r.check_name("A" * 33)
    assert r.check_name("No #1")
    assert r.check_name(" lead")
    assert r.check_name("tab\there")
    assert r.check_name("")


if __name__ == "__main__":
    if sys.argv[1:] != ["--regen"]:
        raise SystemExit("usage: test_recipes.py --regen   (run the tests with pytest)")
    VECTORS.write_text(vectors_json())
    print(f"wrote {VECTORS.relative_to(ROOT)}")
    if SCHEMA.parent.exists():
        SCHEMA.write_text(r.schema_json())
        print(f"wrote {SCHEMA.relative_to(ROOT)}")
