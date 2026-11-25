"""Card generators: the bench card for the flashed camera must not be able to write stock words."""
import re
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_debug_card as card  # noqa: E402


def files():
    return card.flashed_card_files()


def test_flashed_card_has_no_arm_rm_or_restore():
    remote = files()[f"{card.FLASHED_DIR}/REMOTE.SH"].decode()
    cgis = set(re.findall(r"cgi-bin/([a-z]+) <<", remote))
    assert cgis == {"uptime", "status", "log", "shell", "put", "run", "peek", "trace", "tail"}
    assert "restore" not in remote
    assert "tools only on this card" in remote


def test_flashed_card_runs_only_its_own_scripts():
    f = files()
    assert set(f) == {"SCRIPT/STARTUP.TTL"} | {f"{card.FLASHED_DIR}/{n}" for n in
                                               ("MARK.SH", "REMOTE.SH", "PEEK.BIN", "TRACE.BIN", "AFCFAST.BIN")}
    ttl = f["SCRIPT/STARTUP.TTL"].decode()
    assert "AFDBG" not in ttl and "EnableAfDebugDataSave" not in ttl and "PATCH.SH" not in ttl
    assert f"lcmd sh {card.FLASHED_WWW}/PULL.SH" in ttl


def test_flashed_tracer_writes_to_ram():
    assert f"{card.FLASHED_WWW}/TRACE.DAT".encode() in files()[f"{card.FLASHED_DIR}/TRACE.BIN"]
    assert b"/media/storage1" not in files()[f"{card.FLASHED_DIR}/TRACE.BIN"]
