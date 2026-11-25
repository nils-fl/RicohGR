#!/usr/bin/env python3
"""Optuna search over the contrast-AF detector constants of the GR IIIx 1.60,
run hands-free against the remote test card (tools/af/af_remote.py).

Parameter space (each trial becomes one uploaded patch, OPT, plus card patches):
  exponent  margin curve scale = (kb * b / y) ** p in dim light (y < b):
            1.0 stock, 0.875 X875, 0.75 SCALE75, 0.5 SQRTNS (code variants on the card)
  kb        factor on the six per-mode bases b (table 0x53c16bb0; HALFB = 0.5)
  rise      rise floor of detector mode 0 = phase grade 0 (0x53c16d14; stock 0.02, RISE05 0.005)
  falls     falling samples before a peak counts (0x53c16cc0; stock 3, FALLS2 2)

A trial presses `--presses` times in every condition (ND out / ND in through the
shell relay; a "-g0" condition adds FORCEG0, the forced phase grade 0). Cost per
press, lower is better: AF time in s, plus err_weight x pulses off the reference;
a failed press costs fail_cost, a lock more than wrong_pulses off costs
wrong_cost (a false green frame is worse than a red one). The trial's value is
the mean. Stock controls between trials track drift in the light (not part of
the study). The study lives in SQLite, so a session resumes after a power cycle.

Safety: the study only sees the scene in front of the camera. Validate the best
settings in other scenes (no-signal, bright low-contrast) before trusting them.
"""
import argparse
import json
import math
import statistics
import struct
import sys
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_debug_card as card  # noqa: E402
import af_remote  # noqa: E402

EXPONENTS = {"1.0": None, "0.875": "X875", "0.75": "SCALE75", "0.5": "SQRTNS"}
STOCK_PARAMS = {"exponent": "1.0", "kb": 1.0, "rise": 0.02, "falls": 3}
# AF frame rate: auto (29 fps in the light of these studies; the camera's gain
# is at its cap) or forced with the card patches FORCE49 / FORCE100. With the
# stock margin 49 fps fails in dim light; with kb about 0.33 it locked and was
# faster (live check 2026-11-19). The trace's level byte does not show a forced
# rate; the sweep time per sample does.
FPS = {"auto": None, "49": "FORCE49", "100": "FORCE100"}
# Space 3 searches kb, exponent, falls, step and fps; the rest stays here
# (study 1: rise and exponent carry about 2-3 % of the effect each).
FROZEN3 = {"rise": 0.014}
f2w = lambda f: struct.unpack("<I", struct.pack("<f", f))[0]
w2f = lambda w: struct.unpack("<f", struct.pack("<I", w))[0]


def arm_imm(old, value):
    """`old` (an ARM data-processing instruction with an immediate operand)
    with the immediate replaced by `value`; ValueError if not encodable."""
    for rot in range(16):
        imm = ((value << (2 * rot)) | (value >> (32 - 2 * rot))) & 0xFFFFFFFF if rot else value
        if imm < 256:
            return (old & ~0xFFF) | (rot << 8) | imm
    raise ValueError(f"{value:#x} is not an ARM immediate")


# Study 2 knobs: name -> (stock value, sites(value) -> [(addr, old, new)]). Values
# equal to stock give no sites. Verified 2026-11-19 (docs af-debug-log#opt-study):
#   rise_run  0x53c16d04: a new maximum latches as a peak only at index >= N
#             (0x5340de8c), before the rise test
#   grade_lim the two "cmp rX, #0x20000" in 0x533f88c4: levels at CAF
#             +0x41e4/+0x41e8 at or below it give phase grade 0
#   scale_floor 0x53c16bd4: lower bound of b/y before the square root (bright light)
GRADE_LIM_SITES = ((0x533F88E8, 0xE3520802), (0x533F88F4, 0xE3530802))
GRADE_LIMS = (0x8000, 0xC000, 0x10000, 0x14000, 0x18000, 0x1C000, 0x20000)
KNOBS = {
    "rise_run": (3, lambda v: [(0x53C16D04, 3, v)]),
    "grade_lim": (0x20000, lambda v: [(a, o, arm_imm(o, v)) for a, o in GRADE_LIM_SITES]),
    "scale_floor": (0.25, lambda v: [(0x53C16BD4, f2w(0.25), f2w(v))]),
    # 0x533fc13c "moveq r0, #8": the sweep step in pulses per frame (CAF +0x43e6;
    # 12 and 6 read back live). Coarser steps shifted the lock by ~30 pulses.
    "step": (8, lambda v: [(0x533FC13C, 0x03A00008, 0x03A00000 | v)]),
}


def space(n):
    import optuna.distributions as D
    sp = {
        "exponent": D.CategoricalDistribution(list(EXPONENTS)),
        "kb": D.FloatDistribution(0.25, 1.0, log=True),
        "rise": D.FloatDistribution(0.002, 0.02, log=True),
        "falls": D.CategoricalDistribution([3, 2]),
    }
    if n == 3:
        return {
            "exponent": D.CategoricalDistribution(list(EXPONENTS)),
            "kb": D.FloatDistribution(0.15, 1.0, log=True),
            "falls": D.CategoricalDistribution([3, 2]),
            "step": D.IntDistribution(5, 12),
            "fps": D.CategoricalDistribution(list(FPS)),
        }
    if n >= 2:
        sp.update(rise_run=D.CategoricalDistribution([3, 2]),
                  grade_lim=D.CategoricalDistribution(list(GRADE_LIMS)),
                  scale_floor=D.FloatDistribution(0.1, 0.25),
                  step=D.IntDistribution(5, 14))
    return sp


def opt_sites(p):
    """Data words of the trial: the b table times kb, the mode-0 rise floor,
    and the study-2 knobs present in p."""
    sites = []
    for name, (stock, build) in KNOBS.items():
        if name in p and p[name] != stock:
            sites += [s for s in build(p[name]) if s[1] != s[2]]
    for a, old, _ in card.HALFB_SITES:
        new = f2w(w2f(old) * p["kb"])
        if new != old:
            sites.append((a, old, new))
    a, old, _ = card.RISE05_SITES[0]
    new = f2w(p["rise"])
    if new != old:
        sites.append((a, old, new))
    return sites


def arm_for(p, cond, cam):
    """Upload OPT for the trial (if it has data words) and return the arm."""
    parts = [x for x in (EXPONENTS[p["exponent"]], "FALLS2" if p["falls"] == 2 else None,
                         FPS[p.get("fps", "auto")]) if x]
    sites = opt_sites(p)
    if sites:
        body = card.multipatch_bin(sites)
        reply = cam.cgi_post("put", "OPT", body).decode().split()
        import hashlib
        if reply[:1] != ["patch"] or reply[-1] != hashlib.md5(body).hexdigest():
            raise SystemExit(f"upload of OPT failed: {' '.join(reply)}")
        parts.append("OPT")
    if cond.endswith("-g0"):
        parts.append("FORCEG0")
    return "+".join(parts) or "STOCK"


def time_split(path, r):
    """ms the press spent waiting before the sweep (action 2), sweeping (4)
    and finishing (8, 128), and the samples of the longest pass."""
    seg, prev, ns = {}, None, 0
    for t, w in card.trace_records(path):
        if t < r["t"] - 0.05:
            continue
        if t > r["t"] + (r.get("ms") or 3000) / 1000 + 0.6:
            break
        act = w[0] & 0xffff
        if prev:
            seg[prev[1]] = seg.get(prev[1], 0) + t - prev[0]
        if act == 4:
            ns = max(ns, w[29])
        prev = (t, act)
    ms = lambda k: round(1000 * seg.get(k, 0))
    return dict(wait_ms=ms(2), sweep_ms=ms(4), finish_ms=ms(8) + ms(128), samples=ns)


def press_cost(r, a):
    if r["outcome"] in ("lock", "done"):
        if r.get("final") is None:
            return r["ms"] / 1000
        err = abs(r["final"] - a.ref)
        if err > a.wrong_pulses:
            return a.wrong_cost
        return r["ms"] / 1000 + a.err_weight * err
    return a.fail_cost


class Bench:
    def __init__(self, a):
        self.a = a
        self.cam = af_remote.Camera(a.host, a.iface)
        self.cam.sync_clock()
        self.cam.find_defocus()
        self.orig = self.cam.props().get("focusSetting", "spot")
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.nd = None
        self.uptime_limit = a.max_uptime_min * 60

    def status(self):
        st = {}
        for line in self.cam.cgi("status").decode().splitlines():
            f = line.split(None, 1)
            if len(f) == 2:
                st[f[0]] = f[1]
        return st

    def check_time(self):
        st = self.status()
        up = float(st["uptime"])
        tb = int(st.get("trace_bytes", "0").split()[0] or 0)
        if getattr(self, "last_trace", None) is not None and tb <= self.last_trace:
            raise SystemExit(f"the tracer has stopped (TRACE.DAT stays at {tb} bytes): restart it or power-cycle")
        self.last_trace = tb
        if self.cam.props().get("hot"):
            raise SystemExit("the camera reports hot: stopping")
        if up > self.uptime_limit:
            raise SystemExit(f"camera uptime {up / 60:.0f} min: the tracer stops at about 46 min. "
                             "Power-cycle the camera, then run the same command again (the study resumes).")

    def set_nd(self, cond):
        want = "on" if cond.startswith("on") else "off"
        if self.a.no_relay:
            return
        if want == self.nd:
            return
        # Both relay stalls so far came on an ND command: give the camera time
        # to finish the last AF run first.
        time.sleep(1.5)
        seq, ok = self.cam.shell(["ND_close" if want == "on" else "ND_open"])
        if not ok:
            raise SystemExit(f"relay {seq} not acknowledged: the shell relay is not running (power-cycle)")
        self.nd = want
        time.sleep(0.8)

    def run(self, label, params, arm_of):
        """All presses of one trial or control; returns (mean cost, per-condition summary)."""
        costs, summary = [], {}
        # Alternate the order so consecutive runs share an ND state at the
        # seam (half the ND switches).
        self.runs = getattr(self, "runs", 0) + 1
        for cond in (self.a.conds if self.runs % 2 else self.a.conds[::-1]):
            self.set_nd(cond)
            arm = arm_of(cond)
            cc = []
            for _ in range(self.a.presses):
                tail = self.out / "last-tail.DAT"
                r = af_remote.press(self.cam, arm, True, self.orig, keep=tail)
                if r["outcome"] == "none":
                    r = af_remote.press(self.cam, arm, True, self.orig, keep=tail)   # one retry
                if r["outcome"] != "none" and tail.exists():
                    try:
                        r.update(time_split(tail, r))
                    except SystemExit:
                        pass
                if r["outcome"] == "none":
                    print(f"   {cond:7s} {af_remote.fmt(r)}  (not counted)")
                    continue
                bad = "" if r["arm_status"] == "-" else r["arm_status"].replace(",", "").strip("0")
                if bad:
                    raise SystemExit(f"arm {arm} reported status {r['arm_status']}: patch not applied")
                c = press_cost(r, self.a)
                r.update(label=label, cond=cond, params=params, cost=round(c, 3),
                         wrong=r["outcome"] in ("lock", "done") and r.get("final") is not None
                         and abs(r["final"] - self.a.ref) > self.a.wrong_pulses)
                with open(self.out / "presses.jsonl", "a") as fh:
                    fh.write(json.dumps(r) + "\n")
                print(f"   {cond:7s} cost {c:5.2f}  {af_remote.fmt(r)}  wait {r.get('wait_ms', '-')} sweep {r.get('sweep_ms', '-')} n {r.get('samples', '-')}")
                cc.append(c)
            costs += cc
            summary[cond] = round(statistics.mean(cc), 3) if cc else None
        if not costs:
            raise SystemExit("no press of this trial was seen in the trace")
        return statistics.mean(costs), summary


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.1")
    ap.add_argument("--iface", default="en0")
    ap.add_argument("--out", default="work/af-logs/opt1")
    ap.add_argument("--study", default="af-opt1")
    ap.add_argument("--trials", type=int, default=20, help="new trials in this run")
    ap.add_argument("--presses", type=int, default=4, help="presses per condition per trial")
    ap.add_argument("--conds", default="off,on", help="comma list of off, on, off-g0, on-g0")
    ap.add_argument("--ref", type=int, required=True, help="reference lens position (stock, bright)")
    ap.add_argument("--wrong-pulses", type=int, default=150)
    ap.add_argument("--err-weight", type=float, default=0.01, help="s per pulse off the reference")
    ap.add_argument("--fail-cost", type=float, default=5.0)
    ap.add_argument("--wrong-cost", type=float, default=20.0)
    ap.add_argument("--control-every", type=int, default=6, help="stock control after every N trials (0 = off)")
    ap.add_argument("--max-uptime-min", type=float, default=43)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--space", type=int, default=1, choices=(1, 2, 3), help="1: four knobs; 2: plus rise_run, grade_lim, scale_floor, step; 3: kb, exponent, falls, step, fps (rise frozen)")
    ap.add_argument("--seed-best", help="JSON params to enqueue first (e.g. the best of an earlier study)")
    ap.add_argument("--no-relay", action="store_true",
                    help="do not switch the ND filter (relay stalled): the ND state stays as it is; only off conditions")
    ap.add_argument("--report", action="store_true", help="print the study and exit")
    a = ap.parse_args()
    a.conds = a.conds.split(",")
    for c in a.conds:
        if c not in ("off", "on", "off-g0", "on-g0"):
            sys.exit(f"--conds: unknown condition {c}")
        if a.no_relay and c.startswith("on"):
            sys.exit("--no-relay: the ND filter cannot be switched; use off and off-g0")
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    study = optuna.create_study(study_name=a.study, storage=f"sqlite:///{Path(a.out) / 'study.db'}",
                                direction="minimize", load_if_exists=True,
                                sampler=optuna.samplers.TPESampler(seed=a.seed, multivariate=True,
                                                                   n_startup_trials=8))
    if a.report:
        report(study)
        return
    if not study.trials:
        # Anchors: stock and the hand-picked patches, so the search starts from them.
        stock = dict(STOCK_PARAMS)
        if a.space == 2:
            stock.update({k: v for k, (v, _) in KNOBS.items()})
        if a.space == 3:
            # rise is frozen below stock here, so there is no stock anchor: the
            # stock controls between the trials are the reference.
            stock = {"exponent": "1.0", "kb": 1.0, "falls": 3, "step": 8, "fps": "auto"}
        anchors = [] if a.space == 3 else [stock]
        if a.space == 3 and a.seed_best:
            anchors.append(dict(stock, **json.loads(a.seed_best), fps="49"))
        if a.seed_best:
            anchors.append(dict(stock, **json.loads(a.seed_best)))
        if a.space == 1:
            anchors += [dict(stock, exponent="0.75"), dict(stock, rise=0.005), dict(stock, exponent="0.75", rise=0.005)]
        for p in anchors:
            study.enqueue_trial(p)
    bench = Bench(a)
    if not a.no_relay:
        seq, ok = bench.cam.shell(["Af_Led_Off"])
        if not ok:
            sys.exit("the shell relay does not answer: power-cycle the camera, or use --no-relay with off conditions")
    done = 0
    try:
        while done < a.trials:
            bench.check_time()
            if a.control_every and done % a.control_every == 0:
                v, s = bench.run("control", STOCK_PARAMS,
                                 lambda cond: "FORCEG0" if cond.endswith("-g0") else "STOCK")
                with open(Path(a.out) / "controls.jsonl", "a") as fh:
                    fh.write(json.dumps(dict(t=time.time(), value=v, conds=s)) + "\n")
                print(f"== stock control: {v:.3f} {s}")
            trial = study.ask(space(a.space))
            p = dict(FROZEN3, **trial.params) if a.space == 3 else trial.params
            print(f"== trial {trial.number}: " + "  ".join(
                f"{k} {v:.4g}" if isinstance(v, float) else f"{k} {v}" for k, v in p.items()))
            try:
                v, s = bench.run(f"trial{trial.number}", p, lambda cond: arm_for(p, cond, bench.cam))
            except BaseException:
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                raise
            trial.set_user_attr("conds", s)
            study.tell(trial, v)
            print(f"== trial {trial.number}: value {v:.3f} {s}   best so far {study.best_value:.3f} "
                  f"(trial {study.best_trial.number})")
            done += 1
    finally:
        try:
            bench.set_nd("off")
            bench.cam.cgi("arm", "STOCK")
        except (SystemExit, OSError, RuntimeError):
            print("could not restore ND out / STOCK: check the camera")
    report(study)


def report(study):
    rows = [t for t in study.trials if t.state.name == "COMPLETE"]
    rows.sort(key=lambda t: t.value)
    print(f"{len(rows)} complete trials")
    for t in rows[:10]:
        p = t.params
        desc = "  ".join(f"{k} {v:.3g}" if isinstance(v, float) else f"{k} {v}" for k, v in p.items())
        print(f"  {t.value:6.3f}  trial {t.number:3d}  {desc}  {t.user_attrs.get('conds')}")


if __name__ == "__main__":
    main()
