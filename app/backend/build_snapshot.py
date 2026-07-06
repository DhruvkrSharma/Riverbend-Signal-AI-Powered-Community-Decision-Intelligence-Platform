"""
build_snapshot.py — Capture REAL data snapshots for a set of cities and bundle
them (plus pre-computed anomalies / forecasts / recommendations) into a single
JSON the standalone web Artifact embeds.

The Artifact cannot make network calls (Claude Artifact CSP blocks all external
hosts), so we fetch real data here at build time. Every number in the output is
real, captured from the live Open-Meteo feeds — nothing synthetic.

Usage:  python build_snapshot.py ../../web/snapshot.json
"""
import json
import sys

import analytics
import data as datamod

CITIES = ["Delhi", "Mumbai", "Bengaluru", "New York", "London"]


def build():
    out = {"cities": [], "data": {}, "generated_utc": None}
    for name in CITIES:
        print(f"  fetching {name} ...", flush=True)
        snap = datamod.get_city_data(name)
        an = analytics.detect_anomalies(snap)
        recs = analytics.recommend(snap, an)
        # pre-compute a forecast for every metric so the Artifact needn't refit
        forecasts = {}
        for dkey, dom, mkey, m in analytics.iter_metrics(snap):
            fc = analytics.forecast_metric(snap, dkey, mkey, 14)
            if fc:
                forecasts[f"{dkey}.{mkey}"] = {
                    "forecast": fc["forecast"], "trend": fc["trend"],
                    "projected_change": fc["projected_change"],
                    "slope_per_day": fc["slope_per_day"],
                }
        loc = snap["location"]
        key = loc["name"]
        out["cities"].append({
            "key": key, "name": loc["name"], "country": loc.get("country", ""),
            "country_code": loc.get("country_code", ""),
            "lat": loc["latitude"], "lon": loc["longitude"],
        })
        snap["anomalies"] = an
        snap["recommendations"] = recs
        snap["forecasts"] = forecasts
        out["data"][key] = snap
        out["generated_utc"] = snap["generated_utc"]
    return out


if __name__ == "__main__":
    dest = sys.argv[1] if len(sys.argv) > 1 else "snapshot.json"
    print("Building real-data snapshot for:", ", ".join(CITIES))
    bundle = build()
    with open(dest, "w") as f:
        json.dump(bundle, f, separators=(",", ":"))
    import os
    print(f"Wrote {dest}  ({os.path.getsize(dest)/1024:.0f} KB, "
          f"{len(bundle['cities'])} cities)")
