#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY_RAW="https://raw.githubusercontent.com/farstar-team/farstar-warner/main"

if [[ ${EUID} -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo --preserve-env=PATH bash <(curl -fsSL "${REPOSITORY_RAW}/update.sh")
  fi
  printf 'Error: run this updater as root or install sudo.\n' >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates
fi
if ! command -v git >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y git
fi

temp_manager="$(mktemp)"
cleanup() {
  rm -f -- "${temp_manager}"
}
trap cleanup EXIT

curl -fsSL "${REPOSITORY_RAW}/farstar.sh" -o "${temp_manager}"
bash -n "${temp_manager}"
install -m 755 "${temp_manager}" /usr/local/bin/farstar
rm -f -- "${temp_manager}"

printf '\n[Farstar] Latest server manager installed. Starting the safe update...\n'
exec /usr/local/bin/farstar update
