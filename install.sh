#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
MANAGER_DIR="/etc/farstar-warner"
INSTANCE_DIR="${MANAGER_DIR}/instances"
MAIN_INSTANCE="warner"
MAIN_INSTANCE_ENV="${INSTANCE_DIR}/${MAIN_INSTANCE}.env"

log() {
  printf '\n[Farstar Warner] %s\n' "$1"
}

fail() {
  printf '\nError: %s\n' "$1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Linux" ]] || ! command -v apt-get >/dev/null 2>&1; then
  fail "This installer supports Ubuntu Linux only."
fi

[[ -r /etc/os-release ]] || fail "Unable to identify this operating system."
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fail "This installer supports Ubuntu Linux only."

if [[ ${EUID} -eq 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required when the installer is not run as root."
  SUDO=(sudo)
fi

install_docker() {
  log "Installing Docker Engine and the Docker Compose plugin..."
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y ca-certificates curl
  "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | "${SUDO[@]}" tee /etc/apt/keyrings/docker.asc >/dev/null
  "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc

  # shellcheck disable=SC1091
  . /etc/os-release
  local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  [[ -n "${codename}" ]] || fail "Unable to determine the Ubuntu release codename."
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "${codename}" | "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null

  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  "${SUDO[@]}" systemctl enable --now docker

  local login_user="${SUDO_USER:-${USER:-}}"
  if [[ -n "${login_user}" && "${login_user}" != "root" ]]; then
    "${SUDO[@]}" usermod -aG docker "${login_user}"
    log "Added ${login_user} to the docker group. The installer will use sudo for this run."
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  install_docker
elif ! docker compose version >/dev/null 2>&1 && ! "${SUDO[@]}" docker compose version >/dev/null 2>&1; then
  log "Docker is installed, but the Compose plugin is missing. Installing it..."
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker-compose-plugin
fi

if [[ ${EUID} -eq 0 ]] && docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif "${SUDO[@]}" docker info >/dev/null 2>&1; then
  DOCKER=("${SUDO[@]}" docker)
else
  fail "Docker is installed but the daemon is unavailable. Start Docker and run this installer again."
fi

if ! command -v git >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
  log "Installing Git and OpenSSL for updates and secure credential generation..."
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y git openssl
fi

prompt_required() {
  local variable_name="$1"
  local prompt_text="$2"
  local secret="${3:-false}"
  local value=""
  while [[ -z "${value}" ]]; do
    if [[ "${secret}" == "true" ]]; then
      read -r -s -p "${prompt_text}: " value
      printf '\n'
    else
      read -r -p "${prompt_text}: " value
    fi
    [[ -n "${value}" ]] || printf 'A value is required.\n'
  done
  printf -v "${variable_name}" '%s' "${value}"
}

prompt_default() {
  local variable_name="$1"
  local prompt_text="$2"
  local default_value="$3"
  local value=""
  read -r -p "${prompt_text} [${default_value}]: " value
  printf -v "${variable_name}" '%s' "${value:-${default_value}}"
}

valid_identifier() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

valid_env_secret() {
  [[ "$1" =~ ^[A-Za-z0-9._~!@%+=:-]+$ ]]
}

REUSE_EXISTING=false
if [[ -f "${ENV_FILE}" || -L "${ENV_FILE}" ]] && "${SUDO[@]}" test -s "${ENV_FILE}"; then
  read -r -p "An existing .env configuration was found. Reuse it? [Y/n]: " reuse_answer
  if [[ "${reuse_answer,,}" != "n" && "${reuse_answer,,}" != "no" ]]; then
    REUSE_EXISTING=true
    log "Reusing the existing bot configuration."
  fi
fi

if [[ "${REUSE_EXISTING}" == "false" ]]; then
  log "Collecting application settings. Passwords are hidden; the Telegram token is visible."

  while true; do
    prompt_required TELEGRAM_BOT_TOKEN "Telegram bot token (visible while typing)"
    [[ "${TELEGRAM_BOT_TOKEN}" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && break
    printf 'The Telegram bot token format is invalid.\n'
  done

  while true; do
    prompt_required ADMIN_TELEGRAM_ID "Administrator Telegram numeric ID"
    [[ "${ADMIN_TELEGRAM_ID}" =~ ^[1-9][0-9]*$ ]] && break
    printf 'Enter a positive numeric Telegram ID.\n'
  done

  while true; do
    prompt_default POSTGRES_DB "PostgreSQL database name" "farstar_warner"
    valid_identifier "${POSTGRES_DB}" && break
    printf 'Use letters, numbers, and underscores; the first character must be a letter or underscore.\n'
  done

  while true; do
    prompt_default POSTGRES_USER "PostgreSQL username" "farstar"
    valid_identifier "${POSTGRES_USER}" && break
    printf 'Use letters, numbers, and underscores; the first character must be a letter or underscore.\n'
  done

  while true; do
    prompt_required POSTGRES_PASSWORD "PostgreSQL password" true
    valid_env_secret "${POSTGRES_PASSWORD}" && break
    printf 'Use only letters, numbers, and these symbols: . _ ~ ! @ %% + = : -\n'
  done

  DEFAULT_REDIS_PASSWORD="$(openssl rand -base64 36 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 40 || true)"
  if [[ -z "${DEFAULT_REDIS_PASSWORD}" ]]; then
    DEFAULT_REDIS_PASSWORD="$(date +%s%N)${RANDOM}${RANDOM}"
  fi
  read -r -s -p "Redis password [press Enter to generate one]: " REDIS_PASSWORD
  printf '\n'
  REDIS_PASSWORD="${REDIS_PASSWORD:-${DEFAULT_REDIS_PASSWORD}}"
  valid_env_secret "${REDIS_PASSWORD}" || fail "The Redis password contains unsupported characters."
  read -r -p "Zarinpal Merchant ID [optional, press Enter to disable]: " ZARINPAL_MERCHANT_ID
  ZARINPAL_CALLBACK_URL=""
  if [[ -n "${ZARINPAL_MERCHANT_ID}" ]]; then
    [[ "${ZARINPAL_MERCHANT_ID}" =~ ^[A-Za-z0-9-]{10,100}$ ]] \
      || fail "The Zarinpal Merchant ID contains unsupported characters."
    read -r -p "Zarinpal HTTPS callback URL: " ZARINPAL_CALLBACK_URL
    [[ "${ZARINPAL_CALLBACK_URL}" == https://* ]] \
      || fail "The Zarinpal callback URL must start with https://"
  fi
  umask 077
  if [[ -L "${ENV_FILE}" ]]; then
    "${SUDO[@]}" rm -f -- "${ENV_FILE}"
  fi
  cat >"${ENV_FILE}" <<EOF
INSTANCE_NAME=${MAIN_INSTANCE}
COMPOSE_PROJECT_NAME=farstar-warner
BOT_ENV_FILE=${MAIN_INSTANCE_ENV}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
ADMIN_TELEGRAM_ID=${ADMIN_TELEGRAM_ID}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=${REDIS_PASSWORD}
CHECK_INTERVAL_SECONDS=300
CHECK_CONCURRENCY=4
DEACTIVATION_CONFIRMATIONS=2
DEACTIVATION_CONFIRMATION_DELAY_SECONDS=15
CHECK_JITTER_MIN_SECONDS=0.5
CHECK_JITTER_MAX_SECONDS=3.0
INSTAGRAM_PROXY_URL=socks5://warp_proxy:1080
INSTAGRAM_SEARCH_DOC_ID=26347858941511777
INSTAGRAM_BASELINE_USERNAMES=farstar_vpn,instagram,nasa
PROXY_HEALTH_URL=https://www.cloudflare.com/cdn-cgi/trace
PAGE_CHECK_DELAY_MIN_SECONDS=0.5
PAGE_CHECK_DELAY_MAX_SECONDS=2
RATE_LIMIT_COOLDOWN_SECONDS=900
PREFLIGHT_CACHE_SECONDS=300
RECOVERY_BATCH_SIZE=25
HEALTH_FAILURE_ALERT_THRESHOLD=2
HEALTH_ALERT_REMINDER_SECONDS=21600
GUEST_SEARCH_AUDIT_SECONDS=21600
OUTBOX_BATCH_SIZE=25
OUTBOX_MAX_ATTEMPTS=12
FOLLOWER_SPIKE_THRESHOLD=1000
FOLLOWER_SPIKE_WINDOW_SECONDS=3600
USD_TOMAN_FALLBACK_RATE=650000
ZARINPAL_MERCHANT_ID=${ZARINPAL_MERCHANT_ID}
ZARINPAL_CALLBACK_URL=${ZARINPAL_CALLBACK_URL}
ZARINPAL_TIMEOUT_SECONDS=15
FREE_TRIAL_DAYS=7
LOG_LEVEL=INFO
EOF
  chmod 600 "${ENV_FILE}"
fi

"${SUDO[@]}" chmod 600 "${ENV_FILE}"

# Preserve custom tuning while migrating only the known legacy defaults.
if "${SUDO[@]}" grep -q '^CHECK_CONCURRENCY=8$' "${ENV_FILE}"; then
  "${SUDO[@]}" sed -i 's/^CHECK_CONCURRENCY=8$/CHECK_CONCURRENCY=4/' "${ENV_FILE}"
fi
if "${SUDO[@]}" grep -q '^PAGE_CHECK_DELAY_MIN_SECONDS=15\(\.0\)\?$' "${ENV_FILE}"; then
  "${SUDO[@]}" sed -i 's/^PAGE_CHECK_DELAY_MIN_SECONDS=.*/PAGE_CHECK_DELAY_MIN_SECONDS=0.5/' "${ENV_FILE}"
fi
if "${SUDO[@]}" grep -q '^PAGE_CHECK_DELAY_MAX_SECONDS=45\(\.0\)\?$' "${ENV_FILE}"; then
  "${SUDO[@]}" sed -i 's/^PAGE_CHECK_DELAY_MAX_SECONDS=.*/PAGE_CHECK_DELAY_MAX_SECONDS=2/' "${ENV_FILE}"
fi
for key_value in \
  'RATE_LIMIT_COOLDOWN_SECONDS=900' \
  'PREFLIGHT_CACHE_SECONDS=300' \
  'RECOVERY_BATCH_SIZE=25' \
  'HEALTH_FAILURE_ALERT_THRESHOLD=2' \
  'HEALTH_ALERT_REMINDER_SECONDS=21600' \
  'GUEST_SEARCH_AUDIT_SECONDS=21600' \
  'OUTBOX_BATCH_SIZE=25' \
  'OUTBOX_MAX_ATTEMPTS=12'; do
  if ! "${SUDO[@]}" grep -q "^${key_value%%=*}=" "${ENV_FILE}"; then
    printf '%s\n' "${key_value}" | "${SUDO[@]}" tee -a "${ENV_FILE}" >/dev/null
  fi
done

log "Installing the Farstar server management command..."
if [[ -d "${PROJECT_DIR}/.git" ]]; then
  git -C "${PROJECT_DIR}" config core.fileMode false
fi
"${SUDO[@]}" mkdir -p "${INSTANCE_DIR}"
CURRENT_ENV_TARGET="$(readlink -f -- "${ENV_FILE}" 2>/dev/null || true)"
if [[ "${CURRENT_ENV_TARGET}" != "${MAIN_INSTANCE_ENV}" ]]; then
  {
    if ! "${SUDO[@]}" grep -q '^INSTANCE_NAME=' "${ENV_FILE}"; then
      printf '\nINSTANCE_NAME=%s\n' "${MAIN_INSTANCE}"
    fi
    if ! "${SUDO[@]}" grep -q '^BOT_ENV_FILE=' "${ENV_FILE}"; then
      printf 'BOT_ENV_FILE=%s\n' "${MAIN_INSTANCE_ENV}"
    fi
  } | "${SUDO[@]}" tee -a "${ENV_FILE}" >/dev/null
  "${SUDO[@]}" install -m 600 "${ENV_FILE}" "${MAIN_INSTANCE_ENV}"
  "${SUDO[@]}" rm -f -- "${ENV_FILE}"
  "${SUDO[@]}" ln -s "${MAIN_INSTANCE_ENV}" "${ENV_FILE}"
fi

{
  printf 'APP_DIR=%q\n' "${PROJECT_DIR}"
  printf 'INSTANCE_DIR=%q\n' "${INSTANCE_DIR}"
  printf 'REPOSITORY_URL=%q\n' "https://github.com/farstar-team/farstar-warner.git"
} | "${SUDO[@]}" tee "${MANAGER_DIR}/farstar.conf" >/dev/null
"${SUDO[@]}" chmod 644 "${MANAGER_DIR}/farstar.conf"
"${SUDO[@]}" chmod 755 "${PROJECT_DIR}/farstar.sh"
"${SUDO[@]}" ln -sfn "${PROJECT_DIR}/farstar.sh" /usr/local/bin/farstar

log "Building and starting Farstar Warner..."
cd "${PROJECT_DIR}"
if ! "${DOCKER[@]}" compose \
  --project-name "farstar-${MAIN_INSTANCE}" \
  --env-file "${MAIN_INSTANCE_ENV}" \
  --file "${PROJECT_DIR}/docker-compose.yml" \
  up --build -d --wait --wait-timeout 300; then
  printf '\nThe application did not become healthy within 300 seconds.\n' >&2
  "${DOCKER[@]}" compose \
    --project-name "farstar-${MAIN_INSTANCE}" \
    --env-file "${MAIN_INSTANCE_ENV}" \
    --file "${PROJECT_DIR}/docker-compose.yml" \
    ps >&2 || true
  "${DOCKER[@]}" compose \
    --project-name "farstar-${MAIN_INSTANCE}" \
    --env-file "${MAIN_INSTANCE_ENV}" \
    --file "${PROJECT_DIR}/docker-compose.yml" \
    logs --tail=100 bot-app warp_proxy >&2 || true
  fail "Installation stopped because the bot or one of its dependencies is unhealthy."
fi
"${DOCKER[@]}" compose \
  --project-name "farstar-${MAIN_INSTANCE}" \
  --env-file "${MAIN_INSTANCE_ENV}" \
  --file "${PROJECT_DIR}/docker-compose.yml" \
  ps

log "Installation completed successfully."
printf 'Open the server manager at any time with: farstar\n'
printf 'View this bot log with: farstar logs %s\n' "${MAIN_INSTANCE}"
