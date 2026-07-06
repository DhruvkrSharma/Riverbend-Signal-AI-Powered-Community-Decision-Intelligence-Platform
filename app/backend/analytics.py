"""
analytics.py — The decision-intelligence engine (pure Python, no ML deps).

Operates on the real data snapshots produced by data.py:
  * detect_anomalies  — trailing-window z-score, direction & severity aware
  * forecast_metric   — least-squares trend + weekday seasonality + conf. band
  * recommend         — rule engine: anomalies + current state -> stakeholder actions
  * answer_question   — heuristic NL parser used as the /ask fallback (no LLM needed)

The same maths is re-implemented in JavaScript inside the standalone web Artifact
so the two deliverables agree.
"""
from __future__ import annotations

import math
import re
from typing import Optional

WINDOW = 28          # trailing baseline window for anomaly z-scores
Z_WARN, Z_SERIOUS, Z_CRIT = 2.5, 3.2, 4.2


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _observed(snap, values):
    """Values up to and including 'today' (drop the forecast tail)."""
    return values[: snap["forecast_start"]]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs, mu=None):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    mu = _mean(xs) if mu is None else mu
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))


def iter_metrics(snap):
    for dkey, dom in snap["domains"].items():
        for mkey, m in dom["metrics"].items():
            yield dkey, dom, mkey, m


def find_metric(snap, domain, metric):
    return snap["domains"][domain]["metrics"][metric]


# --------------------------------------------------------------------------- #
# anomaly detection
# --------------------------------------------------------------------------- #
def _severity(z):
    az = abs(z)
    if az >= Z_CRIT:
        return "critical"
    if az >= Z_SERIOUS:
        return "serious"
    if az >= Z_WARN:
        return "warning"
    return None


def detect_anomalies(snap, min_z=Z_WARN):
    """Flag points that deviate from their trailing-window baseline."""
    out = []
    dates = snap["dates"]
    for dkey, dom, mkey, m in iter_metrics(snap):
        vals = _observed(snap, m["values"])
        for i in range(WINDOW, len(vals)):
            v = vals[i]
            if v is None:
                continue
            base = vals[max(0, i - WINDOW): i]
            mu, sd = _mean(base), _std(base)
            if mu is None or sd == 0:
                continue
            z = (v - mu) / sd
            sev = _severity(z)
            if sev is None or abs(z) < min_z:
                continue
            out.append({
                "domain": dkey, "domain_label": dom["label"],
                "metric": mkey, "metric_label": m["label"], "unit": m["unit"],
                "date": dates[i], "index": i, "value": round(v, 2),
                "baseline": round(mu, 2), "z": round(z, 2),
                "direction": "spike" if z > 0 else "drop",
                "severity": sev, "good_direction": m["good_direction"],
            })
    # newest & most extreme first
    out.sort(key=lambda a: (a["date"], abs(a["z"])), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# forecasting: linear trend + weekday seasonality + confidence band
# --------------------------------------------------------------------------- #
def _theil_sen_slope(xs, ys):
    """Median pairwise slope — robust to the extreme outliers real data throws."""
    slopes = []
    n = len(xs)
    for i in range(n):
        for j in range(i + 1, n):
            dx = xs[j] - xs[i]
            if dx:
                slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return 0.0
    slopes.sort()
    mid = len(slopes) // 2
    return slopes[mid] if len(slopes) % 2 else (slopes[mid - 1] + slopes[mid]) / 2


def forecast_metric(snap, domain, metric, horizon=14):
    """Robust forecast: recent-level anchor + Theil-Sen trend, damped, with a
    residual-based confidence band. Resists the outliers real feeds contain."""
    from datetime import date, timedelta
    POSITIVE_UNITS = {"µg/m³", "mm", "AQI", "index", "%", "MJ/m²", "hours",
                      "m³/s", "°C·day"}
    DAMP = 0.90          # damped-trend factor so long horizons don't explode

    m = find_metric(snap, domain, metric)
    dates = snap["dates"]
    vals = _observed(snap, m["values"])
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 8:
        return None

    # fit on a recent window so old regime shifts don't dominate
    win = pts[-45:] if len(pts) > 45 else pts
    xs = [p[0] for p in win]
    ys = [p[1] for p in win]
    last_i = pts[-1][0]
    last_date = date.fromisoformat(dates[last_i])

    slope = _theil_sen_slope(xs, ys)
    level = _mean([v for _, v in pts[-7:]])           # anchor at recent level
    med = sorted(ys)[len(ys) // 2]

    # weekday seasonality from residuals around a simple recent line
    def wd(i):
        return date.fromisoformat(dates[i]).weekday()
    resid_by_wd = {}
    for x, y in zip(xs, ys):
        base = level + slope * (x - last_i)
        resid_by_wd.setdefault(wd(x), []).append(y - base)
    seasonal = {k: sum(v) / len(v) for k, v in resid_by_wd.items()}
    resids = [y - (level + slope * (x - last_i) + seasonal.get(wd(x), 0.0))
              for x, y in zip(xs, ys)]
    # robust dispersion (MAD) so a single extreme day doesn't blow up the band
    absr = sorted(abs(r) for r in resids)
    mad = absr[len(absr) // 2] if absr else 0.0
    resid_std = 1.4826 * mad or (_std(resids) or 0.0)

    hist = [{"date": dates[i], "value": round(v, 2)}
            for i, v in enumerate(vals) if v is not None]
    fc = []
    trend_accum = 0.0
    for h in range(1, horizon + 1):
        trend_accum += DAMP ** (h - 1)                # damped cumulative trend
        dd = last_date + timedelta(days=h)
        mu = level + slope * trend_accum + seasonal.get(dd.weekday(), 0.0)
        band = resid_std * (0.8 + 0.12 * h)           # ~1σ near-term, grows gently
        lo, hi = mu - band, mu + band
        if m["unit"] in POSITIVE_UNITS:
            lo, mu = max(lo, 0.0), max(mu, 0.0)
        fc.append({"date": dd.isoformat(), "value": round(mu, 2),
                   "lower": round(lo, 2), "upper": round(hi, 2)})

    # trend label with a deadband relative to the metric's own scale
    scale = abs(med) or 1.0
    if slope * horizon > 0.05 * scale:
        direction = "rising"
    elif slope * horizon < -0.05 * scale:
        direction = "falling"
    else:
        direction = "flat"
    change = fc[-1]["value"] - round(level, 2)
    return {
        "domain": domain, "metric": metric, "label": m["label"], "unit": m["unit"],
        "good_direction": m["good_direction"], "history": hist, "forecast": fc,
        "slope_per_day": round(slope, 4), "trend": direction,
        "projected_change": round(change, 2), "horizon": horizon,
    }


# --------------------------------------------------------------------------- #
# recommendations: rule engine
# --------------------------------------------------------------------------- #
US_AQI_BANDS = [
    (0, 50, "Good"), (51, 100, "Moderate"), (101, 150, "Unhealthy (sensitive)"),
    (151, 200, "Unhealthy"), (201, 300, "Very Unhealthy"), (301, 9999, "Hazardous"),
]


def _latest(snap, domain, metric):
    vals = _observed(snap, find_metric(snap, domain, metric)["values"])
    for v in reversed(vals):
        if v is not None:
            return v
    return None


def _band(v, bands):
    for lo, hi, name in bands:
        if lo <= v <= hi:
            return name
    return "—"


def recommend(snap, anomalies=None):
    anomalies = anomalies or detect_anomalies(snap)
    recent = {(a["domain"], a["metric"]): a
              for a in anomalies if a["index"] >= snap["forecast_start"] - 7}
    recs = []

    def add(priority, domain, title, action, rationale, stakeholders, evidence):
        recs.append({
            "priority": priority, "domain": domain, "title": title,
            "action": action, "rationale": rationale,
            "stakeholders": stakeholders, "evidence": evidence,
        })

    # --- Air quality ---
    aqi = _latest(snap, "air", "us_aqi")
    if aqi is not None:
        band = _band(aqi, US_AQI_BANDS)
        if aqi > 150:
            add("critical", "air", f"Issue public air-quality advisory ({band})",
                "Activate health advisory: recommend masks (N95) outdoors, move outdoor "
                "school/sports indoors, and open clean-air shelters.",
                f"US AQI is {aqi:.0f} — '{band}'. Prolonged exposure risks respiratory harm.",
                ["Public Health", "Schools", "Emergency Mgmt"],
                f"air / us_aqi = {aqi:.0f}")
        elif aqi > 100:
            add("serious", "air", f"Alert sensitive groups ({band})",
                "Notify sensitive groups (children, elderly, respiratory patients) to "
                "limit prolonged outdoor exertion.",
                f"US AQI is {aqi:.0f} — '{band}'.",
                ["Public Health", "Community Programs"],
                f"air / us_aqi = {aqi:.0f}")
    if ("air", "pm2_5") in recent and recent[("air", "pm2_5")]["direction"] == "spike":
        a = recent[("air", "pm2_5")]
        add("serious", "air", "Investigate PM2.5 spike source",
            "Cross-check against traffic, industrial activity and regional wildfire/dust; "
            "consider temporary construction or burning restrictions.",
            f"PM2.5 spiked to {a['value']} {a['unit']} on {a['date']} "
            f"({a['z']:+.1f}σ vs baseline {a['baseline']}).",
            ["Environment", "Enforcement"], f"air / pm2_5 anomaly {a['date']}")

    # --- Climate / heat ---
    feels = _latest(snap, "wellness", "feels_like")
    if feels is not None and feels >= 40:
        add("critical", "climate", "Activate heat action plan",
            "Open cooling centres, extend public-water access, adjust outdoor-worker "
            "hours, and run heat-risk messaging for vulnerable residents.",
            f"Feels-like temperature is {feels:.0f}°C — extreme-heat territory.",
            ["Emergency Mgmt", "Public Health", "Labour/Works"],
            f"wellness / feels_like = {feels:.0f}°C")
    elif feels is not None and feels >= 35:
        add("serious", "climate", "Pre-position for heat stress",
            "Stage water points and issue a heat-caution advisory ahead of peak hours.",
            f"Feels-like temperature is {feels:.0f}°C.",
            ["Public Health", "Community Programs"], f"wellness / feels_like = {feels:.0f}°C")

    # --- Energy / solar ---
    cdd = _latest(snap, "energy", "cooling_demand")
    solar = _latest(snap, "energy", "solar_energy")
    if cdd is not None and cdd >= 6:
        extra = ""
        if solar is not None and solar >= 18:
            extra = (" Solar potential is high today — schedule flexible/industrial load "
                     "into midday to soak up rooftop-solar output.")
        add("serious", "energy", "Manage peak cooling demand",
            "Pre-cool public buildings in the morning, trim non-essential afternoon load, "
            "and stand up demand-response." + extra,
            f"Cooling degree-days at {cdd:.0f} °C·day signals heavy AC demand and grid stress.",
            ["Utilities", "Facilities"], f"energy / cooling_demand = {cdd:.0f}")
    elif solar is not None and solar >= 20:
        add("info", "energy", "Capitalise on high solar output",
            "Shift shiftable municipal load (pumping, EV charging) into the midday solar peak.",
            f"Solar energy potential is {solar:.0f} MJ/m² — strong generation window.",
            ["Utilities", "Sustainability"], f"energy / solar_energy = {solar:.0f}")

    # --- Water / flood ---
    rq = recent.get(("water", "river_discharge"))
    precip = _latest(snap, "water", "precip_water")
    if rq and rq["direction"] == "spike":
        add("critical", "water", "Raise flood watch on river reach",
            "Alert low-lying wards, pre-position pumps and rescue teams, and monitor "
            "upstream discharge hourly.",
            f"River discharge spiked to {rq['value']} {rq['unit']} on {rq['date']} "
            f"({rq['z']:+.1f}σ).",
            ["Emergency Mgmt", "Water/Drainage"], f"water / river_discharge anomaly {rq['date']}")
    elif precip is not None and precip >= 40:
        add("serious", "water", "Prepare drainage for heavy rain",
            "Clear priority storm drains and stage crews; warn flood-prone neighbourhoods.",
            f"Rainfall of {precip:.0f} mm can overwhelm drainage.",
            ["Water/Drainage", "Emergency Mgmt"], f"water / precip = {precip:.0f} mm")

    # --- Wellness / UV ---
    uv = _latest(snap, "wellness", "uv_index")
    if uv is not None and uv >= 8:
        add("serious", "wellness", "Issue UV / sun-safety advisory",
            "Advise midday shade, sunscreen and hydration; add shade at outdoor public venues.",
            f"UV index reaches {uv:.0f} — very high exposure risk.",
            ["Public Health", "Parks & Recreation"], f"wellness / uv_index = {uv:.0f}")

    order = {"critical": 0, "serious": 1, "info": 2}
    recs.sort(key=lambda r: order.get(r["priority"], 3))
    if not recs:
        add("info", "air", "No urgent interventions — steady state",
            "Conditions are within normal ranges across all monitored domains. "
            "Maintain routine monitoring.",
            "No metric currently breaches an action threshold.",
            ["Operations"], "all domains nominal")
    return recs


# --------------------------------------------------------------------------- #
# heuristic natural-language Q&A (fallback when no Gemini key is configured)
# --------------------------------------------------------------------------- #
_METRIC_KEYWORDS = {
    "us_aqi": ["aqi", "air quality index", "air quality", "air"],
    "pm2_5": ["pm2.5", "pm2_5", "pm25", "fine particulate", "particulate"],
    "pm10": ["pm10", "coarse particulate"],
    "nitrogen_dioxide": ["no2", "nitrogen"],
    "ozone": ["ozone", "o3"],
    "temp_mean": ["mean temperature", "average temperature", "temperature", "temp"],
    "temp_max": ["max temperature", "hottest", "high temperature"],
    "precipitation": ["precipitation", "rain", "rainfall"],
    "wind_max": ["wind"],
    "solar_energy": ["solar", "solar energy", "radiation", "renewable"],
    "sunshine": ["sunshine", "sunny"],
    "cooling_demand": ["cooling", "ac demand", "cooling demand"],
    "heating_demand": ["heating", "heating demand"],
    "river_discharge": ["river", "discharge", "flood"],
    "uv_index": ["uv", "ultraviolet", "uv index"],
    "daylight": ["daylight", "day length"],
    "feels_like": ["feels like", "feels-like", "heat index", "apparent"],
    "humidity": ["humidity", "humid"],
}
def _metric_domain_index(snap):
    idx = {}
    for dkey, dom, mkey, m in iter_metrics(snap):
        idx[mkey] = dkey
    return idx


def answer_question(snap, question: str) -> dict:
    q = question.lower().strip()
    dom_idx = _metric_domain_index(snap)

    # pick metric by keyword hit (longest keyword wins for specificity)
    best, best_len = None, 0
    for mkey, kws in _METRIC_KEYWORDS.items():
        if mkey not in dom_idx:
            continue
        for kw in kws:
            if kw in q and len(kw) > best_len:
                best, best_len = mkey, len(kw)
    if best is None:
        return {
            "answer": ("I can answer questions about air quality, temperature, rainfall, "
                       "solar energy, river/flood levels, UV and more for "
                       f"{snap['location']['name']}. Try: \"worst air-quality day\", "
                       "\"forecast temperature\", or \"any anomalies this week?\"."),
            "chart": None, "engine": "heuristic",
        }

    domain = dom_idx[best]
    m = find_metric(snap, domain, best)
    vals = _observed(snap, m["values"])
    dates = snap["dates"]
    pairs = [(dates[i], v) for i, v in enumerate(vals) if v is not None]
    label, unit = m["label"], m["unit"]

    def chart(highlight=None, kind="line"):
        return {"domain": domain, "metric": best, "highlight": highlight, "kind": kind}

    # operation intent
    if any(w in q for w in ["forecast", "predict", "next week", "will ", "expect", "outlook"]):
        fc = forecast_metric(snap, domain, best, horizon=14)
        if fc:
            nxt = fc["forecast"][-1]
            ans = (f"Projected {label} in {snap['location']['name']} is about "
                   f"{nxt['value']} {unit} in ~2 weeks (14-day trend is {fc['trend']}, "
                   f"{fc['projected_change']:+.1f} {unit} change). "
                   f"Range: {nxt['lower']}–{nxt['upper']} {unit}.")
            return {"answer": ans, "chart": {**chart(kind="forecast")}, "engine": "heuristic",
                    "forecast": fc}
    if any(w in q for w in ["anomaly", "anomalies", "unusual", "spike", "abnormal", "strange"]):
        anos = [a for a in detect_anomalies(snap) if a["metric"] == best]
        if anos:
            a = anos[0]
            ans = (f"Yes — {label} showed a {a['severity']} {a['direction']} on {a['date']}: "
                   f"{a['value']} {unit} ({a['z']:+.1f}σ vs a baseline of {a['baseline']}).")
            return {"answer": ans, "chart": chart(highlight=a["date"]), "engine": "heuristic"}
        return {"answer": f"No significant anomalies detected in {label} recently.",
                "chart": chart(), "engine": "heuristic"}
    if any(w in q for w in ["worst", "highest", "max", "peak", "most"]):
        d, v = max(pairs, key=lambda p: p[1])
        ans = f"The highest {label} was {v} {unit} on {d}."
        return {"answer": ans, "chart": chart(highlight=d), "engine": "heuristic"}
    if any(w in q for w in ["best", "lowest", "min", "cleanest", "least"]):
        d, v = min(pairs, key=lambda p: p[1])
        ans = f"The lowest {label} was {v} {unit} on {d}."
        return {"answer": ans, "chart": chart(highlight=d), "engine": "heuristic"}
    if any(w in q for w in ["average", "mean", "typical", "usual"]):
        avg = _mean([v for _, v in pairs])
        ans = f"The average {label} over the last {len(pairs)} days is {avg:.1f} {unit}."
        return {"answer": ans, "chart": chart(), "engine": "heuristic"}
    if any(w in q for w in ["trend", "rising", "falling", "increasing", "getting", "changed"]):
        fc = forecast_metric(snap, domain, best, horizon=14)
        tr = fc["trend"] if fc else "flat"
        ans = f"{label} is trending {tr} over the observed period in {snap['location']['name']}."
        return {"answer": ans, "chart": chart(), "engine": "heuristic"}

    # default: current status
    cur_d, cur_v = pairs[-1]
    avg = _mean([v for _, v in pairs])
    delta = cur_v - avg
    ans = (f"Latest {label} in {snap['location']['name']} is {cur_v} {unit} "
           f"(as of {cur_d}), {'above' if delta >= 0 else 'below'} the "
           f"{len(pairs)}-day average of {avg:.1f} {unit}.")
    return {"answer": ans, "chart": chart(), "engine": "heuristic"}
