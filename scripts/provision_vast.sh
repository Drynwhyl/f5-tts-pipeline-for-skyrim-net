#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE:-/workspace}"
F5_TTS_BASE_DIR="${F5_TTS_BASE_DIR:-$WORKSPACE_DIR/f5-tts}"
F5_TTS_REPO_URL="${F5_TTS_REPO_URL:-https://github.com/Drynwhyl/f5-tts-pipeline-for-skyrim-net.git}"
LOG_FILE="${F5_TTS_BOOTSTRAP_LOG:-$WORKSPACE_DIR/bootstrap.log}"
MIGRATION_DIR="${F5_TTS_MIGRATION_DIR:-$WORKSPACE_DIR/migration}"
STATUS_FILE="$MIGRATION_DIR/bootstrap-status.md"
LOCK_FILE="$WORKSPACE_DIR/.f5-tts-bootstrap.lock"
DONE_FILE="$WORKSPACE_DIR/.f5-tts-bootstrap-complete"
GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

mkdir -p "$WORKSPACE_DIR" "$MIGRATION_DIR"
touch "$LOG_FILE"
exec > >(tee -a "$LOG_FILE") 2>&1

log() {
  printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

write_status() {
  mkdir -p "$MIGRATION_DIR"
  printf '# Bootstrap status\n\nLast update: %s\n\n%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$STATUS_FILE"
}

source_environment() {
  set -a
  [[ -f /etc/environment ]] && . /etc/environment
  [[ -f "$WORKSPACE_DIR/.env" ]] && . "$WORKSPACE_DIR/.env"
  set +a
}

clone_or_update_repo() {
  local clone_url="$F5_TTS_REPO_URL"

  if [[ -n "$GITHUB_TOKEN_VALUE" && "$clone_url" =~ ^https://github.com/(.*)$ ]]; then
    clone_url="https://x-access-token:${GITHUB_TOKEN_VALUE}@github.com/${BASH_REMATCH[1]}"
  fi

  git config --global --add safe.directory "$F5_TTS_BASE_DIR" >/dev/null 2>&1 || true
  mkdir -p "$(dirname "$F5_TTS_BASE_DIR")"

  if [[ ! -d "$F5_TTS_BASE_DIR/.git" ]]; then
    log "Cloning repo into $F5_TTS_BASE_DIR."
    git clone "$clone_url" "$F5_TTS_BASE_DIR"
  else
    log "Updating repo in $F5_TTS_BASE_DIR."
    git -C "$F5_TTS_BASE_DIR" fetch origin master
    git -C "$F5_TTS_BASE_DIR" checkout master
    git -C "$F5_TTS_BASE_DIR" pull --ff-only origin master
  fi

  git -C "$F5_TTS_BASE_DIR" remote set-url origin "$F5_TTS_REPO_URL"
}

determine_instance_id() {
  local instance_id="${VAST_INSTANCE_ID:-${CONTAINER_ID:-${INSTANCE_ID:-}}}"
  if [[ -z "$instance_id" ]] && command -v vast-capabilities >/dev/null 2>&1; then
    instance_id="$(vast-capabilities | jq -r '.instance.container_id // .instance.id // empty')"
  fi
  printf '%s\n' "$instance_id"
}

restore_codex_from_cloud() {
  local api_key="${VAST_API_KEY:-${VASTAI_API_KEY:-}}"
  local connection_id="${VAST_CLOUD_CONNECTION_ID:-${F5_TTS_CLOUD_CONNECTION_ID:-}}"
  local instance_id="$1"
  local cloud_src="${CODEX_CLOUD_SRC:-/F5-TTS-Vast/codex/v2/current/}"
  local current_dir="${CODEX_BACKUP_DIR:-$WORKSPACE_DIR/cloudsync/codex/current}"
  local incoming_root="$current_dir/incoming"
  local run_id="run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  local incoming_dir="$incoming_root/$run_id"
  local checksum_retry_dir="$incoming_root/${run_id}-checksum-retry"
  local archive_retry_dir="$incoming_root/${run_id}-archive-retry"
  local timeout_sec="${CODEX_RESTORE_TIMEOUT_SEC:-300}"
  local checksum_timeout_sec="${CODEX_CHECKSUM_TIMEOUT_SEC:-90}"
  local archive_path sha_path

  if [[ "${RESTORE_CODEX:-1}" != "1" ]]; then
    log "Skipping Codex state restore (RESTORE_CODEX=${RESTORE_CODEX:-})."
    return 0
  fi
  if [[ -z "$api_key" || -z "$connection_id" || -z "$instance_id" ]]; then
    log "Skipping Codex state restore; Vast API key, connection id, or instance id is missing."
    return 0
  fi
  if [[ ! "$connection_id" =~ ^[0-9]+$ ]]; then
    log "Skipping Codex state restore; VAST_CLOUD_CONNECTION_ID is not numeric."
    return 0
  fi

  # shellcheck source=scripts/cloud_copy_restore_lib.sh
  source "$F5_TTS_BASE_DIR/scripts/cloud_copy_restore_lib.sh"

  mkdir -p "$incoming_dir"

  log "Requesting Codex state restore from $cloud_src."
  if ! cc_request_cloud_copy "$cloud_src" "$incoming_dir" "$instance_id" "$connection_id" "$api_key"; then
    log "Codex state Cloud Copy request failed; continuing without restored Codex state."
    return 0
  fi

  sha_path="$(cc_wait_named_file "$incoming_dir" "codex-state.tar.zst.sha256" "$checksum_timeout_sec" 5 2)" || true
  if [[ -z "$sha_path" ]]; then
    log "Codex checksum was absent from the directory restore; requesting it separately."
    if ! cc_request_cloud_copy \
        "${cloud_src%/}/codex-state.tar.zst.sha256" \
        "$checksum_retry_dir" "$instance_id" "$connection_id" "$api_key"; then
      log "Codex checksum retry request failed; refusing to restore unverified state."
      return 0
    fi
    sha_path="$(cc_wait_named_file "$checksum_retry_dir" "codex-state.tar.zst.sha256" 120 5 2)" || {
      log "Codex state checksum retry timed out; refusing to restore unverified state."
      return 0
    }
  fi

  archive_path="$(cc_wait_checksum_match "$incoming_dir" "$sha_path" "codex-state*.tar.zst" "$timeout_sec" 10)" || true
  if [[ -z "$archive_path" ]]; then
    log "No Codex archive matched after the first restore; retrying the cloud directory once."
    if ! cc_request_cloud_copy "$cloud_src" "$archive_retry_dir" "$instance_id" "$connection_id" "$api_key"; then
      log "Codex archive retry request failed; continuing without restored Codex state."
      return 0
    fi
    archive_path="$(cc_wait_checksum_match "$archive_retry_dir" "$sha_path" "codex-state*.tar.zst" "$timeout_sec" 10)" || {
      log "No Codex archive matched the restored checksum; refusing to extract it."
      return 0
    }
  fi
  log "Using checksum-matched Codex archive $(basename "$archive_path")."

  mkdir -p "$current_dir"
  cp -f "$sha_path" "$current_dir/codex-state.tar.zst.sha256"
  cp -f "$archive_path" "$current_dir/codex-state.tar.zst"

  if ! (cd "$current_dir" && sha256sum -c codex-state.tar.zst.sha256); then
    log "Codex state checksum verification failed; refusing to extract the archive."
    return 0
  fi

  bash "$F5_TTS_BASE_DIR/scripts/restore_codex_state.sh" "$current_dir/codex-state.tar.zst" || {
    log "Codex state restore failed; continuing without restored Codex state."
    return 0
  }
}

main() {
  source_environment

  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "Another F5-TTS bootstrap is already running; exiting."
    write_status "Another F5-TTS bootstrap is already running."
    exit 0
  fi

  if [[ -f "$DONE_FILE" && "${F5_TTS_FORCE_BOOTSTRAP:-0}" != "1" ]]; then
    log "Bootstrap already completed; set F5_TTS_FORCE_BOOTSTRAP=1 to rerun."
    write_status "Bootstrap already completed."
    exit 0
  fi

  write_status "Starting Vast provisioning bootstrap."
  log "Starting Vast provisioning bootstrap."

  clone_or_update_repo
  cd "$F5_TTS_BASE_DIR"

  bash scripts/setup_github_auth.sh

  if [[ "${INSTALL_CODEX:-1}" == "1" ]]; then
    bash scripts/install_codex_workspace.sh
    restore_codex_from_cloud "$(determine_instance_id)"
  fi

  bash scripts/bootstrap_vast_from_cloudcopy.sh

  touch "$DONE_FILE"
  write_status "Provisioning bootstrap completed successfully."
  log "Provisioning bootstrap completed successfully."
}

main "$@"
