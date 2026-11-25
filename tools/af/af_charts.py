#!/usr/bin/env python3
"""Charts of the AF measurements: writes docs/af-charts.html.

Usage:  python3 tools/af/af_charts.py [--out docs/af-charts.html]
Reads the camera's debug CSVs and TRACE.DAT files under work/af-logs (see
docs/af-debug-log.html) with the parsers in af_csv, af_light and
af_debug_card, and draws every chart as inline SVG (standard library only).
The page carries its own data, so it is committed although work/ is not.
Prints the aggregates it charts, to compare with the tables in the docs.
Offline; reads your own files.
"""
import argparse, hashlib, html, json, math, random, re, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / a) for a in ("firmware", "af", "effects", "recipes")]
import af_csv, af_light, af_debug_card  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "work" / "af-logs"

# Traced dim AF-S sessions and the margin each ran with (AB.LOG, *.ERR and
# docs/af-debug-log.html: dim-ab2 is power-on 2 = stock, dim-ab1/dim-ab3 are
# power-ons 1 and 3 = SCALE75). f49-ab is kept apart: SCALE75 in both halves.
TRACE_VARIANT = {
    "dim-contrast": "stock", "dim-arrays": "stock", "dim-ab2": "stock",
    "dim-sqrtns": "x0.5", "dim-scale75": "x0.75", "dim-ab1": "x0.75", "dim-ab3": "x0.75",
}
VARIANTS = [("stock", "Stock", "s1", "circle"),
            ("x0.5", "Strong margin cut (SQRTNS)", "s3", "triangle"),
            ("x0.75", "Mild margin cut (SCALE75)", "s2", "square")]
BUCKETS = [(0, 60e3, "<60k"), (60e3, 150e3, "60–150k"), (150e3, 400e3, "150–400k"),
           (400e3, float("inf"), ">400k")]
NEAR_STOP = -16000  # a lock that ends nearer than this sits at the near stop: false

# Sessions whose CSVs were taken with a patch that changes the frame rate or
# the margin; left out of the stock frame-rate and noise charts.
PATCHED = {"force100", "afc-patched", "gaincap-24x", "gaincap-log", "f49-ab",
           "dim-scale75", "dim-sqrtns", "dim-ab1", "dim-ab3"}
DIM_AFS_STOCK = ["low-light", "low-light-face", "af-area", "dim-afs-2"]

# ---------------------------------------------------------------- data


def csv_runs(session):
    """Unique finished sweeps of one session, as af_csv.run() dicts plus the
    sample table and a few header fields."""
    out, seen = [], set()
    for p in sorted((LOGS / session / "csv").glob("*.csv")):
        text = p.read_text(encoding="latin-1")
        k = hashlib.sha256(af_csv.key(text).encode("latin-1")).hexdigest()
        if k in seen:
            continue
        seen.add(k)
        r = af_csv.run(p)
        if not r["n"] or r["fps"] is None:
            continue
        r["ms"] = r["n"] * r["frame_us"] / 1000
        r["session"] = session
        r["rows"] = af_csv.samples(text)
        r["peak"] = "*" in r["areas"]
        alg = af_light.algorithm_row(text)
        r["thr"] = alg[3] / 1e6 if alg else None
        out.append(r)
    return out


def presses():
    out = []
    for session, variant in TRACE_VARIANT.items():
        _, runs = af_debug_card.trace_presses(LOGS / session / "TRACE.DAT")
        for r in runs:
            if not r["y"]:
                continue
            out.append(press(r, session, variant))
    return out


def press(r, session, variant):
    ok = r["outcome"] in ("lock", "done")
    false = ok and r["final"] is not None and r["final"] > NEAR_STOP
    return {"session": session, "variant": variant,
            "y": statistics.median(r["y"]) if r["y"] else None,
            "scale": max(r["scale"]) if r["scale"] else None,
            "ms": r["ms"], "passes": r["passes"], "final": r["final"],
            "outcome": "false" if false else "lock" if ok else r["outcome"]}


def f49_presses():
    out = {}
    for name, label in (("TRACE1.DAT", "Forced 49 fps"), ("TRACE2.DAT", "Stock 29 fps")):
        _, runs = af_debug_card.trace_presses(LOGS / "f49-ab" / name)
        out[label] = [press(r, "f49-ab", "x0.75") for r in runs]
    return out


def sweeps_light():
    """af_light per-sweep values plus the noise floor per pixel."""
    out = []
    for d in sorted(p for p in LOGS.glob("*/csv") if p.is_dir()):
        if d.parent.name in PATCHED:
            continue
        seen = set()
        for p in sorted(d.glob("*.csv")):
            text = p.read_text(encoding="latin-1")
            k = af_csv.key(text)
            if k in seen:
                continue
            seen.add(k)
            s = af_light.sweep(p, text)
            if not s:
                continue
            h = [row[1] for row in af_csv.samples(text)]
            s["session"] = d.parent.name
            s["floor_px"] = min(h) / s["px"]
            s["light"] = 1000 * s["y_px"] / (s["integ"] * s["gain"] / 65536) if s["integ"] and s["gain"] else 0
            out.append(s)
    return out


# ---------------------------------------------------------------- svg


def esc(s):
    return html.escape(str(s), quote=True)


def fmt(v):
    if v is None:
        return "–"
    if abs(v) >= 1e6:
        return f"{v / 1e6:g}M"
    if abs(v) >= 1e4:
        return f"{v / 1e3:g}k"
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 1:
        return f"{v:g}"
    return f"{v:.2g}"


def lin_ticks(lo, hi, n=5):
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    t = math.ceil(lo / step) * step
    out = []
    while t <= hi + 1e-9:
        out.append(round(t, 10))
        t += step
    return out


def log_ticks(lo, hi):
    a, b = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
    mults = (1, 2, 5) if b - a <= 3 else (1,)
    return [m * 10 ** k for k in range(a, b + 1) for m in mults if lo <= m * 10 ** k <= hi]


class Scale:
    def __init__(self, lo, hi, a, b, log=False):
        self.lo, self.hi, self.a, self.b, self.log = lo, hi, a, b, log

    def __call__(self, v):
        f = (lambda x: math.log10(x)) if self.log else (lambda x: x)
        return self.a + (f(v) - f(self.lo)) / (f(self.hi) - f(self.lo)) * (self.b - self.a)

    def ticks(self):
        return log_ticks(self.lo, self.hi) if self.log else lin_ticks(self.lo, self.hi)


def shape(kind, x, y, cls, tip, r=4.5):
    t = f' data-tip="{esc(tip)}"' if tip else ""
    if kind == "circle":
        m = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" class="{cls}"/>'
    elif kind == "ring":
        m = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r - 0.75}" class="{cls}"/>'
    elif kind == "square":
        m = f'<rect x="{x - r + .5:.1f}" y="{y - r + .5:.1f}" width="{2 * r - 1}" height="{2 * r - 1}" rx="1" class="{cls}"/>'
    elif kind == "triangle":
        m = (f'<path d="M{x:.1f},{y - r - .5:.1f} L{x + r + .5:.1f},{y + r - .5:.1f} '
             f'L{x - r - .5:.1f},{y + r - .5:.1f}Z" class="{cls}"/>')
    elif kind == "diamond":
        r += .8
        m = f'<path d="M{x:.1f},{y - r:.1f} L{x + r:.1f},{y:.1f} L{x:.1f},{y + r:.1f} L{x - r:.1f},{y:.1f}Z" class="{cls}"/>'
    elif kind == "cross":
        d = r * .8
        m = (f'<path d="M{x - d:.1f},{y - d:.1f} L{x + d:.1f},{y + d:.1f} M{x + d:.1f},{y - d:.1f} '
             f'L{x - d:.1f},{y + d:.1f}" class="{cls} x"/>')
    return f'<g class="mk"{t}><circle cx="{x:.1f}" cy="{y:.1f}" r="9" class="hit"/>{m}</g>'


def axes(xs, ys, x_label=None, y_label=None, x_fmt=fmt, y_fmt=fmt, x_ticks=None, y_ticks=None):
    """Hairline grid at the y ticks, baseline, tick labels, axis titles."""
    o = []
    for t in (ys.ticks() if y_ticks is None else y_ticks):
        y = ys(t)
        o.append(f'<line x1="{xs.a}" x2="{xs.b}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        o.append(f'<text x="{xs.a - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{esc(y_fmt(t))}</text>')
    o.append(f'<line x1="{xs.a}" x2="{xs.b}" y1="{ys.a}" y2="{ys.a}" class="axis"/>')
    for t in (xs.ticks() if x_ticks is None else x_ticks):
        x = xs(t)
        o.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ys.a}" y2="{ys.a + 4}" class="axis"/>')
        o.append(f'<text x="{x:.1f}" y="{ys.a + 18}" class="tick" text-anchor="middle">{esc(x_fmt(t))}</text>')
    if x_label:
        o.append(f'<text x="{(xs.a + xs.b) / 2:.1f}" y="{ys.a + 38}" class="lab" text-anchor="middle">{esc(x_label)}</text>')
    if y_label:
        o.append(f'<text x="{xs.a - 40}" y="{ys.b - 16}" class="lab" text-anchor="start">{esc(y_label)}</text>')
    return o


def svg(w, h, body, label):
    return (f'<div class="chart"><svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            + "".join(body) + "</svg></div>")


def legend(items):
    """items: (label, mark html) pairs, shown above the chart."""
    return ('<div class="legend">' + "".join(
        f'<span><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">{m}</svg>{esc(l)}</span>'
        for l, m in items) + "</div>")


def key_mark(kind, cls):
    return shape(kind, 8, 8, cls, None).replace('<circle cx="8.0" cy="8.0" r="9" class="hit"/>', "")


def table(head, rows):
    return ("<details><summary>Data</summary><table><thead><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
                                                for r in rows) + "</tbody></table></details>")


def jitter(key, spread):
    h = int(hashlib.md5(str(key).encode()).hexdigest()[:8], 16) / 0xffffffff
    return (h - .5) * spread


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "–"


def rounded_top(x, y0, y1, w, r=4):
    """Column from baseline y0 up to y1 with a rounded data end."""
    r = min(r, (y0 - y1) / 2, w / 2)
    return (f"M{x:.1f},{y0:.1f} V{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{y1:.1f} {x + w:.1f},{y1 + r:.1f} V{y0:.1f}Z")


# ---------------------------------------------------------------- charts

OUTCOMES = [("lock", "Locked", "circle", "good"), ("false", "False lock (near stop)", "diamond", "warn"),
            ("fail", "Failed", "cross", "crit"), ("abort", "Released before a result", "ring", "abort")]


def chart_lock_rate(ps):
    tot, ok, false = Counter(), Counter(), Counter()
    for p in ps:
        b = next(i for i, (lo, hi, _) in enumerate(BUCKETS) if lo <= p["y"] < hi)
        tot[p["variant"], b] += 1
        ok[p["variant"], b] += p["outcome"] in ("lock", "false")
        false[p["variant"], b] += p["outcome"] == "false"
    W, H = 720, 330
    xs = Scale(0, len(BUCKETS), 70, W - 20)
    ys = Scale(0, 100, H - 60, 40)
    o = axes(xs, ys, "Light the AF sees (camera brightness value, median per press)", "% of presses locked",
             y_fmt=lambda v: f"{v:g}%", x_ticks=[])
    bw, gap = 24, 10
    for b, (_, _, name) in enumerate(BUCKETS):
        cx = (xs(b) + xs(b + 1)) / 2
        o.append(f'<text x="{cx:.1f}" y="{ys.a + 18}" class="tick" text-anchor="middle">{esc(name)}</text>')
        for i, (v, vname, col, _) in enumerate(VARIANTS):
            n, k, f = tot[v, b], ok[v, b], false[v, b]
            x = cx + (i - 1) * (bw + gap) - bw / 2
            tip = f"{vname}, Y {name}: {k} of {n} locked ({pct(k, n)})" + (f", {f} of them false" if f else "")
            if n:
                yt, yf = ys(100 * (k - f) / n), ys(100 * k / n)
                g = [f'<g class="mk" data-tip="{esc(tip)}"><rect x="{x - 4}" y="{ys.b}" width="{bw + 8}" '
                     f'height="{ys.a - ys.b}" class="hit"/>']
                if k - f:
                    g.append(f'<path d="{rounded_top(x, ys.a, yt, bw, 0 if f else 4)}" class="f-{col}"/>')
                if f:
                    lo = yt - 2 if k - f else ys.a
                    g.append(f'<path d="{rounded_top(x, lo, yf, bw)}" class="hatch-{col}"/>')
                g.append("</g>")
                o += g
            o.append(f'<text x="{x + bw / 2:.1f}" y="{ys(100 * k / n if n else 0) - 6:.1f}" class="val" '
                     f'text-anchor="middle">{k}/{n}</text>')
    rows = [[vname] + [f"{ok[v, b]}/{tot[v, b]}" + (f" ({false[v, b]} false)" if false[v, b] else "")
                       for b in range(len(BUCKETS))] for v, vname, _, _ in VARIANTS]
    print("lock rate by brightness:", *rows, sep="\n  ")
    leg = legend([(n, key_mark(s, f"f-{c}")) for _, n, c, s in VARIANTS]
                 + [("False lock (hatched)", '<rect x="2" y="2" width="12" height="12" rx="2" class="hatch-s1"/>')])
    return leg + svg(W, H, o, "Lock rate by brightness bucket and margin patch") + \
        table(["Margin"] + [b[2] for b in BUCKETS], rows)


def chart_presses(ps):
    W, H = 720, 330
    pw, x0 = 200, 60
    o = []
    ymax = math.ceil(max(p["ms"] for p in ps if p["ms"]) / 1000)
    for i, (v, vname, _, _) in enumerate(VARIANTS):
        a = x0 + i * (pw + 30)
        xs = Scale(10e3, 3e6, a, a + pw, log=True)
        ys = Scale(0, ymax, H - 60, 64)
        o += axes(xs, ys, None, "Time to result, s" if i == 0 else None, y_fmt=lambda t: f"{t:g}",
                  x_ticks=[1e4, 1e5, 1e6]) if i == 0 else \
            [x for x in axes(xs, ys, None, None, y_fmt=lambda t: "", x_ticks=[1e4, 1e5, 1e6])]
        o.append(f'<text x="{a}" y="16" class="lab strong">{esc(vname)}</text>')
        for lo in (60e3, 150e3, 400e3):
            o.append(f'<line x1="{xs(lo):.1f}" x2="{xs(lo):.1f}" y1="{ys.b}" y2="{ys.a}" class="grid"/>')
        sel = [p for p in ps if p["variant"] == v]
        for p in sorted(sel, key=lambda p: p["outcome"] == "lock"):
            ms = p["ms"] if p["ms"] is not None else ymax * 1000
            kind, cls = next((s, c) for k, _, s, c in OUTCOMES if k == p["outcome"])
            tip = (f"{p['session']}: Y {p['y']:,.0f}, {ms / 1000:.2f} s, {p['passes']} pass"
                   f"{'es' if p['passes'] != 1 else ''}, {dict((k, n) for k, n, _, _ in OUTCOMES)[p['outcome']].lower()}")
            o.append(shape(kind, xs(p["y"]), ys(ms / 1000), f"f-{cls}" if kind != "ring" else "o-abort", tip))
    o.append(f'<text x="{W / 2}" y="{H - 18}" class="lab" text-anchor="middle">'
             "Light the AF sees (camera brightness value, log); lines at 60k / 150k / 400k</text>")
    leg = legend([(n, key_mark(s, f"f-{c}" if s != "ring" else "o-abort")) for _, n, s, c in OUTCOMES])
    rows = [[p["variant"], p["session"], f"{p['y']:,.0f}", f"{p['ms']:.0f}" if p["ms"] else "–", p["passes"],
             p["outcome"], f"{p['scale']:.2f}" if p["scale"] else "–"] for p in sorted(ps, key=lambda p: (p["variant"], p["y"]))]
    return leg + svg(W, H, o, "Every traced press: brightness against time to result") + \
        table(["Margin", "Session", "Y", "ms", "Passes", "Outcome", "Scale max"], rows)


def strip(rows, x_lo, x_hi, x_label, log=True, label_fmt=None, W=720, row_h=34, left=210, title=""):
    """rows: (label, values, color class, tip prefix). One row of jittered
    dots per label with a median tick and label."""
    H = 30 + row_h * len(rows) + 46
    xs = Scale(x_lo, x_hi, left, W - 115, log=log)
    ys = Scale(0, len(rows), 30 + row_h * len(rows), 30)
    o = []
    for t in xs.ticks():
        o.append(f'<line x1="{xs(t):.1f}" x2="{xs(t):.1f}" y1="{ys.b}" y2="{ys.a}" class="grid"/>')
        o.append(f'<text x="{xs(t):.1f}" y="{ys.a + 18}" class="tick" text-anchor="middle">{esc(fmt(t))}</text>')
    o.append(f'<text x="{(xs.a + xs.b) / 2:.1f}" y="{ys.a + 38}" class="lab" text-anchor="middle">{esc(x_label)}</text>')
    summary = []
    for i, (label, vals, col, kind) in enumerate(rows):
        cy = 30 + row_h * (i + .5)
        o.append(f'<line x1="{xs.a}" x2="{xs.b}" y1="{cy:.1f}" y2="{cy:.1f}" class="grid"/>')
        o.append(f'<text x="{left - 12}" y="{cy + 4:.1f}" class="tick ink" text-anchor="end">{esc(label)}</text>')
        for j, (v, tip) in enumerate(vals):
            v = min(max(v, x_lo), x_hi)
            o.append(shape(kind, xs(v), cy + jitter((label, j, v), row_h * .55), f"f-{col} dense", tip, r=3.5))
        m = median([v for v, _ in vals])
        if m is not None:
            o.append(f'<line x1="{xs(m):.1f}" x2="{xs(m):.1f}" y1="{cy - row_h * .42:.1f}" '
                     f'y2="{cy + row_h * .42:.1f}" class="med"/>')
            o.append(f'<text x="{xs.b + 10}" y="{cy + 4:.1f}" class="tick">{esc((label_fmt or fmt)(m))} · n {len(vals)}</text>')
        vs = [v for v, _ in vals]
        summary.append([label, len(vs), (label_fmt or fmt)(m), fmt(min(vs)) if vs else "–", fmt(max(vs)) if vs else "–"])
    return svg(W, H, o, title), summary


def chart_ladder(light):
    rows = []
    for fps, label in ((200, "200 fps (AF-S)"), (100, "100 fps (AF-S)"), (59, "59 fps (AF-C live view)"),
                       (49, "49 fps (AF-S)"), (29, "29 fps (AF-S)")):
        vals = [(s["light"], f"{s['session']}: {s['fps']} fps, light {s['light']:.2g}, gain {s['gain'] / 65536:.1f}×, "
                 f"{s['integ']:,} µs") for s in light if s["fps"] == fps and s["light"] > 0]
        rows.append((label, vals, "s1", "circle"))
    c, summary = strip(rows, 0.005, 300, "Scene light in camera units: brightness ÷ (exposure × gain), log",
                       label_fmt=lambda v: f"{v:.2g}", title="Scene light by frame rate")
    print("ladder medians:", [(r[0], r[2]) for r in summary])
    return c + table(["Frame rate", "Sweeps", "Median", "Min", "Max"], summary)


def fit_loglog(pts):
    lx = [math.log10(x) for x, _ in pts]
    ly = [math.log10(y) for _, y in pts]
    mx, my = sum(lx) / len(lx), sum(ly) / len(ly)
    k = sum((a - mx) * (b - my) for a, b in zip(lx, ly)) / sum((a - mx) ** 2 for a in lx)
    return k, my - k * mx


def chart_noise(light):
    pts = [(s["y_px"] * s["gain"] / 65536, s["floor_px"], s) for s in light if s["floor_px"] > 0 and s["y_px"] > 0]
    k, c = fit_loglog([(x, y) for x, y, _ in pts])
    print(f"noise floor slope {k:.2f} over {len(pts)} sweeps")
    W, H = 720, 340
    xlo = 10 ** math.floor(math.log10(min(x for x, _, _ in pts)))
    xhi = 10 ** math.ceil(math.log10(max(x for x, _, _ in pts)))
    ylo = 10 ** math.floor(math.log10(min(y for _, y, _ in pts)))
    yhi = 10 ** math.ceil(math.log10(max(y for _, y, _ in pts)))
    xs = Scale(xlo, xhi, 70, W - 20, log=True)
    ys = Scale(ylo, yhi, H - 60, 40, log=True)
    o = axes(xs, ys, "Brightness per pixel × gain (log)", "Contrast floor per pixel (log)")
    for x, y, s in pts:
        o.append(shape("circle", xs(x), ys(y), "f-s1 dense",
                       f"{s['session']}: {s['fps']} fps, Y/px {s['y_px']:.0f}, gain {s['gain'] / 65536:.1f}×, "
                       f"floor/px {y:.3g}", r=3.5))
    y_at = lambda x: 10 ** (c + k * math.log10(x))
    o.append(f'<line x1="{xs(xlo):.1f}" y1="{ys(max(y_at(xlo), ylo)):.1f}" x2="{xs(xhi):.1f}" '
             f'y2="{ys(min(y_at(xhi), yhi)):.1f}" class="fit"/>')
    o.append(f'<text x="{xs.b - 6}" y="{ys.a - 10}" class="val" text-anchor="end">'
             f'fit: slope {k:.2f} (shot noise: 0.50)</text>')
    return svg(W, H, o, "Contrast noise floor against brightness × gain"), k, len(pts)


def chart_snr(light):
    rows = []
    for peak, label in ((True, "Peak confirmed"), (False, "No peak found")):
        vals = [(s["snr"], f"{s['session']}: {s['fps']} fps, rise {100 * s['mod']:.0f}%, {s['snr']:.1f} noise widths")
                for s in light if s["peak"] == peak and math.isfinite(s["snr"]) and s["snr"] > 0]
        rows.append((label, vals, "s1", "circle"))
    c, summary = strip(rows, 1, 300, "Height of the focus peak, in noise widths (log)",
                       label_fmt=lambda v: f"{v:.1f}", row_h=44, left=150, title="Peak-to-noise by outcome")
    print("snr medians:", [(r[0], r[1], r[2]) for r in summary])
    return c + table(["Sweeps", "n", "Median", "Min", "Max"], summary)


def chart_conditions():
    groups = [
        ("Bright AF-S", ["check5", "bright-edge"], "s1"),
        ("Bright AF-S, repetitive", ["repetitive"], "s1"),
        ("Bright AF-C", ["afc", "afc-stock"], "s1"),
        ("Bright AF-C, fast-drive patch", ["afc-patched"], "s2"),
        ("Dim AF-S", DIM_AFS_STOCK + ["dim-trace", "dim-contrast", "dim-arrays"], "s1"),
        ("Dim AF-C (eye detect)", ["low-light-eye"], "s1"),
        ("Dim AF-S, mild margin cut", ["dim-scale75", "dim-ab1", "dim-ab3"], "s2"),
        ("Dim AF-S, forced 100 fps", ["force100"], "s2"),
    ]
    rows = []
    for label, sessions, col in groups:
        vals = [(r["ms"], f"{r['session']} {r['file']}: {r['fps']} fps, {r['n']} samples, {r['ms']:.0f} ms, path {r['path']}")
                for s in sessions for r in csv_runs(s)]
        rows.append((label, vals, col, "circle" if col == "s1" else "square"))
    c, summary = strip(rows, 20, 2000, "Sampling time of the logged sweep, ms (samples × frame time; log)",
                       label_fmt=lambda v: f"{v:.0f} ms", title="Sweep sampling time by condition")
    print("sweep ms by condition:", [(r[0], r[1], r[2]) for r in summary])
    leg = legend([("Stock firmware", key_mark("circle", "f-s1")), ("Early test patches", key_mark("square", "f-s2"))])
    return leg + c + table(["Condition", "Sweeps", "Median", "Min", "Max"], summary)


def chart_afc(site=False):
    """AF-C sweep time against defocus. site=True: tooltips without session and file names."""
    series = [("Stock AF-C", ["afc", "afc-stock"], "s1", "circle"), ("Fast-drive patch (AFCFAST)", ["afc-patched"], "s2", "square")]
    W, H = 720, 320
    xs = Scale(0, 2500, 70, W - 20)
    ys = Scale(0, 1400, H - 60, 40)
    o = axes(xs, ys, "Phase-detect defocus before the sweep, |µm|", "Sweep sampling time, ms")
    rows = []
    for name, sessions, col, kind in series:
        rs = [r for s in sessions for r in csv_runs(s) if r["defocus_um"] not in (None, 65535)]
        for r in rs:
            o.append(shape(kind, xs(min(abs(r["defocus_um"]), 2500)), ys(min(r["ms"], 1400)), f"f-{col}",
                           ("" if site else f"{r['session']} {r['file']}: ")
                           + f"{name}: {r['fps']} fps, |defocus| {abs(r['defocus_um'])} µm, {r['ms']:.0f} ms"))
        mid = [r["ms"] for r in rs if 500 <= abs(r["defocus_um"]) <= 1100]
        rows.append([name, len(rs), f"{median([r['ms'] for r in rs]):.0f}", len(mid), f"{median(mid):.0f}" if mid else "–",
                     ", ".join(f"{f} fps: {n}" for f, n in sorted(Counter(r["fps"] for r in rs).items(), reverse=True))])
    print("AF-C:", rows)
    leg = legend([(n, key_mark(k, f"f-{c}")) for n, _, c, k in series])
    return leg + svg(W, H, o, "AF-C sweep time against defocus, stock and AFCFAST") + \
        table(["Firmware", "Sweeps", "Median ms", "Sweeps at 500–1,100 µm", "Median ms there", "Frame rates"], rows)


def chart_scale(ps):
    W, H = 720, 320
    sel = [p for p in ps if p["scale"]]
    xs = Scale(10e3, 3e6, 70, W - 20, log=True)
    ys = Scale(0.5, 100, H - 60, 40, log=True)
    o = axes(xs, ys, "Light the AF sees (camera brightness value, log)", "How far the margin is raised, × (log)")
    for v, vname, col, kind in VARIANTS:
        for p in sel:
            if p["variant"] == v:
                o.append(shape(kind, xs(p["y"]), ys(max(p["scale"], .5)), f"f-{col}",
                               f"{vname}, {p['session']}: Y {p['y']:,.0f}, scale {p['scale']:.2f}, {p['outcome']}"))
    leg = legend([(n, key_mark(s, f"f-{c}")) for _, n, c, s in VARIANTS])
    return leg + svg(W, H, o, "Margin scale against brightness per patch")


def chart_force100():
    stock = [r for s in DIM_AFS_STOCK for r in csv_runs(s)]
    forced = csv_runs("force100")
    groups = [("Stock dim AF-S", stock, "s1"), ("Forced 100 fps", forced, "s2")]
    metrics = [("No peak found", lambda r: not r["peak"]), ("Ran to path 2 (full travel)", lambda r: r["path"] == 2)]
    W, H = 720, 260
    xs = Scale(0, len(metrics), 70, W - 20)
    ys = Scale(0, 100, H - 50, 40)
    o = axes(xs, ys, None, "% of sweeps", y_fmt=lambda v: f"{v:g}%", x_ticks=[])
    rows = []
    for mi, (mname, f) in enumerate(metrics):
        cx = (xs(mi) + xs(mi + 1)) / 2
        o.append(f'<text x="{cx:.1f}" y="{ys.a + 18}" class="tick" text-anchor="middle">{esc(mname)}</text>')
        for gi, (gname, rs, col) in enumerate(groups):
            k = sum(f(r) for r in rs)
            x = cx + (gi - .5) * 34 - 12
            y = ys(100 * k / len(rs))
            tip = f"{gname}: {mname.lower()} in {k} of {len(rs)} sweeps ({pct(k, len(rs))})"
            o.append(f'<g class="mk" data-tip="{esc(tip)}"><rect x="{x - 4}" y="{ys.b}" width="32" height="{ys.a - ys.b}" class="hit"/>'
                     + (f'<path d="{rounded_top(x, ys.a, y, 24)}" class="f-{col}"/>' if k else "") + "</g>")
            o.append(f'<text x="{x + 12:.1f}" y="{y - 6:.1f}" class="val" text-anchor="middle">{pct(k, len(rs))}</text>')
    for gname, rs, _ in groups:
        rows.append([gname, len(rs), sum(not r["peak"] for r in rs), sum(r["path"] == 2 for r in rs),
                     f"{median([r['n'] for r in rs]):g}", f"{median([r['ms'] for r in rs]):.0f} ms"])
    print("force100:", rows)
    leg = legend([(g, key_mark("square", f"f-{c}")) for g, _, c in groups])
    return leg + svg(W, H, o, "FORCE100 against stock dim AF-S") + \
        table(["Sweeps", "n", "No peak", "Path 2", "Median samples", "Median sampling"], rows)


def chart_f49(f49):
    W, row_h, left = 720, 46, 170
    H = 20 + row_h * len(f49) + 10
    o = []
    rows = []
    bar_w = W - left - 20
    for i, (label, ps) in enumerate(f49.items()):
        cy = 20 + row_h * i
        o.append(f'<text x="{left - 12}" y="{cy + 19}" class="tick ink" text-anchor="end">{esc(label)}</text>')
        x = left
        counts = Counter(p["outcome"] for p in ps)
        for k, name, _, cls in OUTCOMES:
            n = counts[k]
            if not n:
                continue
            w = bar_w * n / len(ps)
            o.append(f'<g class="mk" data-tip="{esc(f"{label}: {name.lower()} {n} of {len(ps)}")}">'
                     f'<rect x="{x:.1f}" y="{cy + 2}" width="{w - 2:.1f}" height="28" rx="4" class="f-{cls}"/>')
            if w > 30:
                o.append(f'<text x="{x + 8:.1f}" y="{cy + 21}" class="inbar{" dark" if cls == "warn" else ""}">{n}</text>')
            o.append("</g>")
            x += w
        rows.append([label, len(ps)] + [counts[k] for k, *_ in OUTCOMES] +
                    [f"{median([p['ms'] for p in ps if p['ms']]):.0f}"])
    print("FORCE49:", rows)
    leg = legend([(n, f'<rect x="2" y="2" width="12" height="12" rx="3" class="f-{c}"/>') for _, n, _, c in OUTCOMES])
    return leg + svg(W, H, o, "FORCE49 against 29 fps: outcome per press") + \
        table(["Power-on", "Presses"] + [n for _, n, _, _ in OUTCOMES] + ["Median ms"], rows)


def chart_curves():
    """One clean bright sweep and one dim sweep that found no peak."""
    bright = max((r for s in ("bright-edge", "check5") for r in csv_runs(s) if r["n"] >= 10 and r["peak"] and "-" not in r["areas"]),
                 key=lambda r: r["n"])
    dim = max((r for s in DIM_AFS_STOCK + ["dim-arrays", "dim-contrast"] for r in csv_runs(s)
               if not r["peak"] and 12 <= r["n"] <= 30), key=lambda r: r["n"])
    W, H, pw = 720, 330, 300
    o = []
    for i, (r, title) in enumerate(((bright, "Bright, 200 fps: peak confirmed"),
                                    (dim, f"Dim, {dim['fps']} fps: no peak found"))):
        a = 70 + i * (pw + 50)
        h = [row[1] for row in r["rows"]]
        p = list(range(1, len(h) + 1))
        base = min(h)
        rel = [100 * (v / base - 1) for v in h]
        xs = Scale(min(p), max(p), a, a + pw)
        if xs.lo == xs.hi:
            continue
        ytop = max(10, math.ceil(max(rel) / 10) * 10)
        ys = Scale(0, ytop, H - 60, 64)
        o += axes(xs, ys, "Sample (one per frame)", "Contrast above sweep minimum, %" if i == 0 else None, y_fmt=lambda v: f"{v:g}%")
        o.append(f'<text x="{a}" y="16" class="lab strong">{esc(title)}</text>')
        pts = " ".join(f"{xs(x):.1f},{ys(y):.1f}" for x, y in zip(p, rel))
        o.append(f'<polyline points="{pts}" class="ln-s1"/>')
        for j, (x, y) in enumerate(zip(p, rel)):
            o.append(shape("circle", xs(x), ys(y), "f-s1", f"{r['session']} {r['file']}: sample {x}, lens pulse {r['rows'][j][0]}, +{y:.1f}%", r=4))
        if r["peak"]:
            k = rel.index(max(rel)) + 1
            o.append(f'<line x1="{xs(k):.1f}" x2="{xs(k):.1f}" y1="{ys.b}" y2="{ys.a}" class="fit"/>')
            o.append(f'<text x="{xs(k) + 6:.1f}" y="{ys.b + 10}" class="val">peak</text>')
    print("example curves:", bright["session"], bright["file"], dim["session"], dim["file"])
    return svg(W, H, o, "Example contrast curves, bright and dim"), bright, dim



# ---------------------------------------------------------------- candidate (remote A/B runs)

CAND = "Patched (lower margin + 49 fps)"
STOCK_ARM = "STOCK"
# Comparison runs of the candidate (X875+FALLS2+OPT, +NO29 where it matters) against
# stock, from tools/af/af_remote.py sessions and the scratchpad ndblocks runs. Each entry:
# label, frame rate stock ran at, [(session, ND filter or None)], reference position,
# candidate arms. Bright: NO29 does nothing at 100 fps, so both candidate arms pool.
CAND_RUNS = [
    ("Bright, two objects", "100 fps", [("remote-bright", None), ("remote-bright2", None), ("remote-obj2", None)],
     -16294, ("X875+FALLS2+OPT", "X875+FALLS2+OPT+NO29")),
    ("Easy target, ND out", "200 fps", [("remote-easy1", "out")], -16305, ("X875+FALLS2+OPT+NO29",)),
    ("Easy target, ND in", "100 fps", [("remote-easy1", "in")], -16305, ("X875+FALLS2+OPT+NO29",)),
    ("Medium light, day 1", "29 fps", [("remote-ndin-no29", None), ("remote-ndin-no29b", None)], -16294,
     ("X875+FALLS2+OPT+NO29",)),
    ("Dim, day 1", "29 fps", [("remote-dim2", "in"), ("remote-dim-ndin2", None)], -16294, ("X875+FALLS2+OPT+NO29",)),
    ("Dim, day 2, easy target", "29 fps", [("remote-dim29", None), ("remote-dim29b", None)], -16305,
     ("X875+FALLS2+OPT+NO29",)),
]
HARD_RUNS = [("Hard target", "remote-hard1"), ("Blank wall", "remote-wall1"), ("Too dark", "remote-dark")]
HARD_OUT = [("lock", "Locked", "good"), ("done", "Ended without a confirmed peak", "warn"),
            ("fail", "Failed", "crit"), ("abort", "Released before a result", "abort")]


def jsonl(session, nd=None):
    f = LOGS / session / "presses.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        r = json.loads(line)
        if nd is None or r.get("nd") == nd:
            out.append(r)
    return out


def boot_change(a, b, n=4000, seed=7):
    """95% bootstrap interval of median(b) / median(a) - 1."""
    rnd = random.Random(seed)
    rs = sorted(statistics.median(rnd.choices(b, k=len(b))) / statistics.median(rnd.choices(a, k=len(a)))
                for _ in range(n))
    return rs[int(.025 * n)] - 1, rs[int(.975 * n)] - 1


def perm_p(a, b, n=20000, seed=1):
    rnd = random.Random(seed)
    d = abs(statistics.median(a) - statistics.median(b))
    v, c = a + b, 0
    for _ in range(n):
        rnd.shuffle(v)
        c += abs(statistics.median(v[:len(a)]) - statistics.median(v[len(a):])) >= d
    return c / n


def cand_data():
    out = []
    for label, fps, sessions, ref, arms in CAND_RUNS:
        rs = [r for s, nd in sessions for r in jsonl(s, nd)]
        st = [r for r in rs if r["arm"] == STOCK_ARM and r.get("ms")]
        ca = [r for r in rs if r["arm"] in arms and r.get("ms")]
        if not st or not ca:
            continue
        a, b = [r["ms"] for r in st], [r["ms"] for r in ca]
        lo, hi = boot_change(a, b)
        bad = lambda X: sum(r["outcome"] not in ("lock", "done") for r in X)
        wrong = lambda X: sum(r["outcome"] in ("lock", "done") and r.get("final") is not None
                              and abs(r["final"] - ref) > 150 for r in X)
        out.append(dict(label=label, fps=fps, stock=st, cand=ca, ms_s=median(a), ms_c=median(b),
                        change=median(b) / median(a) - 1, lo=lo, hi=hi, p=perm_p(a, b),
                        fail_s=bad(st), fail_c=bad(ca), wrong_s=wrong(st), wrong_c=wrong(ca),
                        sessions=", ".join(s + (f" ({nd})" if nd else "") for s, nd in sessions)))
    return out


def chart_effect(cd):
    W, row_h, left, right = 720, 40, 230, 170
    H = 34 + row_h * len(cd) + 50
    xs = Scale(-60, 30, left, W - right)
    o = []
    for t in range(-60, 31, 15):
        o.append(f'<line x1="{xs(t):.1f}" x2="{xs(t):.1f}" y1="24" y2="{H - 50}" class="{"axis" if t == 0 else "grid"}"/>')
        o.append(f'<text x="{xs(t):.1f}" y="{H - 32}" class="tick" text-anchor="middle">{t:+d}%</text>'.replace("+0%", "0"))
    o.append(f'<text x="{(xs.a + xs.b) / 2:.1f}" y="{H - 10}" class="lab" text-anchor="middle">'
             'Change in median AF time, patched against stock (bar: 95% interval)</text>')
    o.append(f'<text x="{xs(-58):.1f}" y="16" class="lab">← faster</text>')
    o.append(f'<text x="{xs(28):.1f}" y="16" class="lab" text-anchor="end">slower →</text>')
    for i, d in enumerate(cd):
        cy = 34 + row_h * (i + .5)
        o.append(f'<text x="{left - 12}" y="{cy - 2:.1f}" class="tick ink" text-anchor="end">{esc(d["label"])}</text>')
        o.append(f'<text x="{left - 12}" y="{cy + 13:.1f}" class="tick" text-anchor="end">stock at {esc(d["fps"])}</text>')
        tip = (f'{d["label"]} (stock at {d["fps"]}): {d["ms_s"]:.0f} → {d["ms_c"]:.0f} ms, {100 * d["change"]:+.0f}% '
               f'[{100 * d["lo"]:+.0f}%, {100 * d["hi"]:+.0f}%], p {d["p"]:.3f}, n {len(d["stock"])}/{len(d["cand"])}')
        o.append(f'<g class="mk" data-tip="{esc(tip)}"><rect x="{xs.a}" y="{cy - row_h / 2:.1f}" width="{xs.b - xs.a}" '
                 f'height="{row_h}" class="hit"/>'
                 f'<line x1="{xs(100 * d["lo"]):.1f}" x2="{xs(100 * d["hi"]):.1f}" y1="{cy:.1f}" y2="{cy:.1f}" class="ci"/>'
                 + shape("square", xs(100 * d["change"]), cy, "f-s4", None, r=5.5).replace('<g class="mk">', "").replace("</g>", "")
                 + "</g>")
        o.append(f'<text x="{W - right + 14}" y="{cy - 2:.1f}" class="val">{100 * d["change"]:+.0f}%  '
                 f'{d["ms_s"]:.0f} → {d["ms_c"]:.0f} ms</text>')
        o.append(f'<text x="{W - right + 14}" y="{cy + 13:.1f}" class="tick">n {len(d["stock"])} / {len(d["cand"])}'
                 f'{"  · p " + (f"{d[chr(112)]:.3f}" if d["p"] >= .001 else "<0.001")}</text>')
    rows = [[d["label"], d["fps"], len(d["stock"]), len(d["cand"]), f'{d["ms_s"]:.0f}', f'{d["ms_c"]:.0f}',
             f'{100 * d["change"]:+.0f}%', f'{100 * d["lo"]:+.0f}% to {100 * d["hi"]:+.0f}%', f'{d["p"]:.4f}',
             f'{d["fail_s"]} / {d["fail_c"]}', f'{d["wrong_s"]} / {d["wrong_c"]}', d["sessions"]] for d in cd]
    print("candidate effect:", *rows, sep="\n  ")
    return svg(W, H, o, "Change in median AF time, candidate against stock, by condition") + table(
        ["Condition", "Stock fps", "n stock", "n cand.", "Stock ms", "Cand. ms", "Change", "95% interval",
         "p (permutation)", "Failed s / c", "Wrong lock s / c", "Sessions"], rows)


def chart_cand_spread(cd):
    rows = []
    for d in cd:
        for who, rs, col, kind in (("stock", d["stock"], "s1", "circle"), ("patched", d["cand"], "s4", "square")):
            vals = [(r["ms"], f'{d["label"]}, {who}: {r["ms"]:.0f} ms, {r.get("fps")} fps, '
                              f'{r.get("passes")} pass{"es" if r.get("passes") != 1 else ""}, {r["outcome"]}')
                    for r in rs]
            rows.append((f'{d["label"]} · {who}', vals, col, kind))
    c, summary = strip(rows, 100, 2500, "Time from half-press to result, ms (log)", row_h=26, left=250,
                       label_fmt=lambda v: f"{v:.0f} ms", title="Every compared press, stock and candidate")
    leg = legend([("Stock", key_mark("circle", "f-s1")), (CAND, key_mark("square", "f-s4"))])
    return leg + c + table(["Run", "Presses", "Median", "Min", "Max"], summary)


def chart_phases(only_stock=False):
    rs = jsonl("opt3")
    groups = [("Stock, 29 fps", lambda r: r["arm"] == STOCK_ARM),
              ("Patched, 49 fps", lambda r: "FORCE49" in r["arm"] and r["params"].get("falls") == 2
               and str(r["params"].get("exponent")) == "0.875" and abs(float(r["params"].get("kb", 1)) - .32) < .08)]
    groups = groups[:1] if only_stock else groups
    phases = [("wait_ms", "Wait for the lens", "ph1"), ("sweep_ms", "Sweep (frames)", "ph2"),
              ("finish_ms", "Accept and finish", "ph3")]
    W, row_h, left = 720, 54, 170
    H = 20 + row_h * len(groups) + 46
    xs = Scale(0, 700, left, W - 70)
    o = []
    for t in range(0, 701, 100):
        o.append(f'<line x1="{xs(t):.1f}" x2="{xs(t):.1f}" y1="14" y2="{H - 46}" class="grid"/>')
        o.append(f'<text x="{xs(t):.1f}" y="{H - 28}" class="tick" text-anchor="middle">{t}</text>')
    o.append(f'<text x="{(xs.a + xs.b) / 2:.1f}" y="{H - 8}" class="lab" text-anchor="middle">'
             'Median time per phase of a press, ms (dim light)</text>')
    table_rows = []
    for i, (label, sel) in enumerate(groups):
        g = [r for r in rs if r.get("cond") == "off" and r.get("wait_ms") is not None and sel(r)]
        cy = 20 + row_h * i
        o.append(f'<text x="{left - 12}" y="{cy + 20}" class="tick ink" text-anchor="end">{esc(label)}</text>')
        o.append(f'<text x="{left - 12}" y="{cy + 35}" class="tick" text-anchor="end">n {len(g)}</text>')
        x = 0
        vals = []
        for k, name, cls in phases:
            v = median([r[k] for r in g])
            vals.append(v)
            o.append(f'<g class="mk" data-tip="{esc(f"{label}: {name.lower()} {v:.0f} ms (median of {len(g)})")}">'
                     f'<rect x="{xs(x):.1f}" y="{cy + 6}" width="{xs(x + v) - xs(x) - 2:.1f}" height="32" rx="4" class="{cls}"/>'
                     f'<text x="{xs(x) + 8:.1f}" y="{cy + 27}" class="inbar t-{cls}">{v:.0f}</text></g>')
            x += v
        o.append(f'<text x="{xs(x) + 8:.1f}" y="{cy + 27}" class="val">= {x:.0f} ms</text>')
        table_rows.append([label, len(g)] + [f"{v:.0f}" for v in vals] + [f"{sum(vals):.0f}",
                          f'{median([r["ms"] for r in g]):.0f}', f'{median([r["samples"] for r in g]):g}'])
    print("time split:", table_rows)
    leg = legend([(n, f'<rect x="2" y="2" width="12" height="12" rx="3" class="{c}"/>') for _, n, c in phases])
    return leg + svg(W, H, o, "Where a dim-light press spends its time") + table(
        ["Setting", "Presses", "Wait ms", "Sweep ms", "Finish ms", "Sum of medians", "Median total ms", "Samples"],
        table_rows)


def chart_hard():
    W, row_h, left = 720, 40, 230
    data = []
    for scene, sess in HARD_RUNS:
        rs = jsonl(sess)
        for who, arm in (("stock", STOCK_ARM), ("patched", "X875+FALLS2+OPT+NO29")):
            g = [r for r in rs if r["arm"] == arm]
            if g:
                data.append((scene, who, g))
    H = 16 + row_h * len(data) + 10
    o, rows = [], []
    bar_w = W - left - 20
    for i, (scene, who, g) in enumerate(data):
        cy = 16 + row_h * i
        o.append(f'<text x="{left - 12}" y="{cy + 19}" class="tick ink" text-anchor="end">{esc(scene)} · {who}</text>')
        counts = Counter("fail" if r["outcome"] in ("fail", "none") else r["outcome"] for r in g)
        x = left
        for k, name, cls in HARD_OUT:
            n = counts[k]
            if not n:
                continue
            w = bar_w * n / len(g)
            o.append(f'<g class="mk" data-tip="{esc(f"{scene}, {who}: {name.lower()} {n} of {len(g)}")}">'
                     f'<rect x="{x:.1f}" y="{cy + 2}" width="{w - 2:.1f}" height="26" rx="4" class="f-{cls}"/>')
            if w > 24:
                o.append(f'<text x="{x + 7:.1f}" y="{cy + 20}" class="inbar{" dark" if cls == "warn" else ""}">{n}</text>')
            o.append("</g>")
            x += w
        rows.append([scene, who, len(g)] + [counts[k] for k, *_ in HARD_OUT] +
                    [f'{median([r["ms"] for r in g if r.get("ms")]):.0f}'])
    print("hard scenes:", rows)
    leg = legend([(n, f'<rect x="2" y="2" width="12" height="12" rx="3" class="f-{c}"/>') for _, n, c in HARD_OUT])
    return leg + svg(W, H, o, "Outcomes where AF struggles: hard target, blank wall, too dark") + \
        table(["Scene", "Setting", "Presses"] + [n for _, n, _ in HARD_OUT] + ["Median ms"], rows)


def chart_importance():
    try:
        import optuna
    except ImportError:
        return "<p>(optuna not installed: chart skipped)</p>"
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    studies = []
    for name, title in (("opt1", "Study 1: detector thresholds"), ("opt3", "Study 3: thresholds, step, frame rate")):
        db = LOGS / name / "study.db"
        if not db.exists():
            continue
        st = optuna.load_study(study_name=None, storage=f"sqlite:///{db}")
        n = sum(t.state.name == "COMPLETE" for t in st.trials)
        acc = defaultdict(float)  # fANOVA shifts with its random forest: average 5 seeds
        for seed in range(5):
            ev = optuna.importance.FanovaImportanceEvaluator(seed=seed)
            for k, v in optuna.importance.get_param_importances(st, evaluator=ev).items():
                acc[k] += v / 5
        studies.append((title, n, sorted(acc.items(), key=lambda kv: -kv[1])))
    W, row_h, left = 720, 22, 120
    H = sum(30 + row_h * len(imp) + 14 for _, _, imp in studies) + 20
    xs = Scale(0, 1, left, W - 70)
    o, rows, y = [], [], 0
    for title, n, imp in studies:
        o.append(f'<text x="0" y="{y + 18}" class="lab strong">{esc(title)} · {n} trials</text>')
        y += 30
        for k, v in imp:
            o.append(f'<text x="{left - 10}" y="{y + 15}" class="tick ink" text-anchor="end">{esc(k)}</text>')
            o.append(f'<g class="mk" data-tip="{esc(f"{title}: {k} {100 * v:.0f}% of the explained variance")}">'
                     f'<rect x="{left}" y="{y + 3}" width="{max(xs(v) - left, 2):.1f}" height="15" rx="3" class="f-ink"/></g>')
            o.append(f'<text x="{xs(v) + 6:.1f}" y="{y + 15}" class="val">{100 * v:.0f}%</text>')
            rows.append([title, n, k, f"{100 * v:.1f}%"])
            y += row_h
        y += 14
    print("importance:", rows)
    return svg(W, H, o, "Which knobs mattered in the Optuna studies (fANOVA importance)") + \
        table(["Study", "Trials", "Knob", "Share of explained variance"], rows)


# ---------------------------------------------------------------- page

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1f21;--muted:#5d6166;--line:#e2dfd8;--accent:#9a3412;--code-bg:#f1eee8;--th-bg:#f4f1ea;--side:#f6f4ef;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--abort:#898781;--grid:#e8e6e0;--axis:#c3c2b7;
--ph1:#b9b6ac;--ph2:#4a4d52;--ph3:#d6d3ca;--pt1:#0b0b0b;--pt2:#fff;--pt3:#0b0b0b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#16181b;--fg:#e6e4df;--muted:#a3a7ad;--line:#2e3237;--accent:#f0a875;--code-bg:#202328;--th-bg:#1f2226;--side:#1b1d21;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--grid:#25282c;--axis:#3a3e44;--ph1:#6b6e73;--ph2:#d9d6ce;--ph3:#45484d;--pt1:#fff;--pt2:#0b0b0b;--pt3:#fff}}
:root[data-theme="dark"]{--bg:#16181b;--fg:#e6e4df;--muted:#a3a7ad;--line:#2e3237;--accent:#f0a875;--code-bg:#202328;--th-bg:#1f2226;--side:#1b1d21;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--grid:#25282c;--axis:#3a3e44;--ph1:#6b6e73;--ph2:#d9d6ce;--ph3:#45484d;--pt1:#fff;--pt2:#0b0b0b;--pt3:#fff}
*{box-sizing:border-box}
html{scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.65 Charter,"Iowan Old Style",Georgia,serif;-webkit-text-size-adjust:100%}
.layout{display:grid;grid-template-columns:minmax(0,1fr);max-width:1240px;margin:0 auto}
nav#TOC{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:20px 16px;border-bottom:1px solid var(--line)}
nav#TOC .home{display:block;margin-bottom:12px;color:var(--muted);text-decoration:none}
nav#TOC ul{list-style:none;padding-left:0;margin:0}
nav#TOC li{margin:3px 0}
nav#TOC a{color:var(--fg);text-decoration:none}
nav#TOC a:hover{color:var(--accent)}
main{padding:24px 16px 80px;min-width:0;max-width:820px}
@media (min-width:1000px){.layout{grid-template-columns:290px minmax(0,1fr);gap:40px}
 nav#TOC{position:sticky;top:0;align-self:start;max-height:100vh;overflow:auto;border-bottom:0;border-right:1px solid var(--line);background:var(--side)}
 main{padding-top:48px}}
h1,h2,h3{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.25;letter-spacing:-.01em}
h1{font-size:2rem;margin:0 0 .6em}
h2{font-size:1.45rem;margin:2.2em 0 .6em;padding-top:.6em;border-top:1px solid var(--line)}
a{color:var(--accent)}
code{font:.86em/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--code-bg);padding:.1em .35em;border-radius:4px;overflow-wrap:anywhere}
table{border-collapse:collapse;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-width:60%;font-variant-numeric:tabular-nums}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--th-bg);font-weight:600}
details{margin:.4em 0 1.4em;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
details summary{cursor:pointer;color:var(--muted)}
details>table{margin-top:.6em}
details{overflow-x:auto}
.chart{overflow-x:auto;margin:.4em 0 .2em}
.chart svg{display:block;width:100%;min-width:600px;height:auto;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.legend{display:flex;flex-wrap:wrap;gap:4px 18px;font:13px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--muted);margin-top:1em}
.legend span{display:inline-flex;align-items:center;gap:6px}
.grid{stroke:var(--grid);stroke-width:1}
.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.tick.ink{fill:var(--fg);font-size:13px}
.lab{fill:var(--muted);font-size:12.5px}
.lab.strong{fill:var(--fg);font-weight:600;font-size:13px}
.val{fill:var(--fg);font-size:12px;font-variant-numeric:tabular-nums}
.inbar{fill:#fff;font-size:12.5px;font-weight:600}
.inbar.dark{fill:#0b0b0b}
.f-s1{fill:var(--s1)}.f-s2{fill:var(--s2)}.f-s3{fill:var(--s3)}.f-s4{fill:var(--s4)}.f-ink{fill:var(--muted)}
.ph1{fill:var(--ph1)}.ph2{fill:var(--ph2)}.ph3{fill:var(--ph3)}.inbar.t-ph1{fill:var(--pt1)}.inbar.t-ph2{fill:var(--pt2)}.inbar.t-ph3{fill:var(--pt3)}
.ci{stroke:var(--fg);stroke-width:2;stroke-linecap:round;stroke-opacity:.55}
.f-good{fill:var(--good)}.f-warn{fill:var(--warn)}.f-crit{fill:var(--crit)}.f-abort{fill:var(--abort)}
.hatch-s1{fill:url(#h-s1)}.hatch-s2{fill:url(#h-s2)}.hatch-s3{fill:url(#h-s3)}
.mk circle:not(.hit),.mk rect:not(.hit),.mk path{stroke:var(--bg);stroke-width:1.5}
.mk path.x{fill:none;stroke-width:2.5;stroke-linecap:round}
.mk path.x.f-crit{stroke:var(--crit)}
.o-abort{fill:none;stroke:var(--abort)!important;stroke-width:2!important}
.dense{fill-opacity:.75}
.hit{fill:transparent;stroke:none}
.mk:hover circle:not(.hit),.mk:hover rect:not(.hit),.mk:hover path:not(.x){stroke:var(--fg)}
.med{stroke:var(--fg);stroke-width:2;stroke-linecap:round}
.fit{stroke:var(--fg);stroke-width:1.5;stroke-opacity:.7}
.ln-s1{fill:none;stroke:var(--s1);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.cap{color:var(--muted);font-size:15px}
h3{font-size:1.15rem;margin:2em 0 .3em}
.finding{border-left:4px solid var(--accent);background:var(--side);padding:10px 16px;margin:0 0 1.4em;border-radius:0 8px 8px 0}
.finding p{margin:.5em 0}
.takeaway{font:600 16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:.2em 0 .3em}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:1.4em 0}
@media (max-width:640px){.tiles{grid-template-columns:1fr}}
.tile{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--side)}
.tile .big{font:700 1.6rem/1.2 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-variant-numeric:tabular-nums}
.tile .what{font:13.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--muted);margin-top:4px}
.glossary dl{margin:.6em 0;display:grid;grid-template-columns:max-content 1fr;gap:4px 14px}
.glossary dt{font-weight:600}.glossary dd{margin:0;color:var(--muted)}
@media (max-width:640px){.glossary dl{grid-template-columns:1fr}.glossary dd{margin-bottom:6px}}
nav#TOC ul ul{padding-left:12px;margin:2px 0 8px;font-size:13px}
nav#TOC ul ul a{color:var(--muted)}
ul.cap li{margin:.2em 0}
#tip{position:fixed;pointer-events:none;z-index:10;max-width:320px;padding:6px 9px;border-radius:6px;background:var(--fg);color:var(--bg);
font:12.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;opacity:0;transition:opacity .08s}
"""

PATTERNS = "".join(
    f'<pattern id="h-{c}" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    f'<rect width="6" height="6" fill="var(--{c})" fill-opacity=".28"/><rect width="2.5" height="6" fill="var(--{c})"/></pattern>'
    for c in ("s1", "s2", "s3"))

JS = """
(function(){var t=document.getElementById('tip');
function show(e){var m=e.target.closest&&e.target.closest('[data-tip]');if(!m){t.style.opacity=0;return}
t.textContent=m.getAttribute('data-tip');t.style.opacity=1;var x=e.clientX+14,y=e.clientY+14,r=t.getBoundingClientRect();
if(x+r.width>innerWidth-8)x=e.clientX-r.width-14;if(y+r.height>innerHeight-8)y=e.clientY-r.height-14;t.style.left=x+'px';t.style.top=y+'px'}
document.addEventListener('pointermove',show);document.addEventListener('pointerdown',show);
document.addEventListener('scroll',function(){t.style.opacity=0},true)})();
"""


def sub(sid, title, takeaway, caption, chart, details=None):
    """One chart section: the finding first, a short plain caption, the chart,
    then the technical notes folded away."""
    d = f'<details class="more"><summary>Details</summary><p>{details}</p></details>' if details else ""
    return (sid, title, f'<p class="takeaway">{takeaway}</p><p class="cap">{caption}</p>{chart}{d}')


def stat_tiles(items):
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="big">{esc(big)}</div><div class="what">{what}</div></div>'
        for big, what in items) + "</div>"


GLOSSARY = [
    ("Contrast AF", "The GR moves the lens through a range and measures how sharp (how contrasty) the "
                    "focus area is at each step. The sharpest step is the focus."),
    ("Sweep", "That run of the lens with a measurement per frame."),
    ("fps", "Frames per second the camera measures during a sweep. In good light 200; in dim light it "
            "drops to 100, 49 and finally 29 to collect more light per frame."),
    ("Margin", "How far the sharpness peak must stand out before the camera accepts it. It is raised "
               "in dim light so noise is not taken for a peak."),
    ("Patched", "The combined change tested here: a lower margin (found by the optimizer) plus 49 instead "
                "of 29 fps in the darkest mode. Tested from the camera's memory; since 2026-11-20 flashed into the test camera."),
    ("Locked / ended / failed", "Locked: the camera confirmed a peak (green frame). Ended: it stopped "
                                "without confirming one. Failed: it gave up."),
    ("ND filter", "The GR's built-in grey filter, switched in remotely to make the same scene darker."),
]


def glossary():
    return ('<details class="glossary"><summary>Words used on this page</summary><dl>' + "".join(
        f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in GLOSSARY) + "</dl></details>")


def page(intro, chapters):
    toc = "".join(
        f'<li><a href="#{cid}">{esc(ctitle)}</a><ul>'
        + "".join(f'<li><a href="#{sid}">{esc(t)}</a></li>' for sid, t, _ in subs) + "</ul></li>"
        for cid, ctitle, _, subs in chapters)
    body = "".join(
        f'<h2 id="{cid}">{esc(ctitle)}</h2>{cintro}'
        + "".join(f'<h3 id="{sid}">{esc(t)}</h3>{content}' for sid, t, content in subs)
        for cid, ctitle, cintro, subs in chapters)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GR IIIx autofocus study</title>
<style>{CSS}</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>{PATTERNS}</defs></svg>
<div class="layout">
<nav id="TOC"><a class="home" href="index.html">← All documents</a><ul>{toc}</ul></nav>
<main>
<h1>Faster autofocus in dim light on the GR IIIx</h1>
{intro}
{body}
</main>
</div>
<div id="tip" role="tooltip"></div>
<script>{JS}</script>
</body>
</html>
"""


def fill_site(path, charts):
    """Replace <!-- chart:NAME --> ... <!-- /chart:NAME --> in the public page with
    the chart alone: the Data tables (session names, local log names) stay out."""
    text = path.read_text(encoding="utf-8")
    for name, html_ in charts.items():
        html_ = re.sub(r"<details>.*?</details>", "", html_, flags=re.S)
        pat = re.compile(rf"(<!-- chart:{name} -->).*?(<!-- /chart:{name} -->)", re.S)
        if not pat.search(text):
            print(f"site: no block for {name}")
            continue
        text = pat.sub(lambda m: m.group(1) + f'<div class="fig">{html_}</div>' + m.group(2), text)
    path.write_text(text, encoding="utf-8")
    print(f"filled {path.relative_to(ROOT)}: {', '.join(charts)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "af-charts.html")
    ap.add_argument("--site", type=Path, nargs="?", const=ROOT / "site" / "public" / "index.html",
                    help="also fill the <!-- chart:NAME --> blocks of the public summary page")
    args = ap.parse_args()
    ps = presses()
    light = sweeps_light()
    f49 = f49_presses()
    noise_svg, k_noise, n_noise = chart_noise(light)
    curves_svg, bright, dim = chart_curves()
    n_lock = Counter(p["variant"] for p in ps)
    cd = cand_data()
    n_cmp = sum(len(d["stock"]) + len(d["cand"]) for d in cd)
    n_s = sum(len(d["stock"]) for d in cd)
    n_c = sum(len(d["cand"]) for d in cd)
    slow = [d for d in cd if d["fps"] == "29 fps"]
    fast = [d for d in cd if d["fps"] != "29 fps"]
    rng = lambda ds: (f'{100 * min(d["change"] for d in ds):+.0f} to {100 * max(d["change"] for d in ds):+.0f} %'
                      .replace("-", "−"))
    gain = f'{-100 * max(d["change"] for d in slow):.0f}–{-100 * min(d["change"] for d in slow):.0f} %'
    bad_c = sum(d["fail_c"] + d["wrong_c"] for d in cd)
    bad_s = sum(d["fail_s"] + d["wrong_s"] for d in cd)
    n_csv = sum(1 for _ in LOGS.glob("*/csv/*.csv"))

    intro = (
        f'<div class="finding"><p><strong>What we found:</strong> a stable pair of settings that makes the GR IIIx '
        f'focus faster in dim light. AF took {gain} less time wherever the camera dropped to its slowest '
        f'measuring rate, in {len(slow)} scene and light combinations on two days. In good light it was no slower, '
        f'and it did not fail or focus wrongly more often than stock.</p>'
        f'<p>The two changes: the camera accepts a sharpness peak with a somewhat lower safety margin, and in the '
        f'darkest mode it measures 49 instead of 29 frames per second. Neither helped on its own; together they '
        f'cut the slowest part of a press, the focus sweep, by about a third. The patch is experimental: tested '
        f'from the camera\'s memory on one camera and firmware, then flashed into that camera\'s firmware.</p></div>'
        '<p>The Ricoh GR IIIx focuses by contrast: it moves the lens and looks for the sharpest point. In dim '
        'light that gets slow. This page follows one question through the measurements: <strong>where does '
        'the time go, and can a small firmware change win some of it back without making focus less '
        'reliable?</strong> All presses were made on one camera (firmware 1.60), most of them remote-controlled '
        'over Wi-Fi so that stock and patched settings could be interleaved blind.</p>'
        + stat_tiles([
            (gain, f"less AF time where the camera drops to 29 fps (dim light), in {len(slow)} scene and "
                   f"light combinations on two days"),
            (rng(fast), "change in AF time in good light (100–200 fps): no slower"),
            (str(bad_c), f"failed presses or wrong focus in {n_c} patched presses "
                         f"(stock: {bad_s} in {n_s})"),
        ])
        + glossary()
        + f'<details class="more"><summary>About the data</summary><p>Drawn from the raw logs, not copied: '
          f'{n_csv:,} debug CSV dumps (unique finished sweeps, as in <code>tools/af/af_csv.py</code>), '
          f'{len(TRACE_VARIANT) + 2} state traces ({len(ps)} traced presses with brightness data) and the remote '
          f'A/B sessions under <code>work/af-logs</code>. The lab notebook with every run is '
          f'<a href="af-debug-log.html">AF debug log on the camera</a>. Hover a mark for its values; each chart '
          f'has its numbers under <em>Data</em>. Regenerate with <code>python3 tools/af/af_charts.py</code>.</p></details>'
    )

    chapters = [
        ("why-slow", "1. Why dim light makes AF slow",
         '<p class="cap">Four steps: what the camera measures, what it does when light falls, what that costs, '
         'and where in a press the time ends up.</p>', [
             sub("curves", "What a sweep sees",
                 "In good light the sharpness curve has a clear peak. In dim light the peak is barely larger "
                 "than the noise.",
                 "Two real sweeps. The height is how much sharper each frame was than the least sharp one.",
                 curves_svg,
                 f'Left: {esc(bright["session"])} {esc(bright["file"])}. Right: {esc(dim["session"])} '
                 f'{esc(dim["file"])}, a dim sweep the camera could not resolve. Contrast of area 0, horizontal. '
                 f'Only the last pass is logged, so x is the sample number within that pass.'),
             sub("frame-rate", "The camera slows down to see more",
                 "As light falls, the camera measures fewer frames per second: 200, then 100, 49 and 29.",
                 "Each dot is one sweep, placed by the scene light. A slower frame rate gives each frame more "
                 "light. AF-C uses the 59 fps of the live view.",
                 chart_ladder(light),
                 'Scene light is the logged brightness per pixel with exposure time and gain divided out. The '
                 'ranges overlap because the camera picks the rate from its own metering before the sweep. '
                 'Patched sessions are left out.'),
             sub("sweep-time", "Which makes the sweep long",
                 "At 29 fps a sweep takes 6–8 times as long as in good light.",
                 "Time of each logged sweep: number of frames × frame time. The marks show the median.",
                 chart_conditions(),
                 'Sampling time only, not the whole AF delay. The two patched rows are early tests from chapter 2.'),
             sub("time-split-stock", "Where a dim press spends its time",
                 "In dim light the sweep is the largest part of a press. The wait before it is lens travel and "
                 "does not depend on light.",
                 "A median half-press at 29 fps, split into its phases.",
                 chart_phases(only_stock=True),
                 'From the camera\'s AF state trace (action codes 2, 4, 8/128) in the study-3 stock controls.'),
             sub("noise", "Why: sensor noise",
                 "The floor under the sharpness peak is sensor noise, so it grows when there is less light.",
                 f"The lowest contrast value of each sweep against the light. It rises with a slope of "
                 f"{k_noise:.2f} over {n_noise} sweeps, close to the 0.5 of photon noise. Below: sweeps that "
                 f"found no peak rose only a few noise widths.",
                 noise_svg + chart_snr(light),
                 'Contrast per pixel against brightness per pixel × gain, log-log. See '
                 '<a href="af-debug-log.html#why-af-needs-light">the analysis</a>.'),
         ]),
        ("first-tries", "2. Two levers, tried one at a time",
         '<p class="cap">Two settings decide dim-light AF: the <em>margin</em> (how sure the camera must be) and '
         'the <em>frame rate</em> (how fast it measures). Each was changed on its own first.</p>', [
             sub("lock-rate", "Lowering the margin",
                 "A lower margin lets the camera lock in darker light. Cut too far, it locks on nothing.",
                 "Share of half-presses that locked, by the light the AF saw. Hatched: a false lock (the lens "
                 "ended at the near stop).",
                 chart_lock_rate(ps),
                 f'Stock never locked below 60k and locked 57 % from 60k to 150k. The mild cut (SCALE75) locks '
                 f'almost everything from 60k up, with one false lock in the darkest bucket. The strong cut '
                 f'(SQRTNS) locks more in the dark, but 3 of its 5 locks there are false. Presses: stock '
                 f'{n_lock["stock"]}, SQRTNS {n_lock["x0.5"]}, SCALE75 {n_lock["x0.75"]}. See '
                 f'<a href="af-debug-log.html#ab23">A/B, power-ons 2 and 3</a>.'),
             sub("margin-scale", "How the margin rises in the dark",
                 "Stock raises the margin up to about 30× in dim light; the patches cap it.",
                 "Largest margin factor per press against the light the AF saw.",
                 chart_scale(ps),
                 'The factor at <code>+0x80</code>. Stock rises roughly as 1/brightness. See '
                 '<a href="af-debug-log.html#dim-margin">the dim-light margin</a>.'),
             sub("force", "Forcing a faster frame rate",
                 "Measuring faster alone made dim AF worse: less light per frame, more sweeps without a peak.",
                 "Top: forcing 100 fps in dim light. Bottom: forcing 49 instead of 29 fps (with the mild "
                 "margin cut in both halves), outcome per press.",
                 chart_force100() + chart_f49(f49),
                 'FORCE100 makes dim AF-S sample at 100 fps, with a third of the light per frame. Stock sessions: '
                 + ", ".join(f"<code>{s}</code>" for s in DIM_AFS_STOCK) + '. See '
                 '<a href="af-debug-log.html#force100-test">the force-100 test</a> and '
                 '<a href="af-debug-log.html#force49-ab">FORCE49 A/B</a>. The 49 fps test ran in very dim light '
                 'with the older margin patch.'),
         ]),
        ("search", "3. Letting an optimizer search the combination",
         '<p class="cap">Since neither lever worked alone, an optimizer (Optuna) tried combinations: margin '
         'settings, sweep step and frame rate together. The camera was driven over Wi-Fi, each setting uploaded '
         'to its memory and scored on a batch of half-presses, with stock controls in between.</p>', [
             sub("importance", "What mattered",
                 "The margin settings (factor kb and rise floor) and the frame rate did most of the work.",
                 "Share of the variation in the score each knob explains, per study.",
                 chart_importance(),
                 'fANOVA importance averaged over five seeds. With 34 and 60 trials these are rough rankings, '
                 'good for deciding which knobs to freeze, not for effect sizes; those come from chapter 4. '
                 'Study 2 (11 trials) is too short to rank. In study 1 the order of kb and rise swaps between seeds.'),
         ]),
        ("result", "4. The result: patched against stock",
         '<p class="cap">The best combination: a lower margin (exponent 0.875, kb 0.32, falls 2, rise 0.014) '
         'and 49 instead of 29 fps in the darkest mode. Tested blind against stock in several scenes and light '
         'levels.</p>', [
             sub("time-split", "The sweep gets shorter",
                 "The patch cuts the sweep from about 335 to 210 ms. The wait and the finish stay the same.",
                 "Median dim-light press, stock and patched.",
                 chart_phases(),
                 'Study-3 presses; there 49 fps was forced with FORCE49. NO29 gives the same rate only where '
                 'stock would use 29 fps.'),
             sub("effect", "Faster where it was slow, unchanged elsewhere",
                 f"Where the camera drops to 29 fps, AF takes {gain} less time. In good light it is no "
                 f"slower.",
                 "Each row is one scene and light level. The square is the change in median AF time, the bar "
                 "its 95 % range.",
                 chart_effect(cd),
                 f'{n_cmp} presses, settings interleaved. The light is given by the frame rate stock chose, because '
                 f'the metered exposure did not track it. 95 % bootstrap interval; p from a permutation test of '
                 f'the medians. See <a href="af-debug-log.html#opt-study">the remote studies</a>.'),
             sub("presses-compared", "Every press, not just the median",
                 "The whole spread moves. Most of the slow presses that needed a second pass disappear.",
                 "Each mark is one half-press, stock and patched in adjacent rows.",
                 chart_cand_spread(cd)),
         ]),
        ("limits", "5. Limits and what is still open",
         '<p class="cap">A faster AF is only useful if it does not focus on the wrong thing more often.</p>', [
             sub("hard-scenes", "Where AF struggles",
                 "Without a clear target both settings fail most of the time, and the patch does not "
                 "claim focus on nothing more often.",
                 "A hard target, a blank wall and a scene too dark to focus.",
                 chart_hard(),
                 'On a blank wall a confirmed lock is a false one: stock 0, patched 1 of 20. The hard target '
                 'locked 5 times patched against 2 stock, but those positions could not be scored: the target '
                 'was changed before a reference was taken.'),
             ("open", "Still open",
              '<ul class="cap"><li>Sharpness of real photos at the patched focus positions.</li>'
              '<li>A blind handheld test.</li>'
              '<li>A low-contrast target in 29 fps light.</li>'
              '<li>One camera and one firmware (1.60); other bodies may differ.</li>'
              '<li>The tests patched the camera\'s memory, gone after a restart; the setting is now flashed into the '
              'test camera only. It is a research result, not a released firmware update.</li></ul>'),
         ]),
        ("appendix", "Appendix", "", [
             sub("presses", "Every traced press (early tests)",
                 "With the stock margin, presses in dim light either locked in 0.5–2 s or failed after 2–6 s.",
                 "Each mark is one half-press: light against time to result.",
                 chart_presses(ps),
                 'A press at the top edge ran to the end of the trace.'),
             sub("afc", "Side result: continuous AF",
                 "In AF-C a patch that uses the fast drive makes the sweep about 3.8× faster.",
                 "AF-C sweep time against how far out of focus the lens started.",
                 chart_afc(),
                 'Stock AF-C mostly sweeps at 59 fps. See '
                 '<a href="af-debug-log.html#afc-fast-patch">AF-C fast-drive patch</a>.'),
         ]),
    ]
    args.out.write_text(page(intro, chapters), encoding="utf-8")
    print(f"wrote {args.out.relative_to(ROOT)}")
    if args.site:
        fill_site(args.site, {"effect": chart_effect(cd), "phases": chart_phases(), "hard": chart_hard(),
                              "afc": chart_afc(site=True)})


if __name__ == "__main__":
    main()
