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
    return em.cap_sites() + em.row_sites() + em.tuning_sites() + em.grain_scale_sites(0x100, 0x200, 0x300) + em.planner_sites()


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


def patched_rows():
    """Every row list as it reads with the row sites written."""
    mem = {a: new for a, _, new in em.row_sites()}
    rd = lambda a: mem.get(a, em.word(a))
    out = []
    for i in range(em.ROW_VARIANTS * em.ROW_CONTROLS):
        p, n = rd(em.ROW_TABLE + 8 * i), rd(em.ROW_TABLE + 8 * i + 4)
        out.append([(rd((p + k) & ~3) >> 8 * ((p + k) & 3)) & 0xFF for k in range(n)])
    return out


def test_row_sites_add_grain_last_to_the_colour_lists_only():
    stock = [em.row_list(i) for i in range(em.ROW_VARIANTS * em.ROW_CONTROLS)]
    got = patched_rows()
    for i, (old, new) in enumerate(zip(stock, got)):
        c = i % em.ROW_CONTROLS
        assert new == old or new == old + [em.SLOT_GRAIN], i
        if c in em.STOCK_GRAIN_LISTS or c == 10:
            assert new == old
    assert max(map(len, got)) == max(map(len, stock)) == em.ROW_MAX
    # Standard, Vivid and both films get the row in every variant; Bleach Bypass, Retro and
    # Cross Process in variant 0 only (their variant 1/2 lists are already ROW_MAX long)
    grown = {(i // em.ROW_CONTROLS, i % em.ROW_CONTROLS) for i, (o, n) in enumerate(zip(stock, got)) if n != o}
    assert grown == {(v, c) for v in range(3) for c in (0, 1, 6, 7)} | {(0, 8), (0, 9), (0, 11)}


def test_row_sites_write_only_padding_and_counts():
    counts = {em.ROW_TABLE + 8 * i + 4 for i in range(em.ROW_VARIANTS * em.ROW_CONTROLS)}
    for a, old, new in em.row_sites():
        if a in counts:
            assert new == old + 1
        else:
            diff = [k for k in range(4) if (old >> 8 * k) & 0xFF != (new >> 8 * k) & 0xFF]
            assert len(diff) == 1 and (old >> 8 * diff[0]) & 0xFF == 0 and (new >> 8 * diff[0]) & 0xFF == em.SLOT_GRAIN


def test_tuning_words_load_the_grain_byte_like_negative_film():
    sites = em.tuning_sites()
    assert len(sites) == 5
    for a, old, new in sites:
        assert text(a, old).startswith("mov r6, r")
        assert text(a, new) == "ldrb r6, [r3, #0x795]" == em.insn_text(0x536DC0B8)
        assert em.TUNING < a < 0x536DB9C0 or 0x536DBAA4 <= a < 0x536DC024


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
