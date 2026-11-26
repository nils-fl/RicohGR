"""Focus probe 3 (tools/focus/focus_probe3.py): card structure, and part D (copyright tag) run on the Mac
against a mocked PEEK.BIN; needs the decoded 1.60 image."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af", "effects", "recipes", "focus")]
if not (ROOT / "work" / "fwdc243b-v160.decoded.bin").exists():
    pytest.skip("decoded 1.60 image missing (tools/firmware/gr_unpack.py)", allow_module_level=True)

import focus_probe3 as p3  # noqa: E402
import recipe_card as rc  # noqa: E402

STEPS, PLAN = p3.steps_for()
OBJ = 0x5571A000


def test_patch_words_hold_in_the_image():
    assert p3.check() == []


def test_card_parses_and_holds_the_patch_files():
    files, _, plan = p3.card_files()
    assert {f"{p3.DIR}/{n}" for n in ("PEEK.BIN", "CPTAG.BIN", "CPEMPTY.BIN")} <= set(files)
    r = subprocess.run(["sh", "-n"], input=files[f"{rc.CARD_DIR}/SESSION.SH"], capture_output=True)
    assert r.returncode == 0, r.stderr
    assert [p["kind"] for p in plan].count("snap") == 3 and plan[-1]["kind"] == "tag"


def test_every_copyright_command_is_inside_the_empty_check():
    for kind, text, *_ in STEPS:
        if "SetupCopyright" in text:
            assert kind == "sh" and text.startswith('if [ "$yc0" = 0 ]; then'), text
    # the patches are restored in the same line that applies them
    for s in (s[1] for s in STEPS if s[0] == "sh"):
        for b in ("CPTAG", "CPEMPTY"):
            if f"$W/{b}.BIN;" in s:
                assert f"$W/{b}.BIN restore" in s


def test_variables_do_not_clash_with_solo_lib():
    lib = set(re.findall(r"\b([a-z]\w*)=", rc.solo_lib()))
    text = "\n".join(s[1] for s in STEPS if s[0] == "sh")
    ours = set(re.findall(r"(?<![\w$-])([a-z]\w*)=", re.sub(r"-v \w+=", "", text))) - {"bs", "skip", "count"}
    assert ours and not (ours & lib), ours & lib


def test_awk_programs_have_no_name_before_a_parenthesis():
    builtins = {"if", "match", "substr", "sub", "gsub", "tolower", "length", "print", "exit"}
    for h in p3.HELPERS:
        for prog in re.findall(r"busybox awk [^']*'([^']*)'", h):
            for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", prog):
                assert name in builtins, (name, h)


def run_part_d(tmp, first_byte, attach):
    """HELPERS + part D with mocks; the copyright's first byte and the attach byte as given."""
    t = tmp
    w = t / "w"
    w.mkdir()
    mem = {p3.SETTINGS_PTR: OBJ, OBJ + 0x944: first_byte << 8, OBJ + 0x964: attach << 24}
    for k, off in p3.OFF.items():
        mem[OBJ + off] = 7
    cases = "".join(f" {a:x}) v={v} ;;" for a, v in mem.items())
    (w / "PEEK.BIN").write_text(f"#!/bin/sh\ncase $1 in{cases} *) exit 9 ;; esac\n"
                                "python3 -c \"import sys, struct; sys.stdout.buffer.write(struct.pack('<I', $v) * $2)\"\n")
    for b in ("CPTAG", "CPEMPTY"):
        (w / f"{b}.BIN").write_text(f'#!/bin/sh\necho "{b} $*" >>{t}/relay.txt\n')
    for f in w.iterdir():
        f.chmod(0o755)
    lib = (f"w={t}\nlog() {{ echo \"$*\" >>{t}/log.txt; t=9; k=9; o=9; }}\n"
           f"b() {{ cat >>{t}/relay.txt; t=9; k=9; q=9; o=9; l=9; v=9; a=9; }}\n")
    part = STEPS[[s[1] for s in STEPS].index("part D tag"):]
    part = part[:[s[1] for s in part].index("part E restore")]
    body = [h.replace(f"W={p3.RAM}", f"W={w}") for h in p3.HELPERS] + ["ya0=$(attach)"]
    body += [s[1] for s in part if s[0] == "sh"]
    bindir = t / "bin"
    bindir.mkdir()
    (bindir / "busybox").write_text('#!/bin/sh\nc=$1; shift\nexec "$c" "$@"\n')
    (bindir / "busybox").chmod(0o755)
    (t / "d.sh").write_text(lib + "\n".join(body) + "\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    subprocess.run(["sh", str(t / "d.sh")], env=env, check=True, timeout=60)
    read = lambda n: (t / n).read_text() if (t / n).exists() else ""   # noqa: E731
    return read("log.txt"), read("relay.txt")


def test_a_set_copyright_is_left_alone(tmp_path):
    log, relay = run_part_d(tmp_path, ord("N"), 0)
    assert "tag skipped cpy0 78" in log
    assert "SetupCopyright" not in relay and "CPTAG" not in relay and "CPEMPTY" not in relay


@pytest.mark.parametrize("attach,restored", [(0, "On"), (1, "Off")])
def test_an_empty_copyright_gets_the_tag_then_empty_again(tmp_path, attach, restored):
    log, relay = run_part_d(tmp_path, 0, attach)
    assert "tag go" in log
    assert relay.index("CPTAG") < relay.index(p3.TAG) < relay.index("CPTAG restore") < relay.index("SW_swr 1")
    assert relay.index("CPEMPTY") < relay.index("SetupCopyright x") < relay.index("CPEMPTY restore")
    assert relay.rstrip().splitlines()[-2:] == [f"sendln 'UserData Set SetupCopyrightAttach {restored}'", "mpause 500"]
