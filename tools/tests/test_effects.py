"""Grain / effects map (tools/effects_map.py) against the decoded 1.60 image; skipped without it."""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/gr_unpack.py)", allow_module_level=True)

import af_firmware  # noqa: E402
import capstone  # noqa: E402
import effects_map as em  # noqa: E402

MD = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)


def text(a, w):
    i = next(MD.disasm(struct.pack("<I", w), a))
    return f"{i.mnemonic} {i.op_str}"


def all_sites():
    return em.cap_sites() + em.grain_scale_sites(0x100, 0x200, 0x300) + em.planner_sites()


def test_map_matches_the_official_image():
    assert em.check() == []


def test_stock_grain_rows_are_the_monotone_family_and_negative_film():
    assert {r for r in range(1, em.CAP_ROWS) if em.cap_row(r)[em.SLOT_GRAIN]} == em.STOCK_GRAIN_ROWS


def test_cap_sites_change_only_the_grain_bytes_of_the_chosen_rows():
    changed = set()
    for a, old, new in em.cap_sites():
        for k in range(4):
            if (old >> 8 * k) & 0xFF != (new >> 8 * k) & 0xFF:
                assert (old >> 8 * k) & 0xFF == 0 and (new >> 8 * k) & 0xFF == 1
                changed.add(a + k)
    assert changed == {em.CAP_TABLE + r * em.CAP_COLS + em.SLOT_GRAIN for r in em.UNLOCK_GRAIN_ROWS}


def test_grain_scale_words():
    got = {a: text(a, new) for a, _, new in em.grain_scale_sites(0x100, 0x200, 0x300)}
    assert got == {0x5367AE38: "moveq r6, #0x100", 0x5367AE2C: "moveq r6, #0x200", 0x5367AE3C: "movne r6, #0x300"}
    assert em.grain_scale_sites() == []  # stock values -> nothing to write
    with pytest.raises(ValueError):
        em.enc_mov_imm(0xE, 6, 0x101)


def test_planner_words_branch_to_the_grain_check_and_keep_their_condition():
    for a, old, new in em.planner_sites():
        assert old >> 28 == new >> 28
        assert text(a, new).endswith(f"#0x{em.PLANNER_WITH_GRAIN:x}")


def test_no_site_touches_an_af_word():
    af = {a for a, _, _ in af_firmware.sites(afc=True)}
    ours = [a for a, _, _ in all_sites()]
    assert len(set(ours)) == len(ours)
    assert not af & set(ours)
