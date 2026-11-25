"""RAM test card for grain arms A and B (tools/effects/effects_card.py); needs the decoded 1.60 image."""
import argparse
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes")]
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

import effects_card as ec  # noqa: E402

# Tera Term macro commands that must not be used as label names
TTL_COMMANDS = {"wait", "end", "goto", "call", "return", "pause", "mpause", "send", "sendln", "exit", "if"}


def test_card_layout():
    files = ec.card_files()
    assert set(files) == {"SCRIPT/STARTUP.TTL", "SCRIPT/EFFECTS/MARK.SH", "SCRIPT/EFFECTS/APPLY.SH",
                          "SCRIPT/EFFECTS/A.BIN", "SCRIPT/EFFECTS/A.ON",
                          "SCRIPT/EFFECTS/B.BIN", "SCRIPT/EFFECTS/B.ON"}


def test_each_writer_carries_exactly_its_sites():
    files = ec.card_files()
    for arm, sites in ec.arm_sites().items():
        blob = files[f"SCRIPT/EFFECTS/{arm}.BIN"]
        table = b"".join(struct.pack("<3I", *s) for s in sites)
        assert blob.count(table) == 1
        assert blob.startswith(b"\x7fELF")


def test_script_runs_only_card_scripts_and_never_restores():
    ttl = ec.card_files()["SCRIPT/STARTUP.TTL"].decode()
    sent = [l for l in ttl.split("\r\n") if l.startswith("sendln")]
    assert sent[0] == "sendln 'lcmd_resp on'"
    assert {l for l in sent[1:] if "lcmd" in l} == {
        "sendln 'lcmd sh /media/storage1/SCRIPT/EFFECTS/MARK.SH'",
        "sendln 'lcmd sh /media/storage1/SCRIPT/EFFECTS/APPLY.SH'"}
    assert {l for l in sent if "lcmd" not in l} == {"sendln 'Af_Led_On'", "sendln 'Af_Led_Off'"}
    labels = {l[1:] for l in ttl.split("\r\n") if l.startswith(":")}
    assert not labels & TTL_COMMANDS
    sh = ec.card_files()["SCRIPT/EFFECTS/APPLY.SH"].decode()
    assert "rm " not in sh and ".BIN restore" not in sh
    assert "$d/A.BIN;" in sh and "$d/B.BIN;" in sh


def test_stock_grain_leaves_arm_b_out():
    files = ec.card_files(ec.STOCK_GRAIN)
    assert "SCRIPT/EFFECTS/B.BIN" not in files and "B.BIN" not in files["SCRIPT/EFFECTS/APPLY.SH"].decode()


def test_grain_option():
    assert ec.parse_grain("0x80,0x100,0x200") == (0x80, 0x100, 0x200)
    for bad in ("0x100,0x200", "0,0x100,0x200", "0x101,0x200,0x300", "0x100,0x200,0x800"):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            ec.parse_grain(bad)
