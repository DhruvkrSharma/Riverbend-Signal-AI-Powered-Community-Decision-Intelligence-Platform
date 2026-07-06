# Riverbend Signal — Backend (live data + Gemini)

A FastAPI service that powers the **location-dynamic**, real-data version of the
Community Decision Intelligence Platform. Point it at *any city on Earth* and it
pulls live public data, runs the analytics engine, and answers natural-language
questions with **Google Gemini** (grounded on the real figures).

## What's real here
Every number comes from live public APIs — **nothing is synthetic**:

| Domain | Source (free, no key) |
|---|---|
| Air Quality (PM2.5, PM10, NO₂, O₃, US AQI) | `air-quality-api.open-meteo.com` |
| Climate & Weather (temp, rain, wind) | `api.open-meteo.com` |
| Energy & Solar (radiation, sunshine, degree-days) | `api.open-meteo.com` (+ derived) |
| Water & Flood (river discharge) | `flood-api.open-meteo.com` (GloFAS) |
| UV & Wellness (UV, daylight, feels-like, humidity) | `api.open-meteo.com` |
| Geocoding (city → lat/lon, any city) | `geocoding-api.open-meteo.com` |

The **maths is real Python** (`analytics.py`): trailing-window z-score anomaly
detection, robust Theil–Sen + seasonal forecasting, and a rule-based
recommendation engine. Gemini only phrases answers — it is handed the computed
facts and told to use nothing else, so it can't invent figures.

## Run it

```bash
cd prototype/app/backend
python -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt

# Optional but recommended — enables live Gemini answers on /api/ask:
export GEMINI_API_KEY="your-key-from-aistudio.google.com"
# export GEMINI_MODEL="gemini-2.5-flash"   # default

uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000/> — the full web app, now backed by live data for
**any city you search**. Without a key, `/api/ask` degrades gracefully to the
pure-Python heuristic engine, so the app still runs end-to-end.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | status + whether Gemini is configured |
| `GET /api/data?city=Delhi` | full real snapshot (also `?lat=&lon=`) |
| `GET /api/bundle?city=Delhi` | snapshot + anomalies + forecasts + recommendations (frontend uses this) |
| `GET /api/anomalies?city=Delhi` | detected anomalies |
| `GET /api/forecast?domain=climate&metric=temp_mean&city=Delhi` | 14-day forecast + band |
| `GET /api/recommendations?city=Delhi` | stakeholder action recommendations |
| `POST /api/ask` `{question, city}` | grounded NL answer (Gemini, else heuristic) |

```bash
curl "http://localhost:8000/api/health"
curl -X POST localhost:8000/api/ask -H 'content-type: application/json' \
     -d '{"question":"should we worry about the heat?","city":"Mumbai"}'
```

## Security

The backend is hardened for a real deployment:

- **Locked-down CORS** — only origins in `ALLOWED_ORIGINS` (env) are accepted; no
  wildcard, no credentials.
- **Security headers** on every response — `Content-Security-Policy` (self + inline
  only), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy`, and `HSTS` over HTTPS.
- **Per-IP rate limiting** (token bucket, default 60 req burst) on all `/api/*`.
- **Strict input validation** — question length capped (≤500), city name charset +
  length checked, `lat/lon` range-bounded, 16 KB request-body cap.
- **Safe errors** — generic messages only; stack traces and internals never leak.
- **Secret hygiene** — `GEMINI_API_KEY` read from env only, never logged or returned;
  `.env` is git-ignored (`.env.example` documents the vars).
- **Prompt-injection resistance** — the user question is passed to Gemini as
  untrusted data with explicit "ignore embedded instructions" framing, and the model
  is grounded strictly on precomputed facts (no tool access, no free-form actions).
- **No SSRF** — outbound calls only ever hit the fixed Open-Meteo hosts; URLs are
  never built from user input.
- **Frontend** — all user/API-sourced strings are HTML-escaped; fully inline assets
  (no external subresources); same-origin `fetch` only.

## Files
- `data.py` — live ingestion + normalisation (any location).
- `analytics.py` — anomaly detection, forecasting, recommendations, heuristic Q&A.
- `gemini.py` — grounded Gemini client (RAG over the computed facts).
- `main.py` — FastAPI app + static frontend hosting + TTL cache.
- `build_snapshot.py` — captures a real multi-city snapshot for the standalone Artifact.
- `frontend/index.html` — the live web client (same UI as the Artifact).

> Deploy note: this maps cleanly onto **Cloud Run** (container the backend),
> **Vertex AI / Gemini** for the language layer, and **BigQuery** for the data
> lake if you swap the Open-Meteo calls for warehoused city feeds.
