#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/infra/.env"
COMPOSE_FILE="${PROJECT_DIR}/infra/compose.yaml"
CONTROLLER_IMAGE="rdk-webtoolchain/controller:0.1-dev"

usage() {
  printf '%s\n' \
    "Usage: scripts/rdkwt.sh <doctor|install|up|down|backup|restore|upgrade|diagnostics> [argument]" \
    "  doctor                 validate Docker, Compose, socket and configuration" \
    "  install | up           create local config, build and start the Controller" \
    "  down                   stop the application without deleting data volumes" \
    "  backup                 stop briefly, create a full backup, then restart" \
    "  restore <filename>     restore a verified backup from the Maintenance page" \
    "  upgrade                full backup, fast-forward git pull, rebuild and start" \
    "  diagnostics [path]     save a redacted diagnostics JSON file"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  }
}

ensure_env() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    cp "${PROJECT_DIR}/infra/.env.example" "${ENV_FILE}"
  fi
  local socket_gid
  if docker image inspect "${CONTROLLER_IMAGE}" >/dev/null 2>&1; then
    socket_gid="$(
      docker run --rm --network none --read-only --cap-drop ALL \
        --volume /var/run/docker.sock:/var/run/docker.sock:ro \
        --entrypoint stat "${CONTROLLER_IMAGE}" -c '%g' /var/run/docker.sock
    )"
  else
    socket_gid="$(stat -c '%g' /var/run/docker.sock)"
  fi
  if grep -q '^DOCKER_GID=' "${ENV_FILE}"; then
    sed -i "s/^DOCKER_GID=.*/DOCKER_GID=${socket_gid}/" "${ENV_FILE}"
  else
    printf 'DOCKER_GID=%s\n' "${socket_gid}" >>"${ENV_FILE}"
  fi
  chmod 600 "${ENV_FILE}"
}

compose() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

doctor() {
  require_command docker
  [[ -S /var/run/docker.sock ]] || {
    printf 'Docker socket is unavailable: /var/run/docker.sock\n' >&2
    exit 1
  }
  docker version >/dev/null
  docker compose version
  ensure_env
  compose config --quiet
  printf 'Docker socket GID in container namespace: %s\n' \
    "$(grep '^DOCKER_GID=' "${ENV_FILE}" | cut -d= -f2-)"
  if docker image inspect "$(grep '^RDKWT_CPU_RUNNER_IMAGE=' "${ENV_FILE}" | cut -d= -f2-)" >/dev/null 2>&1; then
    printf 'CPU Runner image: ready\n'
  else
    printf 'CPU Runner image: missing (build runner/Dockerfile.cpu before conversion)\n'
  fi
  printf 'Configuration: OK\n'
}

start_app() {
  doctor
  compose build controller
  ensure_env
  compose up -d controller
  printf 'RDK WebToolChain: http://127.0.0.1:%s/\n' "$(grep '^RDKWT_PORT=' "${ENV_FILE}" | cut -d= -f2-)"
}

offline_backup() {
  compose stop controller >/dev/null 2>&1 || true
  if compose run --rm --no-deps controller rdkwt-maintenance backup; then
    compose up -d controller
  else
    backup_status=$?
    compose up -d controller || true
    return "${backup_status}"
  fi
}

command_name="${1:-}"
case "${command_name}" in
  doctor)
    doctor
    ;;
  install|up)
    start_app
    ;;
  down)
    ensure_env
    compose down
    ;;
  backup)
    ensure_env
    offline_backup
    ;;
  restore)
    ensure_env
    backup_name="${2:-}"
    [[ -n "${backup_name}" && "${backup_name}" != */* ]] || {
      printf 'restore requires a backup filename shown on the Maintenance page\n' >&2
      exit 2
    }
    compose stop controller
    compose run --rm --no-deps controller \
      rdkwt-maintenance restore --archive "${backup_name}" --confirm RESTORE
    compose up -d controller
    ;;
  upgrade)
    ensure_env
    offline_backup
    git -C "${PROJECT_DIR}" pull --ff-only
    start_app
    ;;
  diagnostics)
    require_command curl
    ensure_env
    output_path="${2:-${PROJECT_DIR}/rdkwt-diagnostics.json}"
    port="$(grep '^RDKWT_PORT=' "${ENV_FILE}" | cut -d= -f2-)"
    curl --fail --silent --show-error \
      "http://127.0.0.1:${port}/api/v1/maintenance/diagnostics" \
      --output "${output_path}"
    printf 'Diagnostics written to %s\n' "${output_path}"
    ;;
  *)
    usage
    exit 2
    ;;
esac
