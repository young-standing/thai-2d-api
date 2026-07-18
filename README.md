# thai-2d-api

Thai SET market collection and verified Myanmar 2D calculation with a serverless GitHub Actions + GitHub Pages production path.

The project collects the public SET index JSON through a requests-first client with a compliant Playwright fallback. Numeric inputs remain exact strings and calculations use `Decimal`; no authentication, CAPTCHA, or anti-bot controls are bypassed.

## Production architecture

```text
GitHub Actions schedule
  -> UnifiedSetClient
  -> verified Myanmar 2D calculation
  -> public/latest.json + public/history.json
  -> GitHub Pages
```

Production requires no VM, Docker runtime, continuously running API, or production SQLite database. See [GITHUB_ACTIONS_DEPLOYMENT.md](GITHUB_ACTIONS_DEPLOYMENT.md).

The Pages client fetches `latest.json?t=<current timestamp>` to avoid stale intermediary caches.

## Thai Government Lottery 3D

The separate 3D pipeline reads the official GLO first-prize number, validates it
as exactly six ASCII digits, and publishes its final three digits without any
integer or float conversion. It does not modify the verified Myanmar 2D rule.

- Current result: <https://young-standing.github.io/thai-2d-api/latest-3d.json>
- Recent history: <https://young-standing.github.io/thai-2d-api/history-3d.json>
- All history: <https://young-standing.github.io/thai-2d-api/history-3d-all.json>

Flutter clients should preserve `first_prize` and `three_d` as strings, append
`?t=<current Unix milliseconds>` for cache busting, and retain cached valid data
when a request fails. Source, workflow, schema, and operational limitations are
documented in [THREE_D_DEPLOYMENT.md](THREE_D_DEPLOYMENT.md).

## Local setup

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
pytest -q
```

Ubuntu:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium
pytest -q
```

Scheduled production polling commands (valid only inside the corresponding
weekday result window):

```bash
python github_publisher.py --window morning
python github_publisher.py --window evening
```

Smoke-test collection without modifying production JSON:

```bash
python github_publisher.py --once
python github_publisher.py --window evening --once --artifact-path .tmp/smoke.json
```

Optional manual historical backfill from the untrusted secondary source:

```bash
python historical_backfill.py --days 30 --output-dir backfill-public
```

Backfill records are locally recalculated, tagged as third-party history, and
cannot replace official scheduled records. The backfill workflow has no schedule
and requires explicit production confirmation.

## Local API and persistence

The existing SQLite repository, Yangon scheduler, and read-only FastAPI application remain available for local or optional self-hosted use:

```bash
python fetch_and_save.py
uvicorn api:app --host 127.0.0.1 --port 8000
```

API endpoints:

- `GET /health`
- `GET /api/market/latest`
- `GET /api/market/history?limit=50`
- `GET /api/2d/latest`

## Optional VM/Docker deployment

Google Compute Engine, Docker Compose, Nginx, and systemd files are retained only as an optional alternative. They are not used by the GitHub production workflow. See [DEPLOYMENT.md](DEPLOYMENT.md).

## GitHub-only YouTube Shorts automation

This repository also contains an independent GitHub Actions workflow that creates one original English motivational Short each day at 7:07 PM in `Asia/Yangon`. It uses the free Pexels Videos API, local FFmpeg rendering on `ubuntu-latest`, repository-supplied royalty-free music, and the official YouTube Data API. It does not use n8n, a VM, a database, a paid renderer, or a paid AI API.

Uploads remain private by default. Start with the beginner setup guide in [SETUP.md](SETUP.md). Royalty-free music requirements are in [music/README.md](music/README.md).

## Data-use notice

SET market data usage may be subject to SET terms and licensing. Confirm endpoint use, collection frequency, and redistribution rights for your application. Never commit cookies, browser profiles, credentials, raw response bodies, traces, databases, or logs.
