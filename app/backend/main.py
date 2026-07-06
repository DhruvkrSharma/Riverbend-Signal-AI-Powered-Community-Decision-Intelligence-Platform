"""
main.py — FastAPI backend for the Riverbend Signal decision-intelligence platform.

Security posture (see README "Security"): locked-down CORS, security-headers
middleware (CSP/HSTS/etc.), per-IP rate limiting, strict input validation, request
size limits, and generic error responses that never leak internals or the API key.

Endpoints (all location-dynamic: pass ?city= or ?lat=&lon=):
  GET  /api/health
  GET  /api/data | /api/bundle | /api/anomalies | /api/forecast | /api/recommendations
  POST /api/ask   {question}  -> grounded NL answer (Gemini if key set, else heuristic)

Static frontend is served from ../frontend at /.
Run:  uvicorn main:app --port 8000   (from the backend/ directory)
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import analytics
import data as datamod
import gemini

log = logging.getLogger("riverbend")
app = FastAPI(title="Riverbend Signal API", version="1.1", docs_url=None, redoc_url=None)

# --- config ------------------------------------------------------------------
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
MAX_BODY_BYTES = 16 * 1024          # 16 KB request cap
RATE_CAPACITY = int(os.environ.get("RATE_LIMIT", "60"))   # tokens per bucket
RATE_REFILL = 1.0                    # tokens/sec
CITY_RE = re.compile(r"^[\w\s.,'’\-()]{1,60}$", re.UNICODE)

# --- input validation --------------------------------------------------------
def validate_location(city, lat, lon):
    if lat is not None or lon is not None:
        try:
            lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            raise HTTPException(422, "lat and lon must both be numbers")
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise HTTPException(422, "lat/lon out of range")
        return None, lat, lon
    if city is None or not CITY_RE.match(city):
        raise HTTPException(422, "invalid city name")
    return city, None, None


# --- middleware: security headers -------------------------------------------
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'; object-src 'none'")

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # request-size guard (POST bodies)
    clen = request.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > MAX_BODY_BYTES:
        return PlainTextResponse("payload too large", status_code=413)
    # per-IP token-bucket rate limit on the API
    if request.url.path.startswith("/api/"):
        if not _rate_ok(request.client.host if request.client else "?"):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                                headers={"Retry-After": "5"})
    try:
        resp = await call_next(request)
    except HTTPException:
        raise
    except Exception:                # never leak stack traces / internals
        log.exception("unhandled error")
        return JSONResponse({"detail": "internal error"}, status_code=500)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(self), microphone=(), camera=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if request.url.scheme == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp


_BUCKETS: dict[str, list] = {}
def _rate_ok(ip: str) -> bool:
    now = time.time()
    tok, last = _BUCKETS.get(ip, (RATE_CAPACITY, now))
    tok = min(RATE_CAPACITY, tok + (now - last) * RATE_REFILL)
    if tok < 1:
        _BUCKETS[ip] = (tok, now)
        return False
    _BUCKETS[ip] = (tok - 1, now)
    return True


app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
    allow_methods=["GET", "POST"], allow_headers=["Content-Type"], max_age=600)

# --- data cache --------------------------------------------------------------
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 1800

def load_snapshot(city, lat, lon, label=None):
    city, lat, lon = validate_location(city, lat, lon)
    if label is not None and not CITY_RE.match(label):
        raise HTTPException(422, "invalid label")
    k = f"{city}|{lat}|{lon}|{label}".lower()
    hit = _CACHE.get(k)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        snap = datamod.get_city_data(name=city, lat=lat, lon=lon, label=label)
    except ValueError:
        raise HTTPException(404, "location not found")
    except HTTPException:
        raise
    except Exception:
        log.exception("data source error")
        raise HTTPException(502, "data source unavailable")
    _CACHE[k] = (time.time(), snap)
    return snap


# --- endpoints ---------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "gemini": gemini.available(),
            "model": gemini.MODEL if gemini.available() else None}


@app.get("/api/data")
def get_data(city: str = Query("Delhi"), lat: float = Query(None), lon: float = Query(None)):
    return load_snapshot(city, lat, lon)


def _enrich(snap: dict) -> dict:
    an = analytics.detect_anomalies(snap)
    snap = dict(snap)
    snap["anomalies"] = an
    snap["recommendations"] = analytics.recommend(snap, an)
    forecasts = {}
    for dkey, dom, mkey, m in analytics.iter_metrics(snap):
        fc = analytics.forecast_metric(snap, dkey, mkey, 14)
        if fc:
            forecasts[f"{dkey}.{mkey}"] = {
                "forecast": fc["forecast"], "trend": fc["trend"],
                "projected_change": fc["projected_change"], "slope_per_day": fc["slope_per_day"]}
    snap["forecasts"] = forecasts
    return snap


@app.get("/api/bundle")
def get_bundle(city: str = Query("Delhi"), lat: float = Query(None), lon: float = Query(None),
               label: str = Query(None, max_length=60)):
    snap = _enrich(load_snapshot(city, lat, lon, label))
    loc = snap["location"]
    return {"cities": [{"key": loc["name"], "name": loc["name"],
                        "country": loc.get("country", ""), "country_code": loc.get("country_code", ""),
                        "lat": loc["latitude"], "lon": loc["longitude"]}],
            "data": {loc["name"]: snap}, "generated_utc": snap["generated_utc"]}


@app.get("/api/anomalies")
def get_anomalies(city: str = Query("Delhi"), lat: float = Query(None), lon: float = Query(None)):
    snap = load_snapshot(city, lat, lon)
    return {"location": snap["location"], "anomalies": analytics.detect_anomalies(snap)}


@app.get("/api/forecast")
def get_forecast(domain: str = Query(..., max_length=20), metric: str = Query(..., max_length=30),
                 horizon: int = Query(14, ge=1, le=60),
                 city: str = Query("Delhi"), lat: float = Query(None), lon: float = Query(None)):
    snap = load_snapshot(city, lat, lon)
    try:
        fc = analytics.forecast_metric(snap, domain, metric, horizon)
    except KeyError:
        raise HTTPException(400, "unknown domain/metric")
    if not fc:
        raise HTTPException(422, "not enough data to forecast")
    return fc


@app.get("/api/recommendations")
def get_recs(city: str = Query("Delhi"), lat: float = Query(None), lon: float = Query(None)):
    snap = load_snapshot(city, lat, lon)
    an = analytics.detect_anomalies(snap)
    return {"location": snap["location"], "recommendations": analytics.recommend(snap, an)}


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    city: str | None = Field(default="Delhi", max_length=60)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)


@app.post("/api/ask")
def ask(body: AskBody):
    snap = load_snapshot(body.city, body.lat, body.lon)
    if gemini.available():
        try:
            return gemini.ask(snap, analytics, body.question)
        except Exception:
            log.warning("gemini call failed; using heuristic fallback")
            out = analytics.answer_question(snap, body.question)
            out["engine"] = "heuristic (gemini unavailable)"
            return out
    return analytics.answer_question(snap, body.question)


# --- serve the frontend (mounted last so /api/* wins) ------------------------
_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND), html=True), name="frontend")
