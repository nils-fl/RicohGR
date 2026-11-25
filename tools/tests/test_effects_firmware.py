"""AF + grain effects build (tools/effects_firmware.py) and patch_guard --allow effects;
needs the decoded 1.60 image."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/gr_unpack.py)", allow_module_level=True)

import af_firmware as af  # noqa: E402
import effects_firmware as ef  # noqa: E402
import effects_map as em  # noqa: E402
import patch_guard as pg  # noqa: E402

ORIG = af.DECODED.read_bytes()


def write(payload, a, word):
    mod = bytearray(payload)
    o = a - af.BASE + af.OFF
    mod[o:o + 4] = word.to_bytes(4, "little")
    return bytes(mod)


@pytest.fixture(scope="module")
def build():
    return ef.patched_payload(ORIG)


def test_build_is_the_flashed_af_words_plus_a_and_b(build):
    sites, arms = ef.sites()
    assert sites[:19] == af.sites() and len(arms["A"]) == 40 and len(arms["B"]) == 3
    changed = {o for o in range(0, len(ORIG), 4) if ORIG[o:o + 4] != build[o:o + 4]}
    assert changed == {a - af.BASE + af.OFF for a, _, _ in sites}


def test_guard_passes_the_build_only_with_the_effects_list(build):
    ok, lines = pg.guard(ORIG, build, allow=ef.ALLOW)
    assert ok, [l for l in lines if "REJECT" in l]
    assert sum("effects word" in l for l in lines) >= 28
    ok, _ = pg.guard(ORIG, build, allow=("raw", "af"))
    assert not ok


def test_guard_rejects_any_other_word_at_an_effects_site(build):
    a, _, new = em.tuning_sites()[0]
    ok, lines = pg.guard(ORIG, write(build, a, new ^ 0x1000), allow=ef.ALLOW)  # ldrb r7 instead of r6
    assert not ok and any(f"0x{a:08x}" in l and "REJECT" in l for l in lines)
    ok, _ = pg.guard(ORIG, write(build, 0x536DB9AC, 0xE1A00000), allow=ef.ALLOW)  # a neighbouring word
    assert not ok


def test_effect_word_kind_accepts_other_grain_scales_only_as_mov_r6():
    for a, stock, new in em.grain_scale_sites(0x80, 0x180, 0x400):
        assert em.effect_word_kind(a, stock, new) == "grain scale"
    a, stock, new = em.grain_scale_sites(0x100, 0x200, 0x300)[0]
    assert em.effect_word_kind(a, stock, new ^ 0x1000) is None              # mov r7
    assert em.effect_word_kind(a, stock, (new & 0x0FFFFFFF) | 0xE0000000) is None  # condition changed


def test_row_list_proof_holds():
    refs, problems = em.row_list_proof(pg.ra.build_xrefs())
    assert problems == [] and refs
