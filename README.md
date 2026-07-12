# thai-2d-api

A production-oriented FastAPI service that collects public Thai SET index data on a schedule, stores current and historical snapshots, and serves only stored data to API clients. API reads never scrape SET.

> Market data usage may be subject to SET terms and licensing. The default URL is the website's current public overview XHR, not a guaranteed public API contract. Confirm that your use complies with SET terms. This project does not bypass authentication, access controls, rate limits, or anti-bot systems.

## Features

- Scheduled background collection with timeout, exponential-backoff retries, and structured JSON logs
- JSON/XHR collector first; optional Playwright fallback against the normal public page
- SQLite via a repository boundary and SQLAlchemy, allowing a later PostgreSQL URL/migration
- String storage for index and market value, preserving source trailing zeros
- Database-level duplicate prevention and explicit stale-data responses
- Constant-time API-key check for manual refresh
- No invented 2D rule: raw SET index/value are returned separately

## API

- `GET /health`
- `GET /api/market/latest`
- `GET /api/market/history?limit=50` (1–1000)
- `GET /api/2d/latest`
- `POST /api/admin/refresh` with `X-API-Key: <ADMIN_API_KEY>`

Interactive documentation is available at `/docs`.

## Windows setup (PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and replace ADMIN_API_KEY.
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run tests with `pytest -q`. If the explicitly enabled browser fallback is needed, run `playwright install chromium`.

## Ubuntu setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
# Edit .env and replace ADMIN_API_KEY.
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run tests with `pytest -q`. If browser fallback is explicitly enabled, install Chromium and its OS dependencies with `playwright install --with-deps chromium`.

## Configuration and operation

All settings use environment variables shown in `.env.example`. Keep `PLAYWRIGHT_FALLBACK_ENABLED=false` unless the JSON endpoint is unavailable and browser collection is acceptable under the upstream terms. Collection begins on application startup and repeats every `COLLECTOR_INTERVAL_SECONDS`. Manual refresh uses the same locked collector, so overlapping refreshes cannot race.

When SET cannot be reached, the failed collection is logged and existing database data remains available. Responses mark old records with `is_stale=true` after `STALE_AFTER_SECONDS`. Before data has ever been collected, data endpoints return HTTP 503.

For multiple production workers, run the collector in exactly one process (set `COLLECTOR_ENABLED=false` on API-only workers) or move scheduling to a dedicated worker. SQLite is suitable for the initial single-node deployment; PostgreSQL should be introduced with a migration tool such as Alembic before scaling writes.

## 2D calculation strategy

There is no assumed calculation rule in this repository. The configured `raw_only` strategy returns the raw `set_index` and `set_value`, with `two_d: null` and `calculation_status: not_configured`.

To add a rule, implement `TwoDStrategy` in `app/services/two_d_service.py`, register it by name, document the exact approved specification and test vectors, then select it with `TWO_D_STRATEGY`. Do not derive a rule from examples alone.

## Production checklist

- Replace the admin key with a secret supplied by your deployment platform.
- Confirm SET data rights, endpoint stability, permitted interval, and redistribution terms.
- Pin and scan dependencies; terminate TLS at a trusted proxy and restrict the admin endpoint.
- Add Alembic migrations and PostgreSQL before horizontal scaling.
- Add external health monitoring, log aggregation, backups, and a retention policy.
- Run a single scheduler or dedicated collector process to avoid duplicate upstream requests.
