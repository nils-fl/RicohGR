"""SAMP.BIN (tools/af/af_debug_card.py SAMP_S) run in the unicorn ARM emulator: the Linux syscalls it uses are
emulated (open, mmap2, clock_gettime, nanosleep, write, close, exit) and /dev/mem is a function of the emulated
time, so the camera's RAM can change while the binary samples it. Needs clang (as every card binary) and
unicorn (pip install unicorn); skipped without it."""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / a) for a in ("firmware", "af")]
unicorn = pytest.importorskip("unicorn")
from unicorn import arm_const as A  # noqa: E402

import af_debug_card as card  # noqa: E402

BASE, HEADERS = 0x10000, 84
STACK, STACK_SIZE = 0x7F000000, 0x10000
DEV_VA = 0x40000000
FILE_FD = 10


class Emu:
    """One run of an ARM ELF from arm_elf(): mem(phys, t_ms) gives the word at a physical address."""

    def __init__(self, elf, args, mem, max_ms=10000):
        self.mem, self.t_ns, self.max_ns = mem, 0, max_ms * 1000000
        self.out, self.files, self.fds, self.maps, self.opened = bytearray(), {}, {}, [], []
        self.exit = None
        uc = self.uc = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_ARM)
        size = (len(elf) + 0xFFF) & ~0xFFF
        uc.mem_map(BASE, size)
        uc.mem_write(BASE, elf)
        uc.mem_map(STACK, STACK_SIZE)
        # argc, argv[], NULL, envp NULL, then the strings
        argv = [b"SAMP.BIN"] + [a.encode() for a in args]
        strs, ptrs, p = b"", [], STACK + 0x8000
        for a in argv:
            ptrs.append(p + len(strs))
            strs += a + b"\0"
        uc.mem_write(STACK + 0x8000, strs)
        sp = STACK + 0x4000
        uc.mem_write(sp, struct.pack(f"<{len(ptrs) + 3}I", len(argv), *ptrs, 0, 0))
        uc.reg_write(A.UC_ARM_REG_SP, sp)
        uc.hook_add(unicorn.UC_HOOK_INTR, self.svc)
        uc.hook_add(unicorn.UC_HOOK_MEM_READ, self.read, begin=DEV_VA, end=DEV_VA + 0x1000000)
        uc.emu_start(BASE + HEADERS, 0, count=20000000)
        assert self.exit is not None, "the binary did not exit"

    def string(self, a):
        b = bytes(self.uc.mem_read(a, 256))
        return b[:b.index(0)].decode()

    def svc(self, uc, intno, _):
        r = lambda n: uc.reg_read(getattr(A, f"UC_ARM_REG_R{n}"))   # noqa: E731
        nr, ret = r(7), 0
        if nr == 1:
            self.exit = r(0)
            uc.emu_stop()
            return
        if nr == 5:
            path = self.string(r(0))
            self.opened.append((path, r(1)))
            if path == "/dev/mem":
                ret = 3
            else:
                ret = FILE_FD + len(self.fds)
                self.fds[ret] = path
                self.files.setdefault(path, bytearray())
        elif nr == 192:
            assert r(4) == 3 and r(1) == 4096 and r(2) == 1 and r(3) == 1 and r(0) == 0
            va = DEV_VA + 0x1000 * len(self.maps)
            uc.mem_map(va, 0x1000, unicorn.UC_PROT_READ)
            self.maps.append(r(5))
            ret = va
        elif nr == 263:
            assert r(0) == 1                                       # CLOCK_MONOTONIC
            uc.mem_write(r(1), struct.pack("<II", self.t_ns // 10**9, self.t_ns % 10**9))
        elif nr == 162:
            sec, nsec = struct.unpack("<II", uc.mem_read(r(0), 8))
            self.t_ns += sec * 10**9 + nsec
            if self.t_ns > self.max_ns:
                raise AssertionError("ran past the emulated time limit")
        elif nr == 4:
            data = bytes(uc.mem_read(r(1), r(2)))
            (self.out if r(0) == 1 else self.files[self.fds[r(0)]]).extend(data)
            ret = r(2)
        elif nr == 6:
            self.fds.pop(r(0), None)
        else:
            raise AssertionError(f"unexpected syscall {nr}")
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)

    def read(self, uc, access, address, size, value, _):
        k = (address - DEV_VA) >> 12
        phys = (self.maps[k] << 12) | (address & 0xFFC)
        uc.mem_write(address & ~3, struct.pack("<I", self.mem(phys, self.t_ns / 1e6) & 0xFFFFFFFF))

    def words(self):
        return list(struct.unpack(f"<{len(self.out) // 4}I", self.out))


SAMP = card.samp_bin()
LENS, FOCUS, CM, S1 = 0x553A0698, 0x55064050, 0x55064064, 0x50001008 + 0x5000000


def ram(**fns):
    """mem(phys, t): fns keyed by name in LENS/FOCUS/CM/S1, each a constant or a function of t (ms)."""
    table = {LENS: fns.get("lens", 2500), FOCUS: fns.get("focus", 0x000D0001), CM: fns.get("cm", 250),
             S1: fns.get("s1", 0)}

    def mem(phys, t):
        v = table.get(phys, 0x11111111)
        return v(t) if callable(v) else v
    return mem


def wait(mem, timeout, stable, base, moved=0, expect=1, extras=(CM,), lens=LENS, focus=FOCUS):
    args = ["w"] + [f"{v:x}" for v in (timeout, stable, lens, base, moved, focus, expect) + tuple(extras)]
    e = Emu(SAMP, args, mem)
    assert e.exit == 0, e.exit
    return e, e.words()


def test_snapshot_reads_every_word_once():
    e, w = wait(ram(lens=1330, cm=150), 0, 0, 1330)
    assert w == [1, 0, 1, 1330, 0, 0x000D0001, 150]
    assert ("/dev/mem", 0x101000) in e.opened                     # read-only, O_SYNC
    assert sorted(e.maps) == sorted([LENS >> 12, FOCUS >> 12, CM >> 12])


def ramp(t0, t1, a, b):
    """The lens from a to b between t0 and t1 ms, a new value every 5 ms."""
    def f(t):
        if t < t0:
            return a
        if t >= t1:
            return b
        return int(a + (b - a) * (int((t - t0) / 5) * 5) / (t1 - t0))
    return f


def test_settles_after_the_lens_moved_and_stood_still():
    e, w = wait(ram(lens=ramp(100, 300, 2500, 1330)), 1000, 200, 2500)
    res, ms, n, lens, moved = w[:5]
    assert (res, lens, moved) == (0, 1330, 1)
    assert 495 <= ms <= 510 and 90 <= n <= 110                    # 300 ms + 200 ms still, a sample every 5 ms


def test_af_that_lands_where_it_started_still_counts():
    def lens(t):
        return 2500 if t < 50 or t >= 250 else 2400 - int(t)
    _, w = wait(ram(lens=lens), 1000, 200, 2500)
    assert w[0] == 0 and w[3] == 2500 and w[4] == 1 and 445 <= w[1] <= 460


def test_an_unmoved_lens_times_out():
    _, w = wait(ram(lens=2500), 1000, 200, 2500)
    assert w[0] == 1 and w[4] == 0 and w[3] == 2500 and 1000 <= w[1] <= 1010 and 195 <= w[2] <= 205


def test_the_moved_flag_carries_over():
    """The previous call timed out while the lens moved: it may stand still at the base now."""
    _, w = wait(ram(lens=1330), 1000, 200, 1330, moved=1)
    assert w[0] == 0 and w[4] == 1 and 200 <= w[1] <= 210


def test_garbage_lens_readings_never_settle():
    _, w = wait(ram(lens=lambda t: 536870893 if int(t / 5) % 2 else 0), 400, 100, 2500)
    assert w[0] == 1 and w[3] == 0xFFFFFFFF and w[4] == 0


def test_a_garbage_reading_restarts_the_stillness():
    def lens(t):
        return 0 if 150 <= t < 155 else 1330
    _, w = wait(ram(lens=lens), 1000, 200, 2500)
    assert w[0] == 0 and 350 <= w[1] <= 360


def test_a_focus_change_ends_the_wait():
    _, w = wait(ram(lens=2500, focus=lambda t: 0x000D0001 if t < 200 else 0x0001000A), 1000, 200, 2500)
    assert w[0] == 2 and w[5] == 0x0001000A and 200 <= w[1] <= 210


def test_snap_state_waits_on_focus_alone():
    _, w = wait(ram(focus=lambda t: 0x0001000A if t < 300 else 0x000D0001), 1000, 0, 0, expect=10, lens=0)
    assert w[0] == 2 and w[3] == 0xFFFFFFFF and w[5] == 0x000D0001 and 300 <= w[1] <= 310


def test_expect_beyond_a_byte_does_not_watch_focus():
    _, w = wait(ram(focus=lambda t: int(t)), 100, 0, 2500, expect=0x100)
    assert w[0] == 1


def test_eight_extras_in_order():
    extras = tuple(0x55000000 + 4 * i for i in range(8))
    _, w = wait(lambda p, t: p - 0x55000000 if p < 0x55000100 else 2500, 0, 0, 2500, extras=extras)
    assert w[6:] == [4 * i for i in range(8)]


@pytest.mark.parametrize("args", [["w"], ["w", "0", "0", "0", "0", "0", "0"], ["x", "0", "0", "0", "0", "0", "0", "1"],
                                  ["wx", "0", "0", "0", "0", "0", "0", "1"],
                                  ["w", "0", "0", "553a0699", "0", "0", "0", "1"],
                                  ["w", "0", "0", "52fffffc", "0", "0", "0", "1"],
                                  ["w", "0", "0", "5ff00000", "0", "0", "0", "1"],
                                  ["w", "0", "0", "0", "0", "0", "0", "1g"],
                                  ["w", "0", "0", "0", "0", "0", "0", "123456789"],
                                  ["w", "0", "0", "0", "0", "0", "0", "1"] + ["55000000"] * 9,
                                  ["t", "64", "10", "/f"], ["t", "64", "10", "/f", "0"]])
def test_bad_arguments(args):
    e = Emu(SAMP, args, lambda p, t: 0)
    assert e.exit == 8 and not e.out and not e.maps


def records(e, path="/tmp/tr"):
    b = e.files.get(path, b"")
    return [struct.unpack_from("<5I", b, i) for i in range(0, len(b), 20)]


def test_trace_records_the_changes():
    mem = ram(lens=ramp(100, 200, 2500, 1330), focus=lambda t: 0x000D0001 if t < 250 else 0x0001000A,
              s1=lambda t: 1 if 120 <= t < 180 else 0)
    e = Emu(SAMP, ["t", "1f4", "c8", "/tmp/tr", f"{LENS:x}", f"{S1:x}", f"{FOCUS:x}"], mem)
    assert e.exit == 0
    rec = records(e)
    assert [r[2:] for r in rec[:3]] == [(0, 2500, 2500), (1, 0, 0), (2, 0x000D0001, 0x000D0001)]
    by = {i: [(r[0] * 1000 + r[1] / 1e6, r[3], r[4]) for r in rec[3:] if r[2] == i] for i in range(3)}
    assert [(o, n) for _, o, n in by[1]] == [(0, 1), (1, 0)] and 120 <= by[1][0][0] <= 125
    assert [(o, n) for _, o, n in by[2]] == [(0x000D0001, 0x0001000A)] and 250 <= by[2][0][0] <= 255
    lt = [t for t, _, _ in by[0]]
    assert by[0][-1][2] == 1330 and all(b - a >= 49 for a, b in zip(lt, lt[1:]))   # the lens every 50 ms
    assert 500 <= e.t_ns / 1e6 <= 510                                                 # stops at the timeout


def test_trace_stops_after_the_record_limit():
    e = Emu(SAMP, ["t", "2710", "a", "/tmp/tr", f"{LENS:x}", f"{S1:x}"], ram(s1=lambda t: int(t / 5)))
    assert e.exit == 0 and len(records(e)) == 10 and e.t_ns / 1e6 < 100


def test_trace_opens_the_file_per_record():
    """The shell moves the file away to drain it: every record opens it again (append, create)."""
    e = Emu(SAMP, ["t", "32", "64", "/tmp/tr", f"{LENS:x}", f"{S1:x}"], ram(s1=lambda t: int(t / 10)))
    opens = [o for o in e.opened if o[0] == "/tmp/tr"]
    assert len(opens) == len(records(e)) >= 5 and all(f == 0x441 for _, f in opens) and not e.fds
