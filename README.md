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

## Data-use notice

SET market data usage may be subject to SET terms and licensing. Confirm endpoint use, collection frequency, and redistribution rights for your application. Never commit cookies, browser profiles, credentials, raw response bodies, traces, databases, or logs.
