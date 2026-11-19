#!/usr/bin/env python3
"""Charts of the AF measurements: writes docs/af-charts.html.

Usage:  python3 tools/af_charts.py [--out docs/af-charts.html]
Reads the camera's debug CSVs and TRACE.DAT files under work/af-logs (see
docs/af-debug-log.html) with the parsers in af_csv, af_light and
af_debug_card, and draws every chart as inline SVG (standard library only).
The page carries its own data, so it is committed although work/ is not.
Prints the aggregates it charts, to compare with the tables in the docs.
Offline; reads your own files.
"""
import argparse, hashlib, html, math, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import af_csv, af_light, af_debug_card  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "work" / "af-logs"

# Traced dim AF-S sessions and the margin each ran with (AB.LOG, *.ERR and
# docs/af-debug-log.html: dim-ab2 is power-on 2 = stock, dim-ab1/dim-ab3 are
# power-ons 1 and 3 = SCALE75). f49-ab is kept apart: SCALE75 in both halves.
TRACE_VARIANT = {
    "dim-contrast": "stock", "dim-arrays": "stock", "dim-ab2": "stock",
    "dim-sqrtns": "x0.5", "dim-scale75": "x0.75", "dim-ab1": "x0.75", "dim-ab3": "x0.75",
}
VARIANTS = [("stock", "Stock margin", "s1", "circle"),
            ("x0.5", "SQRTNS (scale^0.5)", "s3", "triangle"),
            ("x0.75", "SCALE75 (scale^0.75)", "s2", "square")]
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
    for name, label in (("TRACE1.DAT", "49 fps (FORCE49)"), ("TRACE2.DAT", "29 fps (stock rate)")):
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
    o = axes(xs, ys, "Area-0 brightness Y during the search (median per press)", "% of presses locked",
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
             "Area-0 brightness Y (log); vertical lines at the 60k / 150k / 400k bucket edges</text>")
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
    c, summary = strip(rows, 0.005, 300, "Scene light: area-0 Y per pixel ÷ (integration µs × gain) × 1,000 (log)",
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
    c, summary = strip(rows, 1, 300, "Peak height in noise widths: (max − min) ÷ noise of the contrast series (log)",
                       label_fmt=lambda v: f"{v:.1f}", row_h=44, left=150, title="Peak-to-noise by outcome")
    print("snr medians:", [(r[0], r[1], r[2]) for r in summary])
    return c + table(["Sweeps", "n", "Median", "Min", "Max"], summary)


def chart_conditions():
    groups = [
        ("Bright AF-S", ["check5", "bright-edge"], "s1"),
        ("Bright AF-S, repetitive", ["repetitive"], "s1"),
        ("Bright AF-C", ["afc", "afc-stock"], "s1"),
        ("Bright AF-C, AFCFAST", ["afc-patched"], "s2"),
        ("Dim AF-S", DIM_AFS_STOCK + ["dim-trace", "dim-contrast", "dim-arrays"], "s1"),
        ("Dim AF-C (eye detect)", ["low-light-eye"], "s1"),
        ("Dim AF-S, SCALE75", ["dim-scale75", "dim-ab1", "dim-ab3"], "s2"),
        ("Dim AF-S, FORCE100", ["force100"], "s2"),
    ]
    rows = []
    for label, sessions, col in groups:
        vals = [(r["ms"], f"{r['session']} {r['file']}: {r['fps']} fps, {r['n']} samples, {r['ms']:.0f} ms, path {r['path']}")
                for s in sessions for r in csv_runs(s)]
        rows.append((label, vals, col, "circle" if col == "s1" else "square"))
    c, summary = strip(rows, 20, 2000, "Sampling time of the logged sweep, ms (samples × frame time; log)",
                       label_fmt=lambda v: f"{v:.0f} ms", title="Sweep sampling time by condition")
    print("sweep ms by condition:", [(r[0], r[1], r[2]) for r in summary])
    leg = legend([("Stock firmware", key_mark("circle", "f-s1")), ("Patched", key_mark("square", "f-s2"))])
    return leg + c + table(["Condition", "Sweeps", "Median", "Min", "Max"], summary)


def chart_afc():
    series = [("Stock AF-C", ["afc", "afc-stock"], "s1", "circle"), ("AFCFAST patch", ["afc-patched"], "s2", "square")]
    W, H = 720, 320
    xs = Scale(0, 2500, 70, W - 20)
    ys = Scale(0, 1400, H - 60, 40)
    o = axes(xs, ys, "Phase-detect defocus before the sweep, |µm|", "Sweep sampling time, ms")
    rows = []
    for name, sessions, col, kind in series:
        rs = [r for s in sessions for r in csv_runs(s) if r["defocus_um"] not in (None, 65535)]
        for r in rs:
            o.append(shape(kind, xs(min(abs(r["defocus_um"]), 2500)), ys(min(r["ms"], 1400)), f"f-{col}",
                           f"{r['session']} {r['file']}: {r['fps']} fps, |defocus| {abs(r['defocus_um'])} µm, {r['ms']:.0f} ms"))
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
    o = axes(xs, ys, "Area-0 brightness Y (log)", "Margin scale +0x80, max per press (log)")
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


# ---------------------------------------------------------------- page

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1f21;--muted:#5d6166;--line:#e2dfd8;--accent:#9a3412;--code-bg:#f1eee8;--th-bg:#f4f1ea;--side:#f6f4ef;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--good:#0ca30c;--warn:#fab219;--crit:#d03b3b;--abort:#898781;--grid:#e8e6e0;--axis:#c3c2b7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#16181b;--fg:#e6e4df;--muted:#a3a7ad;--line:#2e3237;--accent:#f0a875;--code-bg:#202328;--th-bg:#1f2226;--side:#1b1d21;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--grid:#25282c;--axis:#3a3e44}}
:root[data-theme="dark"]{--bg:#16181b;--fg:#e6e4df;--muted:#a3a7ad;--line:#2e3237;--accent:#f0a875;--code-bg:#202328;--th-bg:#1f2226;--side:#1b1d21;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--grid:#25282c;--axis:#3a3e44}
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
.f-s1{fill:var(--s1)}.f-s2{fill:var(--s2)}.f-s3{fill:var(--s3)}
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


def page(sections, ps, light, k_noise, n_noise):
    toc = "".join(f'<li><a href="#{sid}">{esc(title)}</a></li>' for sid, title, _ in sections)
    body = "".join(f'<h2 id="{sid}">{esc(title)}</h2>{content}' for sid, title, content in sections)
    n_csv = sum(1 for _ in LOGS.glob("*/csv/*.csv"))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AF measurement charts</title>
<style>{CSS}</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>{PATTERNS}</defs></svg>
<div class="layout">
<nav id="TOC"><a class="home" href="index.html">← All documents</a><ul>{toc}</ul></nav>
<main>
<h1>AF measurement charts</h1>
<p>Charts of the GR IIIx autofocus measurements in
<a href="af-debug-log.html">AF debug log on the camera</a>. They are drawn from the
raw logs, not copied from the tables there:
{n_csv:,} debug CSV dumps (unique finished sweeps only, as in <code>tools/af_csv.py</code>)
and {len(TRACE_VARIANT) + 2} state traces ({len(ps)} traced presses with brightness data). Hover a mark for its
values. Each chart has its numbers under <em>Data</em>. Regenerate with
<code>python3 tools/af_charts.py</code>.</p>
{body}
</main>
</div>
<div id="tip" role="tooltip"></div>
<script>{JS}</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "af-charts.html")
    args = ap.parse_args()
    ps = presses()
    light = sweeps_light()
    f49 = f49_presses()
    noise_svg, k_noise, n_noise = chart_noise(light)
    curves_svg, bright, dim = chart_curves()
    n_lock = Counter(p["variant"] for p in ps)
    sections = [
        ("lock-rate", "Lock rate by light level and margin",
         f'<p class="cap">Share of half-presses that locked, by the light during the search, for the stock dim-light '
         f'margin and the two margin patches. Stock never locked below Y 60k and locked 57% from 60k to 150k. '
         f'SCALE75 locks almost everything from 60k up, with one false lock in the darkest bucket. SQRTNS locks '
         f'more in the dark, but 3 of its 5 locks there are false (the lens parked at the near stop). '
         f'Presses: stock {n_lock["stock"]}, SQRTNS {n_lock["x0.5"]}, SCALE75 {n_lock["x0.75"]}. See '
         f'<a href="af-debug-log.html#ab23">A/B, power-ons 2 and 3</a>.</p>' + chart_lock_rate(ps)),
        ("presses", "Every traced press",
         '<p class="cap">Each dot is one half-press: the light during the search against the time from half-press '
         'to the result. With the stock margin, failures cluster below about 150k and take 2–6 s, while most locks '
         'take 0.5–2 s. A press at the top edge ran to the end of the trace.</p>' + chart_presses(ps)),
        ("margin-scale", "Margin scale against light",
         '<p class="cap">The scale the camera multiplies its peak margin by (<code>+0x80</code>, the largest value '
         'per press). Stock rises roughly as 1/Y in dim light, to about 30. SCALE75 holds it to about 4–16 over the '
         'same range, and SQRTNS lower still. See <a href="af-debug-log.html#dim-margin">the dim-light margin</a>.</p>'
         + chart_scale(ps)),
        ("frame-rate", "Frame rate the camera picks",
         '<p class="cap">Per stock sweep: the scene light behind each frame rate. That is the logged brightness per '
         'pixel with exposure time and gain divided out, so it no longer depends on the exposure the camera chose. '
         'The bars mark medians. AF-S drops from 200 to 100, 49 and 29 fps as the light falls, and AF-C runs at '
         'the 59 fps live-view rate. The ranges overlap because the camera picks the rate from its own metering '
         'before the sweep. Patched sessions are left out.</p>' + chart_ladder(light)),
        ("noise", "Why contrast AF needs light",
         f'<p class="cap">The lowest contrast value of each sweep, per pixel, against brightness per pixel × gain. '
         f'It grows with a log-log slope of {k_noise:.2f} over {n_noise} stock sweeps, close to the 0.5 of sensor '
         f'shot noise. So the floor under the focus peak is sensor noise, not scene detail. See '
         f'<a href="af-debug-log.html#why-af-needs-light">the analysis</a>.</p>' + noise_svg
         + '<p class="cap">How far the focus peak stood above that noise. Sweeps that found no peak barely cleared '
           'a few noise widths.</p>' + chart_snr(light)),
        ("curves", "What a sweep sees",
         f'<p class="cap">Two logged sweeps, with the contrast (area 0, horizontal) shown as the % above the sweep\'s '
         f'own minimum. Left: {esc(bright["session"])} {esc(bright["file"])}, a clear peak. Right: '
         f'{esc(dim["session"])} {esc(dim["file"])}, a dim sweep the camera could not resolve. Its rise is about '
         f'the size of the frame-to-frame jitter. Only the last pass is logged, so x is the sample number within that pass.</p>' + curves_svg),
        ("conditions", "Sweep time by condition",
         '<p class="cap">Sampling time of each logged sweep: samples × frame time. That is the sweep only, not the '
         'whole AF delay. Bright AF-S takes about 50 ms at 200 fps. Dim AF-S at 29 fps takes 6–8× as long, and '
         'AF-C pays for its 59 fps live-view rate. The bars mark medians.</p>' + chart_conditions()),
        ("afc", "AF-C: stock against AFCFAST",
         '<p class="cap">AF-C sweep time against the defocus the phase detector reported first. Stock AF-C mostly '
         'sweeps at 59 fps. The AFCFAST patch gives it the 200 fps drive, about 3.8× faster at the same defocus. '
         'See <a href="af-debug-log.html#afc-fast-patch">AF-C fast-drive patch</a>.</p>' + chart_afc()),
        ("force", "Forcing a faster frame rate in dim light",
         '<p class="cap">FORCE100 makes dim AF-S sample at 100 fps, with a third of the light per frame. Far more '
         'sweeps find no peak or run the whole lens travel. The stock sessions are '
         + ", ".join(f"<code>{s}</code>" for s in DIM_AFS_STOCK) + '. See '
         '<a href="af-debug-log.html#force100-test">the force-100 test</a>.</p>' + chart_force100()
         + '<p class="cap">FORCE49 (49 instead of 29 fps, SCALE75 on in both power-ons): outcome of each half-press. '
           'See <a href="af-debug-log.html#force49-ab">FORCE49 A/B</a>.</p>' + chart_f49(f49)),
    ]
    args.out.write_text(page(sections, ps, light, k_noise, n_noise), encoding="utf-8")
    print(f"wrote {args.out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
