#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/opt/thai-2d-api"
DATA_DIR="/var/lib/thai-2d"
DEPLOY_USER="thai2d"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer with sudo." >&2
  exit 1
fi

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${PROJECT_DIR}/.runtime-home" --shell /usr/sbin/nologin "${DEPLOY_USER}"
fi

apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-pip nginx sqlite3 ca-certificates curl git

if [[ "${REPO_DIR}" != "${PROJECT_DIR}" ]]; then
  mkdir -p "${PROJECT_DIR}"
  cp -a "${REPO_DIR}/." "${PROJECT_DIR}/"
fi

mkdir -p "${DATA_DIR}" "${PROJECT_DIR}/.runtime-home" "${PROJECT_DIR}/.cache/ms-playwright"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${PROJECT_DIR}" "${DATA_DIR}"
chmod 0750 "${PROJECT_DIR}" "${DATA_DIR}" "${PROJECT_DIR}/.runtime-home"

sudo -u "${DEPLOY_USER}" python3 -m venv "${PROJECT_DIR}/.venv"
sudo -u "${DEPLOY_USER}" "${PROJECT_DIR}/.venv/bin/python" -m pip install --upgrade pip
sudo -u "${DEPLOY_USER}" "${PROJECT_DIR}/.venv/bin/python" -m pip install --requirement "${PROJECT_DIR}/requirements.txt"
"${PROJECT_DIR}/.venv/bin/python" -m playwright install-deps chromium
sudo -u "${DEPLOY_USER}" env PLAYWRIGHT_BROWSERS_PATH="${PROJECT_DIR}/.cache/ms-playwright" \
  "${PROJECT_DIR}/.venv/bin/python" -m playwright install chromium

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
  cat >"${PROJECT_DIR}/.env" <<'ENVIRONMENT'
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
PLAYWRIGHT_BROWSERS_PATH=/opt/thai-2d-api/.cache/ms-playwright
COLLECTOR_LOCK_FILE=/opt/thai-2d-api/.runtime-home/collector.lock
ENVIRONMENT
fi
chown root:"${DEPLOY_USER}" "${PROJECT_DIR}/.env"
chmod 0640 "${PROJECT_DIR}/.env"

# Initialize the one shared database before the read-only API preflight runs.
sudo -u "${DEPLOY_USER}" env DATABASE_PATH="${DATA_DIR}/thai_2d.sqlite3" \
  "${PROJECT_DIR}/.venv/bin/python" -c \
  "from market_repository import MarketRepository; MarketRepository('${DATA_DIR}/thai_2d.sqlite3').initialize()"
chmod 0640 "${DATA_DIR}/thai_2d.sqlite3"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${DATA_DIR}/thai_2d.sqlite3"

install -m 0644 "${PROJECT_DIR}/deploy/thai-2d-api.service" /etc/systemd/system/thai-2d-api.service
install -m 0644 "${PROJECT_DIR}/deploy/thai-2d-collector.service" /etc/systemd/system/thai-2d-collector.service

# The checked-in Nginx config targets Docker DNS. Native systemd uses loopback.
sed 's/server api:8000;/server 127.0.0.1:8000;/' "${PROJECT_DIR}/deploy/nginx.conf" >/etc/nginx/nginx.conf
nginx -t
systemctl daemon-reload
systemctl enable thai-2d-api thai-2d-collector nginx

set -a
# shellcheck disable=SC1091
source "${PROJECT_DIR}/.env"
set +a
sudo -u "${DEPLOY_USER}" --preserve-env=DATABASE_PATH,STALE_AFTER_SECONDS,ALLOWED_ORIGINS,MORNING_TARGET,EVENING_TARGET,FETCH_INTERVAL_SECONDS,MORNING_WINDOW_START,MORNING_WINDOW_END,EVENING_WINDOW_START,EVENING_WINDOW_END,HEADLESS,PLAYWRIGHT_BROWSERS_PATH \
  "${PROJECT_DIR}/.venv/bin/python" "${PROJECT_DIR}/deploy/predeploy_check.py" --mode all

echo "Installation verified. Start with: systemctl start thai-2d-api thai-2d-collector nginx"
