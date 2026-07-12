# Google Compute Engine deployment

This project supports two deployment modes on an Ubuntu 24.04 LTS VM: Docker Compose or native systemd. Do not run both simultaneously. In both modes, the API is read-only and the scheduled collector is the only process that writes SQLite or launches Chromium.

## VM and network

- Start with an `e2-small`; Playwright/Chromium can be unreliable with `e2-micro` memory.
- Reserve and attach a static external IPv4 address.
- Enable Google Cloud HTTP and HTTPS firewall traffic. HTTPS is reserved for later TLS configuration.
- Never create a firewall rule for TCP 8000. Uvicorn is bound to loopback or an internal Docker network.
- Do not configure a domain or TLS certificate yet.

Create a non-root operator after connecting to the VM:

```bash
sudo apt update
sudo apt install -y git ca-certificates curl
sudo adduser --disabled-password --gecos '' deployer
sudo usermod -aG sudo deployer
```

Use `deployer` for clones and updates. The native installer creates a non-login `thai2d` service account.

## Environment and SQLite

Copy `.env.example` to `.env` and review every value. No cookies, authorization tokens, browser profiles, or API keys are needed.

```dotenv
DATABASE_PATH=/var/lib/thai-2d/thai_2d.sqlite3
STALE_AFTER_SECONDS=86400
ALLOWED_ORIGINS=
MORNING_TARGET=12:01
EVENING_TARGET=16:30
FETCH_INTERVAL_SECONDS=30
MORNING_WINDOW_START=11:59:30
MORNING_WINDOW_END=12:02:00
EVENING_WINDOW_START=16:28:30
EVENING_WINDOW_END=16:32:00
HEADLESS=true
```

`DATABASE_PATH` must be absolute and identical for API and collector. Never create a second database path. The collector retains WAL and busy-timeout behavior; API reads use SQLite `mode=ro` and `query_only`.

Initially use exactly one Uvicorn worker. Multiple workers are unnecessary for this small read workload and complicate SQLite/WAL operations and diagnosis.

## Docker Compose deployment

Install Docker Engine and its Compose plugin using Docker's Ubuntu instructions, then:

```bash
git clone YOUR_REPOSITORY_URL thai-2d-api
cd thai-2d-api
cp .env.example .env
mkdir -p data backups
# The pinned image's pwuser normally uses UID/GID 1001.
sudo chown -R 1001:1001 data
sudo chmod 0750 data
docker compose build --pull
docker compose run --rm collector python -c \
  "from market_repository import MarketRepository; MarketRepository('/var/lib/thai-2d/thai_2d.sqlite3').initialize()"
docker compose up -d
```

The image pins `mcr.microsoft.com/playwright/python:v1.61.0-noble`, matching `playwright==1.61.0`. Playwright recommends matching image/package versions and using an init process plus host IPC for Chromium workloads. [Official Playwright Docker guidance](https://playwright.dev/python/docs/docker)

Only Nginx publishes port 80. The API port exists solely on the internal Compose network. Both Python containers mount the same absolute SQLite location; API mounts it read-only and collector mounts it read/write.

```bash
docker compose ps
docker compose logs --tail=100 api
docker compose logs --tail=100 collector
curl --fail http://127.0.0.1/health
curl --fail http://127.0.0.1/api/2d/latest
```

Update only after tests pass:

```bash
git fetch --all --prune
git pull --ff-only
docker compose build --pull
docker compose run --rm api python -m pytest -q
docker compose up -d --remove-orphans
```

## Native systemd deployment

```bash
sudo git clone YOUR_REPOSITORY_URL /opt/thai-2d-api
cd /opt/thai-2d-api
sudo bash deploy/install.sh
sudo systemctl start thai-2d-api thai-2d-collector nginx
```

The installer creates `thai2d`, `/var/lib/thai-2d`, a shared virtual environment, Chromium, protected `.env`, initial SQLite file, systemd units, and Nginx. It changes the Docker upstream `api:8000` to `127.0.0.1:8000` for host Nginx.

Both units start after networking and restart on failure. The API binds only to `127.0.0.1:8000`; both units share `/opt/thai-2d-api/.venv` and `.env`.

```bash
sudo systemctl status thai-2d-api thai-2d-collector nginx
sudo journalctl -u thai-2d-api -n 100 --no-pager
sudo journalctl -u thai-2d-collector -n 100 --no-pager
sudo journalctl -u thai-2d-collector -f
curl --fail http://127.0.0.1/health
```

Update:

```bash
cd /opt/thai-2d-api
sudo -u thai2d git fetch --all --prune
sudo -u thai2d git pull --ff-only
sudo -u thai2d .venv/bin/python -m pip install -r requirements.txt
sudo -u thai2d .venv/bin/python -m pytest -q
sudo systemctl restart thai-2d-api thai-2d-collector
sudo nginx -t && sudo systemctl reload nginx
```

Verify a reboot:

```bash
sudo reboot
# Reconnect:
systemctl is-active thai-2d-api thai-2d-collector nginx
curl --fail http://127.0.0.1/health
```

## Pre-deployment verification

The verifier checks Python 3.12+, required environment values, absolute database path, database directory permissions, Chromium, API and collector imports, and the full suite:

```bash
cd /opt/thai-2d-api
set -a; source .env; set +a
.venv/bin/python deploy/predeploy_check.py --mode all
```

Each systemd unit also runs a fast mode-specific `ExecStartPre` check. Always run the full check before a deployment restart.

## Nginx and security

Nginx forwards standard proxy headers, disables directory listing and version tokens, blocks database/environment filenames, rate-limits public routes, limits per-IP connections, adds defensive headers, and returns safe JSON errors. SQLite is stored under `/var/lib`, never an Nginx document root.

Keep SSH access before enabling UFW:

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp  # reserved; TLS is not configured yet
sudo ufw deny 8000/tcp
sudo ufw enable
sudo ufw status verbose
```

Google Cloud VPC firewall rules must likewise permit 80/443 and omit 8000. Keep `/opt/thai-2d-api/.env` mode `0640`, data directory `0750`, and database `0640`.

## Backup and restore

Use SQLite's online backup API instead of copying a live WAL database:

```bash
sudo install -d -m 0750 -o thai2d -g thai2d /var/backups/thai-2d
sudo -u thai2d /opt/thai-2d-api/.venv/bin/python -c \
  "import sqlite3,datetime; src=sqlite3.connect('/var/lib/thai-2d/thai_2d.sqlite3'); dst=sqlite3.connect('/var/backups/thai-2d/thai_2d-' + datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ') + '.sqlite3'); src.backup(dst); dst.close(); src.close()"
```

For Docker bind mounts, use `data/thai_2d.sqlite3` and `backups/`, or run the same Python backup inside the collector container with both paths mounted.

Restore only while readers and writer are stopped:

```bash
sudo systemctl stop thai-2d-collector thai-2d-api
sudo install -m 0640 -o thai2d -g thai2d BACKUP.sqlite3 /var/lib/thai-2d/thai_2d.sqlite3
sudo rm -f /var/lib/thai-2d/thai_2d.sqlite3-wal /var/lib/thai-2d/thai_2d.sqlite3-shm
sudo systemctl start thai-2d-api thai-2d-collector
curl --fail http://127.0.0.1/health
```

For Docker use `docker compose stop`, restore the bind-mounted file, then `docker compose up -d`.

## Rollback

Record a known-good commit before updates:

```bash
cd /opt/thai-2d-api
git rev-parse HEAD | sudo tee /var/lib/thai-2d/known-good-commit
```

Rollback code without changing market history:

```bash
sudo systemctl stop thai-2d-collector thai-2d-api
cd /opt/thai-2d-api
GOOD=$(cat /var/lib/thai-2d/known-good-commit)
sudo -u thai2d git switch --detach "$GOOD"
sudo -u thai2d .venv/bin/python -m pip install -r requirements.txt
sudo -u thai2d .venv/bin/python -m pytest -q
sudo systemctl start thai-2d-api thai-2d-collector
```

For Docker, check out the known-good commit, rebuild, test, and run `docker compose up -d`. Restore SQLite only if the database itself caused the incident.

## Operations

- Check API health: `curl --fail http://127.0.0.1/health`.
- Check collector logs: `journalctl -u thai-2d-collector -f` or `docker compose logs -f collector`.
- Review disk usage and backups regularly.
- The collector launches Chromium only in Yangon collection windows; API requests never launch it.
- Add a real domain and TLS in a later change.
