#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_FILE="/etc/farstar-warner/farstar.conf"
DEFAULT_REPOSITORY="https://github.com/farstar-team/farstar-warner.git"

log() {
  printf '\n[Farstar] %s\n' "$1"
}

warn() {
  printf '\nWarning: %s\n' "$1" >&2
}

fail() {
  printf '\nError: %s\n' "$1" >&2
  exit 1
}

if [[ ${EUID} -eq 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null 2>&1 || fail "sudo is required."
  SUDO=(sudo)
fi

load_config() {
  if [[ -r "${CONFIG_FILE}" ]]; then
    # The installer creates this root-owned file with shell-escaped values.
    # shellcheck disable=SC1090
    . "${CONFIG_FILE}"
  elif [[ -e "${CONFIG_FILE}" ]]; then
    local config_text
    config_text="$("${SUDO[@]}" cat "${CONFIG_FILE}")"
    eval "${config_text}"
  else
    local script_path
    script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
    APP_DIR="$(dirname -- "${script_path}")"
    INSTANCE_DIR="/etc/farstar-warner/instances"
    REPOSITORY_URL="${DEFAULT_REPOSITORY}"
  fi
  : "${APP_DIR:?APP_DIR is missing from ${CONFIG_FILE}}"
  INSTANCE_DIR="${INSTANCE_DIR:-/etc/farstar-warner/instances}"
  REPOSITORY_URL="${REPOSITORY_URL:-${DEFAULT_REPOSITORY}}"
  COMPOSE_FILE="${APP_DIR}/docker-compose.yml"
  [[ -f "${COMPOSE_FILE}" ]] || fail "docker-compose.yml was not found in ${APP_DIR}."
}

select_docker() {
  command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
  if [[ ${EUID} -eq 0 ]]; then
    DOCKER=(docker)
  else
    DOCKER=("${SUDO[@]}" docker)
  fi
  "${DOCKER[@]}" info >/dev/null 2>&1 || fail "Docker is not running or cannot be accessed."
  "${DOCKER[@]}" compose version >/dev/null 2>&1 || fail "The Docker Compose plugin is missing."
}

instance_env() {
  printf '%s/%s.env' "${INSTANCE_DIR}" "$1"
}

validate_instance_name() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{0,31}$ ]]
}

require_instance() {
  local instance="$1"
  validate_instance_name "${instance}" || fail "Invalid instance name. Use lowercase letters, numbers, and hyphens."
  "${SUDO[@]}" test -f "$(instance_env "${instance}")" || fail "Instance '${instance}' does not exist."
}

compose() {
  local instance="$1"
  shift
  local env_file
  env_file="$(instance_env "${instance}")"
  "${DOCKER[@]}" compose \
    --project-name "farstar-${instance}" \
    --env-file "${env_file}" \
    --file "${COMPOSE_FILE}" \
    "$@"
}

list_instances() {
  log "Configured bot instances"
  local found=false
  while IFS= read -r env_file; do
    found=true
    local instance status
    instance="$(basename -- "${env_file}" .env)"
    if [[ -n "$(compose "${instance}" ps --status running -q bot-app 2>/dev/null || true)" ]]; then
      status="running"
    else
      status="stopped"
    fi
    printf '  %-32s %s\n' "${instance}" "${status}"
  done < <("${SUDO[@]}" find "${INSTANCE_DIR}" -maxdepth 1 -type f -name '*.env' -print 2>/dev/null | sort)
  if [[ "${found}" == "false" ]]; then
    printf '  No instances are configured.\n'
  fi
}

prompt_instance() {
  local prompt_text="${1:-Instance name}"
  local value=""
  read -r -p "${prompt_text}: " value
  require_instance "${value}"
  SELECTED_INSTANCE="${value}"
}

prompt_required() {
  local variable_name="$1"
  local prompt_text="$2"
  local hidden="${3:-false}"
  local value=""
  while [[ -z "${value}" ]]; do
    if [[ "${hidden}" == "true" ]]; then
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

random_secret() {
  local secret
  secret="$(openssl rand -base64 48 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 48 || true)"
  if [[ -z "${secret}" ]]; then
    secret="$(date +%s%N)${RANDOM}${RANDOM}${RANDOM}"
  fi
  printf '%s' "${secret}"
}

build_image() {
  log "Building the latest Farstar Warner image..."
  "${DOCKER[@]}" build --tag farstar-warner:latest "${APP_DIR}"
}

add_instance() {
  log "Add and install a new bot instance"
  local instance=""
  while true; do
    prompt_required instance "Instance name (lowercase letters, numbers, hyphens)"
    if ! validate_instance_name "${instance}"; then
      printf 'Invalid name. Example: sales-bot\n'
      continue
    fi
    if "${SUDO[@]}" test -e "$(instance_env "${instance}")"; then
      printf 'That instance already exists.\n'
      continue
    fi
    break
  done

  local telegram_bot_token admin_telegram_id postgres_db postgres_user
  local postgres_password redis_password default_database temp_file env_file
  while true; do
    prompt_required telegram_bot_token "Telegram bot token (visible while typing)"
    [[ "${telegram_bot_token}" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] && break
    printf 'The Telegram bot token format is invalid.\n'
  done
  while true; do
    prompt_required admin_telegram_id "Administrator Telegram numeric ID"
    [[ "${admin_telegram_id}" =~ ^[1-9][0-9]*$ ]] && break
    printf 'Enter a positive numeric Telegram ID.\n'
  done

  default_database="farstar_${instance//-/_}"
  while true; do
    prompt_default postgres_db "PostgreSQL database name" "${default_database}"
    valid_identifier "${postgres_db}" && break
    printf 'Use letters, numbers, and underscores; the first character must be a letter or underscore.\n'
  done
  while true; do
    prompt_default postgres_user "PostgreSQL username" "farstar"
    valid_identifier "${postgres_user}" && break
    printf 'Use letters, numbers, and underscores; the first character must be a letter or underscore.\n'
  done
  while true; do
    prompt_required postgres_password "PostgreSQL password" true
    valid_env_secret "${postgres_password}" && break
    printf 'Use only letters, numbers, and these symbols: . _ ~ ! @ %% + = : -\n'
  done
  read -r -s -p "Redis password [press Enter to generate one]: " redis_password
  printf '\n'
  redis_password="${redis_password:-$(random_secret)}"
  valid_env_secret "${redis_password}" || fail "The Redis password contains unsupported characters."

  env_file="$(instance_env "${instance}")"
  temp_file="$(mktemp)"
  chmod 600 "${temp_file}"
  cat >"${temp_file}" <<EOF
INSTANCE_NAME=${instance}
COMPOSE_PROJECT_NAME=farstar-${instance}
BOT_ENV_FILE=${env_file}
TELEGRAM_BOT_TOKEN=${telegram_bot_token}
ADMIN_TELEGRAM_ID=${admin_telegram_id}
POSTGRES_DB=${postgres_db}
POSTGRES_USER=${postgres_user}
POSTGRES_PASSWORD=${postgres_password}
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=${redis_password}
CHECK_INTERVAL_SECONDS=300
CHECK_CONCURRENCY=8
DEACTIVATION_CONFIRMATIONS=2
CHECK_JITTER_MIN_SECONDS=0.5
CHECK_JITTER_MAX_SECONDS=3.0
FREE_TRIAL_DAYS=7
LOG_LEVEL=INFO
EOF
  "${SUDO[@]}" install -m 600 "${temp_file}" "${env_file}"
  rm -f -- "${temp_file}"

  if ! "${DOCKER[@]}" image inspect farstar-warner:latest >/dev/null 2>&1; then
    build_image
  fi
  compose "${instance}" up -d --no-build
  compose "${instance}" ps
  log "Instance '${instance}' was installed successfully."
}

start_instance() {
  local instance="$1"
  require_instance "${instance}"
  if ! "${DOCKER[@]}" image inspect farstar-warner:latest >/dev/null 2>&1; then
    build_image
  fi
  compose "${instance}" up -d --no-build
}

stop_instance() {
  local instance="$1"
  require_instance "${instance}"
  compose "${instance}" stop
}

restart_instance() {
  local instance="$1"
  require_instance "${instance}"
  compose "${instance}" restart
}

apply_instance() {
  local instance="$1"
  require_instance "${instance}"
  if ! "${DOCKER[@]}" image inspect farstar-warner:latest >/dev/null 2>&1; then
    build_image
  fi
  compose "${instance}" up -d --no-build --force-recreate
}

show_status() {
  local instance="$1"
  require_instance "${instance}"
  compose "${instance}" ps
}

show_logs() {
  local instance="$1"
  require_instance "${instance}"
  compose "${instance}" logs --tail=150 --follow bot-app
}

update_application() {
  log "Updating Farstar Warner from ${REPOSITORY_URL}"
  command -v git >/dev/null 2>&1 || fail "git is not installed."
  [[ -d "${APP_DIR}/.git" ]] || fail "${APP_DIR} is not a Git checkout. Clone ${REPOSITORY_URL} to enable updates."
  if [[ -n "$(git -C "${APP_DIR}" status --porcelain --untracked-files=no)" ]]; then
    fail "The application has local tracked changes. Commit or discard them before updating."
  fi

  local running_instances=()
  while IFS= read -r env_file; do
    local instance
    instance="$(basename -- "${env_file}" .env)"
    if [[ -n "$(compose "${instance}" ps --status running -q bot-app 2>/dev/null || true)" ]]; then
      running_instances+=("${instance}")
    fi
  done < <("${SUDO[@]}" find "${INSTANCE_DIR}" -maxdepth 1 -type f -name '*.env' -print 2>/dev/null | sort)

  git -C "${APP_DIR}" fetch --prune origin
  git -C "${APP_DIR}" pull --ff-only origin main
  build_image
  for instance in "${running_instances[@]}"; do
    compose "${instance}" up -d --no-build --force-recreate bot-app
  done
  log "Update completed. Running bot instances were recreated."
}

remove_instance() {
  local instance="$1"
  require_instance "${instance}"
  printf 'This will remove the containers for instance %s.\n' "${instance}"
  local confirmation delete_data
  read -r -p "Type the instance name to continue: " confirmation
  [[ "${confirmation}" == "${instance}" ]] || fail "Confirmation did not match."
  read -r -p "Delete PostgreSQL and Redis data volumes too? [y/N]: " delete_data
  if [[ "${delete_data,,}" == "y" || "${delete_data,,}" == "yes" ]]; then
    compose "${instance}" down --volumes --remove-orphans
    "${SUDO[@]}" rm -f -- "$(instance_env "${instance}")"
  else
    compose "${instance}" down --remove-orphans
    local archive_dir archive_file
    archive_dir="/etc/farstar-warner/removed"
    archive_file="${archive_dir}/${instance}-$(date -u +%Y%m%dT%H%M%SZ).env"
    "${SUDO[@]}" mkdir -p "${archive_dir}"
    "${SUDO[@]}" mv -- "$(instance_env "${instance}")" "${archive_file}"
    "${SUDO[@]}" chmod 600 "${archive_file}"
    log "Data volumes were preserved. Credentials were archived at ${archive_file}."
  fi
  log "Instance '${instance}' was removed."
}

backup_instance() {
  local instance="$1"
  require_instance "${instance}"
  local backup_dir timestamp backup_file
  backup_dir="/var/backups/farstar-warner/${instance}"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_file="${backup_dir}/postgres-${timestamp}.sql"
  "${SUDO[@]}" mkdir -p "${backup_dir}"
  # Variables in this command are intentionally expanded inside the container.
  # shellcheck disable=SC2016
  compose "${instance}" exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
    | "${SUDO[@]}" tee "${backup_file}" >/dev/null
  "${SUDO[@]}" chmod 600 "${backup_file}"
  log "Backup created: ${backup_file}"
}

restore_instance() {
  local instance="$1"
  require_instance "${instance}"
  local backup_file confirmation
  prompt_required backup_file "Absolute path to the PostgreSQL SQL backup"
  [[ -f "${backup_file}" ]] || "${SUDO[@]}" test -f "${backup_file}" || fail "Backup file not found."
  warn "Restoring SQL can modify or replace existing database objects."
  read -r -p "Type RESTORE to continue: " confirmation
  [[ "${confirmation}" == "RESTORE" ]] || fail "Restore cancelled."
  # Variables in this command are intentionally expanded inside the container.
  # shellcheck disable=SC2016
  "${SUDO[@]}" cat "${backup_file}" \
    | compose "${instance}" exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" "$POSTGRES_DB"'
  log "Restore completed."
}

edit_instance() {
  local instance="$1"
  require_instance "${instance}"
  local editor="${EDITOR:-nano}"
  command -v "${editor}" >/dev/null 2>&1 || editor="vi"
  "${SUDO[@]}" "${editor}" "$(instance_env "${instance}")"
  read -r -p "Recreate this instance now? [Y/n]: " recreate
  if [[ "${recreate,,}" != "n" && "${recreate,,}" != "no" ]]; then
    compose "${instance}" up -d --no-build --force-recreate
  fi
}

doctor() {
  log "System diagnostics"
  printf 'Application directory: %s\n' "${APP_DIR}"
  printf 'Instance directory:    %s\n' "${INSTANCE_DIR}"
  printf 'Repository:            %s\n' "${REPOSITORY_URL}"
  printf 'Docker:                %s\n' "$("${DOCKER[@]}" version --format '{{.Server.Version}}')"
  printf 'Docker Compose:        %s\n' "$("${DOCKER[@]}" compose version --short)"
  printf '\nContainer resource snapshot:\n'
  "${DOCKER[@]}" stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' \
    | { head -n 1; grep '^farstar-' || true; }
}

usage() {
  cat <<'EOF'
Usage: farstar [command] [instance]

Commands:
  menu                 Open the interactive management panel
  list                 List configured bot instances
  add                  Add and install a bot instance
  status INSTANCE      Show instance service status
  start INSTANCE       Start an instance
  stop INSTANCE        Stop an instance
  restart INSTANCE     Restart an instance
  apply INSTANCE       Recreate an instance and apply environment changes
  logs INSTANCE        Follow bot logs
  update               Pull main from GitHub, rebuild, and recreate running bots
  edit INSTANCE        Edit instance environment settings
  backup INSTANCE      Create a PostgreSQL backup
  restore INSTANCE     Restore a PostgreSQL SQL backup
  remove INSTANCE      Remove an instance, optionally including its data
  doctor               Show Docker and resource diagnostics
  help                 Show this help
EOF
}

interactive_menu() {
  while true; do
    printf '\n========================================\n'
    printf ' Farstar Warner Server Manager\n'
    printf '========================================\n'
    printf ' 1) List bot instances\n'
    printf ' 2) Add and install a bot\n'
    printf ' 3) Show instance status\n'
    printf ' 4) Start an instance\n'
    printf ' 5) Stop an instance\n'
    printf ' 6) Restart an instance\n'
    printf ' 7) Follow bot logs\n'
    printf ' 8) Update all running bots\n'
    printf ' 9) Edit instance settings\n'
    printf '10) Back up PostgreSQL\n'
    printf '11) Restore PostgreSQL\n'
    printf '12) Remove a bot instance\n'
    printf '13) System diagnostics\n'
    printf ' 0) Exit\n\n'
    local choice
    read -r -p "Choose an option: " choice
    case "${choice}" in
      1) list_instances ;;
      2) add_instance ;;
      3) list_instances; prompt_instance; show_status "${SELECTED_INSTANCE}" ;;
      4) list_instances; prompt_instance; start_instance "${SELECTED_INSTANCE}" ;;
      5) list_instances; prompt_instance; stop_instance "${SELECTED_INSTANCE}" ;;
      6) list_instances; prompt_instance; restart_instance "${SELECTED_INSTANCE}" ;;
      7) list_instances; prompt_instance; show_logs "${SELECTED_INSTANCE}" ;;
      8) update_application ;;
      9) list_instances; prompt_instance; edit_instance "${SELECTED_INSTANCE}" ;;
      10) list_instances; prompt_instance; backup_instance "${SELECTED_INSTANCE}" ;;
      11) list_instances; prompt_instance; restore_instance "${SELECTED_INSTANCE}" ;;
      12) list_instances; prompt_instance; remove_instance "${SELECTED_INSTANCE}" ;;
      13) doctor ;;
      0) return ;;
      *) printf 'Invalid option.\n' ;;
    esac
  done
}

main() {
  load_config
  local command="${1:-menu}"
  if [[ "${command}" == "help" || "${command}" == "-h" || "${command}" == "--help" ]]; then
    usage
    return
  fi
  "${SUDO[@]}" mkdir -p "${INSTANCE_DIR}"
  select_docker
  case "${command}" in
    menu) interactive_menu ;;
    list) list_instances ;;
    add) add_instance ;;
    status) [[ $# -ge 2 ]] || fail "An instance name is required."; show_status "$2" ;;
    start) [[ $# -ge 2 ]] || fail "An instance name is required."; start_instance "$2" ;;
    stop) [[ $# -ge 2 ]] || fail "An instance name is required."; stop_instance "$2" ;;
    restart) [[ $# -ge 2 ]] || fail "An instance name is required."; restart_instance "$2" ;;
    apply) [[ $# -ge 2 ]] || fail "An instance name is required."; apply_instance "$2" ;;
    logs) [[ $# -ge 2 ]] || fail "An instance name is required."; show_logs "$2" ;;
    update) update_application ;;
    edit) [[ $# -ge 2 ]] || fail "An instance name is required."; edit_instance "$2" ;;
    backup) [[ $# -ge 2 ]] || fail "An instance name is required."; backup_instance "$2" ;;
    restore) [[ $# -ge 2 ]] || fail "An instance name is required."; restore_instance "$2" ;;
    remove) [[ $# -ge 2 ]] || fail "An instance name is required."; remove_instance "$2" ;;
    doctor) doctor ;;
    *) usage; fail "Unknown command: ${command}" ;;
  esac
}

main "$@"
