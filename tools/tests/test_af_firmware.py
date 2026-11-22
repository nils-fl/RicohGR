"""Site lists of the AF firmware build (no firmware image needed)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import af_firmware as fw  # noqa: E402


def test_default_build_is_the_flashed_19_words():
    s = fw.sites()
    assert len(s) == 19
    assert 0x533F7118 not in {a for a, _, _ in s}


def test_afc_build_adds_only_the_fast_drive_word():
    base, afc = fw.sites(), fw.sites(afc=True)
    assert afc[:len(base)] == base
    assert afc[len(base):] == [(0x533F7118, 0x13A03001, 0xE3A03001)]


def test_afc_word_differs_only_in_the_condition_field():
    _, old, new = fw.AFC_SITES[0]
    assert old & 0x0FFFFFFF == new & 0x0FFFFFFF
    assert old >> 28 == 0x1 and new >> 28 == 0xE  # NE -> always
