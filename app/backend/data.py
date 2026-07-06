"""
data.py — Real, location-dynamic data ingestion for the Riverbend Signal platform.

All numbers here come from live public APIs (Open-Meteo family). Nothing is
synthetic. Given a city name (or lat/lon) anywhere on Earth, this module:

  1. Geocodes the place            -> geocoding-api.open-meteo.com
  2. Pulls ~90 days of daily data across 5 community-intelligence domains:
       - Air Quality        -> air-quality-api.open-meteo.com
       - Climate & Weather  -> api.open-meteo.com/v1/forecast
       - Energy & Solar     -> api.open-meteo.com/v1/forecast (+ derived degree-days)
       - Water & Flood      -> flood-api.open-meteo.com
       - UV & Wellness      -> api.open-meteo.com/v1/forecast
  3. Normalises everything onto one daily date axis, marking which days are the
     official model *forecast* (future) vs. observed history.

The exact same structure is embedded in the standalone web Artifact (captured at
build time) and served live by the FastAPI backend, so both tell the same story.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from typing import Optional

import requests

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
WX_URL = "https://api.open-meteo.com/v1/forecast"
FLOOD_URL = "https://flood-api.open-meteo.com/v1/flood"

PAST_DAYS = 92
FORECAST_DAYS = 7
TIMEOUT = 40

# --- domain / metric catalogue -------------------------------------------------
# good_direction: which way is "better" for the community ("up", "down", or None
# when the metric is contextual rather than good/bad).
METRIC_META = {
    # Air Quality
    "us_aqi":       ("Air Quality Index (US)", "AQI",   "down", "air",     "Open-Meteo Air Quality"),
    "pm2_5":        ("PM2.5",                  "µg/m³", "down", "air",     "Open-Meteo Air Quality"),
    "pm10":         ("PM10",                   "µg/m³", "down", "air",     "Open-Meteo Air Quality"),
    "nitrogen_dioxide": ("Nitrogen Dioxide",   "µg/m³", "down", "air",     "Open-Meteo Air Quality"),
    "ozone":        ("Ozone",                  "µg/m³", "down", "air",     "Open-Meteo Air Quality"),
    # Climate & Weather
    "temp_mean":    ("Mean Temperature",       "°C",    None,   "climate", "Open-Meteo Forecast"),
    "temp_max":     ("Max Temperature",        "°C",    None,   "climate", "Open-Meteo Forecast"),
    "precipitation":("Precipitation",          "mm",    None,   "climate", "Open-Meteo Forecast"),
    "wind_max":     ("Max Wind Speed",         "km/h",  None,   "climate", "Open-Meteo Forecast"),
    # Energy & Solar
    "solar_energy": ("Solar Energy Potential", "MJ/m²", "up",   "energy",  "Open-Meteo Forecast"),
    "sunshine":     ("Sunshine Duration",      "hours", "up",   "energy",  "Open-Meteo Forecast"),
    "cooling_demand":("Cooling Demand (CDD)",  "°C·day","down", "energy",  "Derived from temperature"),
    "heating_demand":("Heating Demand (HDD)",  "°C·day","down", "energy",  "Derived from temperature"),
    # Water & Flood
    "river_discharge":("River Discharge",      "m³/s",  None,   "water",   "Open-Meteo Flood (GloFAS)"),
    "precip_water": ("Rainfall",               "mm",    None,   "water",   "Open-Meteo Forecast"),
    # UV & Wellness
    "uv_index":     ("UV Index (max)",         "index", "down", "wellness","Open-Meteo Forecast"),
    "daylight":     ("Daylight",               "hours", "up",   "wellness","Open-Meteo Forecast"),
    "feels_like":   ("Feels-Like Max",         "°C",    None,   "wellness","Open-Meteo Forecast"),
    "humidity":     ("Relative Humidity",      "%",     None,   "wellness","Open-Meteo Forecast"),
}

DOMAIN_META = {
    "air":     ("Air Quality",        "Public health & clean air",       "◉"),
    "climate": ("Climate & Weather",  "Climate resilience",              "☁"),
    "energy":  ("Energy & Solar",     "Energy efficiency & renewables",  "⚡"),
    "water":   ("Water & Flood",      "Disaster & water resilience",     "≈"),
    "wellness":("UV & Wellness",      "Community wellness & safety",      "✦"),
}


def _get(url: str, params: dict) -> dict:
    last = None
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # transient proxy/network hiccup -> backoff
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def geocode(name: str) -> Optional[dict]:
    """Resolve a place name to a location record (anywhere on Earth)."""
    data = _get(GEO_URL, {"name": name, "count": 1, "language": "en", "format": "json"})
    res = data.get("results")
    if not res:
        return None
    r = res[0]
    return {
        "name": r["name"],
        "admin1": r.get("admin1", ""),
        "country": r.get("country", ""),
        "country_code": r.get("country_code", ""),
        "latitude": r["latitude"],
        "longitude": r["longitude"],
        "timezone": r.get("timezone", "auto"),
        "population": r.get("population"),
    }


def _daily_from_hourly(times: list[str], values: list, agg: str) -> dict[str, float]:
    """Aggregate an hourly series into {date: value} using mean or max."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for t, v in zip(times, values):
        if v is None:
            continue
        buckets[t[:10]].append(v)
    out = {}
    for day, vals in buckets.items():
        if not vals:
            continue
        out[day] = max(vals) if agg == "max" else sum(vals) / len(vals)
    return out


def _round(v, nd=1):
    return None if v is None else round(v, nd)


def fetch_city(location: dict) -> dict:
    """Fetch all five domains of real daily data for a resolved location."""
    lat, lon, tz = location["latitude"], location["longitude"], location["timezone"]

    # 1) Weather / solar / UV / wellness (daily) + hourly humidity.
    wx = _get(WX_URL, {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS,
        "daily": ",".join([
            "temperature_2m_mean", "temperature_2m_max", "temperature_2m_min",
            "apparent_temperature_max", "precipitation_sum", "wind_speed_10m_max",
            "shortwave_radiation_sum", "sunshine_duration", "uv_index_max",
            "daylight_duration",
        ]),
        "hourly": "relative_humidity_2m",
    })
    dates = wx["daily"]["time"]
    d = wx["daily"]
    humidity_daily = _daily_from_hourly(
        wx["hourly"]["time"], wx["hourly"]["relative_humidity_2m"], "mean")

    # 2) Air quality (hourly -> daily).
    aq = _get(AQ_URL, {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "past_days": PAST_DAYS, "forecast_days": 5,
        "hourly": ",".join([
            "pm2_5", "pm10", "nitrogen_dioxide", "ozone", "us_aqi",
        ]),
    })
    at = aq["hourly"]["time"]
    aq_daily = {
        "us_aqi": _daily_from_hourly(at, aq["hourly"]["us_aqi"], "max"),
        "pm2_5": _daily_from_hourly(at, aq["hourly"]["pm2_5"], "mean"),
        "pm10": _daily_from_hourly(at, aq["hourly"]["pm10"], "mean"),
        "nitrogen_dioxide": _daily_from_hourly(at, aq["hourly"]["nitrogen_dioxide"], "mean"),
        "ozone": _daily_from_hourly(at, aq["hourly"]["ozone"], "mean"),
    }

    # 3) Flood / river discharge (daily).
    try:
        fl = _get(FLOOD_URL, {
            "latitude": lat, "longitude": lon,
            "past_days": PAST_DAYS, "forecast_days": FORECAST_DAYS,
            "daily": "river_discharge",
        })
        flood_dates = fl["daily"]["time"]
        flood_map = {dd: v for dd, v in zip(flood_dates, fl["daily"]["river_discharge"])}
    except Exception:
        flood_map = {}

    def series(key_lookup):
        return [_round(key_lookup(dd)) for dd in dates]

    # Assemble each metric onto the unified `dates` axis.
    raw = {}
    # air
    for k, agg_map in aq_daily.items():
        raw[k] = [_round(agg_map.get(dd)) for dd in dates]
    # climate / wellness (index-aligned daily arrays from wx)
    idx = {dd: i for i, dd in enumerate(dates)}
    def wxd(name):
        arr = d.get(name) or []
        return [arr[idx[dd]] if idx[dd] < len(arr) else None for dd in dates]
    temp_mean = wxd("temperature_2m_mean")
    raw["temp_mean"] = [_round(v) for v in temp_mean]
    raw["temp_max"] = [_round(v) for v in wxd("temperature_2m_max")]
    raw["precipitation"] = [_round(v) for v in wxd("precipitation_sum")]
    raw["wind_max"] = [_round(v) for v in wxd("wind_speed_10m_max")]
    # energy
    raw["solar_energy"] = [_round(v) for v in wxd("shortwave_radiation_sum")]
    raw["sunshine"] = [_round(v / 3600.0, 2) if v is not None else None
                       for v in wxd("sunshine_duration")]
    raw["cooling_demand"] = [_round(max(v - 18.0, 0.0)) if v is not None else None
                             for v in temp_mean]
    raw["heating_demand"] = [_round(max(18.0 - v, 0.0)) if v is not None else None
                             for v in temp_mean]
    # water
    raw["river_discharge"] = [_round(flood_map.get(dd), 2) for dd in dates]
    raw["precip_water"] = raw["precipitation"]
    # wellness
    raw["uv_index"] = [_round(v) for v in wxd("uv_index_max")]
    raw["daylight"] = [_round(v / 3600.0, 2) if v is not None else None
                       for v in wxd("daylight_duration")]
    raw["feels_like"] = [_round(v) for v in wxd("apparent_temperature_max")]
    raw["humidity"] = [_round(humidity_daily.get(dd)) for dd in dates]

    # Forecast boundary: first date strictly after "today" in the location tz.
    today = dt.datetime.now().astimezone().date().isoformat()
    # Use the location's own clock via timezone offset returned by the API.
    tz_today = _local_today(wx.get("utc_offset_seconds", 0))
    forecast_start = next((i for i, dd in enumerate(dates) if dd > tz_today), len(dates))

    # Package metrics grouped by domain.
    domains = {}
    for dom_key, (dlabel, dsub, dicon) in DOMAIN_META.items():
        metrics = {}
        for mkey, (label, unit, good, dk, source) in METRIC_META.items():
            if dk != dom_key:
                continue
            metrics[mkey] = {
                "label": label, "unit": unit, "good_direction": good,
                "source": source, "values": raw.get(mkey, []),
            }
        domains[dom_key] = {"label": dlabel, "subtitle": dsub, "icon": dicon,
                            "metrics": metrics}

    return {
        "location": location,
        "as_of": tz_today,
        "generated_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "dates": dates,
        "forecast_start": forecast_start,
        "domains": domains,
        "attribution": "Live data: Open-Meteo (air quality, weather, solar, flood / GloFAS). CC-BY 4.0.",
    }


def _local_today(utc_offset_seconds: int) -> str:
    now = dt.datetime.utcnow() + dt.timedelta(seconds=utc_offset_seconds or 0)
    return now.date().isoformat()


def get_city_data(name: str = None, lat: float = None, lon: float = None,
                  label: str = None) -> dict:
    """Public entrypoint: by city name, or by explicit lat/lon.

    Inputs are validated defensively (this module is reused by the snapshot
    builder as well as the API). Outbound requests only ever target the fixed
    Open-Meteo hosts above — no user-controlled URLs, so no SSRF surface."""
    if lat is not None and lon is not None:
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise ValueError("lat/lon out of range")
        location = {
            "name": (label or f"{lat:.2f}, {lon:.2f}")[:60], "admin1": "", "country": "",
            "country_code": "", "latitude": lat, "longitude": lon,
            "timezone": "auto", "population": None,
        }
    else:
        if not name or len(str(name)) > 60:
            raise ValueError("invalid city name")
        name = str(name).strip()
        location = geocode(name)
        if not location:
            raise ValueError(f"Could not geocode location: {name!r}")
    return fetch_city(location)


if __name__ == "__main__":
    import json, sys
    city = sys.argv[1] if len(sys.argv) > 1 else "Delhi"
    snap = get_city_data(city)
    print(f"{snap['location']['name']}, {snap['location']['country']} "
          f"({len(snap['dates'])} days, forecast starts @ {snap['forecast_start']})")
    print("domains:", ", ".join(snap["domains"].keys()))
    print(json.dumps(snap["domains"]["air"]["metrics"]["us_aqi"]["values"][-8:]))
