"""Tests for tools/af_replay.py (run: pytest tools/tests).

The synthetic tests run anywhere. The data test needs the recorded sessions in
work/af-logs (not in the repository) and is skipped without them.
"""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import af_replay as R  # noqa: E402


def camera_array(b):
    """The detector's value array after the last frame: smoothed values, the newest entry
    the prediction (first entry as recorded)."""
    n = len(b)
    return [b[0]] + [(b[i - 1] + 2 * b[i] + b[i + 1]) / 4 for i in range(1, n - 1)] + [(b[-2] + 3 * b[-1]) / 4]


def peak(n=14, top=7, width=3.0, height=0.04, base=0.01):
    return [base + height * math.exp(-0.5 * ((i - top) / width) ** 2) for i in range(n)]


def test_bsolve_recovers_b_from_the_array_and_a_few_records():
    b = peak()
    area = dict(arr=camera_array(b), b={0: b[0], 5: b[5], 9: b[9], len(b) - 2: b[-2]})
    got = R.bsolve(area)
    assert max(abs(x - y) for x, y in zip(got, b)) < 1e-6


def test_bsolve_refuses_a_stale_entry():
    assert R.bsolve(dict(arr=[0.01, 0.02, 3285.3, 0.02], b={})) is None


def run_detector(b, floor=0.001, T=0.002):
    area = dict(arr=camera_array(b), b={})
    det, states = R.Detector(), []
    for m, v in R.frame_arrays(area, b):
        det.update(v, m - 1, floor, T)
        states.append((m, det.rise, det.fall, det.state))
    return states


def test_detector_confirms_a_clean_peak_after_the_first_real_fall():
    states = run_detector(peak())
    first = {s: next(m for m, *_, st in states if st == s) for s in (1, 3)}
    assert first[1] < 8 < first[3] <= 10      # rise confirmed before the top, fall right after


def test_detector_ignores_a_peak_below_the_margin():
    states = run_detector(peak(height=0.002), T=0.004)
    assert all(st == 0 for *_, st in states)


def test_detector_hysteresis_one_rise_only_takes_one_fall_back():
    v = [0.030, 0.029, 0.028, 0.027, 0.028, 0.027, 0.028, 0.029]
    det, counts = R.Detector(), []
    for i in range(1, len(v)):
        det.update(v, i, 0.001, 0.002)
        counts.append((det.rise, det.fall))
    assert counts[:3] == [(0, 1), (0, 2), (0, 3)]
    assert counts[3] == (1, 2)       # a single rise takes one fall back
    assert counts[4] == (0, 3)
    assert counts[5:] == [(1, 2), (2, 0)]   # the second rise in a row clears the falls


def test_peak_fits_are_exact_on_their_own_shapes():
    x = [0, 8, 16, 24, 32]
    para = [-(xi - 13.0) ** 2 for xi in x]
    gauss = [0.01 + math.exp(-0.5 * ((xi - 13.0) / 9) ** 2) for xi in x]
    assert R.parabola3(x, para, 2) == pytest.approx(13.0)
    assert R.quad5(x, para, 2) == pytest.approx(13.0)
    assert abs(R.gauss3(x, gauss, 2) - 13.0) < 1.0


@pytest.mark.skipif(not (R.LOGS / "remote-nd2" / "tails").is_dir(), reason="needs work/af-logs (local data)")
def test_detector_port_matches_the_recorded_camera_state():
    # The two dim scenes the rule results rest on (97.6 % and 94.5 % when written; all
    # sessions 95.4 %, the lowest 80 % in the forced grade-0 session remote-g0).
    presses = R.load(["remote-nd2", "remote-g0real"])
    agree = total = 0
    for p in presses:
        for q in p["passes"]:
            for a in (0, 1):
                for m, r, f, state, rec in R.replay_area(p, q, a) or ():
                    if rec is not None:
                        total += 1
                        agree += state == rec[4]
    assert total > 1000 and agree / total > 0.93
