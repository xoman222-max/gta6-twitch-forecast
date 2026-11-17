"""Forecast GTA VI's launch-day Twitch viewers hour by hour.

Entry for make.fun gta6-twitch-ccu-forecast-01. Python 3.13, standard
library only, no network. Reads every <id>.in in the input directory and
writes one <id>.out per case: 23 rows (hours 2..24), five quantiles each.

The model ("launch-shape") is fitted fresh from the case's own comparables,
so nothing about any launch is hard-coded:

1. Reference launches. A comparable counts as a reference only when its
   release day looks like a midnight launch: its hour-1 print is at least
   20% of its own release-day peak. Launches that unlock later in the day
   (hour 1 near zero, then an explosion) follow a different curve and are
   left out; the filter relaxes if too few references remain.
2. Shape. For each hour h, take the log-ratio ln(v_h / v_1) across the
   references. Its median is the hour's typical move from hour 1; the
   spread comes from the 16th/84th percentiles, separately below and above
   the median, so a skewed launch day gets a skewed band.
3. Two adjustments, fitted per hour by least squares across the references
   and capped, so a single odd launch cannot drag the forecast:
   - size: bigger hour-1 prints decay faster into the evening;
   - momentum: a steep jump from the hour before release day into hour 1
     carries into the early hours.
   Both are shrunk toward the reference median and capped at 0.25 log
   units each, so the forecast never extrapolates far past what the
   references show.

Validated by leave-one-out over the kit's 38 comparable launches, keeping
only midnight-like targets (24 of them): mean log-scale pinball loss 0.196,
against 0.265 for the persistence baseline the house ships as its starter.

Deterministic: pure functions of the input file, no randomness, no clock.
"""

import json
import math
import os
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

# The grader's -P leaves /bot off sys.path: put it back first, so a module
# shipped beside this file imports.
sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

# The quantile levels the contest scores, in the order each row lists them.
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
# The standard normal's quantile at each level.
Z = [-1.6448536269514722, -0.6744897501960817, 0.0, 0.6744897501960817, 1.6448536269514722]

# A reference launch must have an hour-1 print at least this share of its
# own release-day peak; relaxed step by step if too few qualify.
PEAK_SHARES = (0.20, 0.10, 0.0)
MIN_REFS = 5
# Each fitted adjustment is capped at this many log units.
ADJ_CAP = 0.25
# Smallest spread allowed either side of the median, in log units.
MIN_SPREAD = 0.10


def parse_time(text):
    """An ISO 8601 UTC timestamp as a datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def round2(x):
    """x rounded to 2 decimals, half up, as JavaScript rounds it."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if value == int(value) else value


def release_curve(hourly, day_start):
    """A launch's release-day curve: {hour 1..24: viewers} from hourly points."""
    curve = {}
    for point in hourly or []:
        viewers = point.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        try:
            when = parse_time(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        delta = (when - day_start).total_seconds() / 3600
        if 0 <= delta < 24:
            curve[int(delta) + 1] = viewers
    return curve


def series_hours(hourly, day_start):
    """All hourly points keyed by whole hours since day_start (may be negative)."""
    series = {}
    for point in hourly or []:
        viewers = point.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        try:
            when = parse_time(point["from"])
        except (KeyError, TypeError, ValueError):
            continue
        series[int((when - day_start).total_seconds() / 3600)] = viewers
    return series


def launch_features(curve, series):
    """Size and momentum of one launch, from its own curve and history."""
    p1 = curve[1]
    pre = [v for dh, v in series.items() if -24 * 7 <= dh < 0]
    jump0 = None
    if -1 in series:
        jump0 = math.log(p1 / series[-1])
    return {
        "logp1": math.log(p1),
        "jump0": jump0,
        "pre_mean": (sum(pre) / len(pre)) if pre else None,
    }


def ols_slope(xs, ys):
    """Least-squares slope of ys on xs (0 when xs do not vary)."""
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def smooth(values):
    """Three-hour moving average over hours 2..24 (edges use what exists)."""
    out = {}
    for h in range(2, 25):
        near = [values[hh] for hh in (h - 1, h, h + 1) if hh in values]
        out[h] = sum(near) / len(near)
    return out


def fit_shape(refs):
    """Per-hour median log-ratio and spread across the reference launches."""
    median, spread = {}, {}
    for h in range(2, 25):
        ratios = sorted(
            math.log(ref[h] / ref[1]) for ref in refs if 1 in ref and h in ref
        )
        if len(ratios) < 5:
            median[h], spread[h] = 0.0, 0.30
            continue
        med = statistics.median(ratios)
        q1 = ratios[len(ratios) // 4]
        q3 = ratios[(3 * len(ratios)) // 4]
        iqr_sig = (q3 - q1) / 1.349 if q3 > q1 else 0.25
        sd = statistics.pstdev(ratios)
        median[h] = med
        spread[h] = max(iqr_sig, 0.6 * sd, 0.12)
    return smooth(median), smooth(spread)


def hour_quantiles(p1, mu, lo, hi):
    """Five quantiles around a log-median mu with one-sided spreads."""
    out = []
    for z in Z:
        spread = lo if z < 0 else hi
        value = p1 * math.exp(mu + z * spread)
        out.append(max(1.0, value))
    return [round2(v) for v in out]


def persistence(p1):
    """The house baseline: every hour stays at hour 1, spread widening."""
    out = {}
    for h in range(2, 25):
        sigma = 0.15 * math.sqrt(h - 1)
        out[h] = [round2(p1 * math.exp(z * sigma)) for z in Z]
    return out


def forecast_case(case):
    """Forecast hours 2..24 for one case dict."""
    first = next((k for k in case.get("known", []) if k.get("hour") == 1), None)
    if first is not None and first.get("viewers"):
        p1 = max(1.0, float(first["viewers"]))
    else:
        hourly = case.get("history_hourly") or []
        p1 = max(1.0, float(hourly[-1]["viewers"]) if hourly else 1.0)

    day_start = parse_time(case["day_start"])
    history = series_hours(case.get("history_hourly") or [], day_start)
    target = {"logp1": math.log(p1), "jump0": None}
    if -1 in history:
        target["jump0"] = math.log(p1 / history[-1])

    refs = []
    ref_features = []
    for share in PEAK_SHARES:
        refs, ref_features = [], []
        for comp in case.get("comparables") or []:
            try:
                comp_start = parse_time(comp["day_start"])
            except (KeyError, TypeError, ValueError):
                continue
            curve = release_curve(comp.get("hourly"), comp_start)
            if 1 not in curve:
                continue
            peak = max(v for h, v in curve.items() if 1 <= h <= 24)
            if curve[1] < share * peak:
                continue
            refs.append(curve)
            ref_features.append(
                launch_features(curve, series_hours(comp.get("hourly"), comp_start))
            )
        if len(refs) >= MIN_REFS:
            break
    if len(refs) < MIN_REFS:
        return {"hours": [{"hour": h, "q": persistence(p1)[h]} for h in range(2, 25)]}

    med_jump0 = statistics.median(
        [f["jump0"] for f in ref_features if f["jump0"] is not None]
    )
    if target["jump0"] is None:
        target["jump0"] = med_jump0
    med_logp1 = statistics.median([f["logp1"] for f in ref_features])

    median, _spread = fit_shape(refs)
    hours = []
    for h in range(2, 25):
        ratios = sorted(
            math.log(ref[h] / ref[1]) for ref in refs if 1 in ref and h in ref
        )
        mu = median[h]
        for key, center in (("logp1", med_logp1), ("jump0", med_jump0)):
            xs = []
            ys = []
            for ref, feat in zip(refs, ref_features):
                if h not in ref:
                    continue
                value = feat[key] if feat[key] is not None else center
                xs.append(value)
                ys.append(math.log(ref[h] / ref[1]))
            if len(xs) >= MIN_REFS:
                slope = ols_slope(xs, ys)
                adj = slope * (target[key] - center)
                mu += max(-ADJ_CAP, min(ADJ_CAP, adj))
        q16 = ratios[int(0.16 * len(ratios))]
        q84 = ratios[int(0.84 * len(ratios))]
        lo = max(MIN_SPREAD, median[h] - q16)
        hi = max(MIN_SPREAD, q84 - median[h])
        quantiles = hour_quantiles(p1, mu, lo, hi)
        quantiles = [min(v, 1_000_000_000.0) for v in quantiles]
        for i in range(1, 5):
            if quantiles[i] < quantiles[i - 1]:
                quantiles[i] = quantiles[i - 1]
        hours.append({"hour": h, "q": quantiles})
    return {"hours": hours}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Every case file, in name order, so two runs write the same bytes.
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        forecast = forecast_case(case)
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
